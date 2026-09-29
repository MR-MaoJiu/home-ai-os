import XCTest
import EventKit
import UserNotifications
import UIKit
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
            let recordID = try await api.uploadDocument(name: "备份计划.md", contents: Data("# 备份计划\n周日晚上八点进行家庭备份。".utf8))
            var asset: MediaAsset?
            for _ in 0..<120 {
                asset = try JSONDecoder().decode(MediaAsset.self, from: await api.request("GET", "/api/v1/assets/" + recordID))
                if ["succeeded", "failed", "canceled"].contains(asset?.processing?.status ?? "") { break }
                try await Task.sleep(for: .seconds(1))
            }
            XCTAssertEqual(asset?.processing?.status, "succeeded")
            let parsedID = try XCTUnwrap(asset?.processing?.result_record_id)
            let parsed = try JSONDecoder().decode(DataEntry.self, from: await api.request("GET", "/api/v1/data/" + parsedID))
            XCTAssertTrue(parsed.payload["markdown"]?.description.contains("八点") == true)
            let query = try JSONSerialization.data(withJSONObject: ["query": "家庭备份时间"])
            struct Match: Decodable { let excerpt: String }
            struct Search: Decodable { let mode: String; let matches: [Match] }
            let found = try JSONDecoder().decode(Search.self, from: await api.request("POST", "/api/v1/knowledge/search", body: query))
            XCTAssertEqual(found.mode, "pgvector_chunks")
            XCTAssertTrue(found.matches.contains { $0.excerpt.contains("八点") })
            _ = try await api.request("DELETE", "/api/v1/data/" + (try XCTUnwrap(parsed.source_id)))
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

