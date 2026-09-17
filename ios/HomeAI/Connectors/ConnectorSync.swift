import Foundation
import EventKit
import Contacts
import HealthKit

@MainActor
final class ConnectorSync {
    let api: APIClient
    init(api: APIClient) { self.api = api }

    struct Upload: Encodable {
        let source: String
        let source_id: String
        let kind: String
        let version: Int
        let sensitivity: String
        let cloud_policy: String
        let payload: [String: JSONValue]
    }
    struct Batch: Encodable { let batch_id: String; let records: [Upload] }

    private static var activeUploads: Set<String> = []
    private static var waitingUploads: [String: [CheckedContinuation<Void, Never>]] = [:]
    private static func acquire(_ key: String) async {
        if activeUploads.insert(key).inserted { return }
        await withCheckedContinuation { waitingUploads[key, default: []].append($0) }
    }
    private static func release(_ key: String) {
        if var waiting = waitingUploads[key], !waiting.isEmpty {
            let next = waiting.removeFirst(); waitingUploads[key] = waiting; next.resume()
        } else { activeUploads.remove(key); waitingUploads.removeValue(forKey: key) }
    }

    func uploadRecord(source: String, sourceID: String, kind: String, payload: [String: JSONValue]) async throws {
        // 持久化本次内容与版本，失败重试使用相同版本，服务端确认后才更新本地游标。
        let encoder = JSONEncoder()
        encoder.outputFormatting = .sortedKeys
        let contentHash = DeviceIdentity.hash(try encoder.encode(payload))
        let namespace = try await api.syncNamespace()
        let key = "sync:" + namespace + ":" + source + ":" + sourceID
        await Self.acquire(key)
        defer { Self.release(key) }
        try Task.checkCancellation()
        let cached = DeviceIdentity.read(key).flatMap { try? JSONDecoder().decode(Cursor.self, from: $0) }
        if cached?.hash == contentHash, cached?.acknowledged == true { return }
        let version: Int
        let sensitivity: String
        let cloudPolicy: String
        if let cached, cached.hash == contentHash, !cached.acknowledged {
            version = cached.version
            sensitivity = cached.sensitivity ?? "PRIVATE"
            cloudPolicy = cached.cloudPolicy ?? "LOCAL_ONLY"
        } else {
            let body = try JSONSerialization.data(withJSONObject: ["source": source, "source_id": sourceID])
            let response = try await api.request("POST", "/api/v1/sync/source", body: body, expectedNamespace: namespace)
            let remote = try JSONDecoder().decode(SourceVersion.self, from: response)
            guard !remote.deleted else { throw APIClient.APIError.message("该来源已在服务器删除，不会自动恢复") }
            version = max(remote.version, cached?.version ?? 0) + 1
            sensitivity = remote.sensitivity
            cloudPolicy = remote.cloud_policy
        }
        let batchID = cached?.hash == contentHash ? (cached?.batchID ?? UUID().uuidString) : UUID().uuidString
        let cursor = Cursor(hash: contentHash, version: version, acknowledged: false, batchID: batchID, sensitivity: sensitivity, cloudPolicy: cloudPolicy)
        try DeviceIdentity.save(encoder.encode(cursor), name: key)
        let data = try encoder.encode(Batch(batch_id: batchID, records: [Upload(source: source, source_id: sourceID, kind: kind, version: version, sensitivity: sensitivity, cloud_policy: cloudPolicy, payload: payload)]))
        let response = try await api.request("POST", "/api/v1/data/sync", body: data, expectedNamespace: namespace)
        let receipt = try JSONDecoder().decode(BatchReceipt.self, from: response)
        guard receipt.batch_id == batchID, receipt.acknowledged == 1, receipt.accepted_versions.count == 1, receipt.accepted_versions.values.first == version else { throw APIClient.APIError.message("同步批次未被完整确认") }
        try DeviceIdentity.save(encoder.encode(Cursor(hash: contentHash, version: version, acknowledged: true, batchID: batchID, sensitivity: sensitivity, cloudPolicy: cloudPolicy)), name: key)
    }
    struct BatchReceipt: Decodable { let batch_id: String?; let acknowledged: Int; let accepted_versions: [String: Int] }
    struct SourceVersion: Decodable { let version: Int; let deleted: Bool; let sensitivity: String; let cloud_policy: String }
    struct Cursor: Codable { let hash: String; let version: Int; let acknowledged: Bool; let batchID: String?; let sensitivity: String?; let cloudPolicy: String? }

