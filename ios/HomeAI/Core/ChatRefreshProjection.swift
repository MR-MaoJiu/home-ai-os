import Foundation

/// 会话重验不能用“先清空再加载”表示；确认失权的正文仍必须立即隐藏。
enum ChatRefreshProjection {
    static func replace(_ current: [StoredChatTurn], with page: StoredConversationPage,
                        conversationID: String) throws -> [StoredChatTurn] {
        guard page.id == conversationID else { throw APIClient.APIError.message("会话已变化，请重新同步") }
        let ids = Set(page.turns.map(\.id))
        guard ids.count == page.turns.count else { throw APIClient.APIError.message("会话消息标识重复，未替换当前记录") }
        let redacted = Set(page.turns.filter { $0.result_redacted == true }.map(\.id))
        // 撤权投影优先于本地旧版本；其它回包仍遵循原有版本规则。
        return ChatSnapshot.merge(current.filter { ids.contains($0.id) && !redacted.contains($0.id) }, updates: page.turns)
    }

    static func awaitingValidation(_ turns: [StoredChatTurn]) -> [StoredChatTurn] {
        turns.map { turn in
            let hadAnswer = !turn.assistant_text.isEmpty || !(turn.assistant_parts ?? []).isEmpty || !(turn.acknowledgement ?? "").isEmpty
            return StoredChatTurn(id: turn.id, sequence: turn.sequence, client_key: turn.client_key,
                user_text: turn.user_text, task_id: turn.task_id, status: turn.status,
                assistant_text: hadAnswer ? "正在更新这条回复…" : "", error: nil,
                approvals: [], web_sources: [], sources: [], user_parts: [], assistant_parts: [],
                mentions: nil, references: nil, client_actions: [], requires_online_revalidation: true,
                result_redacted: true, budget_recoverable: turn.budget_recoverable,
                interaction_kind: turn.interaction_kind, acknowledgement: nil,
                created_at: turn.created_at, completed_at: turn.completed_at, display_revision: turn.display_revision)
        }
    }
}