final class ClientDisplayContractTests: XCTestCase {
    func testTaskSnapshotReadsNotificationRevisionWithoutTaskChanges() throws {
        let old = try JSONDecoder().decode(TaskStateEvent.self, from: Data(#"{"type":"task.snapshot","tasks":[],"has_more":false}"#.utf8))
        let updated = try JSONDecoder().decode(TaskStateEvent.self, from: Data(#"{"type":"task.snapshot","tasks":[],"has_more":false,"notification_revision":"notification-new"}"#.utf8))
        XCTAssertEqual(old.tasks, updated.tasks)
        XCTAssertNil(old.notification_revision)
        XCTAssertEqual(updated.notification_revision, "notification-new")
    }

    func testPersonalDataRemainsPersonalWhenReadingOlderCache() throws {
        let entry = try JSONDecoder().decode(DataEntry.self, from: Data(#"{"id":"a","kind":"document","sensitivity":"PRIVATE","payload":{}}"#.utf8))
        XCTAssertFalse(entry.isFamily)
    }

    func testFamilyAutomationCanBeDisplayedWithoutPrivateInstruction() throws {
        let entry = try JSONDecoder().decode(AutomationEntry.self, from: Data(#"{"id":"a","name":"家庭提醒","cron":"0 8 * * *","enabled":true,"visibility":"family","owner_id":"another-member"}"#.utf8))
        XCTAssertTrue(entry.isFamily)
        XCTAssertNil(entry.instruction)
    }

    func testFamilyNotificationDoesNotRequirePrivateTaskOrConversation() throws {
        let entry = try JSONDecoder().decode(ClientNotification.self, from: Data(#"{"id":"a","kind":"automation.updated","scope":"family","created_at":1,"read_at":null,"automation_id":"rule","status":"SUCCEEDED"}"#.utf8))
        XCTAssertEqual(entry.title, "家庭执行结果")
        XCTAssertNil(entry.task_id)
        XCTAssertNil(entry.conversation_id)
    }
}

extension ClientDisplayContractTests {
    @MainActor
    func testPairedDeviceReadsServerDisplaysWithoutMutatingData() async throws {
        let env = ProcessInfo.processInfo.environment
        guard env["HOMEAI_READONLY_CLIENT_CHECK"] == "1" || env["TEST_RUNNER_HOMEAI_READONLY_CLIENT_CHECK"] == "1" else {
            throw XCTSkip("仅在用户授权的已配对真机上执行，只读检查不会建立新配对")
        }
        let api = AppServices.api
        await api.restoreConnectionIfNeeded()
        let namespace = try await api.syncNamespace()
        struct ConversationSummary: Decodable { let id: String }
        let conversations = try JSONDecoder().decode([ConversationSummary].self, from: await api.request("GET", "/api/v1/conversations", expectedNamespace: namespace))
        let data = try JSONDecoder().decode(DataPage.self, from: await api.request("GET", "/api/v1/data", expectedNamespace: namespace))
        let memories = try JSONDecoder().decode([DataEntry].self, from: await api.request("GET", "/api/v1/memory/entries", expectedNamespace: namespace))
        XCTAssertTrue(memories.allSatisfy { $0.kind == "memory.fact" && !$0.isFamily })
        let rules = try JSONDecoder().decode([AutomationEntry].self, from: await api.request("GET", "/api/v1/automations", expectedNamespace: namespace))
        XCTAssertTrue(rules.allSatisfy { ["personal", "family"].contains($0.visibility ?? "personal") })
        let notices = try JSONDecoder().decode(NotificationInboxView.Page.self, from: await api.request("GET", "/api/v1/notifications?limit=50", expectedNamespace: namespace))
        XCTAssertTrue(notices.items.allSatisfy { ["personal", "family"].contains($0.scope) })
        struct Status: Decodable { let push_configured: Bool; let worker_online: Bool; let status: String }
        let status = try JSONDecoder().decode(Status.self, from: await api.request("GET", "/api/v1/notifications/status", expectedNamespace: namespace))
        XCTAssertTrue(status.worker_online)
        XCTAssertTrue(["ready", "not_configured", "invalid_configuration", "disabled"].contains(status.status))
        print("SERVER_DISPLAY_READ_OK conversations=\(conversations.count) data=\(data.records.count) memory=\(memories.count) automation=\(rules.count) notifications=\(notices.items.count) apns_configured=\(status.push_configured)")
    }
}


extension ClientDisplayContractTests {
    @MainActor
    func testCurrentNotificationAuthorizationStatus() async throws {
        let env = ProcessInfo.processInfo.environment
        guard env["HOMEAI_READONLY_CLIENT_CHECK"] == "1" || env["TEST_RUNNER_HOMEAI_READONLY_CLIENT_CHECK"] == "1" else {
            throw XCTSkip("仅在用户授权的真机上只读检查系统通知状态")
        }
        let settings = await UNUserNotificationCenter.current().notificationSettings()
        let authorization: String
        switch settings.authorizationStatus {
        case .authorized: authorization = "authorized"
        case .provisional: authorization = "provisional"
        case .denied: authorization = "denied"
        case .ephemeral: authorization = "ephemeral"
        default: authorization = "notDetermined"
        }
        let hasToken = DeviceIdentity.read("push-device-token")?.isEmpty == false
        print("NOTIFICATION_DEVICE_READ authorization=\(authorization) system_registered=\(UIApplication.shared.isRegisteredForRemoteNotifications) token_saved=\(hasToken) protected_data_available=\(UIApplication.shared.isProtectedDataAvailable)")
    }
}

extension ClientDisplayContractTests {
    @MainActor
    func testDeliveredAPNsNotificationReadOnly() async throws {
        let env = ProcessInfo.processInfo.environment
        guard let notificationID = env["HOMEAI_APNS_VERIFICATION_ID"] ?? env["TEST_RUNNER_HOMEAI_APNS_VERIFICATION_ID"], UUID(uuidString: notificationID) != nil else {
            throw XCTSkip("需要本机提供的单次验收通知标识；本测试只读系统已送达通知")
        }
        let delivered = await UNUserNotificationCenter.current().deliveredNotifications()
        let matching = delivered.contains { $0.request.content.userInfo["notification_id"] as? String == notificationID }
        // 已点击或清除的通知不会留在 delivered 列表，未匹配不能据此判定投递失败。
        print("APNS_DEVICE_DELIVERY_READ matching_delivered=\(matching)")
    }
}


extension ClientDisplayContractTests {
    func testNotificationResponseAlwaysCompletesOnMainThread() async {
        let callback = expectation(description: "通知回调切回主线程")
        callback.assertForOverFulfill = true
        await Task.detached {
            HomeAINotificationDelegate.finishNotificationResponse(identifier: nil) {
                XCTAssertTrue(Thread.isMainThread)
                callback.fulfill()
            }
        }.value
        await fulfillment(of: [callback], timeout: 3)
    }

    @MainActor
    func testExistingNotificationOpensThroughMainThreadRoute() async throws {
        let env = ProcessInfo.processInfo.environment
        guard let notificationID = env["HOMEAI_APNS_VERIFICATION_ID"] ?? env["TEST_RUNNER_HOMEAI_APNS_VERIFICATION_ID"], UUID(uuidString: notificationID) != nil else {
            throw XCTSkip("需要真实已存在通知；不发送新推送")
        }
        let callback = expectation(description: "真实通知路由完成")
        callback.assertForOverFulfill = true
        HomeAINotificationDelegate.finishNotificationResponse(identifier: notificationID) {
            XCTAssertTrue(Thread.isMainThread)
            callback.fulfill()
        }
        await fulfillment(of: [callback], timeout: 3)
        XCTAssertEqual(ClientNotifications.shared.pendingNotificationID, notificationID.lowercased())
        let api = AppServices.api
        await api.restoreConnectionIfNeeded()
        let namespace = try await api.syncNamespace()
        let notification = try JSONDecoder().decode(ClientNotification.self, from: await api.request("GET", "/api/v1/notifications/" + notificationID, expectedNamespace: namespace))
        XCTAssertEqual(notification.id, notificationID)
        print("NOTIFICATION_EXISTING_ROUTE_OK completion_on_main=true authenticated_read=true")
    }
}
