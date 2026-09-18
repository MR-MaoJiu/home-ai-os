import XCTest
import UIKit
@preconcurrency import Network
@testable import HomeAI

final class DirectTests: XCTestCase {
    func testPlatformTLSOnDevice() async throws {
        let env = ProcessInfo.processInfo.environment
        guard env["HOMEAI_PLATFORM_TLS_CHECK"] == "1" else { throw XCTSkip("仅在真实设备网络诊断时启用") }
        let access = DirectAccess(grant_id: "public-diagnostic", credential: "", expires: Date().timeIntervalSince1970 + 300,
                                  portal_url: "https://homeai-connect.pintheworld.cn", instance_id: "public-diagnostic", stun_urls: [], transport_policy: "direct_only")
        for attempt in 1...3 {
            do {
                let data = try await ConnectSignalling.request(access, method: "GET", path: "/api/direct/capabilities")
                let result = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
                XCTAssertEqual(result["protocol"] as? Int, 1)
                print("PLATFORM_TLS attempt=\(attempt) verified=true")
            } catch {
                let error = error as NSError
                let underlying = error.userInfo[NSUnderlyingErrorKey] as? NSError
                print("PLATFORM_TLS attempt=\(attempt) code=\(error.code) underlying=\(underlying?.code ?? 0)")
                throw error
            }
        }
    }

    @MainActor
    func testRecoveredConnectionClearsOnlyItsOwnFailure() async {
        let api = APIClient(persistConnection: false)
        let state = AppState(api: api)
        state.connected = true
        state.reportPairingFailure("二维码已过期")
        await state.confirmConnectionRecovered()
        XCTAssertEqual(state.pairingFeedback, .failure("二维码已过期"))
        state.reportPairingFailure("直连已关闭", recoveryGeneration: UUID())
        await state.confirmConnectionRecovered()
        XCTAssertEqual(state.pairingFeedback, .failure("直连已关闭"))
        state.reportPairingFailure("直连已关闭", recoveryGeneration: await api.connectionGeneration())
        await state.confirmConnectionRecovered()
        XCTAssertEqual(state.pairingFeedback, .success)
        XCTAssertFalse(state.pairingNotice)
    }

    @MainActor
    func testConcurrentReadsDuringRemotePairing() async throws {
        let env = ProcessInfo.processInfo.environment
        guard let encoded = env["HOMEAI_DIRECT_REMOTE_PAIR_FILE_BASE64"], let data = Data(base64Encoded: encoded) else { throw XCTSkip("需要真实家庭服务的短期配对载荷") }
        struct Fixture: Decodable { let pairing: PairingCode }
        let fixture = try JSONDecoder().decode(Fixture.self, from: data)
        let api = APIClient(persistConnection: false)
        let pairing = Task { try await api.pair(fixture.pairing) }
        defer { pairing.cancel() }
        let deadline = Date().addingTimeInterval(100)
        while !(await api.isConnected()) && Date() < deadline { try await Task.sleep(for: .milliseconds(20)) }
        guard await api.isConnected() else { try await pairing.value; return XCTFail("配对没有保存授权") }
        // 模拟真实 App 的前台事件与同步在凭据保存后同时发起请求。
        async let first = api.request("GET", "/api/v1/me")
        async let second = api.request("GET", "/api/v1/me")
        try await pairing.value
        let responses = try await [first, second]
        XCTAssertEqual(responses.count, 2)
        let me = try XCTUnwrap(JSONSerialization.jsonObject(with: responses[0]) as? [String: Any])
        let device = try XCTUnwrap(me["device_id"] as? String)
        _ = try await api.request("DELETE", "/api/v1/devices/" + device)
    }

    @MainActor
    func testInvalidQRCodeAlwaysProducesVisibleFailure() async {
        let state = AppState(api: APIClient(persistConnection: false))
        await state.pair(scannedText: "not-a-homeai-code")
        XCTAssertTrue(state.pairingNotice)
        XCTAssertFalse(state.busy)
        XCTAssertNil(state.error) // 不依赖容易与扫码 sheet 冲突的通用错误路径。
        guard case .failure(let message) = state.pairingFeedback else { return XCTFail("缺少失败状态") }
        XCTAssertTrue(message.contains("二维码"))
        XCTAssertFalse(state.connected)
    }