    func calendar() async throws {
        let store = EKEventStore()
        guard try await store.requestFullAccessToEvents() else { throw APIClient.APIError.message("日历未授权") }
        let end = Calendar.current.date(byAdding: .day, value: 30, to: Date())!
        let events = store.events(matching: store.predicateForEvents(withStart: Date(), end: end, calendars: nil))
        for event in events {
            guard let id = event.eventIdentifier else { continue }
            try await uploadRecord(source: "calendar", sourceID: id, kind: "calendar.event", payload: ["title": .string(event.title ?? "日程"), "start_at": .string(event.startDate.ISO8601Format()), "end_at": .string(event.endDate.ISO8601Format())])
        }
    }

    func reminders() async throws {
        let store = EKEventStore()
        guard try await store.requestFullAccessToReminders() else { throw APIClient.APIError.message("提醒事项未授权") }
        let predicate = store.predicateForReminders(in: nil)
        let items: [(String, String, Bool)] = await withCheckedContinuation { continuation in
            store.fetchReminders(matching: predicate) { reminders in
                continuation.resume(returning: (reminders ?? []).map { ($0.calendarItemIdentifier, $0.title ?? "提醒", $0.isCompleted) })
            }
        }
        for (id, title, completed) in items {
            try await uploadRecord(source: "reminders", sourceID: id, kind: "reminder.item", payload: ["title": .string(title), "completed": .bool(completed)])
        }
    }

    func contacts() async throws {
        let store = CNContactStore()
        guard try await store.requestAccess(for: .contacts) else { throw APIClient.APIError.message("联系人未授权") }
        let request = CNContactFetchRequest(keysToFetch: [CNContactIdentifierKey, CNContactGivenNameKey, CNContactFamilyNameKey, CNContactPhoneNumbersKey] as [CNKeyDescriptor])
        var items: [(String, [String: JSONValue])] = []
        try store.enumerateContacts(with: request) { contact, _ in
            items.append((contact.identifier, ["name": .string(contact.familyName + contact.givenName), "phones": .array(contact.phoneNumbers.map { .string($0.value.stringValue) })]))
        }
        for (id, payload) in items { try await uploadRecord(source: "contacts", sourceID: id, kind: "contacts.person", payload: payload) }
    }

    func sleep() async throws {
        guard HKHealthStore.isHealthDataAvailable(), let type = HKObjectType.categoryType(forIdentifier: .sleepAnalysis) else { throw APIClient.APIError.message("此设备不支持健康数据") }
        let store = HKHealthStore()
        try await store.requestAuthorization(toShare: [], read: [type])
        let start = Calendar.current.date(byAdding: .day, value: -7, to: Date())!
        let predicate = HKQuery.predicateForSamples(withStart: start, end: Date())
        let samples: [HKSample] = try await withCheckedThrowingContinuation { continuation in
            let query = HKSampleQuery(sampleType: type, predicate: predicate, limit: 1000, sortDescriptors: nil) { _, samples, error in
                if let error { continuation.resume(throwing: error) }
                else { continuation.resume(returning: samples ?? []) }
            }
            store.execute(query)
        }
        for sample in samples {
            try await uploadRecord(source: "health", sourceID: sample.uuid.uuidString, kind: "health.sleep", payload: ["start_at": .string(sample.startDate.ISO8601Format()), "end_at": .string(sample.endDate.ISO8601Format()), "value": .number(Double((sample as? HKCategorySample)?.value ?? 0))])
        }
    }
}
