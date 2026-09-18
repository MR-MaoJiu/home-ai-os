import Foundation

struct TaskCreated: Decodable { let id: String }
struct TaskResult: Decodable { let status: String; let error: String?; let result: JSONValue? }

struct RecordReference: Identifiable {
    let id: String
    let title: String
    let version: Int?
}

