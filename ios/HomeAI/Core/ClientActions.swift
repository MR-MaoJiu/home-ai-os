import Foundation
import HealthKit
import Observation
import UIKit

struct ClientActionRequest: Codable, Identifiable, Sendable, Equatable {
    struct Requester: Codable, Sendable, Equatable { let id: String; let name: String }
    let id: String
    let kind: String
    let status: String
    let purpose: String
    let requested_by: Requester
    let target_member_id: String
    let scope: String
    let created_at: Double
    let expires_at: Double
    let parameters: [String: JSONValue]
    let task_id: String?
    let conversation_id: String?
    let notification_id: String?
    let can_respond: Bool
    let can_revoke: Bool
    var data_status: String? = nil
    var note: String? = nil
    var automatic_capture_allowed: Bool? = nil
    func effectiveStatus(at date: Date = Date()) -> String { status == "PENDING" && expires_at <= date.timeIntervalSince1970 ? "EXPIRED" : status }
    var pending: Bool { effectiveStatus() == "PENDING" }
    var finished: Bool { effectiveStatus() != "PENDING" }
    /// 仅用于说明补充入口；实际提交仍由服务端核对当前身份、请求范围和有效期。
    var isPersonalClarification: Bool {
        ["provide_text", "choose_option"].contains(kind) && scope == "personal" &&
            requested_by.id == target_member_id && task_id != nil
    }
    func awaitsPersonalClarification(for taskID: String, at date: Date = Date()) -> Bool {
        isPersonalClarification && task_id == taskID && can_respond && effectiveStatus(at: date) == "PENDING"
    }
    func permitsAutomaticCoarseLocation(at date: Date = Date()) -> Bool {
        kind == "capture_location" && parameters["precision"] == .string("coarse") &&
            scope == "personal" && requested_by.id == target_member_id && task_id != nil &&
            can_respond && effectiveStatus(at: date) == "PENDING"
    }
    /// 自动审核不替代系统权限，也不代替其他成员作出决定。
    func permitsAutomaticallyReviewedCapture(memberID: String, at date: Date = Date()) -> Bool {
        guard automatic_capture_allowed == true, ["capture_location", "read_health"].contains(kind), scope == "personal",
              requested_by.id == memberID, target_member_id == memberID, task_id != nil,
              can_respond, effectiveStatus(at: date) == "PENDING" else { return false }
        if kind == "capture_location" {
            return [JSONValue.string("coarse"), .string("precise")].contains(parameters["precision"] ?? .string("coarse"))
        }
        return (try? HealthReadScope(parameters: parameters)) != nil
    }
    var statusLabel: String {
        label(for: effectiveStatus())
    }
    var summaryLabel: String {
        let state = effectiveStatus()
        guard state == "RESPONDED" else { return label(for: state) }
        if kind == "read_health" {
            return ["no_data": "已告知暂无可读健康数据", "partial": "已提供部分健康数据", "available": "健康数据已提供"][data_status ?? ""] ?? "健康读取结果已提交"
        }
        return ["android.assist": "已继续安卓任务", "cloud.disclose": "已允许本次云端披露", "choose_option": "已完成选择", "provide_text": "补充信息已提交",
                "data.share": "已授权本次使用", "authorize_records": "已授权本次使用",
                "member.notify": task_id == nil ? "成员消息" : "成员消息已发送"][kind] ?? "资料已提供"
    }
    private func label(for state: String) -> String {
        if kind == "android.assist" {
            return ["PENDING": "等待你处理", "RESPONDED": "已继续任务", "DENIED": "已取消任务", "CANCELED": "已取消任务", "EXPIRED": "协助请求已过期", "REVOKED": "协助请求已撤回"][state] ?? state
        }
        if state == "RESPONDED" {
            if kind == "read_health" { return summaryLabel }
            return ["cloud.disclose": "已允许", "choose_option": "已选择", "provide_text": "已补充",
                    "data.share": "已授权", "authorize_records": "已授权", "member.notify": "成员消息"][kind] ?? "已提供"
        }
        return ["PENDING": "等待处理", "DENIED": "已拒绝", "EXPIRED": "已过期", "REVOKED": "已撤回", "CANCELED": "已取消"][state] ?? state
    }
    func applyingReceipt(_ receipt: ClientActionReceipt) -> Self {
        Self(id: id, kind: kind, status: receipt.status, purpose: purpose, requested_by: requested_by,
             target_member_id: target_member_id, scope: scope, created_at: created_at, expires_at: expires_at,
             parameters: parameters, task_id: task_id, conversation_id: conversation_id, notification_id: notification_id,
             can_respond: false, can_revoke: false, data_status: receipt.data_status, note: receipt.note, automatic_capture_allowed: false)
    }
    func integer(_ key: String, default fallback: Int) -> Int {
        if case .number(let value) = parameters[key], let value = Int(exactly: value) { return value }
        return fallback
    }
    func strings(_ key: String) -> [String] {
        guard case .array(let values) = parameters[key] else { return [] }
        return values.compactMap { if case .string(let value) = $0 { return value }; return nil }
    }
}

