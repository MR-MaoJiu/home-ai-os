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
        try Task.checkCancellation()
        let size = (try source.resourceValues(forKeys: [.fileSizeKey])).fileSize ?? 0
        guard size <= 1024 * MediaUploadStore.chunkSize else { throw APIClient.APIError.message("资源超过本客户端的 1 GiB 选择限制") }
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent("HomeAIMediaImport", isDirectory: true).appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true, attributes: [.protectionKey: FileProtectionType.complete])
        let target = folder.appendingPathComponent("selected." + (source.pathExtension.isEmpty ? "data" : source.pathExtension))
        do {
            try FileManager.default.copyItem(at: source, to: target)
            try FileManager.default.setAttributes([.protectionKey: FileProtectionType.complete], ofItemAtPath: target.path)
            try Task.checkCancellation()
            return PickedMediaFile(url: target)
        } catch { try? FileManager.default.removeItem(at: folder); throw error }
    }
}

struct MediaPickerRequest: Identifiable, Equatable {
    enum Kind: Equatable, Sendable { case photos, videos, files }
    let id = UUID()
    let kind: Kind
    init(_ kind: Kind) { self.kind = kind }
}

/// 选择器返回后仍须属于同一连接与同一次选择，不能按届时的新身份归档。
struct MediaSelectionContext: Equatable {
    let id: UUID
    let revision: UUID
    let namespace: String
    func matches(revision: UUID, namespace: String?, selectionID: UUID?) -> Bool {
        self.revision == revision && self.namespace == namespace && id == selectionID
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
    var compact = false
    var pickerRequest: Binding<MediaPickerRequest?> = .constant(nil)
    var expectedNamespace: String? = nil
    var disabled = false
    var onBusyChanged: (Bool) -> Void = { _ in }
    @State private var photos: [PhotosPickerItem] = []
    @State private var photosPresented = false
    @State private var photoFilter: PHPickerFilter = .any(of: [.images, .videos])
    @State private var importing = false
    @State private var staging = false
    @State private var restoring = false
    @State private var preparing = false
    @State private var removing: Set<String> = []
    @State private var selection: MediaSelectionContext?
    @State private var importOperation: Task<Void, Never>?
    @State private var prepareOperation: Task<Void, Never>?
    @State private var preparingID: UUID?
    @State private var restoreID: UUID?
    @State private var visibleNamespace: String?
    @State private var visibleRevision: UUID?
    @State private var localError: String?
    private var busy: Bool { restoring || preparing || staging || photosPresented || importing || !photos.isEmpty || !removing.isEmpty }
    private var belongsToCurrentIdentity: Bool {
        visibleRevision == state.connectionRevision && visibleNamespace != nil && visibleNamespace == draft.namespace &&
            (expectedNamespace == nil || expectedNamespace == draft.namespace)
    }
    private var items: [QueuedMedia] { belongsToCurrentIdentity ? draft.items : [] }
    private var canPick: Bool { !disabled && !busy && state.connected && phase == .active && belongsToCurrentIdentity && items.count < maxCount }
    private var shownError: String? { localError ?? (belongsToCurrentIdentity && (!compact || !items.isEmpty) ? draft.error : nil) }

    var body: some View {
        let pickerContext = selection
        VStack(alignment: .leading, spacing: compact ? 6 : 8) {
            if !compact { standardControls }
            if !compact, let notice = draft.notice, belongsToCurrentIdentity { Text(notice).font(.caption).foregroundStyle(.secondary) }
            if let error = shownError { Text(verbatim: error).font(.caption).foregroundStyle(.red).fixedSize(horizontal: false, vertical: true) }
            if compact {
                compactQueue
                if staging || preparing { Label("正在准备附件…", systemImage: "hourglass").font(.caption).foregroundStyle(.secondary) }
            } else {
                ForEach(items) { item in standardItem(item) }
            }
        }
        // 宿主始终在视图树中，菜单收起不会停止草稿恢复或后台暂停处理。
        .task(id: state.connectionRevision) { await restore() }
        .onChange(of: busy) { _, value in onBusyChanged(value || !belongsToCurrentIdentity) }
        .onChange(of: expectedNamespace) { _, _ in
            cancelSelection(); visibleNamespace = nil; visibleRevision = nil
            Task { await restore() }
        }
        .onChange(of: state.connectionRevision) { _, _ in
            cancelSelection(); draft.pause(); visibleNamespace = nil; visibleRevision = nil
            localError = nil; draft.error = nil; draft.notice = nil
        }
        .onChange(of: phase) { _, value in
            if value == .background { cancelSelection(); draft.pause(); onBusyChanged(true) }
            else if value == .active { Task { await restore() } }
        }
        .onChange(of: disabled) { _, value in if value { cancelSelection() } }
        .onChange(of: pickerRequest.wrappedValue) { _, request in
            guard let request else { return }
            requestPicker(request.kind)
            pickerRequest.wrappedValue = nil
        }
        .onChange(of: photos) { _, values in
            guard !values.isEmpty, let selection = pickerContext else { return }
            staging = true; onBusyChanged(true)
            importOperation?.cancel()
            importOperation = Task { await importPhotos(values, context: selection) }
        }
        .photosPicker(isPresented: $photosPresented, selection: $photos, maxSelectionCount: max(1, maxCount - items.count), matching: photoFilter)
        .fileImporter(isPresented: $importing, allowedContentTypes: [.data], allowsMultipleSelection: true) { result in
            guard let selection = pickerContext else { return }
            staging = true; onBusyChanged(true)
            importOperation?.cancel()
            importOperation = Task { await importFiles(result, context: selection) }
        }
        .onDisappear {
            if !photosPresented && !importing { cancelSelection(); draft.pause() }
            onBusyChanged(true)
        }
    }

    private var standardControls: some View {
        HStack {
            if allowsPhotos || allowsVideos {
                Button(allowsVideos ? "照片/视频" : "选择照片", systemImage: "photo.on.rectangle") {
                    requestPicker(allowsPhotos ? .photos : .videos, combinedLibrary: allowsPhotos && allowsVideos)
                }.disabled(!canPick)
            }
            if allowsFiles { Button("文件", systemImage: "paperclip") { requestPicker(.files) }.disabled(!canPick) }
            if items.contains(where: { $0.asset == nil }) && !draft.uploading {
                Button("继续上传") { resume() }.disabled(disabled || busy)
            }
            if staging || preparing || restoring { ProgressView() }
        }.font(.caption)
    }
    @ViewBuilder private var compactQueue: some View {
        if !items.isEmpty {
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 8) {
                    ForEach(items) { item in
                        MediaQueueChip(item: item, removing: removing.contains(item.id), disabled: disabled || busy,
                                       onRemove: { remove(item) }, onRetry: resume)
                    }
                }.padding(.vertical, 2)
            }
        }
    }
    private func standardItem(_ item: QueuedMedia) -> some View {
        HStack {
            Image(systemName: item.kind == "image" ? "photo" : item.kind == "video" ? "video" : "doc")
            VStack(alignment: .leading, spacing: 3) {
                Text(item.name).font(.caption).lineLimit(1)
                if item.asset == nil { ProgressView(value: item.progress) }
                Text(item.error ?? MediaQueueChip.status(item)).font(.caption2).foregroundStyle(item.error == nil ? Color.secondary : .red)
            }
            Button("移出草稿", systemImage: "xmark.circle") { remove(item) }
                .labelStyle(.iconOnly).frame(minWidth: 44, minHeight: 44)
                .disabled(disabled || busy)
        }
    }
    private func restore() async {
        guard phase == .active, !staging, !preparing, !photosPresented, !importing, photos.isEmpty else { return }
        let id = UUID(); restoreID = id
        let revision = state.connectionRevision
        restoring = true; onBusyChanged(true)
        defer { if restoreID == id { restoring = false; onBusyChanged(busy || !belongsToCurrentIdentity) } }
        let restored = await draft.restore(api: state.api)
        let namespace = await state.api.cachedNamespace()
        guard restored, !Task.isCancelled, restoreID == id, revision == state.connectionRevision, phase == .active,
              namespace != nil, namespace == draft.namespace, expectedNamespace == nil || expectedNamespace == namespace else { return }
        visibleNamespace = namespace; visibleRevision = revision
        if !disabled { draft.resume(api: state.api) }
    }
    private func requestPicker(_ kind: MediaPickerRequest.Kind, combinedLibrary: Bool = false) {
        guard canPick else { return }
        guard (kind != .files || allowsFiles), (kind != .photos || allowsPhotos), (kind != .videos || allowsVideos) else { return }
        preparing = true; localError = nil; onBusyChanged(true)
        let revision = state.connectionRevision
        let id = UUID(); preparingID = id
        prepareOperation?.cancel()
        prepareOperation = Task {
            defer { if preparingID == id { preparingID = nil; preparing = false; onBusyChanged(busy || !belongsToCurrentIdentity) } }
            do {
                let namespace = try await state.api.syncNamespace()
                guard !Task.isCancelled, preparingID == id, revision == state.connectionRevision, namespace == draft.namespace,
                      expectedNamespace == nil || expectedNamespace == namespace, phase == .active, !disabled else { return }
                selection = MediaSelectionContext(id: id, revision: revision, namespace: namespace)
                photos = []
                if kind == .files { importing = true }
                else {
                    photoFilter = combinedLibrary ? .any(of: [.images, .videos]) : kind == .videos ? .videos : .images
                    photosPresented = true
                }
            } catch { if !Task.isCancelled && revision == state.connectionRevision { localError = error.localizedDescription } }
        }
    }
    private func cancelSelection() {
        prepareOperation?.cancel(); prepareOperation = nil; preparingID = nil
        importOperation?.cancel(); importOperation = nil
        selection = nil; photosPresented = false; importing = false; photos = []
        preparing = false; staging = false; pickerRequest.wrappedValue = nil
        onBusyChanged(busy || !belongsToCurrentIdentity)
    }
    private func isCurrent(_ context: MediaSelectionContext) -> Bool {
        !Task.isCancelled && !disabled && phase == .active &&
            context.matches(revision: state.connectionRevision, namespace: draft.namespace, selectionID: selection?.id) &&
            (expectedNamespace == nil || expectedNamespace == context.namespace)
    }
    private func valid(_ context: MediaSelectionContext) async -> Bool {
        guard isCurrent(context) else { return false }
        let actual = await state.api.cachedNamespace()
        return isCurrent(context) && actual == context.namespace
    }
    private func finishSelection(_ context: MediaSelectionContext) {
        guard selection?.id == context.id else { return }
        selection = nil; photos = []; staging = false; importOperation = nil
        onBusyChanged(busy || !belongsToCurrentIdentity)
    }
    private func checkMIME(_ mime: String) throws {
        guard acceptedMIMEs.isEmpty || acceptedMIMEs.contains(where: { $0 == mime || ($0.hasSuffix("/*") && mime.hasPrefix(String($0.dropLast()))) }) else { throw APIClient.APIError.message("附件类型不在本次请求允许范围内") }
    }
    private func checkSize(_ url: URL) throws {
        if let maximumBytes, ((try url.resourceValues(forKeys: [.fileSizeKey])).fileSize ?? 0) > maximumBytes { throw APIClient.APIError.message("附件超过本次请求允许的大小") }
    }
    private func enqueue(file: URL? = nil, data: Data? = nil, name: String, kind: String, mime: String, context: MediaSelectionContext) async throws {
        guard await valid(context), isCurrent(context) else { throw CancellationError() }
        let item: QueuedMedia
        if let file {
            item = try await MediaUploadStore.shared.enqueue(file: file, name: name, kind: kind, mime: mime, namespace: context.namespace, scope: draft.scope, maximumBytes: draft.limits.maximum(kind))
        } else if let data {
            item = try await MediaUploadStore.shared.enqueue(data: data, name: name, kind: kind, mime: mime, namespace: context.namespace, scope: draft.scope, maximumBytes: draft.limits.maximum(kind))
        } else { return }
        // 迟到结果只留原身份的加密队列，不能加入新身份，也不删除用户选择的文件。
        guard await valid(context), isCurrent(context) else { throw CancellationError() }
        if !draft.items.contains(where: { $0.id == item.id }) { draft.items.append(item) }
        draft.error = nil; draft.resume(api: state.api)
    }
    private func importFiles(_ result: Result<[URL], Error>, context: MediaSelectionContext) async {
        defer { finishSelection(context) }
        do {
            guard await valid(context), isCurrent(context) else { return }
            let urls = try result.get()
            guard urls.count + draft.items.count <= maxCount else { throw APIClient.APIError.message("选择附件数量超过本次限制") }
            for url in urls {
                guard await valid(context), isCurrent(context) else { return }
                let access = url.startAccessingSecurityScopedResource(); defer { if access { url.stopAccessingSecurityScopedResource() } }
                try checkSize(url)
                let mime = UTType(filenameExtension: url.pathExtension)?.preferredMIMEType ?? "application/octet-stream"
                try checkMIME(mime)
                try await enqueue(file: url, name: url.lastPathComponent, kind: "file", mime: mime, context: context)
            }
        } catch { if !(error is CancellationError), await valid(context) { localError = error.localizedDescription } }
    }
    private func importPhotos(_ values: [PhotosPickerItem], context: MediaSelectionContext) async {
        defer { finishSelection(context) }
        do {
            guard await valid(context), isCurrent(context) else { return }
            guard values.count + draft.items.count <= maxCount else { throw APIClient.APIError.message("选择附件数量超过本次限制") }
            for item in values {
                guard await valid(context), isCurrent(context) else { return }
                guard let selected = try await item.loadTransferable(type: PickedMediaFile.self) else { throw APIClient.APIError.message("所选照片或视频无法读取") }
                defer { try? FileManager.default.removeItem(at: selected.url.deletingLastPathComponent()) }
                guard await valid(context), isCurrent(context) else { return }
                let video = item.supportedContentTypes.contains { $0.conforms(to: .movie) }
                guard !video || allowsVideos else { throw APIClient.APIError.message("本次请求只接受照片") }
                try checkSize(selected.url)
                if video {
                    let mime = UTType(filenameExtension: selected.url.pathExtension)?.preferredMIMEType ?? "video/quicktime"
                    try checkMIME(mime)
                    try await enqueue(file: selected.url, name: "视频." + selected.url.pathExtension, kind: "video", mime: mime, context: context)
                } else {
                    let mime = UTType(filenameExtension: selected.url.pathExtension)?.preferredMIMEType ?? ""
                    if ["image/jpeg", "image/png", "image/webp", "image/gif"].contains(mime) {
                        try checkMIME(mime)
                        try await enqueue(file: selected.url, name: "照片." + selected.url.pathExtension, kind: "image", mime: mime, context: context)
                    } else {
                        let jpeg = try PhotoPreparation.jpeg(file: selected.url, maximumBytes: min(draft.limits.image_max_bytes, maximumBytes ?? draft.limits.image_max_bytes))
                        try checkMIME("image/jpeg")
                        if let maximumBytes, jpeg.count > maximumBytes { throw APIClient.APIError.message("照片超过本次请求允许的大小") }
                        try await enqueue(data: jpeg, name: "照片.jpg", kind: "image", mime: "image/jpeg", context: context)
                    }
                }
            }
        } catch { if !(error is CancellationError), await valid(context) { localError = error.localizedDescription } }
    }
    private func resume() {
        guard !disabled, !busy, belongsToCurrentIdentity, phase == .active else { return }
        localError = nil; draft.resume(api: state.api)
    }
    private func remove(_ item: QueuedMedia) {
        guard !disabled, !busy, belongsToCurrentIdentity, let namespace = visibleNamespace else { return }
        let revision = state.connectionRevision
        removing.insert(item.id); onBusyChanged(true); draft.pause()
        Task {
            defer { removing.remove(item.id); onBusyChanged(busy || !belongsToCurrentIdentity) }
            // 暂停的上传退出后才移除；每个挂起点之后都重新检查身份。
            while draft.uploading && draft.namespace == namespace && revision == state.connectionRevision {
                do { try await Task.sleep(for: .milliseconds(20)) } catch { return }
            }
            guard await state.api.cachedNamespace() == namespace, revision == state.connectionRevision, draft.namespace == namespace else { return }
            if let serverID = item.serverID, item.asset == nil, state.serverReachable {
                _ = try? await state.api.request("DELETE", "/api/v1/uploads/" + serverID, expectedNamespace: namespace)
            }
            guard await state.api.cachedNamespace() == namespace, revision == state.connectionRevision, draft.namespace == namespace else { return }
            try? await MediaUploadStore.shared.remove(id: item.id, namespace: namespace)
            guard revision == state.connectionRevision, draft.namespace == namespace else { return }
            draft.items.removeAll { $0.id == item.id }
            if phase == .active && !disabled { draft.resume(api: state.api) }
        }
    }
}

