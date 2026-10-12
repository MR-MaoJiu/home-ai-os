import Foundation
import Observation

/// 先保存再发出。连接变化只重试同一个消息标识，不创建新的服务端任务。
@MainActor @Observable
final class ChatOutbox {
    static let shared = ChatOutbox()
    struct Entry: Codable, Identifiable {
        var message: PendingChatSend
        var queuedAt = Date()
        var uncertain = false
        var blocked = false
        var error: String?
        var id: String { message.clientKey }
    }
    private(set) var entries: [Entry] = []
    private(set) var sendingID: String?
    private(set) var owner = ""
    private var storageKey: String { "chat.outbox:" + owner }
    private var retryAfter = Date.distantPast
    private var run: UUID?

    func activate(owner: String) throws {
        guard self.owner != owner else { return }
        var restored: [Entry] = []
        if let data = DeviceIdentity.read("chat.outbox:" + owner), !data.isEmpty { restored = try JSONDecoder().decode([Entry].self, from: data) }
        self.owner = owner; entries = restored; sendingID = nil; run = nil; retryAfter = .distantPast
        let legacy = "chat.pending:" + owner
        if let data = DeviceIdentity.read(legacy), let old = try? JSONDecoder().decode(PendingChatSend.self, from: data), !entries.contains(where: { $0.id == old.clientKey }) {
            try save(entries + [Entry(message: old, blocked: true, error: "旧版未确认的草稿已保留，请确认后重试")])
            try DeviceIdentity.save(Data(), name: legacy)
        }
    }
    private func save(_ value: [Entry]) throws {
        guard !owner.isEmpty else { throw APIClient.APIError.message("发送身份尚未恢复") }
        try DeviceIdentity.save(JSONEncoder().encode(value), name: storageKey)
        entries = value
    }
    func enqueue(_ value: PendingChatSend) throws {
        guard entries.count < 20 else { throw APIClient.APIError.message("待发送消息已达20条，请连接后继续") }
        guard !entries.contains(where: { $0.id == value.clientKey }) else { return }
        try save(entries + [Entry(message: value)])
    }
    func remove(_ id: String) throws {
        guard sendingID != id else { throw APIClient.APIError.message("正在确认发送结果，请稍后移除") }
        try save(entries.filter { $0.id != id }); retryAfter = .distantPast
    }
    func retry(_ id: String) throws {
        guard let index = entries.firstIndex(where: { $0.id == id }), sendingID == nil else { return }
        var updated = entries; updated[index].blocked = false; updated[index].error = nil; updated[index].queuedAt = Date()
        try save(updated); retryAfter = .distantPast
    }
    func acknowledge(_ clientKeys: Set<String>) throws {
        let remaining = entries.filter { !clientKeys.contains($0.id) }
        if remaining.count != entries.count { try save(remaining) }
    }
    func clearIdentity() { owner = ""; entries = []; sendingID = nil; run = nil }

    func sendNext(api: APIClient, namespace: String) async -> StoredChatTurn? {
        guard sendingID == nil, Date() >= retryAfter, let entry = entries.first, !entry.blocked else { return nil }
        if Date().timeIntervalSince(entry.queuedAt) > 900 {
            var updated = entries; updated[0].blocked = true; updated[0].error = "离线超过15分钟，请确认后重试"
            try? save(updated)
            return nil
        }
        let expectedOwner = owner, operation = UUID()
        run = operation; sendingID = entry.id
        defer { if run == operation { sendingID = nil; run = nil } }
        var mayHaveSent = entry.uncertain
        do {
            guard await api.cachedNamespace() == namespace else { throw CancellationError() }
            let identity = try await api.ownerIdentity(expectedNamespace: namespace)
            guard identity.namespace == expectedOwner, owner == expectedOwner else { throw CancellationError() }
            var message = entry.message
            if message.conversationID.isEmpty {
                struct Conversation: Decodable { let id: String }
                message.conversationID = try JSONDecoder().decode(Conversation.self, from: await api.request("POST", "/api/v1/conversations/default", expectedNamespace: namespace)).id
                guard owner == expectedOwner, run == operation, let index = entries.firstIndex(where: { $0.id == entry.id }) else { return nil }
                var updated = entries; updated[index].message = message; try save(updated)
            }
            let body = try Self.payload(message)
            try Task.checkCancellation()
            guard owner == expectedOwner, run == operation, let index = entries.firstIndex(where: { $0.id == entry.id }) else { return nil }
            var inFlight = entries; inFlight[index].uncertain = true; try save(inFlight)
            mayHaveSent = true
            let data = try await api.request("POST", "/api/v1/conversations/" + message.conversationID + "/messages", body: body, expectedNamespace: namespace)
            let received = try JSONDecoder().decode(StoredChatTurn.self, from: data)
            guard received.client_key == entry.id, owner == expectedOwner, run == operation,
                  await api.cachedNamespace() == namespace else { throw CancellationError() }
            try save(entries.filter { $0.id != entry.id })
            return received
        } catch {
            guard owner == expectedOwner, run == operation, let index = entries.firstIndex(where: { $0.id == entry.id }) else { return nil }
            var updated = entries
            if case APIClient.APIError.http(let code, let reason) = error {
                updated[index].blocked = (400..<500).contains(code) && ![408, 409, 429].contains(code)
                updated[index].uncertain = code >= 500 && mayHaveSent
                updated[index].error = updated[index].blocked ? reason : "等待服务器接收"
            } else {
                updated[index].uncertain = mayHaveSent
                updated[index].error = mayHaveSent ? "等待确认，恢复后使用原消息标识重试" : "等待连接家庭服务器"
            }
            try? save(updated)
            retryAfter = Date().addingTimeInterval(3)
            return nil
        }
    }

    static func payload(_ pending: PendingChatSend) throws -> Data {
        var value: [String: Any] = ["client_key": pending.clientKey, "content": pending.content, "timezone": pending.timezone]
        if let schema = pending.schemaVersion {
            value["schema_version"] = schema
            value["parts"] = try JSONSerialization.jsonObject(with: JSONEncoder().encode(pending.parts ?? []))
            value["mentions"] = try JSONSerialization.jsonObject(with: JSONEncoder().encode(pending.mentions ?? []))
            value["references"] = (pending.references ?? []).map(\.wireValue)
            if let context = pending.clientContext { value["client_context"] = try context.json() }
            if let reply = pending.replyToTaskID { value["reply_to_task_id"] = reply }
        }
        return try JSONSerialization.data(withJSONObject: value, options: [.sortedKeys])
    }
}
