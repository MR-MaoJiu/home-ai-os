import Foundation
import CryptoKit

struct MediaUploadLimits: Codable, Sendable {
    let chunk_size: Int
    let image_max_bytes: Int
    let file_max_bytes: Int
    let video_max_bytes: Int
    let video_max_seconds: Int
    static let defaults = MediaUploadLimits(chunk_size: 1048576, image_max_bytes: 20 * 1048576, file_max_bytes: 50 * 1048576, video_max_bytes: 200 * 1048576, video_max_seconds: 600)
    func maximum(_ kind: String) -> Int { kind == "video" ? video_max_bytes : kind == "image" ? image_max_bytes : file_max_bytes }
    static func fetch(api: APIClient, namespace: String) async -> MediaUploadLimits {
        do {
            let data = try await api.request("GET", "/api/v1/uploads/limits", expectedNamespace: namespace)
            let value = try JSONDecoder().decode(Self.self, from: data)
            guard value.chunk_size == 1048576, value.image_max_bytes > 0, value.file_max_bytes > 0, value.video_max_bytes > 0 else { return .defaults }
            try? await ClientViewCache.shared.write(data, key: "upload-limits", namespace: namespace)
            return value
        } catch {
            if let raw = await ClientViewCache.shared.read(key: "upload-limits", namespace: namespace), let cached = try? JSONDecoder().decode(Self.self, from: raw) { return cached }
            return .defaults
        }
    }
}

struct MediaAsset: Codable, Identifiable, Sendable {
    struct Processing: Codable, Sendable { let status: String; let task_id: String?; let error: String?; let result_record_id: String? }
    let record_id: String
    let version: Int
    let kind: String
    let mime_type: String
    let name: String
    let size: Int
    let sha256: String
    let duration_seconds: Double?
    let processing: Processing?
    var id: String { record_id }
}

struct MediaUploadState: Codable, Sendable {
    let upload_id: String
    let status: String
    let chunk_size: Int
    let total_chunks: Int
    let received_parts: [Int]
    let expires_at: Double
    let asset: MediaAsset?
}

struct QueuedMedia: Codable, Identifiable, Sendable {
    let id: String
    var clientID: String
    let name: String
    let kind: String
    let mime: String
    let size: Int
    let sha256: String
    let totalChunks: Int
    let scope: String
    let createdAt: Double
    var serverID: String?
    var received: [Int]
    var status: String
    var asset: MediaAsset?
    var error: String?
    var progress: Double { asset == nil ? Double(received.count) / Double(max(totalChunks, 1)) : 1 }
}