struct ClientActionReceipt: Codable, Equatable, Sendable {
    let id: String
    let status: String
    var data_status: String? = nil
    var note: String? = nil
    static let handledStatuses: Set<String> = ["RESPONDED", "DENIED", "EXPIRED", "REVOKED", "CANCELED"]
}

/// 同一身份下每个请求仅自动尝试一次；多张卡片共用采集锁，失败后保留手动重试。
@MainActor
final class AutomaticClientActionAttempts {
    static let shared = AutomaticClientActionAttempts()
    private var attempted: [String: Double] = [:]
    private var active: String?

    func begin(actionID: String, namespace: String, expiresAt: Double, now: Double = Date().timeIntervalSince1970) -> Bool {
        attempted = attempted.filter { $0.value > now }
        let key = namespace + ":" + actionID
        guard expiresAt > now, active == nil, attempted[key] == nil else { return false }
        attempted[key] = expiresAt; active = key
        return true
    }

    func finish(actionID: String, namespace: String, retryable: Bool = false) {
        let key = namespace + ":" + actionID
        if active == key { active = nil }
        if retryable { attempted.removeValue(forKey: key) }
    }
}

/// 处理回执立即结束交互；较早返回的列表不能把已处理请求重新变成待操作。
struct ClientActionPresentation {
    private(set) var current: ClientActionRequest
    init(request: ClientActionRequest) { current = request }
    static func accepts(from: String, to: String) -> Bool {
        if from == to { return true }
        if from == "PENDING" { return true }
        if from == "RESPONDED" { return ClientActionReceipt.handledStatuses.contains(to) }
        return false
    }
    @discardableResult mutating func receive(_ request: ClientActionRequest) -> Bool {
        guard request.id == current.id else { current = request; return true }
        guard Self.accepts(from: current.status, to: request.status) else { return false }
        current = request
        return true
    }
    @discardableResult mutating func acknowledge(_ receipt: ClientActionReceipt) -> Bool {
        guard receipt.id == current.id, ClientActionReceipt.handledStatuses.contains(receipt.status),
              Self.accepts(from: current.status, to: receipt.status) else { return false }
        if receipt.status == current.status && receipt.data_status == nil && receipt.note == nil { return true }
        current = current.applyingReceipt(receipt)
        return true
    }
}

@MainActor @Observable
final class ClientActionStore {
    var items: [ClientActionRequest] = []
    var loading = false
    var error: String?
    var hasMore = false
    private var nextBefore: Double?
    private var namespace: String?
    private var dirty = false

