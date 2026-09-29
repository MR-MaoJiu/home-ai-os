import SwiftUI
import PhotosUI

struct TaskProgressView: View {
    @Environment(AppState.self) private var state
    let identifier: String
    @State private var result: TaskResult?
    @State private var approval: ApprovalEntry?
    @State private var loading = false
    @State private var error: String?
    var body: some View {
        List {
            if let error { Text(error).foregroundStyle(.red) }
            if let result {
                LabeledContent("状态", value: taskStatusLabel(result.status))
                if let message = result.error { Text(message) }
                if let approval {
                    Section("确认本次操作") {
                        ForEach(approval.arguments.keys.sorted(), id: \.self) { key in Text("\(key)：\(approval.arguments[key]?.description ?? "")") }
                        Button("确认执行") { decide(approval.id, "APPROVED") }.disabled(state.busy)
                        Button("拒绝", role: .destructive) { decide(approval.id, "REJECTED") }.disabled(state.busy)
                    }
                }
                if let hits = webSearchHits(result.result) {
                    Section("公开搜索结果") {
                        if hits.isEmpty { Text("没有找到结果，可以修改搜索词后再试。") }
                        ForEach(hits) { hit in
                            VStack(alignment: .leading) {
                                Link(hit.title, destination: hit.url)
                                Text(hit.snippet).font(.caption)
                            }
                        }
                        Text("网页内容来自外部来源，需要自行核实。").font(.caption)
                    }
                } else if let value = ChatView.answer(result.result) { Text(value).textSelection(.enabled) }
                ForEach(ChatView.sources(result.result)) { source in
                    NavigationLink { RecordSourceView(source: source) } label: { Text(source.title) }
                }
                if ["RECEIVED", "APPROVED", "AWAITING_APPROVAL", "EXECUTING"].contains(result.status) {
                    Button("取消未完成步骤", role: .destructive) { Task { await state.perform {
                        _ = try await state.api.request("POST", "/api/v1/tasks/" + identifier + "/cancel")
                        await reload()
                    } } }
                }
            } else if error == nil { ProgressView("读取任务状态…") }
        }.navigationTitle("任务 " + identifier.prefix(8))
        .task(id: identifier) { await reload() }
        .onChange(of: state.taskEventRevision) { _, _ in Task { await reload() } }
        .refreshable { await reload() }
    }
    func decide(_ id: String, _ decision: String) {
        Task { await state.perform {
            _ = try await state.api.request("POST", "/api/v1/approvals/" + id, body: JSONSerialization.data(withJSONObject: ["decision": decision]))
            await reload()
        } }
    }
    func reload() async {
        guard !loading else { return }
        loading = true; defer { loading = false }
        do {
            let namespace = try await state.api.syncNamespace()
            let data = try await state.api.request("GET", "/api/v1/tasks/" + identifier, expectedNamespace: namespace)
            guard !Task.isCancelled else { return }
            result = try JSONDecoder().decode(TaskResult.self, from: data)
            if result?.status == "AWAITING_APPROVAL" {
                let pending = try await state.api.request("GET", "/api/v1/approvals", expectedNamespace: namespace)
                approval = try JSONDecoder().decode([ApprovalEntry].self, from: pending).first { $0.task_id == identifier }
            } else { approval = nil }
            error = nil
        } catch {
            guard !Task.isCancelled else { return }
            result = nil
            self.error = error.localizedDescription
        }
    }
}