/// 上传源以 1 MiB 分块加密持久化；重连后以服务端接收清单为准续传。
actor MediaUploadStore {
    static let shared = MediaUploadStore()
    static let chunkSize = 1024 * 1024
    private let root: URL?
    private let testKey: SymmetricKey?
    private var rejected: Set<String> = []
    private let uploads = SharedClientOperation<QueuedMedia>()
    private var uploadLeases: [String: UUID] = [:]
    init(root: URL? = nil, key: SymmetricKey? = nil) { self.root = root; self.testKey = key }

    static func limit(_ kind: String) -> Int { kind == "video" ? 200 * chunkSize : kind == "image" ? 20 * chunkSize : 50 * chunkSize }

    func enqueue(data: Data, name: String, kind: String, mime: String, namespace: String, scope: String = "chat", maximumBytes: Int? = nil) throws -> QueuedMedia {
        let source = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try data.write(to: source, options: [.completeFileProtection])
        defer { try? FileManager.default.removeItem(at: source) }
        return try enqueue(file: source, name: name, kind: kind, mime: mime, namespace: namespace, scope: scope, maximumBytes: maximumBytes)
    }

    func enqueue(file: URL, name: String, kind: String, mime: String, namespace: String, scope: String = "chat", maximumBytes: Int? = nil) throws -> QueuedMedia {
        guard ["image", "file", "video"].contains(kind), !rejected.contains(namespace) else { throw APIClient.APIError.message("附件类型或当前授权无效") }
        let pending = try list(namespace: namespace)
        guard pending.count < 20 else { throw APIClient.APIError.message("待发送附件过多，请先发送或移除部分附件") }
        let size = (try file.resourceValues(forKeys: [.fileSizeKey])).fileSize ?? 0
        let maximum = maximumBytes ?? Self.limit(kind)
        guard size > 0, size <= maximum, pending.reduce(0, { $0 + $1.size }) + size <= max(1024 * Self.chunkSize, maximum * 2) else { throw APIClient.APIError.message("附件超过大小或本机待发送空间限制") }
        let identifier = UUID().uuidString.lowercased()
        let folder = try directory(namespace).appendingPathComponent(identifier, isDirectory: true)
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true, attributes: [.protectionKey: FileProtectionType.complete])
        do {
            let input = try FileHandle(forReadingFrom: file); defer { try? input.close() }
            var hash = SHA256(); var count = 0; var total = 0
            let encryptionKey = try key(namespace)
            while let bytes = try input.read(upToCount: Self.chunkSize), !bytes.isEmpty {
                total += bytes.count
                guard total <= size else { throw APIClient.APIError.message("选择的文件在读取期间发生变化") }
                hash.update(data: bytes)
                try seal(bytes, file: folder.appendingPathComponent("\(count).enc"), key: encryptionKey, purpose: namespace + ":" + identifier + ":" + String(count))
                count += 1
            }
            guard total == size else { throw APIClient.APIError.message("选择的文件未能完整读取") }
            let item = QueuedMedia(id: identifier, clientID: identifier, name: String((name as NSString).lastPathComponent.prefix(200)), kind: kind, mime: mime,
                size: size, sha256: hash.finalize().map { String(format: "%02x", $0) }.joined(), totalChunks: count, scope: scope, createdAt: Date().timeIntervalSince1970,
                serverID: nil, received: [], status: "queued", asset: nil, error: nil)
            try save(item, namespace: namespace)
            return item
        } catch { try? FileManager.default.removeItem(at: folder); throw error }
    }

    func list(namespace: String, scope: String? = nil) throws -> [QueuedMedia] {
        guard !rejected.contains(namespace) else { return [] }
        let folder = try directory(namespace)
        guard FileManager.default.fileExists(atPath: folder.path) else { return [] }
        return try FileManager.default.contentsOfDirectory(at: folder, includingPropertiesForKeys: nil).compactMap { folder in
            guard UUID(uuidString: folder.lastPathComponent) != nil,
                  let data = try? open(folder.appendingPathComponent("manifest.enc"), key: key(namespace), purpose: namespace + ":" + folder.lastPathComponent + ":manifest"),
                  let item = try? JSONDecoder().decode(QueuedMedia.self, from: data), item.id == folder.lastPathComponent,
                  scope == nil || scope == item.scope else { return nil }
            return item
        }.sorted { $0.createdAt < $1.createdAt }
    }

    func remove(id: String, namespace: String) throws {
        guard UUID(uuidString: id) != nil else { return }
        let folder = try directory(namespace).appendingPathComponent(id, isDirectory: true)
        if FileManager.default.fileExists(atPath: folder.path) { try FileManager.default.removeItem(at: folder) }
    }

    func retry(id: String, namespace: String) throws {
        guard var item = try list(namespace: namespace).first(where: { $0.id == id }) else { return }
        if ["expired", "canceled"].contains(item.status) { item.clientID = UUID().uuidString.lowercased(); item.serverID = nil; item.received = [] }
        item.status = "queued"; item.error = nil
        try save(item, namespace: namespace)
    }

    func upload(id: String, namespace: String, api: APIClient, progress: @escaping @Sendable (QueuedMedia) async -> Void) async throws -> QueuedMedia {
        try await uploads.run(key: namespace + ":" + id) { try await self.performUpload(id: id, namespace: namespace, api: api, progress: progress) }
    }

    private func performUpload(id: String, namespace: String, api: APIClient, progress: @escaping @Sendable (QueuedMedia) async -> Void) async throws -> QueuedMedia {
        let operationKey = namespace + ":" + id
        let lease = UUID(); uploadLeases[operationKey] = lease
        defer { if uploadLeases[operationKey] == lease { uploadLeases[operationKey] = nil } }
        guard var item = try list(namespace: namespace).first(where: { $0.id == id }) else { throw APIClient.APIError.message("本机附件已移除") }
        if item.asset != nil { return item }
        do {
            let current: MediaUploadState
            if let serverID = item.serverID {
                current = try JSONDecoder().decode(MediaUploadState.self, from: await api.request("GET", "/api/v1/uploads/" + serverID, expectedNamespace: namespace))
            } else {
                let body: [String: Any] = ["client_id": item.clientID, "name": item.name, "kind": item.kind, "mime_type": item.mime, "size": item.size, "sha256": item.sha256]
                current = try JSONDecoder().decode(MediaUploadState.self, from: await api.request("POST", "/api/v1/uploads", body: JSONSerialization.data(withJSONObject: body), expectedNamespace: namespace))
            }
            try Task.checkCancellation()
            guard uploadLeases[operationKey] == lease else { throw CancellationError() }
            guard current.chunk_size == Self.chunkSize, current.total_chunks == item.totalChunks else { throw APIClient.APIError.message("服务器上传分块契约不匹配") }
            item.serverID = current.upload_id; item.received = current.received_parts
            guard UUID(uuidString: current.upload_id) != nil, Set(current.received_parts).count == current.received_parts.count,
                  current.received_parts.allSatisfy({ $0 >= 0 && $0 < item.totalChunks }) else { throw APIClient.APIError.message("服务器上传状态无效") }
            if let asset = current.asset {
                guard asset.sha256 == item.sha256, asset.size == item.size, asset.kind == item.kind, UUID(uuidString: asset.record_id) != nil, asset.version > 0 else { throw APIClient.APIError.message("已完成附件与本机原件不一致") }
                item.asset = asset; item.status = "completed"; try save(item, namespace: namespace); await progress(item); return item
            }
            guard current.status == "receiving", current.expires_at > Date().timeIntervalSince1970 else {
                item.status = current.status == "canceled" ? "canceled" : "expired"
                throw APIClient.APIError.message("上传已过期或取消，请点击重试")
            }
            item.status = "uploading"; item.error = nil; try save(item, namespace: namespace); await progress(item)
            let folder = try directory(namespace).appendingPathComponent(item.id, isDirectory: true)
            let encryptionKey = try key(namespace)
            for index in 0..<item.totalChunks where !item.received.contains(index) {
                try Task.checkCancellation()
                let bytes = try open(folder.appendingPathComponent("\(index).enc"), key: encryptionKey, purpose: namespace + ":" + item.id + ":" + String(index))
                struct Receipt: Decodable { let index: Int; let size: Int; let sha256: String; let received: Bool }
                let response = try await api.request("PUT", "/api/v1/uploads/" + current.upload_id + "/chunks/" + String(index), body: bytes, expectedNamespace: namespace, contentType: "application/octet-stream")
                try Task.checkCancellation()
                guard uploadLeases[operationKey] == lease else { throw CancellationError() }
                let receipt = try JSONDecoder().decode(Receipt.self, from: response)
                guard receipt.received, receipt.index == index, receipt.size == bytes.count, receipt.sha256 == DeviceIdentity.hash(bytes) else { throw APIClient.APIError.message("服务器未确认完整附件分块") }
                item.received.append(index); try save(item, namespace: namespace); await progress(item)
            }
            try Task.checkCancellation()
            let completed = try JSONDecoder().decode(MediaUploadState.self, from: await api.request("POST", "/api/v1/uploads/" + current.upload_id + "/complete", expectedNamespace: namespace))
            try Task.checkCancellation()
            guard uploadLeases[operationKey] == lease else { throw CancellationError() }
            guard let asset = completed.asset, completed.status == "completed", asset.sha256 == item.sha256, asset.size == item.size, asset.kind == item.kind, UUID(uuidString: asset.record_id) != nil, asset.version > 0 else { throw APIClient.APIError.message("附件尚未完成校验") }
            item.asset = asset; item.status = "completed"; item.error = nil
            try save(item, namespace: namespace); await progress(item)
            return item
        } catch {
            // 上一轮取消后的迟到响应不能覆盖已启动的新续传状态。
            guard uploadLeases[operationKey] == lease, !rejected.contains(namespace) else { throw error }
            if Task.isCancelled || error is CancellationError { item.status = "paused"; item.error = "等待回到前台后继续" }
            else {
                if case APIClient.APIError.http(410, _) = error { item.status = "expired" }
                else if !["expired", "canceled"].contains(item.status) { item.status = "failed" }
                item.error = error.localizedDescription
            }
            try? save(item, namespace: namespace); await progress(item)
            throw error
        }
    }

    func invalidate(namespace: String) {
        rejected.insert(namespace)
        if let folder = try? directory(namespace) { try? FileManager.default.removeItem(at: folder) }
    }
    func authorize(namespace: String) { rejected.remove(namespace) }

    private func save(_ item: QueuedMedia, namespace: String) throws {
        guard !rejected.contains(namespace) else { throw APIClient.APIError.message("附件授权已失效") }
        let file = try directory(namespace).appendingPathComponent(item.id).appendingPathComponent("manifest.enc")
        try seal(JSONEncoder().encode(item), file: file, key: key(namespace), purpose: namespace + ":" + item.id + ":manifest")
    }
    private func directory(_ namespace: String) throws -> URL {
        let base = try root ?? FileManager.default.url(for: .applicationSupportDirectory, in: .userDomainMask, appropriateFor: nil, create: true).appendingPathComponent("HomeAI/MediaQueue", isDirectory: true)
        let folder = base.appendingPathComponent(DeviceIdentity.hash(Data(namespace.utf8)), isDirectory: true)
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true, attributes: [.protectionKey: FileProtectionType.complete])
        var url = folder; var attrs = URLResourceValues(); attrs.isExcludedFromBackup = true; try url.setResourceValues(attrs)
        return folder
    }
    private func key(_ namespace: String) throws -> SymmetricKey {
        if let testKey { return testKey }
        let name = "media-upload-key:" + namespace
        if let raw = DeviceIdentity.read(name), raw.count == 32 { return SymmetricKey(data: raw) }
        let value = SymmetricKey(size: .bits256)
        try DeviceIdentity.save(value.withUnsafeBytes { Data($0) }, name: name)
        return value
    }
    private func seal(_ data: Data, file: URL, key: SymmetricKey, purpose: String) throws {
        let box = try AES.GCM.seal(data, using: key, authenticating: Data(purpose.utf8))
        guard let value = box.combined else { throw APIClient.APIError.message("附件加密失败") }
        try value.write(to: file, options: [.atomic, .completeFileProtection])
    }
    private func open(_ file: URL, key: SymmetricKey, purpose: String) throws -> Data {
        try AES.GCM.open(AES.GCM.SealedBox(combined: Data(contentsOf: file)), using: key, authenticating: Data(purpose.utf8))
    }
}