    func load(api: APIClient, notificationID: String? = nil, older: Bool = false) async {
        guard !loading else { dirty = true; return }
        loading = true
        defer { loading = false; if dirty { dirty = false; Task { await self.load(api: api, notificationID: notificationID) } } }
        do {
            let current = try await api.syncNamespace()
            if namespace != current { items = []; namespace = current; nextBefore = nil; hasMore = false }
            struct Page: Decodable { let items: [ClientActionRequest]; let has_more: Bool; let next_before: Double? }
            let path = "/api/v1/client-actions?limit=100" + (notificationID.map { "&notification_id=" + $0 } ?? "") + (older ? (nextBefore.map { "&before=" + String($0) } ?? "") : "")
            let data = try await api.request("GET", path, expectedNamespace: current)
            guard !Task.isCancelled, await api.cachedNamespace() == current else { return }
            let page = try JSONDecoder().decode(Page.self, from: data)
            if older { items += page.items.filter { item in !items.contains(where: { $0.id == item.id }) } } else { items = page.items }
            nextBefore = page.next_before; hasMore = page.has_more; error = nil
        } catch { self.error = error.localizedDescription; if case APIClient.APIError.http(401, _) = error { items = [] } }
    }

    private static func resolveNamespace(api: APIClient, expected: String?) async throws -> String {
        let current = try await api.syncNamespace()
        guard expected == nil || current == expected else { throw APIClient.APIError.message("连接身份已变化，请重新打开请求") }
        return current
    }
    static func cachedReceipt(actionID: String, namespace: String) async -> ClientActionReceipt? {
        guard let bytes = await ClientViewCache.shared.read(key: "client-action-receipt:" + actionID, namespace: namespace),
              let receipt = try? JSONDecoder().decode(ClientActionReceipt.self, from: bytes), receipt.id == actionID,
              ClientActionReceipt.handledStatuses.contains(receipt.status) else { return nil }
        return receipt
    }
    static func cacheReceipt(_ receipt: ClientActionReceipt, namespace: String) async throws {
        guard ClientActionReceipt.handledStatuses.contains(receipt.status) else { return }
        if let previous = await cachedReceipt(actionID: receipt.id, namespace: namespace),
           !ClientActionPresentation.accepts(from: previous.status, to: receipt.status) { return }
        try await ClientViewCache.shared.write(JSONEncoder().encode(receipt), key: "client-action-receipt:" + receipt.id, namespace: namespace)
    }
    private static func receipt(_ raw: Data, actionID: String, namespace: String) async throws -> ClientActionReceipt {
        let value = try JSONDecoder().decode(ClientActionReceipt.self, from: raw)
        guard value.id == actionID, ClientActionReceipt.handledStatuses.contains(value.status) else { throw APIClient.APIError.message("服务器未返回可核对的处理回执，请刷新状态") }
        try? await cacheReceipt(value, namespace: namespace)
        return value
    }
    @discardableResult
    static func respond(_ action: ClientActionRequest, recordIDs: [String] = [], text: String? = nil, api: APIClient, expectedNamespace: String? = nil,
                        automaticReview: Bool = false) async throws -> ClientActionReceipt {
        if action.kind == "android.assist" && (!recordIDs.isEmpty || text != nil) {
            throw APIClient.APIError.message("请直接在安卓设备完成处理，再继续任务。")
        }
        let namespace = try await resolveNamespace(api: api, expected: expectedNamespace)
        let key = "client-action-response:" + action.id
        let body: Data
        var intended: [String: Any] = [:]
        if !["cloud.disclose", "android.assist"].contains(action.kind) { intended["record_ids"] = recordIDs.sorted(); if let text { intended["text"] = text } }
        if automaticReview { intended["automatic_review"] = true }
        let intendedData = try JSONSerialization.data(withJSONObject: intended, options: [.sortedKeys])
        if let pending = await ClientViewCache.shared.read(key: key, namespace: namespace),
           var previous = (try? JSONSerialization.jsonObject(with: pending)) as? [String: Any] {
            previous.removeValue(forKey: "idempotency_key")
            if let ids = previous["record_ids"] as? [String] { previous["record_ids"] = ids.sorted() }
            if (try? JSONSerialization.data(withJSONObject: previous, options: [.sortedKeys])) == intendedData { body = pending }
            else { intended["idempotency_key"] = UUID().uuidString.lowercased(); body = try JSONSerialization.data(withJSONObject: intended) }
        } else {
            intended["idempotency_key"] = UUID().uuidString.lowercased()
            body = try JSONSerialization.data(withJSONObject: intended)
        }
        try await ClientViewCache.shared.write(body, key: key, namespace: namespace)
        let route = action.kind == "android.assist" ? "/android-continue" : "/respond"
        let raw = try await api.request("POST", "/api/v1/client-actions/" + action.id + route, body: body, expectedNamespace: namespace)
        let confirmed = try await receipt(raw, actionID: action.id, namespace: namespace)
        await ClientViewCache.shared.remove(key: key, namespace: namespace)
        await ClientViewCache.shared.remove(key: "client-action-capture:" + action.id, namespace: namespace)
        return confirmed
    }