    @MainActor
    func testExpiredQRCodeEndsConnectingState() async throws {
        let state = AppState(api: APIClient(persistConnection: false))
        let code = PairingCode(expires_at: 1, url: "https://example.invalid", fingerprint: String(repeating: "a", count: 64), token: "expired")
        let text = String(decoding: try JSONEncoder().encode(code), as: UTF8.self)
        await state.pair(scannedText: text)
        XCTAssertEqual(state.pairingFeedback, .failure("配对二维码已过期，请在管理端重新生成"))
        XCTAssertTrue(state.pairingNotice)
        XCTAssertFalse(state.busy)
    }

    @MainActor
    func testDuplicateScanCannotReplaceActiveProgress() async {
        let state = AppState(api: APIClient(persistConnection: false))
        state.pairingFeedback = .connecting
        await state.pair(scannedText: "duplicate")
        XCTAssertEqual(state.pairingFeedback, .connecting)
        XCTAssertFalse(state.pairingNotice)
    }

    @MainActor
    func testRealCoreOverDirectChannel() async throws {
        try await runFlow(remote: false)
    }

    @MainActor
    func testRealCoreViaPlatformSignal() async throws {
        try await runFlow(remote: true)
    }

    @MainActor
    private func waitForCellular() async throws {
        let monitor = NWPathMonitor()
        let updates = AsyncStream<Bool> { continuation in
            monitor.pathUpdateHandler = { path in
                print("DIRECT_PATH cellular=\(path.usesInterfaceType(.cellular)) wifi=\(path.usesInterfaceType(.wifi)) status=\(path.status) interfaces=\(path.availableInterfaces.map { $0.name })")
                continuation.yield(path.status == .satisfied && path.usesInterfaceType(.cellular) && !path.usesInterfaceType(.wifi))
            }
            monitor.start(queue: DispatchQueue(label: "homeai.direct.cellular-check"))
            continuation.onTermination = { _ in monitor.cancel() }
        }
        defer { monitor.cancel() }
        try await withThrowingTaskGroup(of: Void.self) { group in
            group.addTask {
                for await cellular in updates { if cellular { return } }
                throw CancellationError()
            }
            group.addTask {
                try await Task.sleep(for: .seconds(180))
                throw APIClient.APIError.message("尚未切换至蜂窝网络")
            }
            _ = try await group.next()
            group.cancelAll()
        }
    }

    @MainActor
    private func waitForForeground() async throws {
        if UIApplication.shared.applicationState != .active { print("DIRECT_WAITING_FOR_FOREGROUND") }
        let deadline = Date().addingTimeInterval(60)
        while UIApplication.shared.applicationState != .active {
            guard Date() < deadline else { throw APIClient.APIError.message("请保持 Home AI 前台进行真机验收") }
            try await Task.sleep(for: .milliseconds(100))
        }
        print("DIRECT_APP_FOREGROUND")
    }

