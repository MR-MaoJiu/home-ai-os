import XCTest
import EventKit
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
    func testAnswerSourcesOnlyAcceptCanonicalIdentifiers() {
        let result = JSONValue.object(["sources": .array([.object(["record_id": .string("../secrets"), "title": .string("不应成为链接"), "version": .number(1)])])])
        XCTAssertTrue(ChatView.sources(result).isEmpty)
    }

    @MainActor
    func testLivePagedSyncAndCursorRecovery() async throws {
        let environment = ProcessInfo.processInfo.environment
        guard let path = environment["HOMEAI_SYNC_PAIR_FILE"] ?? environment["TEST_RUNNER_HOMEAI_SYNC_PAIR_FILE"] else {
            throw XCTSkip("需要临时真实服务配对文件；不使用网络模拟结果")
        }
        struct Fixture: Decodable { let pairing: PairingCode; let count: Int; let documents: Bool? }
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
        if fixture.documents == true {
            let taskID = try await api.uploadDocument(name: "备份计划.md", contents: Data("# 备份计划\n周日晚上八点进行家庭备份。".utf8))
            _ = try await api.request("POST", "/_test/run/" + taskID)
            struct Parsed: Decodable { let source_id: String; let markdown: String }
            struct ParsedTask: Decodable { let status: String; let result: Parsed? }
            let task = try JSONDecoder().decode(ParsedTask.self, from: await api.request("GET", "/api/v1/tasks/" + taskID))
            XCTAssertEqual(task.status, "SUCCEEDED")
            let parsed = try XCTUnwrap(task.result)
            XCTAssertTrue(parsed.markdown.contains("八点"))
            let query = try JSONSerialization.data(withJSONObject: ["query": "家庭备份时间"])
            struct Match: Decodable { let excerpt: String }
            struct Search: Decodable { let mode: String; let matches: [Match] }
            let found = try JSONDecoder().decode(Search.self, from: await api.request("POST", "/api/v1/knowledge/search", body: query))
            XCTAssertEqual(found.mode, "pgvector_chunks")
            XCTAssertTrue(found.matches.contains { $0.excerpt.contains("八点") })
            _ = try await api.request("DELETE", "/api/v1/data/" + parsed.source_id)
        }
        // Intent 的不确定提交恢复：真实服务已收下请求，但客户端尚未清除待确认记录。
        let intentNamespace = try await api.syncNamespace()
        let intentStorage = "intent-test-" + UUID().uuidString
        let intentService = ReminderIntentService(storageKey: intentStorage)
        let dueDate = try XCTUnwrap(ISO8601DateFormatter().date(from: "2026-10-01T00:30:00Z"))
        let intentPending = try await intentService.prepare(title: "快捷指令真实提醒", namespace: intentNamespace, dueDate: dueDate, notify: true)
        let intentBody = try JSONSerialization.data(withJSONObject: ["idempotency_key": intentPending.idempotencyKey,
            "capability": "reminder.create@v1", "arguments": ["title": intentPending.title, "due_at": intentPending.dueAt!, "notify_at_due": true]])
        struct IntentCreated: Decodable { let id: String }
        let firstIntent = try JSONDecoder().decode(IntentCreated.self, from: await api.request("POST", "/api/v1/tasks", body: intentBody, expectedNamespace: intentNamespace))
        let restoredIntent = ReminderIntentService(storageKey: intentStorage)
        let recoveredID = try await restoredIntent.submit(title: "快捷指令真实提醒", dueDate: dueDate, notify: true, api: api)
        XCTAssertEqual(recoveredID, firstIntent.id)
        _ = try await api.request("POST", "/_test/run/" + recoveredID)
        struct ReminderReceipt: Decodable { let record_id: String }
        struct ReminderTask: Decodable { let status: String; let result: ReminderReceipt? }
        let intentTask = try JSONDecoder().decode(ReminderTask.self, from: await api.request("GET", "/api/v1/tasks/" + recoveredID))
        XCTAssertEqual(intentTask.status, "SUCCEEDED")
        let intentRecordID = try XCTUnwrap(intentTask.result?.record_id)
        let reminder = try JSONDecoder().decode(DataEntry.self, from: await api.request("GET", "/api/v1/data/" + intentRecordID))
        XCTAssertEqual(reminder.title, "快捷指令真实提醒")
        XCTAssertEqual(reminder.sensitivity, "PRIVATE")
        // 实际 EventKit 本机列表：创建、去重、完成状态回传和服务器删除传播。
        let reminderStore = EKEventStore()
        XCTAssertEqual(EKEventStore.authorizationStatus(for: .reminder), .fullAccess)
        let localSource = try XCTUnwrap(reminderStore.sources.first { $0.sourceType == .local }, "测试模拟器需要本机提醒来源")
        let testCalendar = EKCalendar(for: .reminder, eventStore: reminderStore)
        testCalendar.title = "Home AI 原生提醒验收 " + UUID().uuidString.prefix(8)
        testCalendar.source = localSource
        try reminderStore.saveCalendar(testCalendar, commit: true)
        defer { try? reminderStore.removeCalendar(testCalendar, commit: true) }
        let reminderBridge = SystemReminderSync()
        do {
            _ = try await reminderBridge.synchronize(records: [reminder], api: api, calendarID: testCalendar.calendarIdentifier,
                expectedNamespace: intentNamespace, expectedSourceID: "changed-account", allowCloudExport: true, store: reminderStore)
            XCTFail("目标账号变化后必须拒绝旧确认")
        } catch APIClient.APIError.message { }
        do {
            _ = try await reminderBridge.synchronize(records: [reminder], api: api, calendarID: testCalendar.calendarIdentifier,
                expectedNamespace: intentNamespace, expectedSourceID: testCalendar.source.sourceIdentifier,
                expectedSourceType: EKSourceType.calDAV.rawValue, allowCloudExport: true, store: reminderStore)
            XCTFail("账号类型变化后必须拒绝旧确认")
        } catch APIClient.APIError.message { }
        let exported = try await reminderBridge.synchronize(records: [reminder], api: api, calendarID: testCalendar.calendarIdentifier, expectedNamespace: intentNamespace, store: reminderStore)
        XCTAssertEqual(exported.created, 1)
        let repeated = try await reminderBridge.synchronize(records: [reminder], api: api, calendarID: testCalendar.calendarIdentifier, expectedNamespace: intentNamespace, store: reminderStore)
        XCTAssertEqual(repeated.created, 0)
        let systemIDs: [String] = await withCheckedContinuation { continuation in
            reminderStore.fetchReminders(matching: reminderStore.predicateForReminders(in: [testCalendar])) { values in
                continuation.resume(returning: (values ?? []).map(\.calendarItemIdentifier))
            }
        }
        XCTAssertEqual(systemIDs.count, 1)
        let systemReminder = try XCTUnwrap(reminderStore.calendarItem(withIdentifier: systemIDs[0]) as? EKReminder)
        XCTAssertEqual(systemReminder.title, "快捷指令真实提醒")
        XCTAssertEqual(systemReminder.url?.scheme, SystemReminderSync.markerScheme)
        struct PairTicket: Decodable { let token: String }
        let ticket = try JSONDecoder().decode(PairTicket.self, from: await api.request("POST", "/_test/pair-ticket"))
        let repaired = APIClient(persistConnection: false)
        try await repaired.pair(PairingCode(url: fixture.pairing.url, fingerprint: fixture.pairing.fingerprint, token: ticket.token))
        let repairedNamespace = try await repaired.syncNamespace()
        XCTAssertNotEqual(repairedNamespace, intentNamespace)
        let repairedReport = try await SystemReminderSync().synchronize(records: [reminder], api: repaired, calendarID: testCalendar.calendarIdentifier, expectedNamespace: repairedNamespace, store: reminderStore)
        XCTAssertEqual(repairedReport.created, 0)
        XCTAssertNil(systemReminder.dueDateComponents)
        _ = try await reminderBridge.synchronize(records: [reminder], api: api, calendarID: testCalendar.calendarIdentifier,
            expectedNamespace: intentNamespace, includeSchedule: true, store: reminderStore)
        XCTAssertTrue(systemReminder.refresh())
        let actualSchedule = try ReminderSchedule.local(systemReminder)
        XCTAssertEqual(actualSchedule.due, dueDate)
        XCTAssertTrue(actualSchedule.notify)
        systemReminder.isCompleted = true
        try reminderStore.save(systemReminder, commit: true)
        _ = try await reminderBridge.synchronize(records: [reminder], api: api, calendarID: testCalendar.calendarIdentifier, expectedNamespace: intentNamespace, store: reminderStore)
        let completedReminder = try JSONDecoder().decode(DataEntry.self, from: await api.request("GET", "/api/v1/data/" + intentRecordID))
        XCTAssertEqual(completedReminder.payload["completed"]?.description, "true")
        // 两端同时编辑同一字段时保留双方，不覆盖；对齐内容后再推进版本。
        systemReminder.title = "手机侧编辑"
        try reminderStore.save(systemReminder, commit: true)
        var changedPayload = completedReminder.payload
        changedPayload["title"] = .string("服务器侧编辑")
        let changedUpload = ConnectorSync.Upload(source: "core", source_id: try XCTUnwrap(completedReminder.source_id), kind: "reminder.item", version: try XCTUnwrap(completedReminder.version) + 1, sensitivity: "PRIVATE", cloud_policy: "LOCAL_ONLY", payload: changedPayload)
        _ = try await api.request("POST", "/api/v1/data/sync", body: JSONEncoder().encode(ConnectorSync.Batch(batch_id: UUID().uuidString, records: [changedUpload])))
        let conflicted = try JSONDecoder().decode(DataEntry.self, from: await api.request("GET", "/api/v1/data/" + intentRecordID))
        let conflictReport = try await reminderBridge.synchronize(records: [conflicted], api: api, calendarID: testCalendar.calendarIdentifier, expectedNamespace: intentNamespace, store: reminderStore)
        XCTAssertEqual(conflictReport.conflicts, 1)
        XCTAssertTrue(systemReminder.refresh())
        XCTAssertEqual(systemReminder.title, "手机侧编辑")
        systemReminder.title = "服务器侧编辑"
        try reminderStore.save(systemReminder, commit: true)
        _ = try await reminderBridge.synchronize(records: [conflicted], api: api, calendarID: testCalendar.calendarIdentifier, expectedNamespace: intentNamespace, store: reminderStore)
        _ = try await api.request("DELETE", "/api/v1/data/" + intentRecordID)
        let removedReminder = try await reminderBridge.synchronize(records: [], api: api, calendarID: testCalendar.calendarIdentifier, expectedNamespace: intentNamespace, store: reminderStore)
        XCTAssertEqual(removedReminder.removed, 1)
        XCTAssertNil(reminderStore.calendarItem(withIdentifier: systemIDs[0]))
        let invalidData = try JSONSerialization.data(withJSONObject: ["records": [["source": "core", "source_id": UUID().uuidString, "kind": "reminder.item", "version": 1, "payload": ["title": "无效完成标志", "completed": "not-a-boolean"]]]])
        let invalidBatch = try JSONDecoder().decode(DataPage.self, from: await api.request("POST", "/api/v1/data/sync", body: invalidData))
        let invalidRecordID = try XCTUnwrap(invalidBatch.records.first?.id)
        let invalidRecord = try JSONDecoder().decode(DataEntry.self, from: await api.request("GET", "/api/v1/data/" + invalidRecordID))
        let invalidReport = try await reminderBridge.synchronize(records: [invalidRecord], api: api, calendarID: testCalendar.calendarIdentifier, expectedNamespace: intentNamespace, store: reminderStore)
        XCTAssertEqual(invalidReport.conflicts, 1)
        XCTAssertEqual(invalidReport.created, 0)
        _ = try await api.request("DELETE", "/api/v1/data/" + invalidRecordID)
        // 原生 WSS 使用同一证书固定与设备签名，通知来自真实任务执行。
        let namespace = try await api.syncNamespace()
        let createBody = try JSONSerialization.data(withJSONObject: ["idempotency_key": UUID().uuidString, "capability": "reminder.create@v1", "arguments": ["title": "实时事件验收"]])
        struct CreatedTask: Decodable { let id: String }
        let created = try JSONDecoder().decode(CreatedTask.self, from: await api.request("POST", "/api/v1/tasks", body: createBody))
        let subscription = try await api.taskEvents(taskID: created.id, expectedNamespace: namespace)
        var iterator = subscription.events.makeAsyncIterator()
        let initialEvent = try await iterator.next()
        XCTAssertEqual(initialEvent?.tasks.first?.status, "RECEIVED")
        _ = try await api.request("POST", "/_test/run/" + created.id)
        let completedEvent = try await iterator.next()
        XCTAssertEqual(completedEvent?.tasks.first?.status, "SUCCEEDED")
        await api.closeTaskEvents(subscription.id)
        let resumed = try await api.taskEvents(taskID: created.id, expectedNamespace: namespace)
        var resumedIterator = resumed.events.makeAsyncIterator()
        let resumedEvent = try await resumedIterator.next()
        XCTAssertEqual(resumedEvent?.tasks.first?.status, "SUCCEEDED")
        struct Me: Decodable { let device_id: String }
        let identity = try JSONDecoder().decode(Me.self, from: await api.request("GET", "/api/v1/me"))
        _ = try await api.request("DELETE", "/api/v1/devices/" + identity.device_id)
        do {
            _ = try await resumedIterator.next()
            XCTFail("设备撤销必须断开已建立的状态流")
        } catch let APIClient.APIError.http(code, _) { XCTAssertEqual(code, 401) }
        await api.closeTaskEvents(resumed.id)
        do {
            _ = try await DeviceDataSync().synchronize(api: api)
            XCTFail("撤销设备后不能显示授权成功或返回缓存作为在线数据")
        } catch let APIClient.APIError.http(code, _) {
            XCTAssertEqual(code, 401)
        }
    }
}
