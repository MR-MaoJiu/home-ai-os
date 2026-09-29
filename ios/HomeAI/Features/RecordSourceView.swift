import SwiftUI

struct RecordSourceView: View {
    @Environment(AppState.self) private var state
    let source: RecordReference
    @State private var record: DataEntry?
    @State private var isOwner = false
    @State private var error: String?
    var body: some View {
        Group {
            if let record {
                MemberRecordDetail(record: record, isOwner: isOwner, taskID: source.taskID)
            } else if let error { ContentUnavailableView("资料不可用", systemImage: "lock.doc", description: Text(error)) }
            else { ProgressView("正在验证资料权限…") }
        }.navigationTitle("回答来源")
            .task(id: state.connectionRevision) {
                do {
                    let namespace = try await state.api.syncNamespace()
                    let data = try await state.api.request("GET", ResourceRoutes.record(source.id, taskID: source.taskID), expectedNamespace: namespace)
                    let entry = try JSONDecoder().decode(DataEntry.self, from: data)
                    guard !Task.isCancelled, await state.api.cachedNamespace() == namespace else { return }
                    isOwner = try await state.api.ownerIdentity(expectedNamespace: namespace).userID == entry.owner_id
                    record = entry
                } catch { record = nil; self.error = error.localizedDescription }
            }
    }
}

import QuickLook
import UniformTypeIdentifiers
import ImageIO

struct RecordFieldNode: Identifiable {
    let id: String
    let name: String
    let text: String?
    let isAttachment: Bool
    let children: [RecordFieldNode]?

    init(name: String, value: JSONValue, path: String) {
        self.id = path; self.name = name
        if name.hasSuffix("_base64"), case .string = value {
            isAttachment = true; text = nil; children = nil; return
        }
        isAttachment = false
        switch value {
        case .object(let fields):
            text = fields.isEmpty ? "空对象" : nil
            children = fields.isEmpty ? nil : fields.keys.sorted().map { RecordFieldNode(name: $0, value: fields[$0]!, path: path + "/" + $0.replacingOccurrences(of: "~", with: "~0").replacingOccurrences(of: "/", with: "~1")) }
        case .array(let values):
            text = values.isEmpty ? "空列表" : nil
            children = values.isEmpty ? nil : values.enumerated().map { RecordFieldNode(name: "第 \($0.offset + 1) 项", value: $0.element, path: path + "/" + String($0.offset)) }
        case .null: text = "空值"; children = nil
        case .bool(let value): text = value ? "是" : "否"; children = nil
        default: text = value.description; children = nil
        }
    }
}

struct RecordPayloadView: View {
    let payload: [String: JSONValue]
    var excludedKeys: Set<String> = []
    var onAttachment: (String) -> Void = { _ in }
    var onText: (String, String) -> Void = { _, _ in }
    private var fields: [RecordFieldNode] {
        payload.keys.filter { !excludedKeys.contains($0) }.sorted().map { RecordFieldNode(name: $0, value: payload[$0]!, path: "/" + $0.replacingOccurrences(of: "~", with: "~0").replacingOccurrences(of: "/", with: "~1")) }
    }
    var body: some View {
        OutlineGroup(fields, children: \.children) { field in
            VStack(alignment: .leading, spacing: 6) {
                Text(field.name).font(.caption).foregroundStyle(.secondary)
                if field.isAttachment { Button("打开附件", systemImage: "paperclip") { onAttachment(field.id) } }
                if let text = field.text {
                    if text.count > 4000 { Button("阅读全文（\(text.count) 字）") { onText(field.name, text) } }
                    else { Text(text.isEmpty ? "空文本" : text).textSelection(.enabled).fixedSize(horizontal: false, vertical: true) }
                }
            }
        }
    }
}

enum RecordPreviewFormat: Equatable { case text, quickLook, unsupported }