    @MainActor
    private func runFlow(remote: Bool) async throws {
        let variable = remote ? "HOMEAI_DIRECT_REMOTE_PAIR_FILE" : "HOMEAI_DIRECT_PAIR_FILE"
        let env = ProcessInfo.processInfo.environment
        let fixtureData: Data
        if let encoded = env[variable + "_BASE64"] ?? env["TEST_RUNNER_" + variable + "_BASE64"], let data = Data(base64Encoded: encoded) {
            fixtureData = data
        } else if let path = env[variable] ?? env["TEST_RUNNER_" + variable] {
            fixtureData = try Data(contentsOf: URL(fileURLWithPath: path))
        } else { throw XCTSkip("需要真实直连验收服务") }
        struct Fixture: Decodable { let pairing: PairingCode?; let connection: Connection?; let device_public_key: String? }
        let fixture = try JSONDecoder().decode(Fixture.self, from: fixtureData)
        let cellular = (env["HOMEAI_DIRECT_REQUIRE_CELLULAR"] ?? env["TEST_RUNNER_HOMEAI_DIRECT_REQUIRE_CELLULAR"]) == "1"
        let oldIdle = UIApplication.shared.isIdleTimerDisabled
        UIApplication.shared.isIdleTimerDisabled = true
        defer { UIApplication.shared.isIdleTimerDisabled = oldIdle }
        if remote && cellular && fixture.pairing?.schema_version == "3.0" {
            print("DIRECT_READY_FOR_CELLULAR")
            try await waitForCellular()
            try await waitForForeground()
        }
        let api = APIClient(persistConnection: false)
        #if DEBUG
        if let saved = fixture.connection, let key = fixture.device_public_key {
            try await api.restoreAcceptanceConnection(saved, devicePublicKey: key)
        } else { try await api.pair(XCTUnwrap(fixture.pairing)) }
        #else
        try await api.pair(XCTUnwrap(fixture.pairing))
        #endif
        let namespace = try await api.syncNamespace()
        if remote {
            if fixture.connection == nil { try await api.configureRemoteDirectAccess() }
            if cellular {
                print("DIRECT_READY_FOR_CELLULAR")
                try await waitForCellular()
            }
            if cellular { try await waitForForeground() }
            try await api.connectRemoteDirect(requirePublicPath: cellular)
        } else { try await api.enableDirect() }
        let evidence = try await api.request("GET", "/api/v1/direct/evidence")
        struct Evidence: Decodable {
            struct Pair: Decodable { let remote_private: Bool; let remote_type: String }
            let transport: String
            let pairs: [Pair]
        }
        let pathEvidence = try JSONDecoder().decode(Evidence.self, from: evidence)
        XCTAssertEqual(pathEvidence.transport, "udp-dtls")
        XCTAssertFalse(pathEvidence.pairs.isEmpty)
        XCTAssertTrue(pathEvidence.pairs.allSatisfy { $0.remote_type != "relay" })
        if cellular { XCTAssertTrue(pathEvidence.pairs.allSatisfy { !$0.remote_private }, "必须走公网候选，不能误用 USB 或局域网通道") }
        let me = try await api.request("GET", "/api/v1/me", expectedNamespace: namespace)
        XCTAssertTrue(String(decoding: me, as: UTF8.self).contains("device_id"))
        let text = String(repeating: "原生直连资料", count: 50000)
        let body = try JSONSerialization.data(withJSONObject: ["records": [["source": "direct-test", "source_id": UUID().uuidString,
                    "kind": "document.text", "version": 1, "payload": ["content": text]]]])
        struct Synced: Decodable { let records: [DataEntry] }
        let synced = try JSONDecoder().decode(Synced.self, from: await api.request("POST", "/api/v1/data/sync", body: body, expectedNamespace: namespace))
        let record = try XCTUnwrap(synced.records.first)
        let result = try await api.request("GET", "/api/v1/data/" + record.id, expectedNamespace: namespace)
        XCTAssertTrue(String(decoding: result, as: UTF8.self).contains(text))
        _ = try await api.request("DELETE", "/api/v1/data/" + record.id, expectedNamespace: namespace)
        let subscription = try await api.taskEvents(expectedNamespace: namespace)
        var iterator = subscription.events.makeAsyncIterator()
        let snapshot = try await iterator.next()
        XCTAssertEqual(snapshot?.type, "task.snapshot")
        await api.closeTaskEvents(subscription.id)
        struct Me: Decodable { let device_id: String }
        let device = try JSONDecoder().decode(Me.self, from: me)
        _ = try await api.request("DELETE", "/api/v1/devices/" + device.device_id)
        do {
            _ = try await api.request("GET", "/api/v1/data")
            XCTFail("撤销后仍能读取")
        } catch APIClient.APIError.http(let status, _) { XCTAssertEqual(status, 401) }
        await api.disableDirect()
    }
}