struct DataView: View {
    @Environment(AppState.self) private var state
    @State private var uploads = MediaDraft(scope: "data")
    @State private var scope = "personal"
    @State private var currentUser = ""
    @State private var currentMemberName = ""
    var body: some View {
        List {
            if !state.syncStatus.isEmpty { Text(state.syncStatus).font(.caption).foregroundStyle(.secondary) }
            Section {
                if !currentMemberName.isEmpty { Text("当前成员：" + currentMemberName).font(.caption) }
                Text("个人数据仅自己可见，家庭数据在所有家庭成员的设备上展示。").font(.caption)
            }
            Picker("数据范围", selection: $scope) {
                Text("我的").tag("personal")
                Text("家庭").tag("family")
            }.pickerStyle(.segmented)
            Section("上传资料") { MediaComposer(draft: uploads) }
            Section("服务器数据") {
                if state.records.filter({ !$0.kind.hasPrefix("memory.") && ($0.isFamily || $0.owner_id == currentUser) && ($0.isFamily ? "family" : "personal") == scope }).isEmpty {
                    ContentUnavailableView(scope == "family" ? "暂无家庭数据" : "暂无个人数据", systemImage: "externaldrive", description: Text("导入图片或文件后，可选择仅自己或家庭可见。"))
                }
                ForEach(state.records.filter { !$0.kind.hasPrefix("memory.") && ($0.isFamily || $0.owner_id == currentUser) && ($0.isFamily ? "family" : "personal") == scope }) { record in
                    NavigationLink {
                        MemberRecordDetail(record: record, isOwner: record.owner_id == currentUser)
                    } label: {
                        Label { VStack(alignment: .leading) { Text(record.title); Text(record.isFamily ? "家庭 · " + record.kind : "个人 · " + record.kind).font(.caption).foregroundStyle(.secondary) } } icon: { Image(systemName: "doc.text") }
                    }
                    .swipeActions { if record.owner_id == currentUser { Button("删除", role: .destructive) { Task { await remove(record.id) } } } }
                }
            }
        }

        .navigationTitle("数据")
        .task(id: state.connectionRevision) {
            if state.connected {
                do {
                    let namespace = try await state.api.syncNamespace()
                    currentUser = try await state.api.ownerIdentity(expectedNamespace: namespace).userID
                    let people = try JSONDecoder().decode([FamilyMember].self, from: await state.api.request("GET", "/api/v1/members", expectedNamespace: namespace))
                    currentMemberName = people.first { $0.id == currentUser }?.name ?? ""
                } catch { currentUser = "" }
            }
            await reload()
        }.refreshable { await reload(force: true) }
    }
    func reload(force: Bool = false) async { guard state.connected else { return }; await state.perform { try await state.loadData(force: force) } }
    func remove(_ id: String) async { await state.perform { _ = try await state.api.request("DELETE", "/api/v1/data/" + id); try await state.loadData(force: true) } }
}

struct AutomationsView: View {
    @Environment(AppState.self) private var state
    @State private var scope = "personal"
    var body: some View {
        List {
            if !state.automationStatus.isEmpty { Text(state.automationStatus).font(.caption).foregroundStyle(.secondary) }
            Picker("范围", selection: $scope) {
                Text("我的").tag("personal")
                Text("家庭").tag("family")
            }.pickerStyle(.segmented)
            ForEach(state.automations.filter { ($0.isFamily ? "family" : "personal") == scope }) { item in
                NavigationLink { AutomationRunsView(item: item) } label: {
                    VStack(alignment: .leading, spacing: 6) {
                        HStack { Text(item.name); Spacer(); Text(item.enabled ? "已启用" : "已停用").foregroundStyle(.secondary) }
                        Text(item.triggerDescription).font(.caption).foregroundStyle(.secondary)
                    }
                }
            }
            if state.automations.filter({ ($0.isFamily ? "family" : "personal") == scope }).isEmpty {
                ContentUnavailableView(scope == "family" ? "暂无家庭自动化" : "暂无个人自动化", systemImage: "bolt", description: Text("在 AI 对话中说明执行内容、时间和个人或家庭范围。服务器会负责执行并通知相关设备。"))
            }
        }
        .navigationTitle("自动化")
        .toolbar { NavigationLink { NotificationInboxView() } label: { Label("通知", systemImage: "bell") } }
        .task(id: state.connectionRevision) { await reload() }
        .onChange(of: state.automationEventRevision) { _, _ in Task { await reload(force: true) } }
        .refreshable { await reload(force: true) }
    }
    func reload(force: Bool = false) async { guard state.connected else { return }; await state.perform { try await state.loadAutomations(force: force) } }
}

private struct AutomationRunsView: View {
    @Environment(AppState.self) private var state
    let item: AutomationEntry
    struct Run: Decodable, Identifiable { let id: String; let status: String; let created_at: Double; let task_id: String?; let result_text: String? }
    @State private var runs: [Run] = []
    @State private var error: String?
    @State private var loading = false
    var body: some View {
        List {
            Section {
                LabeledContent("范围", value: item.isFamily ? "家庭所有成员" : "仅自己")
                LabeledContent("状态", value: item.enabled ? "已启用" : "已停用")
                Text(item.triggerDescription)
                if let instruction = item.instruction, !instruction.isEmpty { Text(instruction) }
            }
            Section("服务器执行记录") {
                if loading && runs.isEmpty { ProgressView() }
                if let error { Text(error).foregroundStyle(.red) }
                ForEach(runs) { run in
                    if let task = run.task_id {
                        NavigationLink { TaskProgressView(identifier: task) } label: { runLabel(run) }
                    } else { runLabel(run) }
                }
                if !loading && error == nil && runs.isEmpty { Text("尚未执行").foregroundStyle(.secondary) }
            }
        }.navigationTitle(item.name)
        .task(id: state.connectionRevision) { runs = []; await load() }
        .onChange(of: state.taskEventRevision) { _, _ in Task { await load() } }
        .refreshable { await load() }
    }
    private func runLabel(_ run: Run) -> some View {
        VStack(alignment: .leading) {
            Text(taskStatusLabel(run.status))
            if let result = run.result_text, !result.isEmpty { Text(result).font(.subheadline).textSelection(.enabled) }
            Text(Date(timeIntervalSince1970: run.created_at), format: .dateTime.year().month().day().hour().minute()).font(.caption).foregroundStyle(.secondary)
        }
    }
    private func load() async {
        guard !loading else { return }; loading = true; defer { loading = false }
        do {
            let namespace = try await state.api.syncNamespace()
            let bytes = try await state.api.request("GET", "/api/v1/automations/" + item.id + "/runs", expectedNamespace: namespace)
            guard !Task.isCancelled else { return }
            runs = try JSONDecoder().decode([Run].self, from: bytes); error = nil
        } catch { runs = []; self.error = error.localizedDescription }
    }
}