    @discardableResult
    static func deny(_ action: ClientActionRequest, api: APIClient, expectedNamespace: String? = nil) async throws -> ClientActionReceipt {
        let namespace = try await resolveNamespace(api: api, expected: expectedNamespace)
        let storage = "client-action-denial:" + namespace + ":" + action.id
        let identifier = DeviceIdentity.read(storage).flatMap { String(data: $0, encoding: .utf8) } ?? UUID().uuidString.lowercased()
        try DeviceIdentity.save(Data(identifier.utf8), name: storage)
        let reason = action.kind == "android.assist" ? "用户取消本次安卓任务" : "用户选择不提供本次资料或授权"
        let body = try JSONSerialization.data(withJSONObject: ["idempotency_key": identifier, "reason": reason])
        let raw = try await api.request("POST", "/api/v1/client-actions/" + action.id + "/deny", body: body, expectedNamespace: namespace)
        let confirmed = try await receipt(raw, actionID: action.id, namespace: namespace)
        await ClientViewCache.shared.remove(key: "client-action-response:" + action.id, namespace: namespace)
        return confirmed
    }

    @discardableResult
    static func revoke(_ action: ClientActionRequest, api: APIClient, expectedNamespace: String? = nil) async throws -> ClientActionReceipt {
        let namespace = try await resolveNamespace(api: api, expected: expectedNamespace)
        let raw = try await api.request("POST", "/api/v1/client-actions/" + action.id + "/revoke", expectedNamespace: namespace)
        return try await receipt(raw, actionID: action.id, namespace: namespace)
    }

    static func capture(_ action: ClientActionRequest, api: APIClient, expectedNamespace: String? = nil, allowEmptyHealth: Bool = false, automatically: Bool = false) async throws -> [String] {
        try await performCapture(action, api: api, expectedNamespace: expectedNamespace, allowEmptyHealth: allowEmptyHealth, automatically: automatically)
    }

    /// 调用方还需核对任务来自当前设备，且响应前再次检查当前开关。
    static func captureAfterAutomaticReview(_ action: ClientActionRequest, memberID: String, api: APIClient,
                                           expectedNamespace: String, isEnabled: @escaping @MainActor () -> Bool) async throws -> [String] {
        guard action.permitsAutomaticallyReviewedCapture(memberID: memberID) else {
            throw APIClient.APIError.message("此请求需要你在卡片中处理。")
        }
        return try await performCapture(action, api: api, expectedNamespace: expectedNamespace,
            allowEmptyHealth: false, automatically: false, automaticReviewIsEnabled: isEnabled)
    }

