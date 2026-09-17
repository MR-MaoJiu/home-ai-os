import Foundation
import EventKit

struct ReminderSyncPreview: Sendable {
    let namespace: String
    let calendarID: String
    let sourceID: String
    let sourceType: Int
    let calendarName: String
    let records: [DataEntry]
    func matches(namespace: String, calendarID: String) -> Bool {
        self.namespace == namespace && self.calendarID == calendarID
    }
}

/// 仅处理本设备明确选择的列表与当前成员自己的 Core 提醒。
@MainActor
final class SystemReminderSync {
    static let shared = SystemReminderSync()
    static let markerScheme = "homeai-reminder"
    private let gate = AsyncOperationGate()
    struct Receipt: Codable, Equatable {
        let recordID: String
        let itemID: String
        let calendarID: String
        let title: String
        let completed: Bool
        let version: Int
    }
    struct Report: Sendable {
        var created = 0
        var updated = 0
        var removed = 0
        var conflicts = 0
        var missing = 0
        var summary: String { "系统提醒：新增 \(created)，更新 \(updated)，移除 \(removed)，冲突 \(conflicts)，本机缺失 \(missing)" }
    }

    static func preferenceKey(_ namespace: String) -> String { "homeai.reminder-target." + namespace }
    static func marker(namespace: String, recordID: String) -> URL {
        URL(string: "\(markerScheme)://\(namespace)/\(recordID)")!
    }

    func synchronize(records: [DataEntry], api: APIClient, calendarID: String, expectedNamespace: String? = nil, expectedSourceID: String? = nil, expectedSourceType: Int? = nil, allowCloudExport: Bool = false, store: EKEventStore = EKEventStore()) async throws -> Report {
        try await gate.acquire()
        do {
            try Task.checkCancellation()
            let result = try await run(records: records, api: api, calendarID: calendarID, expectedNamespace: expectedNamespace, expectedSourceID: expectedSourceID, expectedSourceType: expectedSourceType, allowCloudExport: allowCloudExport, store: store)
            await gate.release()
            return result
        } catch {
            await gate.release()
            throw error
        }
    }

