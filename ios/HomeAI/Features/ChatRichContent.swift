import SwiftUI
import QuickLook
import CryptoKit

struct ChatPart: Codable, Sendable, Equatable {
    let type: String
    var text: String? = nil
    var record_id: String? = nil
    var version: Int? = nil
    var action_id: String? = nil
    var task_id: String? = nil
    var status: String? = nil
    var title: String? = nil
    var summary: String? = nil
    var html: String? = nil
    init(type: String) { self.type = type }
    init(asset: MediaAsset) { type = asset.kind; record_id = asset.record_id; version = asset.version }
}

struct ChatPartView: View {
    let part: ChatPart
    var actions: [ClientActionRequest] = []
    var validationTaskID: String? = nil
    var expiresAt: Double? = nil
    var body: some View {
        switch part.type {
        case "text": ChatMarkdownText(part.text ?? "")
        case "image", "video", "file":
            if let id = part.record_id, let version = part.version, UUID(uuidString: id) != nil { ChatAssetBubble(recordID: id, version: version, kind: part.type, taskID: part.task_id) }
            else { Text("附件信息无效").foregroundStyle(.secondary) }
        case "h5":
            if let html = part.html {
                ChatInteractionCard(title: part.title ?? "交互内容", subtitle: "打开查看完整内容", icon: "chart.xyaxis.line") {
                    NavigationLink { IsolatedH5View(title: part.title ?? "交互内容", html: html, taskID: validationTaskID, expiresAt: expiresAt) } label: {
                        Label("打开内容", systemImage: "arrow.up.right.square").frame(maxWidth: .infinity, minHeight: 30)
                    }.buttonStyle(.bordered).tint(.teal)
                }
            }
        case "task":
            ChatInteractionCard(title: part.title ?? "服务器任务", subtitle: "在家庭服务器上执行", icon: "sparkles", status: part.status.map(taskStatusLabel)) {
                if let summary = part.summary { ChatMarkdownText(summary) }
                if let id = part.task_id, UUID(uuidString: id) != nil {
                    NavigationLink { TaskProgressView(identifier: id) } label: {
                        Label("查看任务", systemImage: "arrow.right.circle").frame(maxWidth: .infinity, minHeight: 30)
                    }.buttonStyle(.bordered).tint(.teal)
                }
            }
        case "choice", "approval", "data_request":
            if let id = part.action_id {
                if let action = actions.first(where: { $0.id == id }) { ClientActionCard(action: action).id(action.id) }
                else { ClientActionReference(actionID: id) }
            } else { Text(part.summary ?? "请查看下方待确认操作").font(.caption) }
        default: Text(part.summary ?? "这条内容需要更新客户端后查看").foregroundStyle(.secondary)
        }
    }
}

private struct ClientActionReference: View {
    @Environment(AppState.self) private var state
    @Environment(\.scenePhase) private var phase
    let actionID: String
    @State private var action: ClientActionRequest?
    @State private var error: String?
    @State private var generation = UUID()
    private var refreshKey: String { "\(actionID):\(state.connectionRevision):\(state.taskEventRevision):\(state.dataEventRevision):\(phase)" }
    var body: some View {
        Group {
            if let action { ClientActionCard(action: action).id(action.id) }
            else if let error { Text(error).font(.caption).foregroundStyle(.secondary) }
            else { ProgressView("正在读取请求…") }
        }
        .task(id: refreshKey) { await reload() }
        .onChange(of: state.connectionRevision) { _, _ in clear() }
        .onChange(of: state.dataEventRevision) { _, _ in clear() }
        .onChange(of: actionID) { _, _ in clear() }
        .onDisappear { clear() }
    }
    private func clear() { generation = UUID(); action = nil; error = nil }
    private func reload() async {
        guard phase == .active else { clear(); return }
        guard UUID(uuidString: actionID) != nil else { action = nil; error = "请求标识无效"; return }
        let ticket = UUID(), connection = state.connectionRevision
        generation = ticket
        do {
            let namespace = try await state.api.syncNamespace()
            let raw = try await state.api.request("GET", "/api/v1/client-actions/" + actionID, expectedNamespace: namespace)
            let value = try JSONDecoder().decode(ClientActionRequest.self, from: raw)
            guard !Task.isCancelled, ticket == generation, connection == state.connectionRevision,
                  await state.api.cachedNamespace() == namespace, value.id == actionID else { return }
            if let existing = action {
                var presentation = ClientActionPresentation(request: existing)
                presentation.receive(value); action = presentation.current
            } else { action = value }
            error = nil
        } catch {
            guard !Task.isCancelled, ticket == generation, connection == state.connectionRevision else { return }
            action = nil; self.error = "此请求暂不可读，已停止展示旧操作。"
        }
    }
}