struct FamilyMember: Codable, Identifiable, Sendable { let id: String; let name: String }

struct MemberMemoryView: View {
    @Environment(AppState.self) private var state
    struct Candidate: Codable, Identifiable { let id: String; let content: String }
    private struct Snapshot: Codable { let entries: [DataEntry]; let candidates: [Candidate] }
    @State private var lastRefresh: Date?
    @State private var cached = false
    @State private var editing: DataEntry?
    @State private var entries: [DataEntry] = []
    @State private var candidates: [Candidate] = []
    @State private var deciding = false
    @State private var error: String?
    @State private var loading = false
    @State private var needsRefresh = false
    var body: some View {
        List {
            if cached { Text("显示本机缓存，正在检查更新").font(.caption).foregroundStyle(.secondary) }
            if let error { Text(error).foregroundStyle(.red) }
            if loading && entries.isEmpty { ProgressView("正在读取记忆…") }
            if !entries.isEmpty {
                Section("已记住") {
                    ForEach(entries) { record in
                        VStack(alignment: .leading, spacing: 8) {
                            HStack {
                                Text(record.payload["origin"]?.description == "automatic_preference" ? "自动记忆" : "已确认记忆").font(.caption).foregroundStyle(.secondary)
                                Spacer()
                                Button("修改") { editing = record }.font(.caption)
                            }
                            Text(record.payload["content"]?.description ?? record.title).textSelection(.enabled)
                            if let quote = record.payload["source_quote"]?.description, !quote.isEmpty { Text("来源原话：" + quote).font(.caption).foregroundStyle(.secondary).textSelection(.enabled) }
                        }.swipeActions {
                            Button("删除", role: .destructive) { Task {
                                do { _ = try await state.api.request("DELETE", "/api/v1/data/" + record.id); await load(force: true) }
                                catch { self.error = error.localizedDescription }
                            } }
                        }
                    }
                }
            }
            if !candidates.isEmpty {
                Section("等待确认") {
                    ForEach(candidates) { candidate in
                        VStack(alignment: .leading, spacing: 8) {
                            Text(candidate.content)
                            HStack {
                                Button("确认记住") { Task { await decide(candidate.id, "confirm") } }
                                Button("不记住", role: .destructive) { Task { await decide(candidate.id, "reject") } }
                            }.disabled(deciding)
                        }
                    }
                }
            }
            if !loading && error == nil && entries.isEmpty && candidates.isEmpty {
                ContentUnavailableView("暂无聊天记忆", systemImage: "brain", description: Text("在 AI 对话中告诉它需要记住的内容。记忆由服务器保存，仅你自己可见。"))
            }
        }.navigationTitle("记忆")
            .sheet(item: $editing) { record in MemoryEditor(record: record) { Task { await load(force: true) } } }
            .task(id: state.connectionRevision) { entries = []; candidates = []; lastRefresh = nil; await load() }
            .onChange(of: state.memoryEventRevision) { _, _ in Task { await load(force: true) } }
            .onChange(of: state.dataEventRevision) { _, _ in Task { await load(force: true) } }
            .refreshable { await load(force: true) }
    }
    private func load(force: Bool = false) async {
        guard !loading else { if force { needsRefresh = true }; return }
        loading = true
        defer {
            loading = false
            if needsRefresh && !Task.isCancelled && state.connected {
                needsRefresh = false
                Task { await load(force: true) }
            }
        }
        do {
            let namespace = try await state.api.syncNamespace()
            if entries.isEmpty && candidates.isEmpty, let raw = await ClientViewCache.shared.read(key: "memory", namespace: namespace), let saved = try? JSONDecoder().decode(Snapshot.self, from: raw), await state.api.cachedNamespace() == namespace {
                entries = saved.entries; candidates = saved.candidates; cached = true
            }
            if !force, let lastRefresh, Date().timeIntervalSince(lastRefresh) < 3 { return }
            let bytes = try await state.api.request("GET", "/api/v1/memory/entries", expectedNamespace: namespace)
            let pending = try await state.api.request("GET", "/api/v1/memory/candidates", expectedNamespace: namespace)
            guard !Task.isCancelled, await state.api.cachedNamespace() == namespace else { return }
            entries = try JSONDecoder().decode([DataEntry].self, from: bytes)
            candidates = try JSONDecoder().decode([Candidate].self, from: pending)
            cached = false; lastRefresh = Date(); error = nil
            try? await ClientViewCache.shared.write(JSONEncoder().encode(Snapshot(entries: entries, candidates: candidates)), key: "memory", namespace: namespace)
        } catch {
            if case APIClient.APIError.http(let code, _) = error, [401, 403].contains(code) {
                entries = []; candidates = []; cached = false
                if let namespace = await state.api.cachedNamespace() { await ClientViewCache.shared.remove(key: "memory", namespace: namespace) }
            }
            self.error = error.localizedDescription
        }
    }
    private func decide(_ id: String, _ action: String) async {
        guard !deciding else { return }; deciding = true; defer { deciding = false }
        do {
            _ = try await state.api.request("POST", "/api/v1/memory/candidates/" + id + "/" + action)
            await load(force: true)
        } catch { self.error = error.localizedDescription }
    }
}

