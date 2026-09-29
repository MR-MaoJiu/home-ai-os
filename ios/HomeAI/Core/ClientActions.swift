import Foundation
import HealthKit
import Observation

struct ClientActionRequest: Codable, Identifiable, Sendable {
    struct Requester: Codable, Sendable { let id: String; let name: String }
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
    var pending: Bool { status == "PENDING" && expires_at > Date().timeIntervalSince1970 }
    var statusLabel: String {
        ["PENDING": "等待提供", "RESPONDED": "已提供", "DENIED": "未提供", "EXPIRED": "已过期", "REVOKED": "已撤回", "CANCELED": "已取消"][status] ?? status
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

    static func respond(_ action: ClientActionRequest, recordIDs: [String] = [], text: String? = nil, api: APIClient) async throws {
        let namespace = try await api.syncNamespace()
        let key = "client-action-response:" + action.id
        let body: Data
        var intended: [String: Any] = [:]
        if action.kind != "cloud.disclose" { intended["record_ids"] = recordIDs.sorted(); if let text { intended["text"] = text } }
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
        _ = try await api.request("POST", "/api/v1/client-actions/" + action.id + "/respond", body: body, expectedNamespace: namespace)
        await ClientViewCache.shared.remove(key: key, namespace: namespace)
        await ClientViewCache.shared.remove(key: "client-action-capture:" + action.id, namespace: namespace)
    }

    static func deny(_ action: ClientActionRequest, api: APIClient) async throws {
        let namespace = try await api.syncNamespace()
        let storage = "client-action-denial:" + namespace + ":" + action.id
        let identifier = DeviceIdentity.read(storage).flatMap { String(data: $0, encoding: .utf8) } ?? UUID().uuidString.lowercased()
        try DeviceIdentity.save(Data(identifier.utf8), name: storage)
        let body = try JSONSerialization.data(withJSONObject: ["idempotency_key": identifier, "reason": "用户选择不提供本次资料或授权"])
        _ = try await api.request("POST", "/api/v1/client-actions/" + action.id + "/deny", body: body, expectedNamespace: namespace)
        await ClientViewCache.shared.remove(key: "client-action-response:" + action.id, namespace: namespace)
    }

    static func revoke(_ action: ClientActionRequest, api: APIClient) async throws {
        _ = try await api.request("POST", "/api/v1/client-actions/" + action.id + "/revoke")
    }

    static func capture(_ action: ClientActionRequest, api: APIClient) async throws -> [String] {
        let namespace = try await api.syncNamespace()
        let key = "client-action-capture:" + action.id
        let body: Data
        if let saved = await ClientViewCache.shared.read(key: key, namespace: namespace) { body = saved }
        else {
            let kind: String
            let payload: [String: JSONValue]
            if action.kind == "capture_location" {
                let value = try await ClientContextSampler.shared.captureLocation(precision: action.parameters["precision"]?.description ?? "coarse")
                kind = "location.point"
                payload = ["latitude": .number(value.latitude), "longitude": .number(value.longitude), "accuracy_meters": .number(value.accuracy_meters),
                           "timestamp": .string(value.observed_at), "precision": .string(value.precision)]
            } else if action.kind == "read_health" {
                kind = "health.snapshot"; payload = try await healthPayload(action)
            } else { throw APIClient.APIError.message("此请求不支持设备采集") }
            let upload = ConnectorSync.Upload(source: "client_action", source_id: UUID().uuidString.lowercased(), kind: kind, version: 1,
                sensitivity: "SENSITIVE", cloud_policy: "LOCAL_ONLY", payload: payload)
            body = try JSONEncoder().encode(ConnectorSync.Batch(batch_id: UUID().uuidString.lowercased(), records: [upload]))
            try await ClientViewCache.shared.write(body, key: key, namespace: namespace)
        }
        try Task.checkCancellation()
        let raw = try await api.request("POST", "/api/v1/data/sync", body: body, expectedNamespace: namespace)
        let receipt = try JSONDecoder().decode(ConnectorSync.BatchReceipt.self, from: raw)
        guard receipt.acknowledged == 1, let recordID = receipt.accepted_versions.keys.first else { throw APIClient.APIError.message("服务器未确认本次资料") }
        return [recordID]
    }

    private static func healthPayload(_ action: ClientActionRequest) async throws -> [String: JSONValue] {
        let requested = action.strings("types")
        let allowed = Set(["sleep", "steps", "heart_rate", "body_mass"])
        guard !requested.isEmpty, Set(requested).isSubset(of: allowed), Set(requested).count == requested.count,
              HKHealthStore.isHealthDataAvailable(),
              let startText = action.parameters["start_at"]?.description, let endText = action.parameters["end_at"]?.description,
              let start = instant(startText), let end = instant(endText), end > start, end.timeIntervalSince(start) <= 31 * 86400 else {
            throw APIClient.APIError.message("此设备或请求的健康数据类型暂不支持")
        }
        var types: [String: HKSampleType] = [:]
        for name in requested {
            switch name {
            case "sleep": types[name] = HKObjectType.categoryType(forIdentifier: .sleepAnalysis)
            case "steps": types[name] = HKObjectType.quantityType(forIdentifier: .stepCount)
            case "heart_rate": types[name] = HKObjectType.quantityType(forIdentifier: .heartRate)
            case "body_mass": types[name] = HKObjectType.quantityType(forIdentifier: .bodyMass)
            default: break
            }
        }
        guard types.count == requested.count else { throw APIClient.APIError.message("此设备不支持请求的健康指标") }
        let store = HKHealthStore()
        try await store.requestAuthorization(toShare: [], read: Set(types.values.map { $0 as HKObjectType }))
        var samples: [JSONValue] = []
        for name in requested {
            try Task.checkCancellation()
            let rows: [HKSample] = try await withCheckedThrowingContinuation { continuation in
                let predicate = HKQuery.predicateForSamples(withStart: start, end: end, options: [.strictStartDate, .strictEndDate])
                store.execute(HKSampleQuery(sampleType: types[name]!, predicate: predicate, limit: 1001, sortDescriptors: nil) { _, samples, error in
                    if let error { continuation.resume(throwing: error) } else { continuation.resume(returning: samples ?? []) }
                })
            }
            guard rows.count <= 1000, samples.count + rows.count <= 1000 else { throw APIClient.APIError.message("时间范围内记录较多，请缩小范围后重试") }
            for sample in rows {
                let value: Double
                let unit: String
                switch name {
                case "sleep": guard let item = sample as? HKCategorySample else { continue }; value = Double(item.value); unit = "category"
                case "steps": guard let item = sample as? HKQuantitySample else { continue }; value = item.quantity.doubleValue(for: .count()); unit = "count"
                case "heart_rate": guard let item = sample as? HKQuantitySample else { continue }; value = item.quantity.doubleValue(for: HKUnit.count().unitDivided(by: .minute())); unit = "count/min"
                case "body_mass": guard let item = sample as? HKQuantitySample else { continue }; value = item.quantity.doubleValue(for: .gramUnit(with: .kilo)); unit = "kg"
                default: continue
                }
                samples.append(.object(["type": .string(name), "sample_id": .string(sample.uuid.uuidString.lowercased()), "start_at": .string(sample.startDate.ISO8601Format()), "end_at": .string(sample.endDate.ISO8601Format()), "value": .number(value), "unit": .string(unit)]))
            }
        }
        try Task.checkCancellation()
        return ["types": .array(requested.map(JSONValue.string)), "start_at": .string(startText), "end_at": .string(endText),
                "samples": .array(samples), "status": .string(samples.isEmpty ? "no_data" : "available")]
    }
    private static func instant(_ text: String) -> Date? {
        let format = ISO8601DateFormatter(); format.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return format.date(from: text) ?? ISO8601DateFormatter().date(from: text)
    }
}
