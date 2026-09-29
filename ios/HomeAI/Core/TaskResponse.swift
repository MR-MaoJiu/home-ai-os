import Foundation

struct TaskCreated: Decodable { let id: String }
struct TaskResult: Decodable { let status: String; let error: String?; let result: JSONValue? }

struct RecordReference: Identifiable {
    let id: String
    let title: String
    let version: Int?
    var taskID: String? = nil
}



func taskStatusLabel(_ status: String) -> String {
    ["UNAVAILABLE": "本轮查询未完成", "PENDING": "等待操作", "RESPONDED": "已处理", "DENIED": "已拒绝", "EXPIRED": "已过期", "REVOKED": "已撤回", "READY": "通知已就绪", "DUE": "已到提醒时间", "WAITING_CLIENT": "等待提供资料", "WAITING_MEDIA": "附件处理中", "WAITING_BUDGET": "等待预算调整", "WAITING_PRIVACY": "隐私检查已暂停", "RECEIVED": "已接收", "EXECUTING": "执行中", "APPROVED": "已确认，等待执行", "AWAITING_APPROVAL": "等待你的确认", "SUCCEEDED": "已完成", "FAILED": "执行失败", "CANCELED": "已取消", "NEEDS_RECONCILIATION": "结果待核对"][status] ?? status
}


struct WebSearchHit: Identifiable {
    let title: String
    let url: URL
    let snippet: String
    var id: String { url.absoluteString }
}
func webSearchHits(_ value: JSONValue?) -> [WebSearchHit]? {
    guard case .object(let fields) = value,
          fields["content_trust"]?.description == "untrusted_web",
          case .array(let values) = fields["results"] else { return nil }
    var seen = Set<String>()
    return values.compactMap { item in
        guard case .object(let entry) = item, let raw = entry["url"]?.description,
              let url = URL(string: raw), ["https", "http"].contains(url.scheme?.lowercased()), url.host != nil, url.user == nil, url.password == nil,
              seen.insert(url.absoluteString).inserted else { return nil }
        return WebSearchHit(title: entry["title"]?.description ?? raw, url: url, snippet: entry["content"]?.description ?? entry["snippet"]?.description ?? "")
    }
}

enum ResourceRoutes {
    static func prefix(taskID: String?) throws -> String {
        guard let taskID else { return "/api/v1" }
        guard let identifier = UUID(uuidString: taskID) else { throw APIClient.APIError.message("任务授权标识无效") }
        return "/api/v1/tasks/" + identifier.uuidString.lowercased()
    }
    static func record(_ id: String, taskID: String?) throws -> String {
        guard let value = UUID(uuidString: id) else { throw APIClient.APIError.message("资料标识无效") }
        return try prefix(taskID: taskID) + "/data/" + value.uuidString.lowercased()
    }
    static func asset(_ id: String, taskID: String?) throws -> String {
        guard let value = UUID(uuidString: id) else { throw APIClient.APIError.message("附件标识无效") }
        return try prefix(taskID: taskID) + "/assets/" + value.uuidString.lowercased()
    }
    static func file(_ id: String, taskID: String?) throws -> String {
        guard let value = UUID(uuidString: id) else { throw APIClient.APIError.message("文件标识无效") }
        return try prefix(taskID: taskID) + "/files/" + value.uuidString.lowercased()
    }
    static func cacheScope(_ taskID: String?) -> String { taskID.map { "task:" + $0 + ":" } ?? "" }
}
