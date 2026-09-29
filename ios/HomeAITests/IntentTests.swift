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