enum RecordAttachment {
    static let maximumBytes = 20 * 1024 * 1024
    static func encodedValue(path: String, payload: [String: JSONValue]) -> String? {
        guard path.hasPrefix("/") else { return nil }
        var current: JSONValue = .object(payload)
        for component in path.dropFirst().components(separatedBy: "/") {
            let key = component.replacingOccurrences(of: "~1", with: "/").replacingOccurrences(of: "~0", with: "~")
            switch current {
            case .object(let fields): guard let next = fields[key] else { return nil }; current = next
            case .array(let values): guard let index = Int(key), values.indices.contains(index) else { return nil }; current = values[index]
            default: return nil
            }
        }
        guard case .string(let encoded) = current else { return nil }
        return encoded
    }
    static func format(name: String, mime: String? = nil, data: Data) -> RecordPreviewFormat {
        let ext = (name as NSString).pathExtension.lowercased()
        let mime = mime?.lowercased() ?? ""
        let plainExtensions: Set<String> = ["txt", "md", "markdown", "json", "jsonl", "csv", "tsv", "log", "yaml", "yml", "toml", "xml", "html", "htm", "svg", "css", "js", "ts", "py", "sh", "sql", "ini", "conf"]
        // 先按可解码文本判断，覆盖 BOM、注释前缀和伪装成 Office 文件的 HTML。
        if ["html", "htm", "svg"].contains(ext) || mime == "image/svg+xml" { return .text }
        if data.starts(with: Data("%PDF-".utf8)) { return .quickLook }
        if let decoded = text(data) {
            let clean = decoded.trimmingCharacters(in: CharacterSet(charactersIn: "\u{FEFF}"))
            if clean.unicodeScalars.allSatisfy({ !CharacterSet.controlCharacters.contains($0) || [9, 10, 13].contains(Int($0.value)) }) { return .text }
        }
        if plainExtensions.contains(ext) || mime.hasPrefix("text/") || ["application/json", "application/xml"].contains(mime) { return .text }
        if let source = CGImageSourceCreateWithData(data as CFData, nil), CGImageSourceGetCount(source) > 0, let identifier = CGImageSourceGetType(source) as String?, !identifier.contains("svg"), !identifier.contains("pdf") { return .quickLook }
        let zip = data.starts(with: [0x50, 0x4b, 0x03, 0x04])
        let ole = data.starts(with: [0xd0, 0xcf, 0x11, 0xe0, 0xa1, 0xb1, 0x1a, 0xe1])
        if ["doc", "docx", "xls", "xlsx", "ppt", "pptx", "pages", "numbers", "key"].contains(ext) { return zip || ole ? .quickLook : .unsupported }
        if ["mp4", "m4a", "mov"].contains(ext), data.count >= 12, data.subdata(in: 4..<8) == Data("ftyp".utf8) { return .quickLook }
        if ext == "wav", data.starts(with: Data("RIFF".utf8)), data.count >= 12, data.subdata(in: 8..<12) == Data("WAVE".utf8) { return .quickLook }
        if ext == "mp3", data.starts(with: Data("ID3".utf8)) { return .quickLook }
        return .unsupported
    }

    static func text(_ data: Data) -> String? {
        if data.starts(with: [0xff, 0xfe, 0x00, 0x00]) { return String(data: data, encoding: .utf32LittleEndian) }
        if data.starts(with: [0x00, 0x00, 0xfe, 0xff]) { return String(data: data, encoding: .utf32BigEndian) }
        if data.starts(with: [0xff, 0xfe]) || data.starts(with: [0xfe, 0xff]) { return String(data: data, encoding: .utf16) }
        return String(data: data, encoding: .utf8)
    }

    static func image(_ data: Data) -> UIImage? {
        guard data.count <= maximumBytes, let source = CGImageSourceCreateWithData(data as CFData, nil),
              let image = CGImageSourceCreateThumbnailAtIndex(source, 0, [kCGImageSourceCreateThumbnailFromImageAlways: true, kCGImageSourceThumbnailMaxPixelSize: 2048, kCGImageSourceCreateThumbnailWithTransform: true] as CFDictionary) else { return nil }
        return UIImage(cgImage: image)
    }

    static func safeName(_ raw: String, data: Data) -> String {
        let last = (raw.replacingOccurrences(of: "\\", with: "/") as NSString).lastPathComponent
        let filtered = last.unicodeScalars.filter { !CharacterSet.controlCharacters.contains($0) && $0 != ":" }
        var name = String(String.UnicodeScalarView(filtered)).trimmingCharacters(in: .whitespacesAndNewlines)
        if name.isEmpty || name == "." || name == ".." { name = "附件" }
        let ext = (name as NSString).pathExtension
        let suffix = !ext.isEmpty && ext.utf8.count <= 20 ? "." + ext : ""
        let originalBase = suffix.isEmpty ? name : (name as NSString).deletingPathExtension
        var base = ""
        var bytes = 0
        for character in originalBase {
            let count = String(character).utf8.count
            guard bytes + count <= 180 else { break }
            base.append(character); bytes += count
        }
        name = (base.isEmpty ? "附件" : base) + suffix
        if suffix.isEmpty {
            if data.starts(with: Data("%PDF-".utf8)) { name += ".pdf" }
            else if let source = CGImageSourceCreateWithData(data as CFData, nil), let identifier = CGImageSourceGetType(source) as String?, let ext = UTType(identifier)?.preferredFilenameExtension { name += "." + ext }
        }
        return name
    }
}

final class ProtectedRecordPreview: NSObject, QLPreviewItem, Identifiable, @unchecked Sendable {
    let id = UUID()
    let url: URL
    let title: String
    let contents: Data
    let text: String?
    let format: RecordPreviewFormat
    private let directory: URL
    var previewItemURL: URL? { url }
    var previewItemTitle: String? { title }