struct ChatAssetBubble: View {
    @Environment(AppState.self) private var state
    let recordID: String
    let version: Int
    let kind: String
    var taskID: String? = nil
    @State private var asset: MediaAsset?
    @State private var thumbnail: UIImage?
    @State private var error: String?
    @State private var loadedIdentity: String?
    @State private var loadID = UUID()
    @State private var provenanceVerified = false
    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            NavigationLink { MediaAssetViewer(recordID: recordID, version: version, taskID: taskID) } label: {
                VStack(alignment: .leading, spacing: 6) {
                    if let thumbnail { Image(uiImage: thumbnail).resizable().scaledToFit().frame(maxHeight: 240).clipShape(RoundedRectangle(cornerRadius: 12)) }
                    Label(asset?.name ?? (kind == "image" ? "图片" : kind == "video" ? "视频" : "文件"), systemImage: kind == "image" ? "photo" : kind == "video" ? "play.rectangle" : "doc")
                    if let asset {
                        if provenanceVerified, let label = asset.provenance?.label { Text(label).font(.caption.weight(.semibold)).foregroundStyle(.teal) }
                        Text(ByteCountFormatter.string(fromByteCount: Int64(asset.size), countStyle: .file)).font(.caption)
                        if let processing = asset.processing, processing.status != "succeeded" {
                            Text(["queued": "等待内容分析", "processing": "正在分析内容", "retrying": "内容分析重试中", "failed": "内容分析未完成，原件可查看", "canceled": "内容分析已取消，原件可查看", "unavailable": "原件可查看"][processing.status] ?? processing.status).font(.caption)
                            if let message = processing.error { Text(message).font(.caption).foregroundStyle(.secondary) }
                        }
                    }
                    if let error { Text(error).font(.caption).foregroundStyle(.secondary) }
                }
            }.buttonStyle(.plain)
            if provenanceVerified, let url = asset?.provenance?.safeSourceURL {
                Link("查看来源：" + (url.host ?? "网页"), destination: url).font(.caption)
            }
        }.padding(10).background(.quaternary, in: RoundedRectangle(cornerRadius: 12))
        .task(id: "\(recordID):\(version):\(state.connectionRevision)") { await load() }
        .onChange(of: state.dataEventRevision) { _, _ in Task { await load() } }
        .onChange(of: state.taskEventRevision) { _, _ in if taskID != nil { Task { await load() } } }
        .onChange(of: state.serverReachable) { _, reachable in if !reachable && taskID != nil { loadID = UUID(); asset = nil; thumbnail = nil } }
        .onDisappear { loadID = UUID(); provenanceVerified = false; if taskID != nil { asset = nil; thumbnail = nil } }
    }
    private func load() async {
        let identifier = UUID(); loadID = identifier; provenanceVerified = false
        var validatingMetadata = true
        var cacheNamespace: String?
        do {
            let namespace = try await state.api.syncNamespace(); cacheNamespace = namespace
            let identity = namespace + ":" + ResourceRoutes.cacheScope(taskID) + recordID + ":" + String(version)
            if loadedIdentity != identity { asset = nil; thumbnail = nil; loadedIdentity = identity }
            let key = ResourceRoutes.cacheScope(taskID) + "asset-meta:" + recordID + ":" + String(version)
            if taskID != nil { asset = nil; thumbnail = nil }
            else if let cached = await ClientViewCache.shared.read(key: key, namespace: namespace) {
                guard loadID == identifier, !Task.isCancelled else { return }
                asset = try? JSONDecoder().decode(MediaAsset.self, from: cached)
            }
            let raw = try await state.api.request("GET", ResourceRoutes.asset(recordID, taskID: taskID), expectedNamespace: namespace)
            let value = try JSONDecoder().decode(MediaAsset.self, from: raw)
            guard value.version == version, value.record_id == recordID else { throw APIClient.APIError.http(409, "附件标识或版本已变化") }
            guard await state.api.cachedNamespace() == namespace, loadID == identifier, !Task.isCancelled else { return }
            asset = value; error = nil; validatingMetadata = false; provenanceVerified = true
            try? await ClientViewCache.shared.write(raw, key: key, namespace: namespace)
            if kind == "image" || kind == "video" {
                let thumbnailKey = ResourceRoutes.cacheScope(taskID) + "asset-thumb:" + recordID + ":" + String(version)
                if let cached = await ClientViewCache.shared.read(key: thumbnailKey, namespace: namespace) {
                    guard loadID == identifier, !Task.isCancelled else { return }
                    thumbnail = RecordAttachment.image(cached)
                }
                let bytes = try await state.api.request("GET", ResourceRoutes.asset(recordID, taskID: taskID) + "/thumbnail", expectedNamespace: namespace)
                guard await state.api.cachedNamespace() == namespace, loadID == identifier, !Task.isCancelled else { return }
                thumbnail = RecordAttachment.image(bytes); try? await ClientViewCache.shared.write(bytes, key: thumbnailKey, namespace: namespace)
            }
        } catch {
            guard loadID == identifier else { return }
            if case APIClient.APIError.http(let code, _) = error, [401, 403, 409].contains(code) || (code == 404 && (validatingMetadata || taskID != nil)) {
                if let cacheNamespace { await MediaAssetCache.remove(recordID: recordID, version: version, namespace: cacheNamespace, taskID: taskID) }
                asset = nil; thumbnail = nil; provenanceVerified = false
            }
            self.error = asset == nil ? "附件暂不可用" : nil
        }
    }
}