struct MediaQueueChip: View {
    let item: QueuedMedia
    let removing: Bool
    let disabled: Bool
    let onRemove: () -> Void
    let onRetry: () -> Void
    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            Image(systemName: item.kind == "image" ? "photo" : item.kind == "video" ? "video" : "doc.text")
                .font(.title3).foregroundStyle(.secondary).frame(width: 28, height: 32)
            VStack(alignment: .leading, spacing: 4) {
                Text(item.name).font(.caption.weight(.semibold)).lineLimit(1)
                if item.asset == nil { ProgressView(value: item.progress).tint(.teal) }
                Text(item.error ?? Self.status(item)).font(.caption2).foregroundStyle(item.error == nil ? Color.secondary : .red).lineLimit(2)
                if item.asset == nil && ["failed", "paused", "expired", "canceled"].contains(item.status) {
                    Button("继续上传", action: onRetry).font(.caption2).disabled(disabled || removing)
                }
            }.frame(maxWidth: .infinity, alignment: .leading)
            Button(action: onRemove) {
                if removing { ProgressView().controlSize(.small) }
                else { Image(systemName: "xmark").font(.caption.weight(.semibold)).foregroundStyle(.secondary) }
            }.frame(width: 44, height: 44).contentShape(Rectangle())
                .buttonStyle(.plain).disabled(disabled || removing)
                .accessibilityLabel("移除附件：" + item.name)
        }.padding(.leading, 10).padding(.vertical, 8)
            .frame(width: 228, alignment: .leading)
            .background(Color(uiColor: .secondarySystemBackground), in: RoundedRectangle(cornerRadius: 12))
            .overlay { RoundedRectangle(cornerRadius: 12).strokeBorder(.secondary.opacity(0.15), lineWidth: 1).allowsHitTesting(false) }
    }
    static func status(_ item: QueuedMedia) -> String {
        if let asset = item.asset { return asset.processing?.status == "failed" ? "已上传 · 处理失败" : "已上传" }
        switch item.status {
        case "failed": return "上传未完成"
        case "paused": return "已暂停"
        case "expired": return "上传已过期"
        case "canceled": return "上传已取消"
        case "queued": return "等待上传"
        default: return "上传中 \(Int(item.progress * 100))%"
        }
    }
}