    private static func performCapture(_ action: ClientActionRequest, api: APIClient, expectedNamespace: String?,
                                       allowEmptyHealth: Bool, automatically: Bool,
                                       automaticReviewIsEnabled: (@MainActor () -> Bool)? = nil) async throws -> [String] {
        let namespace = try await resolveNamespace(api: api, expected: expectedNamespace)
        try validateAutomaticReview(action, isEnabled: automaticReviewIsEnabled)
        if automaticReviewIsEnabled != nil, action.kind == "read_health" {
            try await HealthCapture.requirePreviouslyRequestedAuthorization(action)
            try validateAutomaticReview(action, isEnabled: automaticReviewIsEnabled)
        }
        if automatically {
            guard action.permitsAutomaticCoarseLocation(), ClientContextSampler.shared.canAutomaticallyCaptureCoarseLocation else {
                throw APIClient.APIError.message("此请求需要你确认后提供资料。")
            }
        }
        let key = "client-action-capture:" + action.id
        let bodies: [Data]
        if let saved = await ClientViewCache.shared.read(key: key, namespace: namespace) {
            // 保留同一次采集及各批次幂等标识，断线后只续传，避免重复读取和入库。
            bodies = (try? JSONDecoder().decode([Data].self, from: saved)) ?? [saved]
            if action.kind == "read_health", !allowEmptyHealth,
               bodies.allSatisfy({ body in
                   guard let batch = (try? JSONSerialization.jsonObject(with: body)) as? [String: Any],
                         let records = batch["records"] as? [[String: Any]] else { return false }
                   return records.allSatisfy { ($0["payload"] as? [String: Any])?["status"] as? String == "no_data" }
               }) { throw HealthReadFailure.noReadableData }
        } else {
            let kind: String
            let payloads: [[String: JSONValue]]
            if action.kind == "capture_location" {
                let value = try await ClientContextSampler.shared.captureLocation(precision: action.parameters["precision"]?.description ?? "coarse",
                    allowPermissionPrompt: !automatically && automaticReviewIsEnabled == nil, automaticReviewIsEnabled: automaticReviewIsEnabled)
                kind = "location.point"
                payloads = [["latitude": .number(value.latitude), "longitude": .number(value.longitude), "accuracy_meters": .number(value.accuracy_meters),
                             "timestamp": .string(value.observed_at), "precision": .string(value.precision)]]
            } else if action.kind == "read_health" {
                kind = "health.snapshot"
                payloads = try await HealthCapture.collect(action, allowEmpty: allowEmptyHealth,
                    allowPermissionPrompt: automaticReviewIsEnabled == nil, automaticReviewIsEnabled: automaticReviewIsEnabled)
            } else { throw APIClient.APIError.message("此请求不支持设备采集") }
            bodies = try payloads.map { payload in
                let upload = ConnectorSync.Upload(source: "client_action", source_id: UUID().uuidString.lowercased(), kind: kind, version: 1,
                    sensitivity: "SENSITIVE", cloud_policy: "LOCAL_ONLY", payload: payload)
                return try JSONEncoder().encode(ConnectorSync.Batch(batch_id: UUID().uuidString.lowercased(), records: [upload]))
            }
            try await ClientViewCache.shared.write(JSONEncoder().encode(bodies), key: key, namespace: namespace)
        }
        var recordIDs: [String] = []
        for body in bodies {
            try await waitForForeground()
            try validateAutomaticReview(action, isEnabled: automaticReviewIsEnabled)
            if automatically, !ClientContextSampler.shared.canAutomaticallyCaptureCoarseLocation {
                throw APIClient.APIError.message("位置授权已变化，本次没有自动提供资料。")
            }
            let raw = try await api.request("POST", "/api/v1/data/sync", body: body, expectedNamespace: namespace)
            let receipt = try JSONDecoder().decode(ConnectorSync.BatchReceipt.self, from: raw)
            let batch = try JSONSerialization.jsonObject(with: body) as? [String: Any]
            guard receipt.batch_id == batch?["batch_id"] as? String, receipt.acknowledged == 1,
                  receipt.accepted_versions.count == 1, receipt.accepted_versions.values.first == 1,
                  let recordID = receipt.accepted_versions.keys.first else { throw APIClient.APIError.message("服务器未确认本次资料") }
            recordIDs.append(recordID)
        }
        return recordIDs
    }