enum MediaAssetCache {
    static func remove(recordID: String, version: Int, namespace: String, taskID: String? = nil) async {
        let key = ResourceRoutes.cacheScope(taskID) + "asset-meta:" + recordID + ":" + String(version)
        if let raw = await ClientViewCache.shared.read(key: key, namespace: namespace),
           let value = try? JSONDecoder().decode(MediaAsset.self, from: raw), value.record_id == recordID, value.version == version {
            await remove(asset: value, namespace: namespace, taskID: taskID)
        } else {
            await ClientViewCache.shared.remove(key: key, namespace: namespace)
            await ClientViewCache.shared.remove(key: ResourceRoutes.cacheScope(taskID) + "asset-thumb:" + recordID + ":" + String(version), namespace: namespace)
        }
    }
    static func remove(asset: MediaAsset, namespace: String, taskID: String? = nil) async {
        await ClientViewCache.shared.remove(key: ResourceRoutes.cacheScope(taskID) + "asset-meta:" + asset.record_id + ":" + String(asset.version), namespace: namespace)
        await ClientViewCache.shared.remove(key: ResourceRoutes.cacheScope(taskID) + "asset-thumb:" + asset.record_id + ":" + String(asset.version), namespace: namespace)
        guard asset.size >= 0, asset.size <= 1024 * MediaUploadStore.chunkSize else { return }
        for offset in stride(from: 0, to: asset.size, by: MediaUploadStore.chunkSize) {
            await ClientViewCache.shared.remove(key: ResourceRoutes.cacheScope(taskID) + "asset-chunk:" + asset.record_id + ":" + String(asset.version) + ":" + String(offset), namespace: namespace)
        }
    }
}

