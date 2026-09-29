import SwiftUI
import PhotosUI
import UniformTypeIdentifiers
import CoreTransferable

struct PickedMediaFile: Transferable, Sendable {
    let url: URL
    static var transferRepresentation: some TransferRepresentation {
        FileRepresentation(importedContentType: .movie) { received in try copy(received.file) }
        FileRepresentation(importedContentType: .image) { received in try copy(received.file) }
    }
    private static func copy(_ source: URL) throws -> PickedMediaFile {
        let size = (try source.resourceValues(forKeys: [.fileSizeKey])).fileSize ?? 0
        guard size <= 1024 * MediaUploadStore.chunkSize else { throw APIClient.APIError.message("资源超过本客户端的 1 GiB 选择限制") }
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent("HomeAIMediaImport", isDirectory: true).appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true, attributes: [.protectionKey: FileProtectionType.complete])
        let target = folder.appendingPathComponent("selected." + (source.pathExtension.isEmpty ? "data" : source.pathExtension))
        do {
            try FileManager.default.copyItem(at: source, to: target)
            try FileManager.default.setAttributes([.protectionKey: FileProtectionType.complete], ofItemAtPath: target.path)
            return PickedMediaFile(url: target)
        } catch { try? FileManager.default.removeItem(at: folder); throw error }
    }
}

struct MediaComposer: View {
    @Environment(AppState.self) private var state
    @Environment(\.scenePhase) private var phase
    let draft: MediaDraft
    var allowsFiles = true
    var allowsPhotos = true
    var allowsVideos = true
    var maxCount = 5
    var maximumBytes: Int? = nil
    var acceptedMIMEs: [String] = []
    @State private var photos: [PhotosPickerItem] = []
    @State private var importing = false
    @State private var staging = false
    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                if allowsPhotos || allowsVideos {
                    PhotosPicker(selection: $photos, maxSelectionCount: max(1, maxCount - draft.items.count), matching: allowsVideos ? .any(of: [.images, .videos]) : .images) {
                        Label(allowsVideos ? "照片/视频" : "选择照片", systemImage: "photo.on.rectangle")
                    }.disabled(staging || draft.items.count >= maxCount)
                }
                if allowsFiles { Button("文件", systemImage: "paperclip") { importing = true }.disabled(staging || draft.items.count >= maxCount) }
                if draft.items.contains(where: { $0.asset == nil }) && !draft.uploading {
                    Button("继续上传") { draft.resume(api: state.api) }.disabled(staging)
                }
                if staging { ProgressView() }
            }.font(.caption)
            if let notice = draft.notice { Text(notice).font(.caption).foregroundStyle(.secondary) }
            if let error = draft.error { Text(error).font(.caption).foregroundStyle(.red) }
            ForEach(draft.items) { item in
                HStack {
                    Image(systemName: item.kind == "image" ? "photo" : item.kind == "video" ? "video" : "doc")
                    VStack(alignment: .leading, spacing: 3) {
                        Text(item.name).font(.caption).lineLimit(1)
                        if item.asset == nil { ProgressView(value: item.progress); Text(item.error ?? (item.status == "queued" ? "等待上传" : "正在上传")).font(.caption2).foregroundStyle(.secondary) }
                        else { Text(item.asset?.processing?.status == "failed" ? "已上传，服务器处理失败" : "已上传").font(.caption2).foregroundStyle(.secondary) }
                    }
                    Button("移出草稿", systemImage: "xmark.circle") { Task { await draft.remove(item, api: state.api) } }.labelStyle(.iconOnly)
                }
            }
        }
        .task(id: state.connectionRevision) { await draft.restore(api: state.api); if phase == .active { draft.resume(api: state.api) } }
        .onChange(of: phase) { _, phase in if phase == .background { draft.pause() } else if phase == .active { draft.resume(api: state.api) } }
        .onChange(of: state.connectionRevision) { _, _ in draft.pause() }
        .onChange(of: photos) { _, values in Task { await importPhotos(values) } }
        .fileImporter(isPresented: $importing, allowedContentTypes: [.data], allowsMultipleSelection: true) { result in
            Task {
                staging = true; defer { staging = false }
                do {
                    let urls = try result.get()
                    guard urls.count + draft.items.count <= maxCount else { throw APIClient.APIError.message("选择附件数量超过本次限制") }
                    for url in urls {
                        let access = url.startAccessingSecurityScopedResource(); defer { if access { url.stopAccessingSecurityScopedResource() } }
                        try checkSize(url)
                        let mime = UTType(filenameExtension: url.pathExtension)?.preferredMIMEType ?? "application/octet-stream"
                        try checkMIME(mime)
                        await draft.enqueue(file: url, name: url.lastPathComponent, kind: "file", mime: mime, api: state.api)
                    }
                } catch { draft.error = error.localizedDescription }
            }
        }
    }
    private func checkMIME(_ mime: String) throws {
        guard acceptedMIMEs.isEmpty || acceptedMIMEs.contains(where: { $0 == mime || ($0.hasSuffix("/*") && mime.hasPrefix(String($0.dropLast()))) }) else { throw APIClient.APIError.message("附件类型不在本次请求允许范围内") }
    }
    private func checkSize(_ url: URL) throws {
        if let maximumBytes, ((try url.resourceValues(forKeys: [.fileSizeKey])).fileSize ?? 0) > maximumBytes { throw APIClient.APIError.message("附件超过本次请求允许的大小") }
    }
    private func importPhotos(_ values: [PhotosPickerItem]) async {
        guard !values.isEmpty else { return }
        staging = true; defer { staging = false; photos = [] }
        do {
            guard values.count + draft.items.count <= maxCount else { throw APIClient.APIError.message("选择附件数量超过本次限制") }
            for item in values {
                guard let selected = try await item.loadTransferable(type: PickedMediaFile.self) else { throw APIClient.APIError.message("所选照片或视频无法读取") }
                defer { try? FileManager.default.removeItem(at: selected.url.deletingLastPathComponent()) }
                let video = item.supportedContentTypes.contains { $0.conforms(to: .movie) }
                guard !video || allowsVideos else { throw APIClient.APIError.message("本次请求只接受照片") }
                try checkSize(selected.url)
                if video {
                    try checkMIME(UTType(filenameExtension: selected.url.pathExtension)?.preferredMIMEType ?? "video/quicktime")
                    await draft.enqueue(file: selected.url, name: "视频." + selected.url.pathExtension, kind: "video", mime: UTType(filenameExtension: selected.url.pathExtension)?.preferredMIMEType ?? "video/quicktime", api: state.api)
                } else {
                    let mime = UTType(filenameExtension: selected.url.pathExtension)?.preferredMIMEType ?? ""
                    if ["image/jpeg", "image/png", "image/webp", "image/gif"].contains(mime) {
                        try checkMIME(mime)
                        await draft.enqueue(file: selected.url, name: "照片." + selected.url.pathExtension, kind: "image", mime: mime, api: state.api)
                    } else {
                        let jpeg = try PhotoPreparation.jpeg(file: selected.url, maximumBytes: min(draft.limits.image_max_bytes, maximumBytes ?? draft.limits.image_max_bytes))
                        try checkMIME("image/jpeg")
                        if let maximumBytes, jpeg.count > maximumBytes { throw APIClient.APIError.message("照片超过本次请求允许的大小") }
                        await draft.enqueue(data: jpeg, name: "照片.jpg", kind: "image", mime: "image/jpeg", api: state.api)
                    }
                }
            }
        } catch { draft.error = error.localizedDescription }
    }
}
