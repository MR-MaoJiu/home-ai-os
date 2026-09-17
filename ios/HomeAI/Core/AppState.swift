import Foundation
import Observation
import UIKit

@MainActor @Observable
final class AppState {
    let api = APIClient()
    private let dataSync = DeviceDataSync()
    var syncStatus = ""
    var backgroundSyncStatus = ""
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

    func configureBackgroundSync(enabled: Bool) {
        if !enabled { backgroundRun?.1.cancel() }
        backgroundSyncStatus = BackgroundSync.schedule(enabled: enabled && connected)
        if enabled && !connected { backgroundSyncStatus = "配对后才会申请后台同步" }
    }

    func resumeForeground() async {
        await restore()
        guard connected, UIApplication.shared.applicationState == .active, UIApplication.shared.isProtectedDataAvailable else { return }
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
        } catch let APIClient.APIError.http(code, message) {
            if [401, 403].contains(code) { records = []; connected = false; syncStatus = "授权已失效" }
            throw APIClient.APIError.http(code, message)
        }
    }
    func loadActivity() async throws {
        let data = try await api.request("GET", "/api/v1/activity")
        activity = try JSONDecoder().decode(ActivityPage.self, from: data).entries
        let approvalsData = try await api.request("GET", "/api/v1/approvals")
        approvals = try JSONDecoder().decode([ApprovalEntry].self, from: approvalsData)
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
    let payload: [String: JSONValue]
    var title: String { payload["title"]?.description ?? payload["name"]?.description ?? kind }
}
struct DataPage: Decodable { let records: [DataEntry] }
struct ActivityEntry: Decodable, Identifiable { let id: String; let action: String; let resource_id: String; let created_at: Double }
struct ActivityPage: Decodable { let entries: [ActivityEntry] }
struct AutomationEntry: Decodable, Identifiable { let id: String; let name: String; let cron: String; let enabled: Bool }
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