    init(data: Data, name: String, mime: String? = nil, temporaryRoot: URL? = nil) throws {
        guard data.count <= RecordAttachment.maximumBytes else { throw APIClient.APIError.message("附件超过当前客户端的 20 MB 预览限制") }
        title = name; contents = data
        format = RecordAttachment.format(name: name, mime: mime, data: data)
        text = format == .text ? RecordAttachment.text(data) : nil
        let root = temporaryRoot ?? FileManager.default.temporaryDirectory.appendingPathComponent("HomeAIProtectedPreviews", isDirectory: true)
        directory = root.appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true, attributes: [.protectionKey: FileProtectionType.complete])
        var mutable = directory
        var values = URLResourceValues(); values.isExcludedFromBackup = true
        try mutable.setResourceValues(values)
        url = directory.appendingPathComponent(RecordAttachment.safeName(name, data: data))
        do { try data.write(to: url, options: [.atomic, .completeFileProtection]) }
        catch { try? FileManager.default.removeItem(at: directory); throw error }
        super.init()
    }

    static func removeAbandonedFiles() {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent("HomeAIProtectedPreviews", isDirectory: true)
        try? FileManager.default.removeItem(at: root)
    }
    func remove() { try? FileManager.default.removeItem(at: directory) }
    deinit { try? FileManager.default.removeItem(at: directory) }
}

struct RecordFilePreview: View {
    @Environment(\.dismiss) private var dismiss
    let item: ProtectedRecordPreview
    @State private var exporting = false
    @State private var exportError: String?
    var body: some View {
        NavigationStack {
            Group {
                if item.format == .text, let text = item.text { CompleteTextPreview(text: text) }
                else if item.format == .quickLook, QLPreviewController.canPreview(item) { NativeRecordPreview(item: item) }
                else {
                    ContentUnavailableView("暂不支持预览此格式", systemImage: "doc", description: Text("可以导出原文件，使用相应的应用打开。"))
                }
            }.navigationTitle(item.title).navigationBarTitleDisplayMode(.inline)
                .toolbar {
                    ToolbarItem(placement: .cancellationAction) { Button("关闭") { dismiss() } }
                    ToolbarItem(placement: .topBarTrailing) { Button("导出原文件", systemImage: "square.and.arrow.up") { exporting = true } }
                }
        }
        .fileExporter(isPresented: $exporting, document: RecordExportDocument(data: item.contents), contentType: UTType(filenameExtension: item.url.pathExtension) ?? .data, defaultFilename: item.url.lastPathComponent) { result in
            if case .failure(let error) = result { exportError = error.localizedDescription }
        }
        .alert("导出未完成", isPresented: Binding(get: { exportError != nil }, set: { if !$0 { exportError = nil } })) { Button("知道了") { exportError = nil } } message: { Text(exportError ?? "") }
        .onDisappear { item.remove() }
    }
}

struct RecordExportDocument: FileDocument {
    static var readableContentTypes: [UTType] { [.data] }
    let data: Data
    init(data: Data) { self.data = data }
    init(configuration: ReadConfiguration) throws { data = configuration.file.regularFileContents ?? Data() }
    func fileWrapper(configuration: WriteConfiguration) throws -> FileWrapper { FileWrapper(regularFileWithContents: data) }
}

struct RecordTextContent: Identifiable {
    let id = UUID()
    let title: String
    let text: String
}

struct RecordLongTextView: View {
    @Environment(\.dismiss) private var dismiss
    let content: RecordTextContent
    var body: some View {
        NavigationStack {
            CompleteTextPreview(text: content.text).navigationTitle(content.title).navigationBarTitleDisplayMode(.inline)
                .toolbar { ToolbarItem(placement: .cancellationAction) { Button("关闭") { dismiss() } } }
        }
    }
}

private struct CompleteTextPreview: UIViewRepresentable {
    let text: String
    func makeUIView(context: Context) -> UITextView {
        let view = UITextView()
        view.isEditable = false; view.isSelectable = true
        view.font = .preferredFont(forTextStyle: .body)
        view.adjustsFontForContentSizeCategory = true
        view.textContainerInset = UIEdgeInsets(top: 16, left: 12, bottom: 16, right: 12)
        view.dataDetectorTypes = []
        view.text = text
        return view
    }
    func updateUIView(_ uiView: UITextView, context: Context) { if uiView.text != text { uiView.text = text } }
}

private struct NativeRecordPreview: UIViewControllerRepresentable {
    let item: ProtectedRecordPreview
    func makeCoordinator() -> Coordinator { Coordinator(item: item) }
    func makeUIViewController(context: Context) -> QLPreviewController {
        let controller = QLPreviewController(); controller.dataSource = context.coordinator
        return controller
    }
    func updateUIViewController(_ uiViewController: QLPreviewController, context: Context) { }
    final class Coordinator: NSObject, QLPreviewControllerDataSource {
        let item: ProtectedRecordPreview
        init(item: ProtectedRecordPreview) { self.item = item }
        func numberOfPreviewItems(in controller: QLPreviewController) -> Int { 1 }
        func previewController(_ controller: QLPreviewController, previewItemAt index: Int) -> any QLPreviewItem { item }
    }
}
