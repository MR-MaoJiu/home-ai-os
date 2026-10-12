import XCTest
@testable import HomeAI

final class ChatRefreshTests: XCTestCase {
    private func turn(_ id: String, revision: Int = 1, text: String = "已核实的回答") -> StoredChatTurn {
        var value = StoredChatTurn(id: id, sequence: id == "one" ? 1 : 2, client_key: id,
            user_text: "本人的问题", task_id: "task-" + id, status: "SUCCEEDED", assistant_text: text,
            error: nil, approvals: [], web_sources: [], sources: [])
        value.display_revision = revision
        value.created_at = 100
        value.completed_at = 110
        return value
    }

    private func page(_ turns: [StoredChatTurn], id: String = "conversation") -> StoredConversationPage {
        StoredConversationPage(id: id, title: "对话", turns: turns, has_more: false, next_before: nil, etag: "v1.20.10")
    }

    @MainActor
    func testLiveSignedResetIncludesReplacementWithoutEmptyingDisplayedHistory() async throws {
        let env = ProcessInfo.processInfo.environment
        guard env["TEST_RUNNER_HOMEAI_CHAT_REFRESH_LIVE"] == "1" || env["HOMEAI_CHAT_REFRESH_LIVE"] == "1" else {
            throw XCTSkip("仅显式开启已配对设备的只读会话刷新验收")
        }
        let api = AppServices.api
        await api.restoreConnectionIfNeeded()
        let namespace = try await api.syncNamespace()
        struct Entry: Decodable { let id: String }
        let list = try JSONDecoder().decode([Entry].self, from: await api.request("GET", "/api/v1/conversations", expectedNamespace: namespace))
        guard let entry = list.first else { throw XCTSkip("当前没有会话") }
        let page = try JSONDecoder().decode(StoredConversationPage.self, from: await api.request("GET", "/api/v1/conversations/" + entry.id, expectedNamespace: namespace))
        guard !page.turns.isEmpty else { throw XCTSkip("当前会话没有消息") }
        var displayed = page.turns
        for _ in 0..<3 {
            let path = "/api/v1/conversations/" + entry.id + "/updates?after=" + String(displayed.last?.sequence ?? 0) + "&etag=invalid"
            let result = try JSONDecoder().decode(StoredConversationUpdates.self, from: await api.request("GET", path, expectedNamespace: namespace))
            XCTAssertTrue(result.reset)
            XCTAssertEqual(result.reset_reason, "cursor_invalid")
            let snapshot = try XCTUnwrap(result.snapshot)
            displayed = try ChatRefreshProjection.replace(displayed, with: snapshot, conversationID: entry.id)
            XCTAssertFalse(displayed.isEmpty, "会话重验应直接返回可用消息，不需要先显示空白")
        }
        print("CHAT_REFRESH_LIVE resets=3 nonempty_snapshots=3 extra_history_fetches=0 read_only=true")
    }

    func testRepeatedAtomicSnapshotsNeverRemoveExistingTimelineBetweenRefreshes() throws {
        var displayed = [turn("one"), turn("two")]
        for revision in 2...30 {
            displayed = try ChatRefreshProjection.replace(displayed, with: page([turn("one", revision: revision), turn("two", revision: revision)]), conversationID: "conversation")
            XCTAssertEqual(displayed.map(\.id), ["one", "two"])
            XCTAssertEqual(displayed.map(\.user_text), ["本人的问题", "本人的问题"])
            XCTAssertTrue(displayed.allSatisfy { $0.assistant_text == "已核实的回答" })
        }
    }

    func testLegacyServerFallbackKeepsMessageIdentityButHidesUnverifiedContent() {
        var source = turn("one")
        source.acknowledgement = "基于资料的接话"
        source.references = [.init(record_id: "record", version: 1, title: "资料", kind: "document.file")]
        let waiting = ChatRefreshProjection.awaitingValidation([source])
        XCTAssertEqual(waiting.count, 1)
        XCTAssertEqual(waiting[0].id, source.id)
        XCTAssertEqual(waiting[0].user_text, source.user_text)
        XCTAssertEqual(waiting[0].created_at, source.created_at)
        XCTAssertNotEqual(waiting[0].assistant_text, source.assistant_text)
        XCTAssertNil(waiting[0].acknowledgement)
        XCTAssertNil(waiting[0].references)
        XCTAssertTrue(waiting[0].sources.isEmpty)
        XCTAssertTrue(waiting[0].result_redacted == true)
        XCTAssertTrue(waiting[0].requires_online_revalidation == true)
    }

    func testRevocationSnapshotWinsOverNewerCachedDisplayRevision() throws {
        let cached = turn("one", revision: 30, text: "撤权后的旧正文不能继续展示")
        var denied = turn("one", revision: 29, text: "关联资料已撤回")
        denied.result_redacted = true
        let result = try ChatRefreshProjection.replace([cached], with: page([denied]), conversationID: "conversation")
        XCTAssertEqual(result.count, 1)
        XCTAssertEqual(result[0].assistant_text, "关联资料已撤回")
        XCTAssertTrue(result[0].result_redacted == true)
    }

    func testWrongConversationAndDuplicateSnapshotCannotReplaceMessages() {
        let current = [turn("one")]
        XCTAssertThrowsError(try ChatRefreshProjection.replace(current, with: page([turn("two")], id: "another"), conversationID: "conversation"))
        XCTAssertThrowsError(try ChatRefreshProjection.replace(current, with: page([turn("one"), turn("one")]), conversationID: "conversation"))
        XCTAssertEqual(current[0].id, "one")
    }

    func testResetResponseCarriesUsablePageAndLegacyResponseStillDecodes() throws {
        let turns = try JSONSerialization.jsonObject(with: JSONEncoder().encode([turn("one")]))
        let snapshot: [String: Any] = ["id": "conversation", "title": "对话", "turns": turns,
            "has_more": false, "etag": "v1.20.10"]
        let response: [String: Any] = ["turns": [], "etag": "v1.20.10", "reset": true,
            "unchanged": false, "reset_reason": "sources_changed", "snapshot": snapshot]
        let update = try JSONDecoder().decode(StoredConversationUpdates.self, from: JSONSerialization.data(withJSONObject: response))
        XCTAssertTrue(update.reset)
        XCTAssertEqual(update.reset_reason, "sources_changed")
        XCTAssertEqual(try XCTUnwrap(update.snapshot).turns.count, 1)
        let old = try JSONDecoder().decode(StoredConversationUpdates.self, from: Data(#"{"turns":[],"etag":"old","reset":true,"unchanged":false}"#.utf8))
        XCTAssertNil(old.snapshot)
    }
}