final class MediaPreviewFile: NSObject, QLPreviewItem, Identifiable, @unchecked Sendable {
    let id = UUID()
    let url: URL
    let name: String
    private let folder: URL
    var previewItemURL: URL? { url }
    var previewItemTitle: String? { name }
    init(name: String) throws {
        self.name = name
        folder = FileManager.default.temporaryDirectory.appendingPathComponent("HomeAIProtectedPreviews", isDirectory: true).appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true, attributes: [.protectionKey: FileProtectionType.complete])
        url = folder.appendingPathComponent(RecordAttachment.safeName(name, data: Data()))
        guard FileManager.default.createFile(atPath: url.path, contents: nil, attributes: [.protectionKey: FileProtectionType.complete]) else { throw APIClient.APIError.message("预览文件无法创建") }
    }
    func remove() { try? FileManager.default.removeItem(at: folder) }
    deinit { try? FileManager.default.removeItem(at: folder) }
}

struct MediaAssetViewer: View {
    @Environment(AppState.self) private var state
    @Environment(\.scenePhase) private var phase
    let recordID: String
    let version: Int
    var taskID: String? = nil
    @State private var file: MediaPreviewFile?
    @State private var text: RecordTextContent?
    @State private var error: String?
    @State private var progress = 0.0
    @State private var lease: UUID?
    @State private var validationLease: UUID?
    @State private var visible = true
    @State private var workingFile: MediaPreviewFile?
    @State private var workingHandle: FileHandle?
    @State private var asset: MediaAsset?
    @State private var namespace: String?
    @State private var supported = true
    @State private var export: RecordExportDocument?
    @State private var exporting = false
    var body: some View {
        Group {
            if let text { RecordLongTextView(content: text) }
            else if let file, supported { MediaQuickLook(file: file) }
            else if file != nil { ContentUnavailableView("暂不支持预览此格式", systemImage: "doc", description: Text("可以手动导出原文件。")) }
            else if let error {
                ContentUnavailableView {
                    Label("附件暂不可用", systemImage: "doc")
                } description: {
                    Text(error)
                } actions: {
                    Button("重试") { Task { await load() } }.buttonStyle(.bordered)
                }
            }
            else { ProgressView("正在读取附件…", value: progress).padding() }
        }.navigationTitle(asset?.name ?? "附件")
            .toolbar { if let file { Button("导出", systemImage: "square.and.arrow.up") {
                do { export = RecordExportDocument(data: try Data(contentsOf: file.url)); exporting = true }
                catch { self.error = error.localizedDescription }
            } } }
            .fileExporter(isPresented: $exporting, document: export, contentType: .data, defaultFilename: asset?.name ?? "附件") { _ in export = nil }
            .task(id: state.connectionRevision) { clear(); await load() }
            .onAppear { visible = true }
            .onDisappear { visible = false; clear() }
            .onChange(of: phase) { _, phase in
                if phase == .background { clear() }
                else if phase == .active && file == nil && text == nil { Task { await load() } }
            }
            .onChange(of: state.connected) { _, connected in
                if !connected { clear(); error = "连接家庭服务器后可重新读取附件" }
                else if file == nil && text == nil { Task { await load() } }
            }
            .onChange(of: state.serverReachable) { _, reachable in
                if !reachable && taskID != nil { clear(); error = "恢复连接后重新确认资料授权" }
                else if reachable && file == nil && text == nil { Task { await load() } }
            }
            .task {
                guard taskID != nil else { return }
                while !Task.isCancelled {
                    do { try await Task.sleep(for: .seconds(30)) } catch { return }
                    if phase == .active { await validateAccess() }
                }
            }
            .onChange(of: state.dataEventRevision) { _, _ in Task { await validateAccess() } }
            .onChange(of: state.taskEventRevision) { _, _ in if taskID != nil { Task { await validateAccess() } } }
    }
    private func clear() {
        lease = nil; validationLease = nil
        try? workingHandle?.close(); workingHandle = nil
        workingFile?.remove(); workingFile = nil
        file?.remove(); file = nil; text = nil; asset = nil; export = nil; exporting = false
    }
    private func validateAccess() async {
        guard visible, phase == .active, !Task.isCancelled else { return }
        // 权限刷新有独立代次；普通数据事件不能中断已经开始的附件下载。
        let identifier = UUID(); validationLease = identifier
        let revision = state.connectionRevision
        defer { if validationLease == identifier { validationLease = nil } }
        do {
            let current = try await state.api.syncNamespace()
            let raw = try await state.api.request("GET", ResourceRoutes.asset(recordID, taskID: taskID), expectedNamespace: current)
            let value = try JSONDecoder().decode(MediaAsset.self, from: raw)
            let currentNamespace = await state.api.cachedNamespace()
            guard validationLease == identifier, revision == state.connectionRevision, visible, phase == .active,
                  !Task.isCancelled, currentNamespace == current else { return }
            guard value.record_id == recordID, value.version == version,
                  asset == nil || asset?.sha256 == value.sha256 else { throw APIClient.APIError.http(409, "附件版本已变化") }
            if file == nil && text == nil && lease == nil { await load() }
        } catch {
            guard validationLease == identifier, revision == state.connectionRevision, visible,
                  phase == .active, !Task.isCancelled else { return }
            if case APIClient.APIError.http(let code, _) = error, [401, 403, 404, 409].contains(code) {
                clear(); self.error = "附件已删除、更新或不再授权"
                if let namespace { await MediaAssetCache.remove(recordID: recordID, version: version, namespace: namespace, taskID: taskID) }
            } else if taskID != nil { clear(); self.error = "暂时无法确认资料授权，请恢复连接后重试" }
            else if file == nil && text == nil && lease == nil { self.error = "暂时无法读取附件，请检查连接后重试" }
        }
    }
    private func currentLoad(_ identifier: UUID, revision: UUID, namespace: String) async -> Bool {
        let currentNamespace = await state.api.cachedNamespace()
        return lease == identifier && revision == state.connectionRevision && visible && phase == .active &&
            !Task.isCancelled && currentNamespace == namespace
    }
    private func load() async {
        guard visible, phase == .active, !Task.isCancelled, lease == nil else { return }
        clear(); error = nil; progress = 0
        let identifier = UUID(); lease = identifier
        let revision = state.connectionRevision
        defer { if lease == identifier { lease = nil; workingFile = nil; workingHandle = nil } }
        var created: MediaPreviewFile?
        do {
            let current = try await state.api.syncNamespace()
            guard await currentLoad(identifier, revision: revision, namespace: current) else { return }
            namespace = current
            let meta = try JSONDecoder().decode(MediaAsset.self, from: await state.api.request("GET", ResourceRoutes.asset(recordID, taskID: taskID), expectedNamespace: current))
            guard await currentLoad(identifier, revision: revision, namespace: current) else { return }
            guard meta.record_id == recordID, meta.version == version, meta.size >= 0, meta.size <= 1024 * MediaUploadStore.chunkSize else { throw APIClient.APIError.message("附件版本或大小暂不支持") }
            asset = meta
            try? await ClientViewCache.shared.write(JSONEncoder().encode(meta), key: ResourceRoutes.cacheScope(taskID) + "asset-meta:" + recordID + ":" + String(version), namespace: current)
            guard await currentLoad(identifier, revision: revision, namespace: current) else { return }
            let preview = try MediaPreviewFile(name: meta.name); created = preview; workingFile = preview
            let output = try FileHandle(forWritingTo: preview.url); workingHandle = output
            defer { try? output.close(); if lease == identifier { workingFile = nil; workingHandle = nil } }
            var hash = SHA256(); var offset = 0
            while offset < meta.size {
                try Task.checkCancellation()
                let length = min(MediaUploadStore.chunkSize, meta.size - offset)
                let cacheKey = ResourceRoutes.cacheScope(taskID) + "asset-chunk:" + recordID + ":" + String(version) + ":" + String(offset)
                let bytes: Data
                if let cached = await ClientViewCache.shared.read(key: cacheKey, namespace: current), cached.count == length { bytes = cached }
                else {
                    bytes = try await state.api.request("GET", ResourceRoutes.asset(recordID, taskID: taskID) + "/content?offset=" + String(offset) + "&length=" + String(length), expectedNamespace: current)
                    guard bytes.count == length else { throw APIClient.APIError.message("附件分块不完整") }
                    try? await ClientViewCache.shared.write(bytes, key: cacheKey, namespace: current)
                }
                guard await currentLoad(identifier, revision: revision, namespace: current) else { throw CancellationError() }
                hash.update(data: bytes); try output.write(contentsOf: bytes)
                offset += bytes.count; progress = Double(offset) / Double(max(meta.size, 1))
            }
            guard hash.finalize().map({ String(format: "%02x", $0) }).joined() == meta.sha256 else {
                await MediaAssetCache.remove(asset: meta, namespace: current, taskID: taskID)
                throw APIClient.APIError.message("附件校验失败，缓存已清除，请重新下载")
            }
            // 本地分块全部命中也要在呈现前重新校验当前授权，防止迟到预览覆盖撤权结果。
            let latest = try JSONDecoder().decode(MediaAsset.self, from: await state.api.request("GET", ResourceRoutes.asset(recordID, taskID: taskID), expectedNamespace: current))
            guard await currentLoad(identifier, revision: revision, namespace: current) else { throw CancellationError() }
            guard latest.record_id == recordID, latest.version == version, latest.sha256 == meta.sha256 else { throw APIClient.APIError.http(409, "附件版本已变化") }
            try output.synchronize()
            let input = try FileHandle(forReadingFrom: preview.url); defer { try? input.close() }
            let sample = try input.read(upToCount: 4096) ?? Data()
            let format = meta.kind == "video" || meta.kind == "image" ? RecordPreviewFormat.quickLook : RecordAttachment.format(name: meta.name, mime: meta.mime_type, data: sample)
            if format == .text, meta.size <= 50 * 1024 * 1024, let content = RecordAttachment.text(try Data(contentsOf: preview.url)) {
                text = RecordTextContent(title: meta.name, text: content, isHTML: RecordAttachment.isHTML(name: meta.name, mime: meta.mime_type, data: Data(content.utf8))); preview.remove()
            } else { supported = format == .quickLook; file = preview }
        } catch {
            created?.remove()
            guard lease == identifier, revision == state.connectionRevision, visible, phase == .active else { return }
            if case APIClient.APIError.http(let code, _) = error, [401, 403, 404, 409].contains(code), let namespace { await MediaAssetCache.remove(recordID: recordID, version: version, namespace: namespace, taskID: taskID) }
            guard lease == identifier, revision == state.connectionRevision, visible, phase == .active else { return }
            self.error = error is CancellationError ? "附件读取已中断，请重试" : error.localizedDescription
        }
    }
}

private struct MediaQuickLook: UIViewControllerRepresentable {
    let file: MediaPreviewFile
    func makeCoordinator() -> Coordinator { Coordinator(file: file) }
    func makeUIViewController(context: Context) -> QLPreviewController { let view = QLPreviewController(); view.dataSource = context.coordinator; return view }
    func updateUIViewController(_ uiViewController: QLPreviewController, context: Context) { }
    final class Coordinator: NSObject, QLPreviewControllerDataSource {
        let file: MediaPreviewFile
        init(file: MediaPreviewFile) { self.file = file }
        func numberOfPreviewItems(in controller: QLPreviewController) -> Int { 1 }
        func previewController(_ controller: QLPreviewController, previewItemAt index: Int) -> any QLPreviewItem { file }
    }
}
