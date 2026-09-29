import SwiftUI

struct ClientActionCard: View {
    @Environment(AppState.self) private var state
    @Environment(\.scenePhase) private var phase
    let action: ClientActionRequest
    @State private var current: ClientActionRequest
    @State private var draft: MediaDraft
    @State private var text = ""
    @State private var selection: Set<String> = []
    @State private var records: [DataEntry] = []
    @State private var showRecords = false
    @State private var busy = false
    @State private var error: String?
    @State private var operation: Task<Void, Never>?
    init(action: ClientActionRequest) {
        self.action = action
        _current = State(initialValue: action)
        _draft = State(initialValue: MediaDraft(scope: "action:" + action.id))
    }
    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack { Label(title, systemImage: "person.crop.circle.badge.questionmark").font(.headline); Spacer(); Text(current.statusLabel).font(.caption).foregroundStyle(.secondary) }
            Text(current.purpose)
            Text("来自：" + current.requested_by.name).font(.caption).foregroundStyle(.secondary)
            if current.kind == "member.notify" { Text(current.parameters["summary"]?.description ?? "") }
            if let error { Text(error).font(.caption).foregroundStyle(.red) }
            if busy { ProgressView("正在提交…") }
            if current.can_respond && current.pending {
                controls
                Button("本次不提供", role: .destructive) { run { try await ClientActionStore.deny(current, api: state.api) } }.disabled(busy)
            } else if current.can_revoke {
                Button("撤回本次任务授权", role: .destructive) { run { try await ClientActionStore.revoke(current, api: state.api) } }.disabled(busy)
            }
            if current.kind != "member.notify" { Text("只对本次请求提供资料，不改变个人或家庭的长期共享设置。").font(.caption2).foregroundStyle(.secondary) }
        }.padding().background(.quaternary, in: RoundedRectangle(cornerRadius: 14))
            .onChange(of: action.status) { _, _ in current = action }
            .onChange(of: phase) { _, value in if value == .background { operation?.cancel(); draft.pause() } }
            .onChange(of: state.connectionRevision) { _, _ in operation?.cancel(); records = []; selection = []; text = "" }
            .sheet(isPresented: $showRecords) { recordPicker }
    }
    private var title: String {
        ["cloud.disclose": "确认本次云端披露", "capture_location": "提供位置", "read_health": "提供健康数据", "choose_files": "提供文件", "choose_photos": "提供照片", "provide_text": "补充信息", "choose_option": "请选择", "data.share": "资料授权", "authorize_records": "资料授权", "member.notify": "成员消息"][current.kind] ?? "需要你的操作"
    }
    @ViewBuilder private var controls: some View {
        switch current.kind {
        case "cloud.disclose":
            if let provider = current.parameters["provider_id"]?.description { LabeledContent("云端服务", value: provider).font(.caption) }
            Text("允许后，服务器只按本次绑定的资料版本和范围出站；变更后需要重新确认。").font(.caption)
            if case .object(let versions) = current.parameters["record_versions"] {
                ForEach(versions.keys.sorted(), id: \.self) { identifier in
                    NavigationLink {
                        RecordSourceView(source: RecordReference(id: identifier, title: "本次披露资料", version: Int(versions[identifier]?.description ?? "")))
                    } label: { Label("查看资料 · 版本 " + (versions[identifier]?.description ?? ""), systemImage: "doc.text") }
                }
                if versions.isEmpty { Text("范围：本次对话输入与服务器列出的最小上下文。").font(.caption) }
            }
            Button("允许本次披露") { run { try await ClientActionStore.respond(current, api: state.api) } }.disabled(busy)
        case "provide_text":
            TextField(current.parameters["prompt"]?.description ?? "补充说明", text: $text, axis: .vertical).lineLimit(2...6).textFieldStyle(.roundedBorder)
            Button("提交补充") { run { try await ClientActionStore.respond(current, text: text, api: state.api) } }
                .disabled(busy || text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || text.count > current.integer("max_length", default: 4000))
        case "choose_option":
            if case .array(let values) = current.parameters["options"] {
                ForEach(Array(values.enumerated()), id: \.offset) { _, value in
                    if case .object(let option) = value, let identifier = option["id"]?.description, let label = option["label"]?.description {
                        Button(label) { run { try await ClientActionStore.respond(current, text: identifier, api: state.api) } }.disabled(busy)
                    }
                }
            }
        case "capture_location", "read_health":
            if current.kind == "capture_location" { Text(current.parameters["precision"]?.description == "precise" ? "本次请求精确位置，将由系统询问位置权限。" : "本次仅提供大致位置。定位可能需要少量时间。").font(.caption) }
            else {
                Text("指标：" + current.strings("types").map { ["sleep": "睡眠", "steps": "步数", "heart_rate": "心率", "body_mass": "体重"][$0] ?? $0 }.joined(separator: "、")).font(.caption)
                Text("读取范围：\(current.parameters["start_at"]?.description ?? "") 至 \(current.parameters["end_at"]?.description ?? "")").font(.caption)
            }
            Button("允许并提供") {
                run {
                    let ids = try await ClientActionStore.capture(current, api: state.api)
                    try await ClientActionStore.respond(current, recordIDs: ids, api: state.api)
                }
            }.disabled(busy)
            if error != nil {
                Button("重新采集") { run {
                    let namespace = try await state.api.syncNamespace()
                    await ClientViewCache.shared.remove(key: "client-action-capture:" + current.id, namespace: namespace)
                    await ClientViewCache.shared.remove(key: "client-action-response:" + current.id, namespace: namespace)
                    let ids = try await ClientActionStore.capture(current, api: state.api)
                    try await ClientActionStore.respond(current, recordIDs: ids, api: state.api)
                } }.disabled(busy)
            }
        case "choose_files", "choose_photos":
            MediaComposer(draft: draft, allowsFiles: current.kind == "choose_files", allowsPhotos: current.kind == "choose_photos", allowsVideos: false,
                          maxCount: current.integer("max_count", default: 5), maximumBytes: current.integer("max_bytes", default: 10 * 1024 * 1024), acceptedMIMEs: current.strings("accepted_mime_types"))
            Button("提供已选附件") { run {
                try await ClientActionStore.respond(current, recordIDs: draft.items.compactMap { $0.asset?.record_id }, api: state.api)
                await draft.acknowledge(ids: draft.items.map(\.id))
            } }.disabled(busy || !draft.ready)
            Button("选择已有资料") { Task { await loadRecords(); showRecords = true } }.disabled(busy)
        case "authorize_records", "data.share":
            Button("选择并检查我的资料") { Task { await loadRecords(); showRecords = true } }.disabled(busy)
        default:
            Text("当前客户端暂不支持此请求，请更新后重试。").font(.caption)
        }
    }
    private var recordPicker: some View {
        NavigationStack {
            List {
                Text("仅可选择自己的非秘密原始资料；不转授他人资料或聊天记忆。").font(.caption)
                if let error { Text(error).foregroundStyle(.red) }
                ForEach(records) { record in
                    HStack {
                        Button { if selection.contains(record.id) { selection.remove(record.id) } else if selection.count < current.integer("max_count", default: 20) { selection.insert(record.id) } } label: {
                            Image(systemName: selection.contains(record.id) ? "checkmark.circle.fill" : "circle")
                        }.buttonStyle(.plain)
                        NavigationLink { MemberRecordDetail(record: record, isOwner: true) } label: { Text(record.title) }
                    }
                }
            }.navigationTitle("本次提供的资料")
                .toolbar {
                    ToolbarItem(placement: .cancellationAction) { Button("取消") { showRecords = false } }
                    ToolbarItem(placement: .confirmationAction) { Button("提供") { showRecords = false; run { try await ClientActionStore.respond(current, recordIDs: Array(selection), api: state.api) } }.disabled(selection.isEmpty || busy) }
                }
        }
    }
    private func loadRecords() async {
        do {
            let namespace = try await state.api.syncNamespace()
            let owner = try await state.api.ownerIdentity(expectedNamespace: namespace).userID
            try await state.loadData(force: true)
            let allowed = Set(current.strings("record_ids"))
            records = state.records.filter { record in
                record.owner_id == owner && record.sensitivity != "SECRET" && !record.kind.hasPrefix("memory.") &&
                (allowed.isEmpty || allowed.contains(record.id)) &&
                (current.kind != "choose_photos" || ["photo.selected", "photo.file"].contains(record.kind)) &&
                (current.kind != "choose_files" || ["document.file", "document.import"].contains(record.kind))
            }
        } catch { self.error = error.localizedDescription }
    }
    private func run(_ work: @escaping @MainActor () async throws -> Void) {
        guard !busy else { return }; busy = true; error = nil
        operation = Task {
            defer { busy = false }
            do {
                try await work()
                current = try JSONDecoder().decode(ClientActionRequest.self, from: await state.api.request("GET", "/api/v1/client-actions/" + action.id))
                state.taskEventRevision = UUID()
            } catch { if !Task.isCancelled { self.error = error.localizedDescription } }
        }
    }
}

struct ClientActionInbox: View {
    @Environment(AppState.self) private var state
    let notificationID: String?
    @State private var store = ClientActionStore()
    var body: some View {
        ScrollView { LazyVStack(spacing: 12) {
            if let error = store.error { Text(error).foregroundStyle(.red) }
            ForEach(store.items) { ClientActionCard(action: $0) }
            if store.hasMore { Button("加载更早请求") { Task { await store.load(api: state.api, notificationID: notificationID, older: true) } }.disabled(store.loading) }
            if store.items.isEmpty && !store.loading { ContentUnavailableView("暂无成员请求", systemImage: "person.crop.circle.badge.questionmark") }
        }.padding() }.navigationTitle("成员请求")
            .task(id: state.connectionRevision) { await store.load(api: state.api, notificationID: notificationID) }
            .onChange(of: state.taskEventRevision) { _, _ in Task { await store.load(api: state.api, notificationID: notificationID) } }
            .refreshable { await store.load(api: state.api, notificationID: notificationID) }
    }
}