    private func run(records: [DataEntry], api: APIClient, calendarID: String, expectedNamespace: String?, expectedSourceID: String?, expectedSourceType: Int?, allowCloudExport: Bool, store: EKEventStore) async throws -> Report {
        guard EKEventStore.authorizationStatus(for: .reminder) == .fullAccess,
              let calendar = store.calendar(withIdentifier: calendarID), calendar.allowedEntityTypes.contains(.reminder), calendar.allowsContentModifications else {
            throw APIClient.APIError.message("系统提醒权限或目标列表不可用，未进行写入")
        }
        if let expectedSourceID, calendar.source.sourceIdentifier != expectedSourceID { throw APIClient.APIError.message("目标账号已变化，请重新确认") }
        let originalSource = calendar.source.sourceIdentifier
        let originalType = calendar.source.sourceType.rawValue
        if let expectedSourceType, expectedSourceType != originalType { throw APIClient.APIError.message("目标账号类型已变化，请重新确认") }
        func currentCalendar() throws -> EKCalendar {
            guard EKEventStore.authorizationStatus(for: .reminder) == .fullAccess,
                  let current = store.calendar(withIdentifier: calendarID), current.allowsContentModifications,
                  current.source.sourceIdentifier == originalSource, current.source.sourceType.rawValue == originalType else {
                throw APIClient.APIError.message("系统列表权限或账号发生变化，停止同步")
            }
            return current
        }
        let namespace = try await api.syncNamespace()
        if let expectedNamespace, expectedNamespace != namespace { throw APIClient.APIError.message("配对已切换，请重新预览") }
        let cloud = calendar.source.sourceType != .local
        guard !cloud || allowCloudExport else { throw APIClient.APIError.message("此列表可能通过云账号同步，需要本次明确确认") }
        let user = try await api.ownerIdentity(expectedNamespace: namespace)
        let resourceNamespace = user.namespace
        let storageKey = "system-reminder-map:" + resourceNamespace
        var receipts: [String: Receipt] = [:]
        if let data = DeviceIdentity.read(storageKey) { receipts = try JSONDecoder().decode([String: Receipt].self, from: data) }
        // 获取标识后回到主 actor 取 EventKit 对象，不跨并发域传递可变对象。
        let identifiers: [String] = await withCheckedContinuation { continuation in
            store.fetchReminders(matching: store.predicateForReminders(in: nil)) { values in
                continuation.resume(returning: (values ?? []).map(\.calendarItemIdentifier))
            }
        }
        var managed: [String: [EKReminder]] = [:]
        for identifier in identifiers {
            guard let item = store.calendarItem(withIdentifier: identifier) as? EKReminder,
                  let url = item.url, url.scheme == Self.markerScheme, url.host == resourceNamespace else { continue }
            let recordID = String(url.path.dropFirst())
            managed[recordID, default: []].append(item)
        }
        var report = Report()
        for candidate in records where candidate.kind == "reminder.item" && (candidate.source == nil || candidate.source == "core") {
            try Task.checkCancellation()
            let raw = try await api.request("GET", "/api/v1/data/" + candidate.id, expectedNamespace: namespace)
            let remote = try JSONDecoder().decode(DataEntry.self, from: raw)
            guard remote.source == "core", remote.owner_id == user.userID, remote.sensitivity != "SECRET" else { continue }
            guard let version = remote.version, let sourceID = remote.source_id,
                  case .string(let title) = remote.payload["title"], !title.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty, title.count <= 500 else { report.conflicts += 1; continue }
            if cloud && candidate.version != remote.version { report.conflicts += 1; continue }
            let completed: Bool
            if remote.payload["completed"] == nil { completed = false }
            else if case .bool(let value) = remote.payload["completed"] { completed = value }
            else { report.conflicts += 1; continue }
            let matches = managed[remote.id] ?? []
            guard matches.count <= 1 else { report.conflicts += 1; continue }
            let previous = receipts[remote.id]
            if matches.isEmpty && previous != nil {
                // 用户删除、移动到不可见账号或移除标识时，不自动复活。
                report.missing += 1
                continue
            }
            let item = matches.first ?? EKReminder(eventStore: store)
            if let existing = matches.first, existing.calendar.calendarIdentifier != calendarID {
                report.conflicts += 1
                continue
            }
            if !matches.isEmpty && !item.refresh() { report.missing += 1; continue }
            let localModified = item.lastModifiedDate
            var mergedTitle = title
            var mergedCompleted = completed
            if let previous, let existing = matches.first {
                let localTitle = existing.title ?? ""
                let localCompleted = existing.isCompleted
                let titleConflict = localTitle != previous.title && title != previous.title && localTitle != title
                let completedConflict = localCompleted != previous.completed && completed != previous.completed && localCompleted != completed
                if titleConflict || completedConflict { report.conflicts += 1; continue }
                if localTitle != previous.title { mergedTitle = localTitle }
                if localCompleted != previous.completed { mergedCompleted = localCompleted }
            } else if !matches.isEmpty && (item.title != title || item.isCompleted != completed) {
                // 保存成功但本地确认记录丢失时，只识别现有条目，不覆盖未知编辑。
                report.conflicts += 1
                continue
            }
            _ = try currentCalendar()
            if mergedTitle != title || mergedCompleted != completed {
                var payload = remote.payload
                payload["title"] = .string(mergedTitle)
                payload["completed"] = .bool(mergedCompleted)
                let upload = ConnectorSync.Upload(source: "core", source_id: sourceID, kind: "reminder.item", version: version + 1,
                    sensitivity: remote.sensitivity, cloud_policy: remote.cloud_policy ?? "LOCAL_ONLY", payload: payload)
                let batch = ConnectorSync.Batch(batch_id: UUID().uuidString, records: [upload])
                _ = try await api.request("POST", "/api/v1/data/sync", body: JSONEncoder().encode(batch), expectedNamespace: namespace)
            }
            guard try await api.syncNamespace() == namespace else { throw APIClient.APIError.message("连接已切换，停止系统提醒写入") }
            if !matches.isEmpty && (!item.refresh() || item.lastModifiedDate != localModified) { report.conflicts += 1; continue }
            let marker = Self.marker(namespace: resourceNamespace, recordID: remote.id)
            let needsWrite = matches.isEmpty || item.title != mergedTitle || item.isCompleted != mergedCompleted || item.url != marker
            if needsWrite {
                item.calendar = try currentCalendar()
                item.title = mergedTitle
                item.isCompleted = mergedCompleted
                item.url = marker
                try store.save(item, commit: true)
                if matches.isEmpty { report.created += 1 } else { report.updated += 1 }
            } else if mergedTitle != title || mergedCompleted != completed { report.updated += 1 }
            let receipt = Receipt(recordID: remote.id, itemID: item.calendarItemIdentifier, calendarID: calendarID,
                title: mergedTitle, completed: mergedCompleted, version: version + ((mergedTitle != title || mergedCompleted != completed) ? 1 : 0))
            if receipts[remote.id] != receipt {
                receipts[remote.id] = receipt
                try DeviceIdentity.save(JSONEncoder().encode(receipts), name: storageKey)
            }
        }
        let liveIDs = Set(records.filter { $0.sensitivity != "SECRET" }.map(\.id))
        for (identifier, receipt) in receipts where !liveIDs.contains(identifier) {
            var shouldRemove = false
            do {
                let raw = try await api.request("GET", "/api/v1/data/" + identifier, expectedNamespace: namespace)
                shouldRemove = try JSONDecoder().decode(DataEntry.self, from: raw).sensitivity == "SECRET"
            } catch let APIClient.APIError.http(code, _) where code == 404 { shouldRemove = true }
            guard shouldRemove else { continue }
            let matches = managed[identifier] ?? []
            guard matches.count <= 1 else { report.conflicts += 1; continue }
            if let item = matches.first {
                guard try await api.syncNamespace() == namespace else { throw APIClient.APIError.message("连接已切换") }
                guard item.refresh(), item.calendar.calendarIdentifier == calendarID,
                      item.title == receipt.title, item.isCompleted == receipt.completed,
                      (item.notes ?? "").isEmpty, item.dueDateComponents == nil, item.startDateComponents == nil,
                      item.alarms?.isEmpty != false else { report.conflicts += 1; continue }
                _ = try currentCalendar()
                try store.remove(item, commit: true)
                report.removed += 1
            }
            receipts.removeValue(forKey: identifier)
            try DeviceIdentity.save(JSONEncoder().encode(receipts), name: storageKey)
        }
        return report
    }
}