private struct MemoryEditor: View {
    @Environment(AppState.self) private var state
    @Environment(\.dismiss) private var dismiss
    let record: DataEntry
    let saved: () -> Void
    @State private var content: String
    @State private var error: String?
    @State private var busy = false
    init(record: DataEntry, saved: @escaping () -> Void) {
        self.record = record; self.saved = saved
        _content = State(initialValue: record.payload["content"]?.description ?? "")
    }
    var body: some View {
        NavigationStack {
            Form {
                TextField("记忆内容", text: $content, axis: .vertical).lineLimit(4...12)
                if let error { Text(error).foregroundStyle(.red) }
            }.navigationTitle("修改记忆")
                .toolbar {
                    ToolbarItem(placement: .cancellationAction) { Button("取消") { dismiss() }.disabled(busy) }
                    ToolbarItem(placement: .confirmationAction) { Button("保存") { Task { await save() } }.disabled(busy || content.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || content.count > 4000) }
                }
        }
    }
    private func save() async {
        guard let version = record.version else { error = "记忆版本不可用，请刷新后重试"; return }
        busy = true; defer { busy = false }
        do {
            _ = try await state.api.request("PATCH", "/api/v1/memory/entries/" + record.id, body: JSONSerialization.data(withJSONObject: ["content": content, "version": version]))
            saved(); dismiss()
        } catch { self.error = error.localizedDescription }
    }
}

struct MemberRecordDetail: View {
    @Environment(AppState.self) private var state
    @Environment(\.scenePhase) private var phase
    let record: DataEntry
    let isOwner: Bool
    var taskID: String? = nil
    private struct FileDetails: Codable { let record_id: String; let parsed: DataEntry?; let parse_status: String }
    @State private var detail: DataEntry?
    @State private var taskReadValidated = false
    @State private var loadGeneration = UUID()
    @State private var fileDetails: FileDetails?
    @State private var photo: UIImage?
    @State private var error: String?
    @State private var loading = false
    @State private var needsRefresh = false
    @State private var unavailable = false
    @State private var active = true
    @State private var namespace: String?
    @State private var attachmentBusy = false
    @State private var attachmentOperation: Task<Void, Never>?
    @State private var preview: ProtectedRecordPreview?
    @State private var fullText: RecordTextContent?
    @State private var visibility = "personal"
    @State private var saving = false
    private var displayed: DataEntry { detail ?? record }
    private var isNewAsset: Bool { displayed.payload["media_upload_id"] != nil }
    private var isFile: Bool { ["document.file", "document.import"].contains(displayed.kind) }
    private var recordKey: String { ResourceRoutes.cacheScope(taskID) + "record:" + record.id + ":" + String(record.version ?? 0) }

