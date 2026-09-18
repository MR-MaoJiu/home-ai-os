import Foundation

struct TaskCreated: Decodable { let id: String }
struct TaskResult: Decodable { let status: String; let error: String?; let result: JSONValue? }

struct RecordReference: Identifiable {
    let id: String
    let title: String
    let version: Int?
}



func taskStatusLabel(_ status: String) -> String {
    ["RECEIVED": "已接收", "EXECUTING": "执行中", "APPROVED": "已确认，等待执行", "AWAITING_APPROVAL": "等待你的确认", "SUCCEEDED": "已完成", "FAILED": "执行失败", "CANCELED": "已取消", "NEEDS_RECONCILIATION": "结果待核对"][status] ?? status
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
