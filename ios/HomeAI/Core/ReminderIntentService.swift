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
    }
    private let storageKey: String
    private let gate = AsyncOperationGate()
    init(storageKey: String = "intent-reminder-pending") { self.storageKey = storageKey }

    func prepare(title: String, namespace: String) throws -> Pending {
        let title = title.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !title.isEmpty, title.count <= 500 else { throw APIClient.APIError.message("提醒内容需为 1 至 500 个字符") }
        var pending = try load()
        let key = DeviceIdentity.hash(Data((namespace + ":" + title).utf8))
        if let existing = pending[key] { return existing }
        guard pending.count < 20 else { throw APIClient.APIError.message("未确认的快捷指令提交过多，请先恢复连接并检查活动") }
        let request = Pending(namespace: namespace, title: title, idempotencyKey: UUID().uuidString)
        pending[key] = request
        try DeviceIdentity.save(JSONEncoder().encode(pending), name: storageKey)
        return request
    }

    private func load() throws -> [String: Pending] {
        guard let data = DeviceIdentity.read(storageKey) else { return [:] }
        return try JSONDecoder().decode([String: Pending].self, from: data)
    }

    func submit(title: String, api: APIClient = AppServices.api) async throws -> String {
        try await gate.withPermit { try await self.submitLocked(title: title, api: api) }
    }

    private func submitLocked(title: String, api: APIClient) async throws -> String {
        guard await MainActor.run(body: { UIApplication.shared.isProtectedDataAvailable }) else {
            throw APIClient.APIError.message("请先解锁设备")
        }
        await api.restoreConnectionIfNeeded()
        let namespace = try await api.syncNamespace()
        let pending = try prepare(title: title, namespace: namespace)
        let body = try JSONSerialization.data(withJSONObject: ["idempotency_key": pending.idempotencyKey,
            "capability": "reminder.create@v1", "arguments": ["title": pending.title]])
        struct Identifier: Decodable { let id: String }
        let result = try JSONDecoder().decode(Identifier.self, from: await api.request("POST", "/api/v1/tasks", body: body, expectedNamespace: namespace))
        guard namespace == (try await api.syncNamespace()) else { throw APIClient.APIError.message("连接已切换，请在原服务器活动中确认提交结果") }
        guard UUID(uuidString: result.id) != nil else { throw APIClient.APIError.message("服务器返回的任务标识无效") }
        var entries = try load()
        entries.removeValue(forKey: DeviceIdentity.hash(Data((namespace + ":" + pending.title).utf8)))
        try DeviceIdentity.save(JSONEncoder().encode(entries), name: storageKey)
        return result.id
    }
}