    var body: some View {
        List {
            if unavailable {
                ContentUnavailableView("资料不可用", systemImage: "lock", description: Text(error ?? "资料已删除或不再共享给你。"))
            } else if taskID != nil && !taskReadValidated {
                if let error { ContentUnavailableView("需要联网确认资料授权", systemImage: "lock", description: Text(error)) }
                else { ProgressView("正在确认本次任务的资料授权…") }
            } else {
                if loading { ProgressView("正在更新完整资料…") }
                if let error { Text(error).foregroundStyle(.red) }
                if isNewAsset { assetSection }
                if displayed.kind == "photo.selected" {
                    Section("照片") {
                        if let photo { Image(uiImage: photo).resizable().scaledToFit().frame(maxWidth: .infinity, maxHeight: 420).accessibilityLabel("当前照片的实际内容") }
                        else { Text("图片预览暂不可用，可打开原图或导出查看。").foregroundStyle(.secondary) }
                        Button("打开原图", systemImage: "arrow.up.left.and.arrow.down.right") { open(field: "/content_base64") }.disabled(attachmentBusy)
                    }
                }
                if isFile {
                    Section("原文件") {
                        Text(displayed.title).textSelection(.enabled)
                        if !isNewAsset { Button("打开原文件", systemImage: "doc.text.magnifyingglass") { open() }.disabled(attachmentBusy) }
                        if attachmentBusy { ProgressView("正在读取原文件…") }
                    }
                    if let fileDetails {
                        Section("文件全文") {
                            if fileDetails.parse_status == "ready", let parsed = fileDetails.parsed {
                                let text = parsed.payload["markdown"]?.description ?? parsed.payload["content"]?.description ?? ""
                                if text.count > 4000 {
                                    Button("阅读全文（\(text.count) 字）", systemImage: "text.document") { fullText = RecordTextContent(title: "文件全文", text: text) }
                                } else { Text(text).textSelection(.enabled).fixedSize(horizontal: false, vertical: true) }
                            } else {
                                Text(fileDetails.parse_status == "stale" ? "原件已更新，旧解析内容已隐藏。可打开原文件查看。" : "暂无解析文本，可打开原文件查看完整内容。").foregroundStyle(.secondary)
                            }
                        }
                    }
                }
                Section("资料信息") {
                    LabeledContent("类型", value: displayed.kind)
                    if let version = displayed.version { LabeledContent("版本", value: String(version)) }
                    if let source = displayed.source { LabeledContent("来源", value: source) }
                    DisclosureGroup("来源标识") {
                        Text("资料：" + displayed.id).textSelection(.enabled)
                        if let sourceID = displayed.source_id { Text("来源：" + sourceID).textSelection(.enabled) }
                    }
                    if isOwner && displayed.sensitivity != "SECRET" {
                        Picker("可见范围", selection: Binding(get: { visibility }, set: { selected in Task { await change(selected) } })) {
                            Text("仅自己").tag("personal")
                            Text("家庭所有成员").tag("family")
                        }.disabled(saving)
                    } else { LabeledContent("可见范围", value: displayed.isFamily ? "家庭所有成员" : "仅自己") }
                    RecordPayloadView(payload: displayed.payload, excludedKeys: isFile || displayed.kind == "photo.selected" ? ["content_base64"] : [], onAttachment: { path in open(field: path) }, onText: { name, text in fullText = RecordTextContent(title: name, text: text) })
                }
            }
        }
        .navigationTitle(unavailable || (taskID != nil && !taskReadValidated) ? "资料详情" : displayed.title)
        .onAppear { active = true; visibility = record.visibility ?? "personal" }
        .task(id: record.id + ":" + String(record.version ?? 0)) { await loadDetails() }
        .refreshable { await loadDetails() }
        .sheet(item: $preview, onDismiss: { closePreview() }) { RecordFilePreview(item: $0) }
        .sheet(item: $fullText) { RecordLongTextView(content: $0) }
        .onDisappear { active = false; attachmentOperation?.cancel(); closePreview() }
        .onChange(of: phase) { _, phase in
            if phase == .background { attachmentOperation?.cancel(); closePreview(); if taskID != nil { clearSensitiveDisplay() } }
            else if phase == .active && taskID != nil { Task { await loadDetails() } }
        }
        .onChange(of: state.dataEventRevision) { _, _ in Task { await loadDetails() } }
        .onChange(of: state.connectionRevision) { _, _ in clearSensitiveDisplay() }
        .onChange(of: state.connected) { _, connected in if !connected { clearSensitiveDisplay() } }
        .onChange(of: state.serverReachable) { _, reachable in if !reachable && taskID != nil { clearSensitiveDisplay() } }
    }

