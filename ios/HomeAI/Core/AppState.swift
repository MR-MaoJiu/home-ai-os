import Foundation
import Observation

@MainActor @Observable
final class AppState {
    let api = APIClient()
    var connected = false
    var error: String?
    var busy = false
    var activity: [ActivityEntry] = []
    var records: [DataEntry] = []
    var automations: [AutomationEntry] = []
    var approvals: [ApprovalEntry] = []

    func restore() async { connected = await api.isConnected() }
    func perform(_ operation: () async throws -> Void) async {
        busy = true
        defer { busy = false }
        do { try await operation() } catch { self.error = error.localizedDescription }
    }

    func loadData() async throws {
        let data = try await api.request("GET", "/api/v1/data")
        records = try JSONDecoder().decode(DataPage.self, from: data).records
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

struct DataEntry: Decodable, Identifiable {
    let id: String
    let kind: String
    let sensitivity: String
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
