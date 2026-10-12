import Foundation
import Observation

struct AutomaticReviewConfiguration: Decodable, Sendable {
    let enabled: Bool
    let device_id: String
    let scope: String
}

/// 开关以服务器回执为准；不同配对身份之间不继承，也不缓存为离线授权。
@MainActor @Observable
final class AutomaticReviewStore {
    typealias Request = @MainActor (String, Data?) async throws -> Data
    private(set) var enabled = false
    private(set) var verified = false
    private(set) var updating = false
    private(set) var namespace: String?
    private(set) var generation = UUID()
    var message: String?

    func suspend(clearIdentity: Bool = false) {
        generation = UUID(); verified = false; updating = false
        if clearIdentity { enabled = false; namespace = nil; message = nil }
    }

    private func bind(_ value: String) {
        if namespace != value { suspend(clearIdentity: true); namespace = value }
    }

    func load(namespace: String, request: Request) async {
        bind(namespace)
        guard !updating else { return }
        let ticket = UUID(); generation = ticket
        do {
            let bytes = try await request("GET", nil)
            guard generation == ticket, self.namespace == namespace, !Task.isCancelled else { return }
            try receive(bytes)
            message = nil
        } catch {
            guard generation == ticket, self.namespace == namespace else { return }
            verified = false
            if !(error is CancellationError) { message = "无法核对自动审核状态：" + error.localizedDescription }
        }
    }

    func change(enabled desired: Bool, acknowledged: Bool, namespace: String, request: Request) async {
        bind(namespace)
        guard !updating else { return }
        guard !desired || acknowledged else { message = "请先确认自动审核的影响。"; return }
        let ticket = UUID(); generation = ticket
        updating = true; verified = false; message = nil
        defer { if generation == ticket { updating = false } }
        do {
            let body = try JSONSerialization.data(withJSONObject: ["enabled": desired, "acknowledged": acknowledged])
            let bytes = try await request("PUT", body)
            guard generation == ticket, self.namespace == namespace, !Task.isCancelled else { return }
            try receive(bytes)
            guard enabled == desired else { throw APIClient.APIError.message("服务器返回的开关状态与本次操作不一致") }
        } catch {
            guard generation == ticket, self.namespace == namespace else { return }
            verified = false
            // 请求可能已经送达，保留已确认状态，重新读取前不宣称开启或关闭成功。
            message = "自动审核状态尚未确认，请恢复连接后重试。" + (error is CancellationError ? "" : " " + error.localizedDescription)
        }
    }

    private func receive(_ bytes: Data) throws {
        let value = try JSONDecoder().decode(AutomaticReviewConfiguration.self, from: bytes)
        guard value.scope == "current_device_tasks", !value.device_id.isEmpty else {
            throw APIClient.APIError.message("服务器未返回本设备的审核范围")
        }
        enabled = value.enabled; verified = true
    }

    func permitsCapture(namespace: String) -> Bool {
        self.namespace == namespace && verified && enabled && !updating
    }
}

/// 仅完成需要手机采集的明确请求；批准命令、披露和固定资料授权由服务端处理。
@MainActor @Observable
final class AutomaticReviewCapture {
    private var operation: Task<Void, Never>?
    private var namespace: String?
    private var attempted = Set<String>()
    private var generation = UUID()
    private(set) var message: String?
    private var messageActionID: String?

    func stop(clearAttempts: Bool = false) {
        operation?.cancel(); operation = nil; generation = UUID()
        if clearAttempts { attempted = []; namespace = nil; message = nil; messageActionID = nil }
    }

    func process(_ actions: [ClientActionRequest], namespace: String, store: AutomaticReviewStore,
                 api: APIClient, isActive: @escaping @MainActor () -> Bool,
                 didRespond: @escaping @MainActor () async -> Void) {
        if self.namespace != namespace { stop(clearAttempts: true); self.namespace = namespace }
        if let identifier = messageActionID, !actions.contains(where: { $0.id == identifier && $0.pending }) {
            message = nil; messageActionID = nil
        }
        guard operation == nil, store.permitsCapture(namespace: namespace), isActive() else { return }
        let candidates = actions.filter {
            $0.pending && $0.can_respond && $0.automatic_capture_allowed == true &&
            ["read_health", "capture_location"].contains($0.kind) && !attempted.contains($0.id)
        }
        guard !candidates.isEmpty else { return }
        let ticket = UUID(); generation = ticket
        operation = Task {
            defer { if generation == ticket { operation = nil } }
            for candidate in candidates {
                guard generation == ticket, !Task.isCancelled, isActive(), store.permitsCapture(namespace: namespace) else { return }
                guard AutomaticClientActionAttempts.shared.begin(actionID: candidate.id, namespace: namespace, expiresAt: candidate.expires_at) else { continue }
                var finishedAttempt = false
                defer { AutomaticClientActionAttempts.shared.finish(actionID: candidate.id, namespace: namespace, retryable: !finishedAttempt) }
                attempted.insert(candidate.id)
                do {
                    let raw = try await api.request("GET", "/api/v1/client-actions/" + candidate.id, expectedNamespace: namespace)
                    let current = try JSONDecoder().decode(ClientActionRequest.self, from: raw)
                    guard current.id == candidate.id, current.pending, current.automatic_capture_allowed == true else { finishedAttempt = true; continue }
                    let identity = try await api.ownerIdentity(expectedNamespace: namespace)
                    let permits: @MainActor () -> Bool = {
                        self.generation == ticket && !Task.isCancelled && isActive() && store.permitsCapture(namespace: namespace)
                    }
                    let records = try await ClientActionStore.captureAfterAutomaticReview(current, memberID: identity.userID,
                        api: api, expectedNamespace: namespace, isEnabled: permits)
                    guard permits() else { throw CancellationError() }
                    _ = try await ClientActionStore.respond(current, recordIDs: records, api: api,
                        expectedNamespace: namespace, automaticReview: true)
                    guard generation == ticket else { return }
                    finishedAttempt = true; message = nil; messageActionID = nil
                    await didRespond()
                } catch {
                    guard generation == ticket else { return }
                    if error is CancellationError { attempted.remove(candidate.id); return }
                    finishedAttempt = true
                    messageActionID = candidate.id
                    message = "有请求需要你处理：" + error.localizedDescription
                }
            }
        }
    }
}