    private var assetSection: some View {
        Section("原件") {
            NavigationLink { MediaAssetViewer(recordID: displayed.id, version: displayed.version ?? 1, taskID: taskID) } label: {
                Label("查看完整附件", systemImage: "doc.text.magnifyingglass")
            }
            Text("按需读取完整原件，服务端负责解析与处理。").font(.caption).foregroundStyle(.secondary)
        }
    }
    private func closePreview() { preview?.remove(); preview = nil; fullText = nil }
    private func clearSensitiveDisplay() {
        attachmentOperation?.cancel(); closePreview()
        taskReadValidated = false; loadGeneration = UUID()
        detail = nil; photo = nil; fileDetails = nil; unavailable = true
    }
    private func binaryKey(_ entry: DataEntry, field: String?) -> String {
        ResourceRoutes.cacheScope(taskID) + "attachment:" + entry.id + ":" + String(entry.version ?? 0) + ":" + (field ?? "original")
    }
    private func apply(_ entry: DataEntry) {
        detail = entry; visibility = entry.visibility ?? "personal"
        if entry.kind == "photo.selected", let encoded = entry.payload["content_base64"]?.description,
           let bytes = Data(base64Encoded: encoded), let image = RecordAttachment.image(bytes) { photo = image }
        else { photo = nil }
    }
    private func deny(_ failure: Error) async {
        error = failure.localizedDescription
        if case APIClient.APIError.http(let code, _) = failure, [401, 403, 404, 409].contains(code) {
            let previous = displayed
            clearSensitiveDisplay()
            if code == 409 { error = "原件已更新，旧解析正文已隐藏，请查看或重新解析原文件。" }
            if let namespace {
                await ClientViewCache.shared.remove(key: recordKey, namespace: namespace)
                await ClientViewCache.shared.remove(key: ResourceRoutes.cacheScope(taskID) + "file-details:" + previous.id + ":" + String(previous.version ?? 0), namespace: namespace)
                for field in Set(["original", "/content_base64"] + previous.payload.keys.filter { $0.hasSuffix("_base64") }.map { "/" + $0 }) {
                    await ClientViewCache.shared.remove(key: ResourceRoutes.cacheScope(taskID) + "attachment:" + previous.id + ":" + String(previous.version ?? 0) + ":" + field, namespace: namespace)
                }
            }
        }
    }
    private func loadDetails() async {
        guard !loading else { needsRefresh = true; return }
        loading = true
        defer {
            loading = false
            if needsRefresh && !Task.isCancelled && state.connected && active {
                needsRefresh = false; Task { await loadDetails() }
            }
        }
        let revision = state.dataEventRevision
        let generation = loadGeneration
        let connection = state.connectionRevision
        do {
            let current = try await state.api.syncNamespace(); namespace = current
            if taskID == nil, detail == nil, let cached = await ClientViewCache.shared.read(key: recordKey, namespace: current),
               let value = try? JSONDecoder().decode(DataEntry.self, from: cached), await state.api.cachedNamespace() == current { apply(value) }
            let raw = try await state.api.request("GET", ResourceRoutes.record(record.id, taskID: taskID), expectedNamespace: current)
            guard active, !Task.isCancelled, loadGeneration == generation, state.dataEventRevision == revision, state.connectionRevision == connection, await state.api.cachedNamespace() == current else { return }
            let complete = try JSONDecoder().decode(DataEntry.self, from: raw)
            if let previous = detail?.version, previous != complete.version { closePreview(); fileDetails = nil }
            apply(complete); error = nil; unavailable = false; taskReadValidated = true
            try? await ClientViewCache.shared.write(raw, key: ResourceRoutes.cacheScope(taskID) + "record:" + complete.id + ":" + String(complete.version ?? 0), namespace: current)
            if isFile {
                let parsedKey = ResourceRoutes.cacheScope(taskID) + "file-details:" + complete.id + ":" + String(complete.version ?? 0)
                if taskID == nil, fileDetails == nil, let cached = await ClientViewCache.shared.read(key: parsedKey, namespace: current) { fileDetails = try? JSONDecoder().decode(FileDetails.self, from: cached) }
                let parsed = try await state.api.request("GET", ResourceRoutes.file(complete.id, taskID: taskID) + "/details", expectedNamespace: current)
                guard active, !Task.isCancelled, loadGeneration == generation, state.dataEventRevision == revision, state.connectionRevision == connection, await state.api.cachedNamespace() == current else { return }
                fileDetails = try JSONDecoder().decode(FileDetails.self, from: parsed)
                try? await ClientViewCache.shared.write(parsed, key: parsedKey, namespace: current)
            }
        } catch { if !Task.isCancelled && loadGeneration == generation && state.connectionRevision == connection { await deny(error) } }
    }
    private func open(field: String? = nil) {
        guard !attachmentBusy else { return }
        attachmentOperation = Task { await openAttachment(field: field) }
    }
    private func openAttachment(field: String?) async {
        attachmentBusy = true; defer { attachmentBusy = false }
        let connection = state.connectionRevision
        let revision = state.dataEventRevision
        do {
            let current = try await state.api.syncNamespace(); namespace = current
            // 下载/导出前重新读取权限和版本，缓存不能替代服务器授权判断。
            let raw = try await state.api.request("GET", ResourceRoutes.record(record.id, taskID: taskID), expectedNamespace: current)
            let verified = try JSONDecoder().decode(DataEntry.self, from: raw)
            guard active, !Task.isCancelled, state.connectionRevision == connection, state.dataEventRevision == revision, await state.api.cachedNamespace() == current else { return }
            apply(verified)
            let key = binaryKey(verified, field: field)
            let data: Data
            if let cached = await ClientViewCache.shared.read(key: key, namespace: current) { data = cached }
            else if let field, let encoded = RecordAttachment.encodedValue(path: field, payload: verified.payload), let decoded = Data(base64Encoded: encoded) { data = decoded }
            else { data = try await state.api.request("GET", ResourceRoutes.file(verified.id, taskID: taskID) + "/content", expectedNamespace: current) }
            guard data.count <= RecordAttachment.maximumBytes else { throw APIClient.APIError.message("附件超过当前客户端的 20 MB 预览限制") }
            if field == nil, let expected = verified.payload["sha256"]?.description, expected.count == 64, DeviceIdentity.hash(data) != expected.lowercased() {
                await ClientViewCache.shared.remove(key: key, namespace: current)
                throw APIClient.APIError.message("文件完整性校验失败，请重新打开下载")
            }
            guard active, !Task.isCancelled, state.connectionRevision == connection, state.dataEventRevision == revision, await state.api.cachedNamespace() == current else { return }
            try? await ClientViewCache.shared.write(data, key: key, namespace: current)
            closePreview()
            preview = try ProtectedRecordPreview(data: data, name: verified.payload["name"]?.description ?? verified.title, mime: verified.payload["mime_type"]?.description)
        } catch { if !Task.isCancelled && state.connectionRevision == connection { await deny(error) } }
    }
    private func change(_ value: String) async {
        guard !saving else { return }; saving = true; defer { saving = false }
        await state.perform {
            _ = try await state.api.request("PUT", "/api/v1/data/" + record.id + "/visibility", body: JSONSerialization.data(withJSONObject: ["visibility": value]))
            visibility = value; try await state.loadData(force: true)
        }
    }
}

