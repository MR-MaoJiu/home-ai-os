import Foundation
import Observation
import UIKit

@MainActor @Observable
final class AppState {
    let api = AppServices.api
    private let dataSync = DeviceDataSync()
    var syncStatus = ""
    var backgroundSyncStatus = ""
    var systemReminderStatus = ""
    var taskEventStatus = ""
    var taskStates: [TaskStateEvent.Item] = []
    var taskEventRevision = UUID()
    private var eventRun: Task<Void, Never>?
    private var backgroundRun: (UUID, Task<Void, Never>)?
    var connected = false
    var connectionRevision = UUID()
    var error: String?
    var busy = false
    var activity: [ActivityEntry] = []
    var records: [DataEntry] = []
    var automations: [AutomationEntry] = []
    var approvals: [ApprovalEntry] = []

    func restore() async {
        guard UIApplication.shared.isProtectedDataAvailable else { return }
        await api.restoreConnectionIfNeeded()
        connected = await api.isConnected()
    }

    func stopForegroundEvents() async {
        eventRun?.cancel()
        eventRun = nil
        await api.stopTaskEvents()
        taskEventStatus = "后台暂停实时连接"
    }

    func startForegroundEvents() {
        eventRun?.cancel()
        eventRun = Task { [weak self] in
            guard let self else { return }
            var retries = 0
            while !Task.isCancelled && self.connected && UIApplication.shared.applicationState == .active {
                do {
                    let namespace = try await self.api.syncNamespace()
                    let subscription = try await self.api.taskEvents(expectedNamespace: namespace)
                    defer { Task { await self.api.closeTaskEvents(subscription.id) } }
                    self.taskEventStatus = "正在连接任务状态…"
                    for try await event in subscription.events {
                        try Task.checkCancellation()
                        guard namespace == (try await self.api.syncNamespace()) else { return }
                        if event.type == "task.snapshot" {
                            try await self.loadActivity(expectedNamespace: namespace)
                            let owner = try await self.api.ownerIdentity(expectedNamespace: namespace)
                            if UserDefaults.standard.bool(forKey: SystemReminderSync.preferenceKey(owner.namespace) + ".automatic") {
                                try await self.loadData()
                            }
                            guard namespace == (try await self.api.syncNamespace()), !Task.isCancelled else { return }
                            self.taskStates = event.tasks
                            self.taskEventRevision = UUID()
                            self.taskEventStatus = event.has_more ? "显示最近 100 个任务状态" : "任务状态已连接"
                            retries = 0
                        }
                    }
                } catch is CancellationError { return }
                catch {
                    if Task.isCancelled { return }
                    self.taskEventStatus = "连接中断，正在恢复任务状态"
                    // 用正常签名请求确认授权；刷新失败不伪装在线。
                    do { _ = try await self.api.request("GET", "/api/v1/me") }
                    catch let APIClient.APIError.http(code, _) where code == 401 || code == 403 {
                        self.connected = false
                        self.activity = []; self.approvals = []; self.taskStates = []
                        self.taskEventStatus = "授权已失效，请重新配对"
                        return
                    } catch { }
                    retries += 1
                }
                do { try await Task.sleep(for: .seconds(min(30, 1 << min(retries, 5)))) }
                catch { return }
            }
        }
    }

    func configureBackgroundSync(enabled: Bool) {
        if !enabled { backgroundRun?.1.cancel() }
        backgroundSyncStatus = BackgroundSync.schedule(enabled: enabled && connected)
        if enabled && !connected { backgroundSyncStatus = "配对后才会申请后台同步" }
    }

    func resumeForeground() async {
        await restore()
        guard connected, UIApplication.shared.applicationState == .active, UIApplication.shared.isProtectedDataAvailable else { return }
        startForegroundEvents()
        do { try await loadData() }
        catch is CancellationError { }
        catch let error as URLError where error.code == .cancelled { }
        catch { if syncStatus != "授权已失效" { syncStatus = "同步未完成，可下拉重试" } }
    }

    func refreshInBackground() async {
        let enabled = UserDefaults.standard.bool(forKey: BackgroundSync.preference)
        backgroundSyncStatus = BackgroundSync.schedule(enabled: enabled)
        guard enabled, UIApplication.shared.isProtectedDataAvailable, backgroundRun == nil else { return }
        let identifier = UUID()
        let operation = Task { [weak self] in
            guard let self else { return }
            await self.restore()
            guard self.connected, !Task.isCancelled else { return }
            do {
                let result = try await self.dataSync.synchronize(api: self.api)
                guard !Task.isCancelled else { return }
                self.records = result.records
                self.backgroundSyncStatus = result.offline ? "网络不可用，保留缓存等待下次同步" : "后台同步已完成"
            } catch {
                if case APIClient.APIError.http(let code, _) = error, [401, 403].contains(code) {
                    self.records = []
                    self.connected = false
                    _ = BackgroundSync.schedule(enabled: false)
                }
                if !UserDefaults.standard.bool(forKey: BackgroundSync.preference) { self.backgroundSyncStatus = "后台同步已关闭" }
                else { self.backgroundSyncStatus = Task.isCancelled ? "系统结束了本次后台时间，下次继续" : "后台同步未完成，可在前台重试" }
            }
        }
        backgroundRun = (identifier, operation)
        await withTaskCancellationHandler { await operation.value } onCancel: { operation.cancel() }
        if backgroundRun?.0 == identifier { backgroundRun = nil }
    }
    func perform(_ operation: () async throws -> Void) async {
        busy = true
        defer { busy = false }
        do { try await operation() }
        catch is CancellationError { }
        catch let error as URLError where error.code == .cancelled { }
        catch { self.error = error.localizedDescription }
    }

