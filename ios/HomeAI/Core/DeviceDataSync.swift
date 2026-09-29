import Foundation
import CryptoKit

/// 缓存写入先于确认；断网后可重放确认，不能提前推进服务端游标。
actor DeviceDataSync {
    struct Result: Sendable { let records: [DataEntry]; let offline: Bool; var startedAt: Date? = nil }
    private struct Snapshot: Decodable { let snapshot_id: String; let watermark: Int }
    private struct Page: Decodable { let records: [DataEntry]; let removed_ids: [String]; let next_offset: Int; let done: Bool }
    private struct Changes: Decodable { let records: [DataEntry]; let removed_ids: [String]; let next_cursor: Int; let has_more: Bool }
    private struct Ack: Codable { let cursor: Int; let snapshot_id: String? }
    private struct PendingSnapshot: Codable { let id: String; let watermark: Int; var offset: Int; var records: [DataEntry] }
    private struct Cache: Codable { var format_version: Int? = 2; var cursor: Int; var records: [DataEntry]; var pendingAck: Ack?; var snapshot: PendingSnapshot? }
    private let work = SharedClientOperation<Result>()

    func cachedSnapshot(api: APIClient) async -> Result? {
        guard let namespace = await api.cachedNamespace(), let file = try? cacheURL(namespace),
              let key = try? cacheKey(namespace), let cached = try? load(file, key), cached.snapshot == nil,
              await api.cachedNamespace() == namespace else { return nil }
        return Result(records: cached.records.sorted { $0.id < $1.id }, offline: true)
    }

    func invalidate(namespace: String) {
        if let file = try? cacheURL(namespace) { try? FileManager.default.removeItem(at: file) }
    }

    func synchronize(api: APIClient) async throws -> Result {
        let namespace = try await api.syncNamespace()
        return try await work.run(key: namespace) { try await self.performSync(api: api, namespace: namespace) }
    }

    private func performSync(api: APIClient, namespace: String) async throws -> Result {
        let startedAt = Date()
        try Task.checkCancellation()
        let file = try cacheURL(namespace)
        let key = try cacheKey(namespace)
        var cached = try? load(file, key)
        for attempt in 0..<2 {
            do {
                if let pending = cached?.pendingAck {
                    _ = try await api.request("POST", "/api/v1/sync/ack", body: JSONEncoder().encode(pending), expectedNamespace: namespace)
                    try Task.checkCancellation()
                    cached?.pendingAck = nil
                    if let current = cached { try save(current, file, key) }
                }
                if cached == nil {
                    let raw = try await api.request("POST", "/api/v1/sync/snapshot", expectedNamespace: namespace)
                    try Task.checkCancellation()
                    let snapshot = try JSONDecoder().decode(Snapshot.self, from: raw)
                    cached = Cache(cursor: 0, records: [], pendingAck: nil, snapshot: PendingSnapshot(id: snapshot.snapshot_id, watermark: snapshot.watermark, offset: 0, records: []))
                    try save(cached!, file, key)
                }
                while var current = cached, var snapshot = current.snapshot {
                    try Task.checkCancellation()
                    let bytes = try await api.request("GET", "/api/v1/sync/snapshot/\(snapshot.id)?offset=\(snapshot.offset)&limit=100", expectedNamespace: namespace)
                    try Task.checkCancellation()
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
                    try Task.checkCancellation()
                    let changes = try JSONDecoder().decode(Changes.self, from: raw)
                    if !changes.has_more, changes.next_cursor == current.cursor, changes.records.isEmpty, changes.removed_ids.isEmpty {
                        guard await api.cachedNamespace() == namespace else { throw APIClient.APIError.message("连接已切换") }
                        return Result(records: current.records.sorted { $0.id < $1.id }, offline: false, startedAt: startedAt)
                    }
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
                        guard await api.cachedNamespace() == namespace else { throw APIClient.APIError.message("连接已切换") }
                        return Result(records: current.records.sorted { $0.id < $1.id }, offline: false, startedAt: startedAt)
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
                    guard await api.cachedNamespace() == namespace else { throw APIClient.APIError.message("连接已切换") }
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
        let cache = try JSONDecoder().decode(Cache.self, from: AES.GCM.open(box, using: key))
        guard cache.format_version == 2 else { throw APIClient.APIError.message("缓存格式已升级，正在重新获取资料目录") }
        return cache
    }

    private func save(_ cache: Cache, _ file: URL, _ key: SymmetricKey) throws {
        let box = try AES.GCM.seal(JSONEncoder().encode(cache), using: key)
        guard let data = box.combined else { throw APIClient.APIError.message("缓存加密失败") }
        try data.write(to: file, options: [.atomic, .completeFileProtection])
    }
}

/// 同一资源的并发读取共享一次工作；取消最后一个等待者时才取消底层请求。
actor SharedClientOperation<Value: Sendable> {
    private struct Flight {
        let id: UUID
        var task: Task<Void, Never>
        var waiters: [UUID: CheckedContinuation<Value, Error>]
    }
    private var flights: [String: Flight] = [:]
    func waitingCount(key: String) -> Int { flights[key]?.waiters.count ?? 0 }

    func run(key: String, operation: @escaping @Sendable () async throws -> Value) async throws -> Value {
        let waiter = UUID()
        return try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { continuation in
                if Task.isCancelled { continuation.resume(throwing: CancellationError()); return }
                if flights[key] != nil { flights[key]?.waiters[waiter] = continuation; return }
                let identifier = UUID()
                let task = Task {
                    let result: Result<Value, Error>
                    do { result = .success(try await operation()) } catch { result = .failure(error) }
                    self.finish(key: key, id: identifier, result: result)
                }
                flights[key] = Flight(id: identifier, task: task, waiters: [waiter: continuation])
            }
        } onCancel: { Task { await self.cancel(key: key, waiter: waiter) } }
    }

    private func finish(key: String, id: UUID, result: Result<Value, Error>) {
        guard let flight = flights[key], flight.id == id else { return }
        flights[key] = nil
        for waiter in flight.waiters.values { waiter.resume(with: result) }
    }

    private func cancel(key: String, waiter: UUID) {
        guard var flight = flights[key], let continuation = flight.waiters.removeValue(forKey: waiter) else { return }
        continuation.resume(throwing: CancellationError())
        if flight.waiters.isEmpty { flights[key] = nil; flight.task.cancel() }
        else { flights[key] = flight }
    }
}

/// 页面快照只用于立即展示；每次在线读取仍由服务器重新鉴权，缓存不提供权限。
actor ClientViewCache {
    static let shared = ClientViewCache()
    private struct Entry: Codable {
        let version: Int
        let namespace: String
        let key: String
        let payload: Data
    }
    private let directory: URL?
    private let testKey: SymmetricKey?
    private var rejectedNamespaces: Set<String> = []
    private let mediaBudget: Int
    init(directory: URL? = nil, key: SymmetricKey? = nil, mediaBudget: Int = 512 * 1024 * 1024) {
        self.directory = directory; self.testKey = key; self.mediaBudget = max(0, mediaBudget)
    }
    private func isMedia(_ key: String) -> Bool { key.contains("asset-chunk:") || key.contains("asset-thumb:") }

    func read(key: String, namespace: String) -> Data? {
        guard !rejectedNamespaces.contains(namespace) else { return nil }
        do {
            let path = try file(key: key, namespace: namespace)
            let cipher = try AES.GCM.SealedBox(combined: Data(contentsOf: path))
            let raw = try AES.GCM.open(cipher, using: encryptionKey(namespace), authenticating: Data((namespace + ":" + key).utf8))
            let entry = try JSONDecoder().decode(Entry.self, from: raw)
            guard entry.version == 1, entry.namespace == namespace, entry.key == key else { return nil }
            return entry.payload
        } catch { return nil }
    }

    func write(_ data: Data, key: String, namespace: String) throws {
        guard !rejectedNamespaces.contains(namespace) else { throw APIClient.APIError.message("授权已失效，不能保存旧页面缓存") }
        let path = try file(key: key, namespace: namespace)
        try FileManager.default.createDirectory(at: path.deletingLastPathComponent(), withIntermediateDirectories: true)
        var folder = path.deletingLastPathComponent()
        var attributes = URLResourceValues(); attributes.isExcludedFromBackup = true
        try folder.setResourceValues(attributes)
        let payload = try JSONEncoder().encode(Entry(version: 1, namespace: namespace, key: key, payload: data))
        let box = try AES.GCM.seal(payload, using: encryptionKey(namespace), authenticating: Data((namespace + ":" + key).utf8))
        guard let bytes = box.combined else { throw APIClient.APIError.message("页面缓存加密失败") }
        if isMedia(key) {
            guard bytes.count <= mediaBudget else { return }
            try trimMedia(in: path.deletingLastPathComponent(), replacing: path, incomingBytes: bytes.count)
        }
        try bytes.write(to: path, options: [.atomic, .completeFileProtection])
    }

    func remove(key: String, namespace: String) {
        if let path = try? file(key: key, namespace: namespace) { try? FileManager.default.removeItem(at: path) }
    }

    func authorize(namespace: String) { rejectedNamespaces.remove(namespace) }

    func invalidate(namespace: String, revoked: Bool = false) {
        if revoked { rejectedNamespaces.insert(namespace) }
        if let path = try? file(key: "", namespace: namespace).deletingLastPathComponent() { try? FileManager.default.removeItem(at: path) }
    }

    private func file(key: String, namespace: String) throws -> URL {
        let root = try directory ?? FileManager.default.url(for: .applicationSupportDirectory, in: .userDomainMask, appropriateFor: nil, create: true).appendingPathComponent("HomeAI/Views", isDirectory: true)
        var folder = root.appendingPathComponent(DeviceIdentity.hash(Data(namespace.utf8)), isDirectory: true)
        if isMedia(key) { folder = folder.appendingPathComponent("media", isDirectory: true) }
        return folder.appendingPathComponent(DeviceIdentity.hash(Data(key.utf8)) + ".sealed")
    }

    /// 媒体缓存与待发送队列分离，淘汰不会删除用户尚未送达的附件。
    private func trimMedia(in folder: URL, replacing: URL, incomingBytes: Int) throws {
        let files = try FileManager.default.contentsOfDirectory(at: folder, includingPropertiesForKeys: [.fileSizeKey, .contentModificationDateKey])
        let entries = files.filter { $0 != replacing }.compactMap { file -> (URL, Int, Date)? in
            guard let values = try? file.resourceValues(forKeys: [.fileSizeKey, .contentModificationDateKey]) else { return nil }
            return (file, values.fileSize ?? 0, values.contentModificationDate ?? .distantPast)
        }.sorted { $0.2 < $1.2 }
        var total = entries.reduce(incomingBytes) { $0 + $1.1 }
        for entry in entries where total > mediaBudget {
            try FileManager.default.removeItem(at: entry.0); total -= entry.1
        }
    }

    private func encryptionKey(_ namespace: String) throws -> SymmetricKey {
        if let testKey { return testKey }
        let name = "view-cache-key:" + DeviceIdentity.hash(Data(namespace.utf8))
        if let data = DeviceIdentity.read(name), data.count == 32 { return SymmetricKey(data: data) }
        let key = SymmetricKey(size: .bits256)
        try DeviceIdentity.save(key.withUnsafeBytes { Data($0) }, name: name)
        return key
    }
}
