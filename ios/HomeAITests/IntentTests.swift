import XCTest
import AppIntents
@testable import HomeAI

final class IntentTests: XCTestCase {
    func testStandardTOTPVector() throws {
        let secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
        XCTAssertEqual(try AdminTOTP.code(secret, at: Date(timeIntervalSince1970: 59)), "287082")
        XCTAssertEqual(try AdminTOTP.code(secret, at: Date(timeIntervalSince1970: 1111111109)), "081804")
        XCTAssertThrowsError(try AdminTOTP.secret("not-a-valid-secret"))
    }

    @MainActor
    func testOpenIntentUsesSharedNavigation() async throws {
        IntentRouter.shared.destination = .ai
        _ = try await OpenHomeActivityIntent().perform()
        XCTAssertEqual(IntentRouter.shared.destination, .ai)
        XCTAssertEqual(CreateHomeReminderIntent.authenticationPolicy, .requiresLocalDeviceAuthentication)
        XCTAssertTrue(CreateHomeReminderIntent.openAppWhenRun)
        XCTAssertTrue(Bundle.main.localizations.contains("zh-Hans"))
    }

    func testEmptyReminderIsRejectedBeforeNetwork() async throws {
        do {
            _ = try await ReminderIntentService(storageKey: "intent-invalid-test").prepare(title: " \n ", namespace: "test")
            XCTFail("空内容不能提交")
        } catch APIClient.APIError.message { }
    }
}

final class ChatSnapshotTests: XCTestCase {
    private func turn(_ sequence: Int, status: String = "SUCCEEDED", answer: String = "已完成") -> StoredChatTurn {
        StoredChatTurn(id: "turn-\(sequence)", sequence: sequence, client_key: "send-\(sequence)",
                       user_text: "消息\(sequence)", task_id: "task-\(sequence)", status: status,
                       assistant_text: answer, error: nil, approvals: [], web_sources: [], sources: [])
    }

    func testIncrementalUpdatesReplacePendingAndRevokedAnswersWithoutDuplicates() {
        let original = [turn(1), turn(2, status: "EXECUTING", answer: "")]
        let finished = ChatSnapshot.merge(original, updates: [turn(2, answer: "第二轮已完成"), turn(3)])
        XCTAssertEqual(finished.map(\.sequence), [1, 2, 3])
        XCTAssertEqual(finished[1].assistant_text, "第二轮已完成")
        XCTAssertFalse(finished[1].pending)
        let redacted = ChatSnapshot.merge(finished, updates: [turn(1, answer: "关联资料已撤权，旧回答已隐藏。")])
        XCTAssertEqual(redacted.count, 3)
        XCTAssertEqual(redacted[0].assistant_text, "关联资料已撤权，旧回答已隐藏。")
    }

    func testBoundedCacheKeepsOlderPaginationAndIncrementalToken() throws {
        let snapshot = ChatSnapshot(conversationID: "default-chat", ownerNamespace: "owner-scope",
                                    turns: (1...240).map { turn($0) }, before: nil, etag: "signed-event-token", after: 240).bounded
        XCTAssertEqual(snapshot.turns.count, 200)
        XCTAssertEqual(snapshot.turns.first?.sequence, 41)
        XCTAssertEqual(snapshot.before, 41)
        let restored = try JSONDecoder().decode(ChatSnapshot.self, from: JSONEncoder().encode(snapshot))
        XCTAssertEqual(restored.conversationID, "default-chat")
        XCTAssertEqual(restored.ownerNamespace, "owner-scope")
        XCTAssertEqual(restored.etag, "signed-event-token")
        XCTAssertEqual(restored.after, 240)
    }