    func loadData() async throws {
        do {
            let result = try await dataSync.synchronize(api: api)
            records = result.records
            syncStatus = result.offline ? "离线：显示上次同步缓存" : "已完成增量同步"
            if !result.offline && UIApplication.shared.applicationState == .active {
                let namespace = try await api.syncNamespace()
                let owner = try await api.ownerIdentity(expectedNamespace: namespace)
                let key = SystemReminderSync.preferenceKey(owner.namespace)
                if UserDefaults.standard.bool(forKey: key + ".automatic"), let calendar = UserDefaults.standard.string(forKey: key), !calendar.isEmpty {
                    do {
                        let report = try await SystemReminderSync.shared.synchronize(records: result.records, api: api, calendarID: calendar, expectedNamespace: namespace, includeSchedule: UserDefaults.standard.bool(forKey: key + ".schedule"))
                        systemReminderStatus = report.summary
                    } catch { systemReminderStatus = "系统提醒未同步：" + error.localizedDescription }
                }
            }
        } catch let APIClient.APIError.http(code, message) {
            if [401, 403].contains(code) { records = []; connected = false; syncStatus = "授权已失效" }
            throw APIClient.APIError.http(code, message)
        }
    }
    func loadActivity(expectedNamespace: String? = nil) async throws {
        let namespace: String
        if let expectedNamespace { namespace = expectedNamespace }
        else { namespace = try await api.syncNamespace() }
        let data = try await api.request("GET", "/api/v1/activity", expectedNamespace: namespace)
        let entries = try JSONDecoder().decode(ActivityPage.self, from: data).entries
        let approvalsData = try await api.request("GET", "/api/v1/approvals", expectedNamespace: namespace)
        let pending = try JSONDecoder().decode([ApprovalEntry].self, from: approvalsData)
        guard namespace == (try await api.syncNamespace()), !Task.isCancelled else { return }
        activity = entries
        approvals = pending
    }
    func loadAutomations() async throws {
        let data = try await api.request("GET", "/api/v1/automations")
        automations = try JSONDecoder().decode([AutomationEntry].self, from: data)
    }
}

struct DataEntry: Codable, Identifiable, Sendable {
    let id: String
    let kind: String
    let sensitivity: String
    let version: Int?
    let owner_id: String?
    let source: String?
    let source_id: String?
    let cloud_policy: String?
    let payload: [String: JSONValue]
    var title: String { payload["title"]?.description ?? payload["name"]?.description ?? kind }
}
struct DataPage: Decodable { let records: [DataEntry] }
struct ActivityEntry: Decodable, Identifiable { let id: String; let action: String; let resource_id: String; let created_at: Double }
struct ActivityPage: Decodable { let entries: [ActivityEntry] }
struct AutomationEntry: Decodable, Identifiable {
    let id: String
    let name: String
    let cron: String
    let enabled: Bool
    let trigger_kind: String?
    let event_type: String?
    var triggerDescription: String {
        guard trigger_kind == "event" else { return cron }
        return ["record.changed": "数据新增或更新", "record.deleted": "数据删除", "record.revoked": "共享授权撤回"][event_type ?? ""] ?? "数据事件"
    }
}
struct ApprovalEntry: Decodable, Identifiable { let id: String; let capability: String; let arguments: [String: JSONValue] }

indirect enum JSONValue: Codable, Sendable, CustomStringConvertible {
    case string(String), number(Double), bool(Bool), object([String: JSONValue]), array([JSONValue]), null
    init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if c.decodeNil() { self = .null }
        else if let v = try? c.decode(Bool.self) { self = .bool(v) }
        else if let v = try? c.decode(String.self) { self = .string(v) }
        else if let v = try? c.decode(Double.self) { self = .number(v) }
        else if let v = try? c.decode([String: JSONValue].self) { self = .object(v) }
        else { self = .array(try c.decode([JSONValue].self)) }
    }
    func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        switch self {
        case .string(let v): try c.encode(v)
        case .number(let v): try c.encode(v)
        case .bool(let v): try c.encode(v)
        case .object(let v): try c.encode(v)
        case .array(let v): try c.encode(v)
        case .null: try c.encodeNil()
        }
    }
    var description: String {
        switch self {
        case .string(let v): v
        default: String(data: (try? JSONEncoder().encode(self)) ?? Data(), encoding: .utf8) ?? ""
        }
    }
}