    private static func validateAutomaticReview(_ action: ClientActionRequest, isEnabled: (@MainActor () -> Bool)?) throws {
        guard let isEnabled else { return }
        try Task.checkCancellation()
        guard isEnabled(), action.pending, UIApplication.shared.applicationState == .active else { throw CancellationError() }
        if action.kind == "capture_location",
           !ClientContextSampler.shared.canCaptureWithoutPermissionPrompt(precision: action.parameters["precision"]?.description ?? "coarse") {
            throw APIClient.APIError.message("本次位置请求还需要系统授权，请在卡片中处理。")
        }
    }

    /// 系统授权面板暂时使 App inactive，等待面板关闭后继续；真实后台仍停止本次采集。
    static func waitForForeground() async throws {
        while UIApplication.shared.applicationState == .inactive {
            try await Task.sleep(for: .milliseconds(50))
        }
        try Task.checkCancellation()
        guard UIApplication.shared.applicationState == .active else { throw CancellationError() }
    }
}

struct HealthReadScope: Equatable {
    let types: [String]
    let startText: String
    let endText: String
    let start: Date
    let end: Date

    init(parameters: [String: JSONValue]) throws {
        guard case .array(let values) = parameters["types"], !values.isEmpty,
              values.allSatisfy({ if case .string = $0 { return true }; return false }) else { throw HealthReadFailure.invalidRequest }
        let types = values.map(\.description)
        guard Set(types).isSubset(of: Set(HealthMetric.allCases.map(\.rawValue))), Set(types).count == types.count,
              case .string(let startText) = parameters["start_at"], case .string(let endText) = parameters["end_at"],
              let start = Self.instant(startText), let end = Self.instant(endText), end > start,
              end.timeIntervalSince(start) <= 31 * 86400 else { throw HealthReadFailure.invalidRequest }
        self.types = types; self.startText = startText; self.endText = endText; self.start = start; self.end = end
    }

    func payload(samples: [JSONValue], allowEmpty: Bool) throws -> [String: JSONValue] {
        guard !samples.isEmpty || allowEmpty else { throw HealthReadFailure.noReadableData }
        return ["types": .array(types.map(JSONValue.string)), "start_at": .string(startText), "end_at": .string(endText),
                "samples": .array(samples), "status": .string(samples.isEmpty ? "no_data" : "available")]
    }

    private static func instant(_ text: String) -> Date? {
        let format = ISO8601DateFormatter(); format.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return format.date(from: text) ?? ISO8601DateFormatter().date(from: text)
    }
}

enum HealthReadFailure: Error, LocalizedError, Equatable {
    case unavailable, invalidRequest, noReadableData, canceled, restricted, locked, authorizationRequired
    var errorDescription: String? {
        switch self {
        case .unavailable: "此设备无法使用 Apple 健康数据，请换用支持健康数据的 iPhone。"
        case .invalidRequest: "健康请求的指标或时间范围不正确，请让服务端重新请求；一次最多读取 31 天。"
        case .noReadableData: "本次未读到所选范围的健康样本，可能没有记录，也可能尚未允许读取。可重新采集，或选择“告知暂无可读数据并继续任务”。"
        case .canceled: "已取消健康授权，任务仍在等待。可以再次读取或选择本次不提供。"
        case .restricted: "系统限制了健康数据访问，请在系统设置或健康 App 中检查可用权限。"
        case .locked: "健康数据暂时被锁定，请解锁 iPhone 后重新采集。"
        case .authorizationRequired: "本次健康指标还需要系统授权，请在请求卡片中选择读取。"
        }
    }
    nonisolated static func readable(_ error: Error) -> Error {
        let value = error as NSError
        guard value.domain == HKErrorDomain else { return error }
        switch value.code {
        case HKError.errorUserCanceled.rawValue: return Self.canceled
        case HKError.errorHealthDataUnavailable.rawValue: return Self.unavailable
        case HKError.errorHealthDataRestricted.rawValue, HKError.errorAuthorizationDenied.rawValue: return Self.restricted
        case HKError.errorDatabaseInaccessible.rawValue: return Self.locked
        default: return error
        }
    }
}
