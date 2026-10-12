import SwiftUI

/// 草稿仅保存引用元信息；正文和实际权限始终由家庭服务器处理。
struct ChatDataReference: Codable, Identifiable, Equatable, Sendable {
    let record_id: String
    let version: Int
    let title: String
    let kind: String
    var id: String { record_id }
    var source: RecordReference { RecordReference(id: record_id, title: title, version: version) }
    var wireValue: [String: Any] { ["record_id": record_id, "version": version] }
    init(record: DataEntry) {
        record_id = record.id; version = record.version ?? 0; title = String(record.title.prefix(200)); kind = record.kind
    }
    init(record_id: String, version: Int, title: String, kind: String) {
        self.record_id = record_id; self.version = version; self.title = title; self.kind = kind
    }
}

struct ChatDataPickerRequest: Identifiable {
    let id = UUID()
    let namespace: String
    let revision: UUID
}

enum ChatDataReferenceRules {
    static let maximum = 20
    static func selectable(_ record: DataEntry) -> Bool {
        record.isBusinessData && record.sensitivity != "SECRET" && (record.version ?? 0) > 0
    }
    static func icon(_ kind: String) -> String {
        if kind.hasPrefix("photo.") { return "photo" }
        if kind.hasPrefix("video.") { return "video" }
        if kind.hasPrefix("health.") { return "heart.text.clipboard" }
        if kind.hasPrefix("location.") { return "location" }
        if kind.hasPrefix("calendar.") { return "calendar" }
        return "doc.text"
    }
}

struct ChatDataReferencePicker: View {
    @Environment(AppState.self) private var state
    @Environment(\.dismiss) private var dismiss
    @Binding var selected: [ChatDataReference]
    let request: ChatDataPickerRequest
    let attachmentCount: Int
    @State private var query = ""
    @State private var scope = "all"
    @State private var ownerID = ""
    @State private var ready = false
    @State private var loading = false
    @State private var error: String?
    private var capacity: Int { max(0, ChatDataReferenceRules.maximum - attachmentCount) }
    private var records: [DataEntry] {
        guard ready else { return [] }
        let text = query.trimmingCharacters(in: .whitespacesAndNewlines)
        return state.records.filter { record in
            ChatDataReferenceRules.selectable(record) &&
            (scope == "all" || (scope == "mine" ? record.owner_id == ownerID : record.isFamily)) &&
            (text.isEmpty || record.title.localizedCaseInsensitiveContains(text) || record.kind.localizedCaseInsensitiveContains(text))
        }.sorted { $0.title.localizedStandardCompare($1.title) == .orderedAscending }
    }
    var body: some View {
        NavigationStack {
            List {
                Section {
                    Text("引用已保存的数据供本次对话使用，服务器会重新检查权限和版本。").font(.caption).foregroundStyle(.secondary)
                    Picker("范围", selection: $scope) {
                        Text("全部").tag("all"); Text("我的").tag("mine"); Text("家庭共享").tag("family")
                    }.pickerStyle(.segmented)
                    if let error { Text(error).font(.caption).foregroundStyle(.red) }
                    if !state.syncStatus.isEmpty { Text(state.syncStatus).font(.caption).foregroundStyle(.secondary) }
                    if selected.count >= capacity { Text("附件和数据引用合计最多 \(ChatDataReferenceRules.maximum) 项。").font(.caption).foregroundStyle(.secondary) }
                }
                Section("可引用的数据") {
                    if loading && records.isEmpty { ProgressView("正在读取数据…") }
                    if !loading && ready && records.isEmpty {
                        ContentUnavailableView(query.isEmpty ? "暂无可引用数据" : "未找到匹配数据", systemImage: "doc.text.magnifyingglass",
                            description: Text(query.isEmpty ? "在数据页上传资料，或等待已授权数据同步。" : "尝试其他名称或数据类型。"))
                    }
                    ForEach(records) { record in
                        let chosen = selected.contains { $0.record_id == record.id }
                        Button {
                            if chosen { selected.removeAll { $0.record_id == record.id } }
                            else if selected.count < capacity { selected.append(ChatDataReference(record: record)) }
                        } label: {
                            HStack(spacing: 12) {
                                Image(systemName: ChatDataReferenceRules.icon(record.kind)).foregroundStyle(.teal)
                                VStack(alignment: .leading, spacing: 4) {
                                    Text(verbatim: record.title).foregroundStyle(.primary).lineLimit(2)
                                    Text((record.isFamily ? "家庭共享" : "个人") + " · " + record.kind).font(.caption).foregroundStyle(.secondary)
                                }
                                Spacer(minLength: 4)
                                Image(systemName: chosen ? "checkmark.circle.fill" : "circle").foregroundStyle(chosen ? Color.teal : Color.secondary)
                            }.contentShape(Rectangle())
                        }.buttonStyle(.plain).disabled(!chosen && selected.count >= capacity)
                            .accessibilityLabel((chosen ? "取消引用：" : "引用：") + record.title)
                    }
                }
            }.navigationTitle("引用数据")
                .searchable(text: $query, prompt: "搜索名称或数据类型")
                .toolbar { ToolbarItem(placement: .confirmationAction) { Button("完成") { dismiss() } } }
                .task { await load() }
                .refreshable { await load(force: true) }
                .onChange(of: state.connectionRevision) { _, _ in dismiss() }
                .onChange(of: state.dataEventRevision) { _, _ in Task { await load(force: true) } }
        }
    }
    private func load(force: Bool = false) async {
        guard !loading else { return }
        guard state.connectionRevision == request.revision else { dismiss(); return }
        loading = true; defer { loading = false }
        do {
            guard await state.api.cachedNamespace() == request.namespace else { dismiss(); return }
            ownerID = try await state.api.ownerIdentity(expectedNamespace: request.namespace).userID
            guard !Task.isCancelled, state.connectionRevision == request.revision else { return }
            ready = true
            try await state.loadData(force: force)
            guard !Task.isCancelled, state.connectionRevision == request.revision,
                  await state.api.cachedNamespace() == request.namespace else { ready = false; dismiss(); return }
            error = nil
        } catch {
            guard !Task.isCancelled, state.connectionRevision == request.revision else { return }
            self.error = error.localizedDescription
        }
    }
}