struct ContinuousSharingView: View {
    @Environment(AppState.self) private var state
    @State private var categories: [String: String] = [:]
    @State private var namespace: String?
    @State private var error: String?
    @State private var busy = false
    var body: some View {
        List {
            Section {
                Text("开启后，该类已有及后续上传的数据对家庭所有成员可见；关闭后仅自己可见。不会共享聊天记忆，也不会新增手机采集权限。").font(.caption)
            }
            if let error { Text(error).foregroundStyle(.red) }
            ForEach(["health", "location"], id: \.self) { category in
                Toggle(category == "health" ? "家庭可见健康数据" : "家庭可见位置数据", isOn: Binding(get: {
                    categories[category] == "family"
                }, set: { enabled in Task { await change(category, enabled) } }))
                .disabled(busy || namespace == nil)
            }
        }.navigationTitle("持续共享")
            .task(id: state.connectionRevision) { namespace = nil; categories = [:]; await load() }
            .refreshable { await load() }
    }
    private func load() async {
        do {
            let current = try await state.api.syncNamespace()
            let saved = try JSONDecoder().decode([String: String].self, from: await state.api.request("GET", "/api/v1/sharing/categories", expectedNamespace: current))
            guard !Task.isCancelled else { return }
            categories = saved; namespace = current; error = nil
        } catch { self.error = error.localizedDescription; namespace = nil }
    }
    private func change(_ category: String, _ enabled: Bool) async {
        guard let namespace else { return }
        busy = true; defer { busy = false }
        do {
            _ = try await state.api.request("PUT", "/api/v1/sharing/categories/" + category, body: JSONSerialization.data(withJSONObject: ["visibility": enabled ? "family" : "personal"]), expectedNamespace: namespace)
            await load()
        } catch { self.error = error.localizedDescription }
    }
}

struct NotificationSettingsContent: View {
    @State private var notifications = ClientNotifications.shared
    var body: some View {
        if notifications.authorization == "notDetermined" || notifications.authorization == "unknown" {
            Button("允许系统通知") { Task { await notifications.requestPermission() } }
        } else {
            Button(notifications.authorization == "denied" ? "在系统设置中开启通知" : "系统通知设置") {
                if let url = URL(string: UIApplication.openNotificationSettingsURLString) { UIApplication.shared.open(url) }
            }
        }
        if !notifications.status.isEmpty { Text(notifications.status).font(.caption).foregroundStyle(.secondary) }
    }
}

