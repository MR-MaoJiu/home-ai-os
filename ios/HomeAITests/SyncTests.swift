import XCTest
@testable import HomeAI

final class SyncTests: XCTestCase {
    func testMergeUpdatesAndRemovesWithoutDuplicates() throws {
        let decoder = JSONDecoder()
        let original = try decoder.decode(DataEntry.self, from: Data(#"{"id":"a","kind":"note","sensitivity":"PRIVATE","payload":{"title":"old"}}"#.utf8))
        let updated = try decoder.decode(DataEntry.self, from: Data(#"{"id":"a","kind":"note","sensitivity":"PRIVATE","payload":{"title":"new"}}"#.utf8))
        let merged = DeviceDataSync.merge([original], updates: [updated, updated], removed: [])
        XCTAssertEqual(merged.count, 1)
        XCTAssertEqual(merged[0].title, "new")
        XCTAssertTrue(DeviceDataSync.merge(merged, updates: [], removed: ["a"]).isEmpty)
    }

    @MainActor
    func testLivePagedSyncAndCursorRecovery() async throws {
        let environment = ProcessInfo.processInfo.environment
        guard let path = environment["HOMEAI_SYNC_PAIR_FILE"] ?? environment["TEST_RUNNER_HOMEAI_SYNC_PAIR_FILE"] else {
            throw XCTSkip("需要临时真实服务配对文件；不使用网络模拟结果")
        }
        struct Fixture: Decodable { let pairing: PairingCode; let count: Int }
        let fixture = try JSONDecoder().decode(Fixture.self, from: Data(contentsOf: URL(fileURLWithPath: path)))
        let api = APIClient(persistConnection: false)
        try await api.pair(fixture.pairing)
        // 短会话测试服务会强制进入刷新路径，验证前后台并发请求不重复消费旧凭据。
        async let firstIdentity = api.request("GET", "/api/v1/me")
        async let secondIdentity = api.request("GET", "/api/v1/me")
        let identities = try await (firstIdentity, secondIdentity)
        XCTAssertEqual(identities.0, identities.1)
        let wrongTarget = try JSONSerialization.data(withJSONObject: ["records": [["source": "native_sync", "source_id": "wrong-target", "kind": "note", "version": 1, "payload": ["title": "不应上传"]]]])
        do {
            _ = try await api.request("POST", "/api/v1/data/sync", body: wrongTarget, expectedNamespace: "different-connection")
            XCTFail("连接身份不符时必须在发送前停止")
        } catch APIClient.APIError.message { }
        let sync = DeviceDataSync()
        let first = try await sync.synchronize(api: api)
        XCTAssertEqual(first.records.count, fixture.count)
        XCTAssertFalse(first.offline)
        let connector = ConnectorSync(api: api)
        try await connector.uploadRecord(source: "native_sync", sourceID: "item-0", kind: "note", payload: ["title": .string("已更新")])
        try await connector.uploadRecord(source: "native_sync", sourceID: "item-0", kind: "note", payload: ["title": .string("已更新")])
        let changed = try await sync.synchronize(api: api)
        XCTAssertEqual(changed.records.count, fixture.count)
        XCTAssertEqual(changed.records.filter { $0.title == "已更新" }.count, 1)
        let removed = try XCTUnwrap(changed.records.first?.id)
        _ = try await api.request("DELETE", "/api/v1/data/" + removed)
        let recovered = try await DeviceDataSync().synchronize(api: api)
        XCTAssertEqual(recovered.records.count, fixture.count - 1)
        XCTAssertFalse(recovered.records.contains { $0.id == removed })
        struct Me: Decodable { let device_id: String }
        let identity = try JSONDecoder().decode(Me.self, from: await api.request("GET", "/api/v1/me"))
        _ = try await api.request("DELETE", "/api/v1/devices/" + identity.device_id)
        do {
            _ = try await DeviceDataSync().synchronize(api: api)
            XCTFail("撤销设备后不能显示授权成功或返回缓存作为在线数据")
        } catch let APIClient.APIError.http(code, _) {
            XCTAssertEqual(code, 401)
        }
    }
}
