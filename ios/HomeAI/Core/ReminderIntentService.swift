import Foundation
import UIKit

/// 主应用与 Intent 使用同一个认证 actor，避免并发轮换同一刷新凭据。
enum AppServices {
    static let api = APIClient()
}

actor ReminderIntentService {
    static let shared = ReminderIntentService()
    struct Pending: Codable, Sendable {
        let namespace: String
        let title: String
        let idempotencyKey: String
        var dueAt: String? = nil
        var notifyAtDue: Bool? = nil
        var storageID: String? = nil
    }
    private let storageKey: String
    private let gate = AsyncOperationGate()
    init(storageKey: String = "intent-reminder-pending") { self.storageKey = storageKey }

    func prepare(title: String, namespace: String, dueDate: Date? = nil, notify: Bool = false) throws -> Pending {
        let title = title.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !title.isEmpty, title.count <= 500 else { throw APIClient.APIError.message("提醒内容需为 1 至 500 个字符") }
        var pending = try load()
        guard !notify || dueDate != nil else { throw APIClient.APIError.message("请先指定到期时间") }
        let dueAt = dueDate?.ISO8601Format()
        let encoder = JSONEncoder(); encoder.outputFormatting = .sortedKeys
        let identity = [namespace, title, dueAt ?? "", notify ? "notify" : "silent"]
        let key = DeviceIdentity.hash(try encoder.encode(identity))
        if let existing = pending[key] { return existing }
        if dueDate == nil && !notify {
            let legacy = DeviceIdentity.hash(Data((namespace + ":" + title).utf8))
            if let existing = pending[legacy], existing.title == title, existing.namespace == namespace, existing.dueAt == nil { return existing }
        }
        guard pending.count < 20 else { throw APIClient.APIError.message("未确认的快捷指令提交过多，请先恢复连接并检查对话") }
        let request = Pending(namespace: namespace, title: title, idempotencyKey: UUID().uuidString, dueAt: dueAt, notifyAtDue: notify, storageID: key)
        pending[key] = request
        try DeviceIdentity.save(JSONEncoder().encode(pending), name: storageKey)
        return request
    }

    private func load() throws -> [String: Pending] {
        guard let data = DeviceIdentity.read(storageKey) else { return [:] }
        return try JSONDecoder().decode([String: Pending].self, from: data)
    }

    func submit(title: String, dueDate: Date? = nil, notify: Bool = false, api: APIClient = AppServices.api) async throws -> String {
        try await gate.withPermit { try await self.submitLocked(title: title, dueDate: dueDate, notify: notify, api: api) }
    }

    private func submitLocked(title: String, dueDate: Date?, notify: Bool, api: APIClient) async throws -> String {
        guard await MainActor.run(body: { UIApplication.shared.isProtectedDataAvailable }) else {
            throw APIClient.APIError.message("请先解锁设备")
        }
        await api.restoreConnectionIfNeeded()
        let namespace = try await api.syncNamespace()
        let pending = try prepare(title: title, namespace: namespace, dueDate: dueDate, notify: notify)
        var arguments: [String: Any] = ["title": pending.title]
        if let due = pending.dueAt { arguments["due_at"] = due; arguments["notify_at_due"] = pending.notifyAtDue ?? false }
        arguments["idempotency_key"] = pending.idempotencyKey
        arguments["timezone"] = TimeZone.current.identifier
        let body = try JSONSerialization.data(withJSONObject: arguments)
        struct Identifier: Decodable { let id: String; let conversation_id: String }
        let result = try JSONDecoder().decode(Identifier.self, from: await api.request("POST", "/api/v1/input/reminder", body: body, expectedNamespace: namespace))
        guard namespace == (try await api.syncNamespace()) else { throw APIClient.APIError.message("连接已切换，请在原服务器对话中确认提交结果") }
        guard UUID(uuidString: result.id) != nil else { throw APIClient.APIError.message("服务器返回的任务标识无效") }
        var entries = try load()
        entries.removeValue(forKey: pending.storageID ?? DeviceIdentity.hash(Data((namespace + ":" + pending.title).utf8)))
        try DeviceIdentity.save(JSONEncoder().encode(entries), name: storageKey)
        await MainActor.run { IntentRouter.shared.conversationID = result.conversation_id }
        return result.id
    }
}