struct NotificationInboxView: View {
    @Environment(AppState.self) private var state
    @Environment(\.dismiss) private var dismiss
    var highlightID: String? = nil
    struct Page: Decodable { let items: [ClientNotification]; let has_more: Bool; let next_before: Double? }
    @State private var items: [ClientNotification] = []
    @State private var before: Double?
    @State private var viewingOlder = false
    @State private var hasMore = false
    @State private var loading = false
    @State private var error: String?
    var body: some View {
        List {
            if let error { Text(error).foregroundStyle(.red) }
            if loading && items.isEmpty { ProgressView("正在读取通知…") }
            ForEach(items) { item in
                VStack(alignment: .leading, spacing: 8) {
                    HStack {
                        if item.read_at == nil { Circle().fill(.teal).frame(width: 7, height: 7) }
                        Text(item.title).font(.headline)
                        Spacer()
                        Text(item.scope == "family" ? "家庭" : "个人").font(.caption).foregroundStyle(.secondary)
                    }
                    if let status = item.status { Text(status == "DUE" ? "已到期" : taskStatusLabel(status)).font(.subheadline) }
                    Text(Date(timeIntervalSince1970: item.created_at), format: .dateTime.month().day().hour().minute()).font(.caption).foregroundStyle(.secondary)
                    if item.kind == "client.action" {
                        NavigationLink("查看成员请求") { ClientActionInbox(notificationID: item.id).task { await markRead(item) } }
                    } else if let conversation = item.conversation_id {
                        Button("查看对话") { Task { await openConversation(conversation, notification: item) } }
                    } else if let task = item.task_id {
                        NavigationLink { TaskProgressView(identifier: task).task { await markRead(item) } } label: { Text("查看执行结果") }
                    } else if let record = item.record_id {
                        NavigationLink { RecordSourceView(source: RecordReference(id: record, title: "提醒详情", version: nil)).task { await markRead(item) } } label: { Text("查看提醒") }
                    } else if item.automation_id != nil {
                        Button("查看家庭自动化") {
                            Task {
                                await markRead(item)
                                IntentRouter.shared.destination = .automations
                                ClientNotifications.shared.pendingNotificationID = nil
                                dismiss()
                            }
                        }
                    }
                    if item.read_at == nil { Button("标记已读") { Task { await markRead(item); await load() } }.font(.caption) }
                }.listRowBackground(item.id == highlightID ? Color.teal.opacity(0.08) : nil)
            }
            if hasMore { Button("加载更早通知") { Task { await load(older: true) } }.disabled(loading) }
            if !loading && error == nil && items.isEmpty {
                ContentUnavailableView("暂无通知", systemImage: "bell", description: Text("任务和自动化在服务器执行，结果会显示在这里。"))
            }
        }.navigationTitle("通知")
            .task(id: state.connectionRevision) {
                items = []
                while !Task.isCancelled {
                    if !viewingOlder { await load() }
                    do { try await Task.sleep(for: .seconds(15)) } catch { return }
                }
            }
            .onChange(of: state.taskEventRevision) { _, _ in Task { await load() } }
            .refreshable { await load() }
    }
    private func load(older: Bool = false) async {
        guard !loading else { return }; loading = true; defer { loading = false }
        do {
            let namespace = try await state.api.syncNamespace()
            let path = "/api/v1/notifications?limit=50" + (older && before != nil ? "&before=\(before!)" : "")
            let page = try JSONDecoder().decode(Page.self, from: await state.api.request("GET", path, expectedNamespace: namespace))
            guard !Task.isCancelled else { return }
            if older { items += page.items.filter { next in !items.contains { $0.id == next.id } } }
            else { items = page.items }
            viewingOlder = older
            before = page.next_before; hasMore = page.has_more; error = nil
            if !older, let highlightID, !items.contains(where: { $0.id == highlightID }) {
                let highlighted = try JSONDecoder().decode(ClientNotification.self, from: await state.api.request("GET", "/api/v1/notifications/" + highlightID, expectedNamespace: namespace))
                items.insert(highlighted, at: 0)
            }
        } catch { if !older { items = [] }; self.error = error.localizedDescription }
    }
    private func markRead(_ item: ClientNotification) async {
        do { _ = try await state.api.request("POST", "/api/v1/notifications/" + item.id + "/read") }
        catch { self.error = error.localizedDescription }
    }
    private func openConversation(_ id: String, notification: ClientNotification) async {
        guard UUID(uuidString: id) != nil else { return }
        await markRead(notification)
        IntentRouter.shared.conversationID = id
        IntentRouter.shared.destination = .ai
        ClientNotifications.shared.pendingNotificationID = nil
        dismiss()
    }
}
