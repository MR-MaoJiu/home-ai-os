import Foundation
import CryptoKit

/// 缓存写入先于确认；断网后可重放确认，不能提前推进服务端游标。
actor DeviceDataSync {
    struct Result: Sendable { let records: [DataEntry]; let offline: Bool }
    private struct Snapshot: Decodable { let snapshot_id: String; let watermark: Int }
    private struct Page: Decodable { let records: [DataEntry]; let removed_ids: [String]; let next_offset: Int; let done: Bool }
    private struct Changes: Decodable { let records: [DataEntry]; let removed_ids: [String]; let next_cursor: Int; let has_more: Bool }
    private struct Ack: Codable { let cursor: Int; let snapshot_id: String? }
    private struct PendingSnapshot: Codable { let id: String; let watermark: Int; var offset: Int; var records: [DataEntry] }
    private struct Cache: Codable { var cursor: Int; var records: [DataEntry]; var pendingAck: Ack?; var snapshot: PendingSnapshot? }
    private var running = false
    private var waiters: [CheckedContinuation<Void, Never>] = []

    private func acquire() async {
        if !running { running = true; return }
        await withCheckedContinuation { waiters.append($0) }
    }

    private func release() {
        if waiters.isEmpty { running = false } else { waiters.removeFirst().resume() }
    }

    func synchronize(api: APIClient) async throws -> Result {
        await acquire()
        defer { release() }
        try Task.checkCancellation()
        let namespace = try await api.syncNamespace()
        let file = try cacheURL(namespace)
        let key = try cacheKey(namespace)
        var cached = try? load(file, key)
        for attempt in 0..<2 {
            do {
                if let pending = cached?.pendingAck {
                    _ = try await api.request("POST", "/api/v1/sync/ack", body: JSONEncoder().encode(pending), expectedNamespace: namespace)
                    cached?.pendingAck = nil
                    if let current = cached { try save(current, file, key) }
                }
                if cached == nil {
                    let raw = try await api.request("POST", "/api/v1/sync/snapshot", expectedNamespace: namespace)
                    let snapshot = try JSONDecoder().decode(Snapshot.self, from: raw)
                    cached = Cache(cursor: 0, records: [], pendingAck: nil, snapshot: PendingSnapshot(id: snapshot.snapshot_id, watermark: snapshot.watermark, offset: 0, records: []))
                    try save(cached!, file, key)
                }
                while var current = cached, var snapshot = current.snapshot {
                    try Task.checkCancellation()
                    let bytes = try await api.request("GET", "/api/v1/sync/snapshot/\(snapshot.id)?offset=\(snapshot.offset)&limit=100", expectedNamespace: namespace)
                    let page = try JSONDecoder().decode(Page.self, from: bytes)
                    guard page.done || page.next_offset > snapshot.offset else { throw APIClient.APIError.message("同步分页没有前进") }
                    snapshot.records = Self.merge(snapshot.records, updates: page.records, removed: page.removed_ids)
                    snapshot.offset = page.next_offset
                    current.snapshot = snapshot
                    if page.done {
                        current.cursor = snapshot.watermark
                        current.records = snapshot.records
                        current.snapshot = nil
                        current.pendingAck = Ack(cursor: snapshot.watermark, snapshot_id: snapshot.id)
                    }
                    try save(current, file, key)
                    cached = current
                    if page.done {
                        _ = try await api.request("POST", "/api/v1/sync/ack", body: JSONEncoder().encode(current.pendingAck), expectedNamespace: namespace)
                        current.pendingAck = nil
                        try save(current, file, key)
                        cached = current
                        break
                    }
                }
                while var current = cached {
                    try Task.checkCancellation()
                    let raw = try await api.request("GET", "/api/v1/sync/changes?after=\(current.cursor)&limit=100", expectedNamespace: namespace)
                    let changes = try JSONDecoder().decode(Changes.self, from: raw)
                    guard changes.next_cursor >= current.cursor else { throw APIClient.APIError.message("服务器同步游标回退") }
                    current.records = Self.merge(current.records, updates: changes.records, removed: changes.removed_ids)
                    current.cursor = changes.next_cursor
                    current.pendingAck = Ack(cursor: current.cursor, snapshot_id: nil)
                    try save(current, file, key)
                    cached = current
                    _ = try await api.request("POST", "/api/v1/sync/ack", body: JSONEncoder().encode(current.pendingAck), expectedNamespace: namespace)
                    current.pendingAck = nil
                    try save(current, file, key)
                    cached = current
                    if !changes.has_more {
                        guard try await api.syncNamespace() == namespace else { throw APIClient.APIError.message("连接已切换") }
                        return Result(records: current.records.sorted { $0.id < $1.id }, offline: false)
                    }
                }
            } catch let APIClient.APIError.http(code, message) {
                if [404, 409, 410].contains(code), attempt == 0 {
                    try? FileManager.default.removeItem(at: file)
                    cached = nil
                    continue
                }
                if [401, 403].contains(code) { try? FileManager.default.removeItem(at: file) }
                throw APIClient.APIError.http(code, message)
            } catch let error as URLError {
                if [.notConnectedToInternet, .networkConnectionLost, .timedOut, .cannotFindHost, .cannotConnectToHost].contains(error.code), let cached {
                    guard try await api.syncNamespace() == namespace else { throw APIClient.APIError.message("连接已切换") }
                    return Result(records: cached.records.sorted { $0.id < $1.id }, offline: true)
                }
                throw error
            }
        }
        throw APIClient.APIError.message("同步状态已变化，请重试")
    }

    static func merge(_ records: [DataEntry], updates: [DataEntry], removed: [String]) -> [DataEntry] {
        var indexed = Dictionary(records.map { ($0.id, $0) }, uniquingKeysWith: { _, newest in newest })
        for record in updates { indexed[record.id] = record }
        for identifier in removed { indexed.removeValue(forKey: identifier) }
        return Array(indexed.values)
    }

    private func cacheURL(_ namespace: String) throws -> URL {
        let directory = try FileManager.default.url(for: .applicationSupportDirectory, in: .userDomainMask, appropriateFor: nil, create: true).appendingPathComponent("HomeAI/Sync", isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        var values = URLResourceValues(); values.isExcludedFromBackup = true
        var mutable = directory; try mutable.setResourceValues(values)
        return directory.appendingPathComponent(namespace + ".sealed")
    }

    private func cacheKey(_ namespace: String) throws -> SymmetricKey {
        let name = "sync-cache-key:" + namespace
        if let data = DeviceIdentity.read(name) { return SymmetricKey(data: data) }
        let key = SymmetricKey(size: .bits256)
        try DeviceIdentity.save(key.withUnsafeBytes { Data($0) }, name: name)
        return key
    }

    private func load(_ file: URL, _ key: SymmetricKey) throws -> Cache {
        let box = try AES.GCM.SealedBox(combined: Data(contentsOf: file))
        return try JSONDecoder().decode(Cache.self, from: AES.GCM.open(box, using: key))
    }

    private func save(_ cache: Cache, _ file: URL, _ key: SymmetricKey) throws {
        let box = try AES.GCM.seal(JSONEncoder().encode(cache), using: key)
        guard let data = box.combined else { throw APIClient.APIError.message("缓存加密失败") }
        try data.write(to: file, options: [.atomic, .completeFileProtection])
    }
}