    func testUpdateDTORepresentsIdleAndRequiredReset() throws {
        let idle = try JSONDecoder().decode(StoredConversationUpdates.self, from: Data(#"{"turns":[],"etag":"v2","reset":false,"unchanged":true}"#.utf8))
        XCTAssertTrue(idle.unchanged)
        XCTAssertFalse(idle.reset)
        let reset = try JSONDecoder().decode(StoredConversationUpdates.self, from: Data(#"{"turns":[],"etag":"v3","reset":true,"unchanged":false}"#.utf8))
        XCTAssertTrue(reset.reset)
        XCTAssertTrue(reset.turns.isEmpty)
    }
}

import CryptoKit
import CoreLocation
import SwiftUI
import WebKit

final class NativeOrchestrationTests: XCTestCase {
    private func turn(_ status: String = "SUCCEEDED") -> StoredChatTurn {
        StoredChatTurn(id: UUID().uuidString, sequence: 1, client_key: UUID().uuidString, user_text: "请求", task_id: UUID().uuidString,
                       status: status, assistant_text: "仅本次授权可见", error: nil, approvals: [], web_sources: [], sources: [])
    }

    func testBackwardCompatibleAndRichMessageDecoding() throws {
        let old = Data(#"{"id":"turn","sequence":1,"client_key":"key","user_text":"你好","task_id":"task","status":"SUCCEEDED","assistant_text":"你好","approvals":[],"web_sources":[],"sources":[]}"#.utf8)
        XCTAssertNil(try JSONDecoder().decode(StoredChatTurn.self, from: old).user_parts)
        let parts = try JSONDecoder().decode([ChatPart].self, from: Data(#"[{"type":"image","record_id":"8177d1d6-fcfe-45f4-b9b9-6d7bdf4ef317","version":2},{"type":"h5","title":"图表","html":"<div>已登记模板</div>"},{"type":"data_request","action_id":"a"}]"#.utf8))
        XCTAssertEqual(parts[0].version, 2)
        XCTAssertEqual(parts[1].html, "<div>已登记模板</div>")
        XCTAssertEqual(parts[2].action_id, "a")
        let notification = try JSONDecoder().decode(ClientNotification.self, from: Data(#"{"id":"n","kind":"client.action","scope":"personal","created_at":1,"status":"PENDING"}"#.utf8))
        XCTAssertEqual(notification.title, "成员请求与消息")
        XCTAssertNotEqual(taskStatusLabel(try XCTUnwrap(notification.status)), "PENDING")
        for status in ["WAITING_CLIENT", "WAITING_MEDIA", "WAITING_BUDGET", "WAITING_PRIVACY", "AWAITING_APPROVAL"] {
            XCTAssertFalse(turn(status).blocksComposer, status)
            XCTAssertNotEqual(taskStatusLabel(status), status)
        }
        XCTAssertTrue(turn("EXECUTING").blocksComposer)
    }

    func testTemporaryGrantHistoryCannotBecomeOfflinePermanentCopy() throws {
        var scoped = turn()
        scoped = StoredChatTurn(id: scoped.id, sequence: 1, client_key: scoped.client_key, user_text: scoped.user_text, task_id: scoped.task_id,
            status: scoped.status, assistant_text: scoped.assistant_text, error: nil, approvals: [], web_sources: [],
            sources: [.init(record_id: UUID().uuidString, title: "其他成员资料", version: 1, task_id: scoped.task_id)])
        let snapshot = ChatSnapshot(conversationID: "conversation", ownerNamespace: "owner", turns: [scoped], before: nil, etag: "before", after: 1).bounded
        XCTAssertNil(snapshot.etag)
        XCTAssertEqual(snapshot.turns[0].requires_online_revalidation, true)
        XCTAssertTrue(snapshot.turns[0].sources.isEmpty)
        XCTAssertNotEqual(snapshot.turns[0].assistant_text, scoped.assistant_text)
        XCTAssertEqual(turn().cached().assistant_text, "仅本次授权可见")
        let record = UUID().uuidString.lowercased(), task = UUID().uuidString.lowercased()
        XCTAssertEqual(try ResourceRoutes.asset(record, taskID: task), "/api/v1/tasks/" + task + "/assets/" + record)
        XCTAssertNotEqual(ResourceRoutes.cacheScope(task), ResourceRoutes.cacheScope(nil))
        XCTAssertThrowsError(try ResourceRoutes.record(record, taskID: "../other"))
    }

    @MainActor
    func testCoarseContextAndFrozenSendSnapshot() throws {
        let observed = Date(timeIntervalSince1970: 1_800_000_000)
        let source = CLLocation(coordinate: CLLocationCoordinate2D(latitude: 31.23456, longitude: 121.45678), altitude: 0,
                                horizontalAccuracy: 8, verticalAccuracy: 0, timestamp: observed)
        let coarse = ClientContextSampler.coarse(source)
        XCTAssertEqual(coarse.latitude, 31.23, accuracy: 0.0000001)
        XCTAssertEqual(coarse.longitude, 121.46, accuracy: 0.0000001)
        XCTAssertGreaterThanOrEqual(coarse.accuracy_meters, 1500)
        let context = ClientContextSnapshot(schema_version: "1.0", app_version: "1", sampled_at: observed.ISO8601Format(), timezone: "UTC", locale: "zh_CN",
            battery_level: nil, battery_state: "unknown", low_power_mode: false, network_type: "unknown", location: coarse,
            availability: .init(battery: "unavailable", network: "unavailable", location: "available"))
        let pending = PendingChatSend(conversationID: "c", clientKey: "fixed", content: "内容", timezone: "UTC", schemaVersion: "2.0", clientContext: context)
        let restored = try JSONDecoder().decode(PendingChatSend.self, from: JSONEncoder().encode(pending))
        XCTAssertEqual(restored.clientKey, "fixed")
        XCTAssertEqual(restored.clientContext?.sampled_at, context.sampled_at)
        XCTAssertEqual(restored.clientContext?.location?.observed_at, coarse.observed_at)
    }

    func testEncryptedUploadQueuePersistsChunksAndInvalidatesNamespace() async throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent("media-queue-test-" + UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: root) }
        let key = SymmetricKey(size: .bits256)
        let store = MediaUploadStore(root: root, key: key)
        let bytes = Data(repeating: 0x51, count: MediaUploadStore.chunkSize + 87)
        let item = try await store.enqueue(data: bytes, name: "验收.txt", kind: "file", mime: "text/plain", namespace: "test-only")
        XCTAssertEqual(item.totalChunks, 2)
        XCTAssertEqual(item.sha256, DeviceIdentity.hash(bytes))
        let restored = MediaUploadStore(root: root, key: key)
        let list = try await restored.list(namespace: "test-only")
        XCTAssertEqual(list.map(\.id), [item.id])
        let other = try await restored.list(namespace: "another-member")
        XCTAssertTrue(other.isEmpty)
        let folder = root.appendingPathComponent(DeviceIdentity.hash(Data("test-only".utf8))).appendingPathComponent(item.id)
        let encrypted = try Data(contentsOf: folder.appendingPathComponent("0.enc"))
        XCTAssertNotEqual(encrypted, bytes.prefix(MediaUploadStore.chunkSize))
        let clear = try AES.GCM.open(AES.GCM.SealedBox(combined: encrypted), using: key, authenticating: Data(("test-only:" + item.id + ":0").utf8))
        XCTAssertEqual(clear, bytes.prefix(MediaUploadStore.chunkSize))
        await restored.invalidate(namespace: "test-only")
        let removed = try await restored.list(namespace: "test-only")
        XCTAssertTrue(removed.isEmpty)
        do {
            _ = try await restored.enqueue(data: bytes, name: "不可写入.txt", kind: "file", mime: "text/plain", namespace: "test-only")
            XCTFail("撤权后不应继续写入队列")
        } catch APIClient.APIError.message { }
    }

    func testMediaCacheBudgetNeverEvictsChatOrPendingUpload() async throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent("media-cache-budget-" + UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: root) }
        let cache = ClientViewCache(directory: root, key: SymmetricKey(size: .bits256), mediaBudget: 2200)
        try await cache.write(Data("既有聊天".utf8), key: "chat.default", namespace: "qa")
        try await cache.write(Data(repeating: 0x31, count: 1000), key: "asset-chunk:first:0", namespace: "qa")
        try await cache.write(Data(repeating: 0x32, count: 1000), key: "asset-chunk:second:0", namespace: "qa")
        let first = await cache.read(key: "asset-chunk:first:0", namespace: "qa")
        let second = await cache.read(key: "asset-chunk:second:0", namespace: "qa")
        let chat = await cache.read(key: "chat.default", namespace: "qa")
        XCTAssertNil(first)
        XCTAssertEqual(second, Data(repeating: 0x32, count: 1000))
        XCTAssertEqual(chat, Data("既有聊天".utf8))
        await cache.invalidate(namespace: "qa", revoked: true)
        let revoked = await cache.read(key: "asset-chunk:second:0", namespace: "qa")
        XCTAssertNil(revoked)
    }

    @MainActor
    func testH5RunsLocalScriptWithoutNetworkOrLocationBridge() async throws {
        let html = """
        <div id="value">原生隔离验收</div><script>
        window.qa={script:true,network:'pending',geo:'pending'};
        alert('不应展示原生面板'); qa.alertCompleted=true;
        qa.confirmDenied=confirm('不应确认')===false;
        qa.promptDenied=prompt('不应输入')===null;
        fetch('https://example.invalid/homeai-isolation-check').then(()=>qa.network='unexpected').catch(()=>qa.network='blocked');
        navigator.geolocation.getCurrentPosition(()=>qa.geo='unexpected',()=>qa.geo='blocked');
        </script>
        """
        let controller = UIHostingController(rootView: IsolatedHTML(html: html))
        let scene = try XCTUnwrap(UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }.first)
        let window = UIWindow(windowScene: scene); window.rootViewController = controller; window.isHidden = false
        defer { window.isHidden = true; window.rootViewController = nil }
        func web(_ view: UIView) -> WKWebView? { if let value = view as? WKWebView { return value }; return view.subviews.compactMap(web).first }
        var browser: WKWebView?
        var output: [String: Any]?
        for _ in 0..<80 {
            controller.view.layoutIfNeeded(); browser = web(controller.view)
            if let browser, let value = try? await browser.evaluateJavaScript("window.qa"), let fields = value as? [String: Any], fields["network"] as? String == "blocked", fields["geo"] as? String == "blocked" { output = fields; break }
            try await Task.sleep(for: .milliseconds(100))
        }
        let result = try XCTUnwrap(output)
        XCTAssertEqual(result["script"] as? Bool, true)
        XCTAssertEqual(result["alertCompleted"] as? Bool, true)
        XCTAssertEqual(result["confirmDenied"] as? Bool, true)
        XCTAssertEqual(result["promptDenied"] as? Bool, true)
        XCTAssertEqual(result["network"] as? String, "blocked")
        XCTAssertEqual(result["geo"] as? String, "blocked")
        XCTAssertFalse(try XCTUnwrap(browser).configuration.websiteDataStore.isPersistent)
        browser?.stopLoading()
    }

    @MainActor
    func testIsolatedRealUploadConversationAndClientActions() async throws {
        guard let encoded = ProcessInfo.processInfo.environment["HOMEAI_ORCHESTRATION_FIXTURE"], let raw = Data(base64Encoded: encoded) else {
            throw XCTSkip("仅在独立真实验收 API 中运行，不使用正式家庭或模拟网络结果")
        }
        struct Fixture: Decodable { let pairing: PairingCode }
        let fixture = try JSONDecoder().decode(Fixture.self, from: raw)
        let formalBefore = DeviceIdentity.read("connection").flatMap { try? JSONDecoder().decode(Connection.self, from: $0) }
        let api = APIClient(persistConnection: false)
        try await api.pair(fixture.pairing)
        let namespace = try await api.syncNamespace()
        let root = FileManager.default.temporaryDirectory.appendingPathComponent("live-media-qa-" + UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: root) }
        let uploadKey = SymmetricKey(size: .bits256)
        let store = MediaUploadStore(root: root, key: uploadKey)
        let text = "IOS-UPLOAD-42\n" + String(repeating: "资料续传验收。\n", count: 60_000)
        let bytes = Data(text.utf8)
        let item = try await store.enqueue(data: bytes, name: "分块原生验收.txt", kind: "file", mime: "text/plain", namespace: namespace)
        XCTAssertGreaterThan(item.totalChunks, 1)
        do {
            _ = try await store.upload(id: item.id, namespace: namespace, api: api) { progress in
                if progress.received.count == 1 { withUnsafeCurrentTask { $0?.cancel() } }
            }
            XCTFail("验收主动在首块后中断，不能已经完成")
        } catch { XCTAssertTrue(error is CancellationError) }
        let recoveredStore = MediaUploadStore(root: root, key: uploadKey)
        let savedQueue = try await recoveredStore.list(namespace: namespace)
        XCTAssertEqual(savedQueue.first?.received, [0])
        let complete = try await recoveredStore.upload(id: item.id, namespace: namespace, api: api) { _ in }
        let asset = try XCTUnwrap(complete.asset)
        XCTAssertEqual(asset.sha256, DeviceIdentity.hash(bytes))
        XCTAssertEqual(asset.size, bytes.count)
        _ = try await api.request("POST", "/_test/media/run", expectedNamespace: namespace)
        let processed = try JSONDecoder().decode(MediaAsset.self, from: await api.request("GET", "/api/v1/assets/" + asset.record_id, expectedNamespace: namespace))
        XCTAssertEqual(processed.processing?.status, "succeeded")
        let first = try await api.request("GET", "/api/v1/assets/" + asset.record_id + "/content?offset=0&length=1048576", expectedNamespace: namespace)
        XCTAssertEqual(first, bytes.prefix(1_048_576))
        struct Conversation: Decodable { let id: String }
        let conversation = try JSONDecoder().decode(Conversation.self, from: await api.request("POST", "/api/v1/conversations/default", expectedNamespace: namespace))
        let message: [String: Any] = ["schema_version": "2.0", "client_key": UUID().uuidString.lowercased(), "content": "读取附件最前面的验收标记，只回复该标记。", "timezone": "UTC", "parts": [["type": "file", "record_id": asset.record_id, "version": asset.version]]]
        struct Created: Decodable { let task_id: String }
        let created = try JSONDecoder().decode(Created.self, from: await api.request("POST", "/api/v1/conversations/" + conversation.id + "/messages", body: JSONSerialization.data(withJSONObject: message), expectedNamespace: namespace))
        var result: TaskResult?
        for _ in 0..<10 {
            _ = try await api.request("POST", "/_test/run/" + created.task_id, expectedNamespace: namespace)
            result = try JSONDecoder().decode(TaskResult.self, from: await api.request("GET", "/api/v1/tasks/" + created.task_id, expectedNamespace: namespace))
            if ["SUCCEEDED", "FAILED", "CANCELED"].contains(result?.status ?? "") { break }
        }
        XCTAssertEqual(result?.status, "SUCCEEDED", result?.error ?? "")
        let page = try JSONDecoder().decode(StoredConversationPage.self, from: await api.request("GET", "/api/v1/conversations/" + conversation.id, expectedNamespace: namespace))
        let sent = try XCTUnwrap(page.turns.first { $0.task_id == created.task_id })
        XCTAssertEqual(sent.user_parts?.first?.record_id, asset.record_id)
        XCTAssertTrue(sent.assistant_text.contains("IOS-UPLOAD-42"))
        XCTAssertFalse(sent.assistant_text.lowercased().contains("</think>"))
        for scenario in ["provide_text", "choose_option", "deny"] {
            struct Hook: Decodable { let client_action_id: String }
            let hook = try JSONDecoder().decode(Hook.self, from: await api.request("POST", "/_test/client-action", body: JSONSerialization.data(withJSONObject: ["scenario": scenario]), expectedNamespace: namespace))
            let path = "/api/v1/client-actions/" + hook.client_action_id
            let action = try JSONDecoder().decode(ClientActionRequest.self, from: await api.request("GET", path, expectedNamespace: namespace))
            XCTAssertTrue(action.can_respond)
            if let notificationID = action.notification_id {
                let inbox = ClientActionStore(); await inbox.load(api: api, notificationID: notificationID)
                XCTAssertEqual(inbox.items.map(\.id), [action.id])
            } else { XCTFail("动作需关联通知") }
            if scenario == "deny" { try await ClientActionStore.deny(action, api: api) }
            else { try await ClientActionStore.respond(action, text: scenario == "choose_option" ? "first" : "真实原生补充", api: api) }
            let updated = try JSONDecoder().decode(ClientActionRequest.self, from: await api.request("GET", path, expectedNamespace: namespace))
            XCTAssertEqual(updated.status, scenario == "deny" ? "DENIED" : "RESPONDED")
        }
        let formalAfter = DeviceIdentity.read("connection").flatMap { try? JSONDecoder().decode(Connection.self, from: $0) }
        XCTAssertEqual(formalBefore?.namespaceIdentity, formalAfter?.namespaceIdentity)
        XCTAssertEqual(formalBefore?.deviceID, formalAfter?.deviceID)
        print("isolated_native_upload_and_actions_passed=true")
    }
}