import Observation

@MainActor @Observable
final class MediaDraft {
    var items: [QueuedMedia] = []
    var error: String?
    var notice: String?
    var limits = MediaUploadLimits.defaults
    var uploading = false
    var namespace: String?
    let scope: String
    private var operation: Task<Void, Never>?
    private var resumeRequested = false
    init(scope: String = "chat") { self.scope = scope }
    var ready: Bool { !items.isEmpty && items.allSatisfy { $0.asset != nil } }

    func restore(api: APIClient) async {
        do {
            let current = try await api.syncNamespace()
            if namespace != current { pause(); await operation?.value; items = []; namespace = current }
            items = try await MediaUploadStore.shared.list(namespace: current, scope: scope)
            if scope == "data" {
                for item in items where item.asset != nil { try? await MediaUploadStore.shared.remove(id: item.id, namespace: current) }
                items.removeAll { $0.asset != nil }
            }
            limits = await MediaUploadLimits.fetch(api: api, namespace: current)
        } catch { self.error = error.localizedDescription }
    }
    func enqueue(file: URL, name: String, kind: String, mime: String, api: APIClient) async {
        do {
            let current = try await api.syncNamespace(); namespace = current
            let item = try await MediaUploadStore.shared.enqueue(file: file, name: name, kind: kind, mime: mime, namespace: current, scope: scope, maximumBytes: limits.maximum(kind))
            items.append(item); error = nil; resume(api: api)
        } catch { self.error = error.localizedDescription }
    }
    func enqueue(data: Data, name: String, kind: String, mime: String, api: APIClient) async {
        do {
            let current = try await api.syncNamespace(); namespace = current
            let item = try await MediaUploadStore.shared.enqueue(data: data, name: name, kind: kind, mime: mime, namespace: current, scope: scope, maximumBytes: limits.maximum(kind))
            items.append(item); error = nil; resume(api: api)
        } catch { self.error = error.localizedDescription }
    }
    func resume(api: APIClient) {
        if let operation { if operation.isCancelled { resumeRequested = true }; return }
        guard let namespace else { return }
        operation = Task { [weak self] in
            guard let self else { return }
            self.uploading = true; self.error = nil
            defer {
                self.uploading = false; self.operation = nil
                if self.resumeRequested { self.resumeRequested = false; self.resume(api: api) }
            }
            while let item = self.items.first(where: { $0.asset == nil }) {
                if Task.isCancelled { break }
                do {
                    if ["failed", "paused", "expired", "canceled"].contains(item.status) { try await MediaUploadStore.shared.retry(id: item.id, namespace: namespace) }
                    _ = try await MediaUploadStore.shared.upload(id: item.id, namespace: namespace, api: api) { [weak self] progress in
                        await MainActor.run {
                            guard let self, self.namespace == namespace else { return }
                            if let index = self.items.firstIndex(where: { $0.id == progress.id }) { self.items[index] = progress }
                        }
                    }
                    if self.scope == "data" {
                        try? await MediaUploadStore.shared.remove(id: item.id, namespace: namespace)
                        self.items.removeAll { $0.id == item.id }
                        self.notice = "文件已保存，后续处理由服务器完成"
                    }
                } catch { if !Task.isCancelled { self.error = error.localizedDescription }; break }
            }
        }
    }
    func pause() { resumeRequested = false; operation?.cancel() }
    func remove(_ item: QueuedMedia, api: APIClient) async {
        pause()
        await operation?.value
        guard let namespace else { return }
        if let serverID = item.serverID, item.asset == nil { _ = try? await api.request("DELETE", "/api/v1/uploads/" + serverID, expectedNamespace: namespace) }
        try? await MediaUploadStore.shared.remove(id: item.id, namespace: namespace)
        items.removeAll { $0.id == item.id }
        resume(api: api)
    }
    func acknowledge(ids: [String]) async {
        guard let namespace else { return }
        for id in ids { try? await MediaUploadStore.shared.remove(id: id, namespace: namespace) }
        items.removeAll { ids.contains($0.id) }
    }
}
