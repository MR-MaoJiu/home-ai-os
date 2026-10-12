import Foundation

/// 聊天展示和朗读共用同一条可见消息，不展开附件正文或操作卡片。
struct ChatSpeechMessage: Identifiable {
    enum Kind: String { case acknowledgement, answer }
    let turnID: String
    let taskID: String
    let kind: Kind
    let rawText: String
    let isStreaming: Bool
    let sources: [RecordReference]
    let sourceIdentity: String
    var id: String { turnID + ":" + kind.rawValue }
    var text: String { ChatAutomaticSpeech.spokenText(rawText) }
    var validation: ChatSpeechValidation {
        ChatSpeechValidation(turnID: turnID, taskID: taskID, kind: kind, rawText: rawText,
            acceptsContinuation: isStreaming, sourceIdentity: sourceIdentity)
    }

    static func messages(in turn: StoredChatTurn) -> [Self] {
        var messages: [Self] = []
        if let acknowledgement = turn.acknowledgement, !acknowledgement.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            messages.append(Self(turnID: turn.id, taskID: turn.task_id, kind: .acknowledgement,
                rawText: acknowledgement, isStreaming: false, sources: [], sourceIdentity: ""))
        }
        var texts: [String] = []
        var seen = Set<String>()
        for part in turn.displayedAssistantParts where part.action_id == nil {
            let candidates: [String]
            switch part.type {
            case "text": candidates = [part.text ?? ""]
            case "image", "video", "file", "h5":
                let fallback = ["image": "图片", "video": "视频", "file": "文件", "h5": "交互内容"][part.type] ?? "附件"
                candidates = [part.title ?? fallback, part.summary ?? ""]
            case "task", "approval", "choice", "data_request": candidates = []
            default: candidates = [part.summary ?? ""]
            }
            for value in candidates {
                let normalized = value.trimmingCharacters(in: .whitespacesAndNewlines)
                if !normalized.isEmpty, seen.insert(normalized).inserted { texts.append(value) }
            }
        }
        let rawText = texts.joined(separator: "\n\n")
        if !rawText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            let records = turn.sources.map { RecordReference(id: $0.record_id, title: $0.title, version: $0.version, taskID: $0.task_id) }
            let sourceVersions = turn.sources.map { "source:\($0.record_id):\($0.version ?? -1):\($0.task_id ?? "")" }
            let attachments = turn.displayedAssistantParts.filter { $0.action_id == nil }.compactMap { part -> String? in
                guard let id = part.record_id else { return nil }
                return "part:\(part.type):\(id):\(part.version ?? -1):\(part.task_id ?? "")"
            }
            messages.append(Self(turnID: turn.id, taskID: turn.task_id, kind: .answer, rawText: rawText,
                isStreaming: turn.status == "EXECUTING" && turn.interaction_kind == "chat", sources: records,
                sourceIdentity: (sourceVersions + attachments).sorted().joined(separator: "|")))
        }
        return messages
    }
}

/// 保存播放来源的可见文本；每段音频（包括缓存）播放前重新获取服务端投影校验。
struct ChatSpeechValidation {
    let turnID: String
    let taskID: String
    let kind: ChatSpeechMessage.Kind
    let rawText: String
    let acceptsContinuation: Bool
    let sourceIdentity: String

    func matches(_ turn: StoredChatTurn) -> Bool {
        guard turn.id == turnID, turn.task_id == taskID, turn.result_redacted != true,
              let message = ChatSpeechMessage.messages(in: turn).first(where: { $0.kind == kind }),
              Self.matchesSources(sourceIdentity, current: message.sourceIdentity, allowingAdditions: acceptsContinuation) else { return false }
        return acceptsContinuation ? message.rawText.hasPrefix(rawText) : message.rawText == rawText
    }

    static func matchesSources(_ original: String, current: String, allowingAdditions: Bool) -> Bool {
        if original == current { return true }
        guard allowingAdditions else { return false }
        // 流式结束时服务端可补齐来源；已有来源的版本或授权作用域变化则不是补齐。
        let old = Set(original.split(separator: "|"))
        return old.isSubset(of: Set(current.split(separator: "|")))
    }
}
