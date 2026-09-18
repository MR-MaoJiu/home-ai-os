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
    @State private var importing = false
    @State private var currentUser = ""
    @State private var currentMemberName = ""
    var body: some View {
        List {
            if !state.syncStatus.isEmpty { Text(state.syncStatus).font(.caption).foregroundStyle(.secondary) }
            Section {
                if !currentMemberName.isEmpty { Text("当前成员：" + currentMemberName).font(.caption) }
                NavigationLink("我的与共享记忆") { MemberMemoryView() }.disabled(!state.connected)
                Text("显示本人成员的数据和明确共享给你的数据。其他账号的管理后台，需要你逐条共享后才能看到。").font(.caption)
            }
            Section("已授权的数据") {
                ForEach(state.records) { record in
                    NavigationLink {
                        MemberRecordDetail(record: record, isOwner: record.owner_id == currentUser)
                    } label: {
                        Label { VStack(alignment: .leading) { Text(record.title); Text(record.kind).font(.caption).foregroundStyle(.secondary) } } icon: { Image(systemName: "doc.text") }
                    }
                    .swipeActions { if record.owner_id == currentUser { Button("删除", role: .destructive) { Task { await remove(record.id) } } } }
                }
            }
        }
        .overlay { if state.records.isEmpty { ContentUnavailableView("数据由你掌控", systemImage: "externaldrive", description: Text(state.syncStatus.isEmpty ? "在设置中授权同步，或导入文件。" : state.syncStatus)) } }
        .navigationTitle("数据")
        .toolbar { Button("导入文件", systemImage: "plus") { importing = true } }
        .fileImporter(isPresented: $importing, allowedContentTypes: [.data]) { result in
            Task { await state.perform {
                let url = try result.get()
                let access = url.startAccessingSecurityScopedResource()
                defer { if access { url.stopAccessingSecurityScopedResource() } }
                let handle = try FileHandle(forReadingFrom: url)
                defer { try? handle.close() }
                let bytes = try handle.read(upToCount: 20 * 1024 * 1024 + 1) ?? Data()
                guard bytes.count <= 20 * 1024 * 1024 else { throw APIClient.APIError.message("文件超过 20 MB") }
                let task = try await state.api.uploadDocument(name: url.lastPathComponent, contents: bytes)
                try await state.loadData()
                state.syncStatus = "文档解析已提交（\(task.prefix(8))），完成后可直接在 AI 页面提问"
            } }
        }
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
        }.refreshable { await reload() }
    }
    func reload() async { guard state.connected else { return }; await state.perform { try await state.loadData() } }
    func remove(_ id: String) async { await state.perform { _ = try await state.api.request("DELETE", "/api/v1/data/" + id); try await state.loadData() } }
}

struct AutomationsView: View {
    @Environment(AppState.self) private var state
    @State private var showingEditor = false
    var body: some View {
        List(state.automations) { item in
            VStack(alignment: .leading) {
                HStack { Text(item.name); Spacer(); Text(item.enabled ? "已启用" : "已停用").foregroundStyle(.secondary) }
                Text(item.triggerDescription).font(.caption)
            }.swipeActions {
                Button("停用", role: .destructive) { Task { await state.perform {
                    _ = try await state.api.request("DELETE", "/api/v1/automations/" + item.id)
                    try await state.loadAutomations()
                } } }
            }
        }
        .overlay { if state.automations.isEmpty { ContentUnavailableView("让日常按时发生", systemImage: "bolt", description: Text("按时间或数据变化创建提醒，每次执行都遵守你的权限设置。")) } }
        .navigationTitle("自动化")
        .toolbar { Button("新建", systemImage: "plus") { showingEditor = true } }
        .sheet(isPresented: $showingEditor) { AutomationEditor() }
        .task { await reload() }.refreshable { await reload() }
    }
    func reload() async { guard state.connected else { return }; await state.perform { try await state.loadAutomations() } }
}

struct AutomationEditor: View {
    @Environment(AppState.self) private var state
    @Environment(\.dismiss) private var dismiss
    @State private var title = ""
    @State private var time = Date()
    @State private var trigger = "cron"
    @State private var eventType = "record.changed"
    @State private var recordKind = ""
    @State private var includeShared = false
    @State private var saving = false
    var body: some View {
        NavigationStack {
            Form {
                TextField("提醒内容", text: $title)
                Picker("触发方式", selection: $trigger) {
                    Text("每天定时").tag("cron")
                    Text("数据事件").tag("event")
                }
                if trigger == "cron" {
                    DatePicker("每天", selection: $time, displayedComponents: .hourAndMinute)
                } else {
                    Picker("事件", selection: $eventType) {
                        Text("数据新增或更新").tag("record.changed")
                        Text("数据删除").tag("record.deleted")
                        Text("共享授权撤回").tag("record.revoked")
                    }
                    TextField("数据类型，可留空", text: $recordKind).textInputAutocapitalization(.never)
                    Toggle("包含共享给我的数据", isOn: $includeShared)
                    Text("触发间隔为 60 秒，期间的事件保留排队。过时或已撤权的更新会跳过；停用不取消已经创建的任务。").font(.caption).foregroundStyle(.secondary)
                }
                Text("提醒保存到家庭服务器。推送功能尚未完成配置时，请在数据页面查看。").font(.caption).foregroundStyle(.secondary)
            }.disabled(saving).navigationTitle("新建自动化")
                .toolbar {
                    ToolbarItem(placement: .cancellationAction) { Button("取消") { dismiss() } }
                    ToolbarItem(placement: .confirmationAction) { Button("保存") { Task { await save() } }.disabled(title.isEmpty || title.count > 100 || saving) }
                }
        }
    }
    func save() async {
        guard !saving else { return }
        saving = true
        defer { saving = false }
        await state.perform {
            let parts = Calendar.current.dateComponents([.hour, .minute], from: time)
            var arguments: [String: Any] = ["title": title]
            if trigger == "event" { arguments["source_record"] = ["$event": "record_id"] }
            var body: [String: Any] = ["name": title, "trigger_kind": trigger, "timezone": TimeZone.current.identifier, "enabled": true, "skill": ["name": title, "steps": [["capability": "reminder.create@v1", "arguments": arguments]]]]
            if trigger == "cron" {
                body["cron"] = "\(parts.minute ?? 0) \(parts.hour ?? 0) * * *"
            } else {
                body["event_type"] = eventType
                body["cooldown_seconds"] = 60
                body["include_shared"] = includeShared
                if !recordKind.isEmpty { body["record_kind"] = recordKind }
            }
            _ = try await state.api.request("POST", "/api/v1/automations", body: JSONSerialization.data(withJSONObject: body))
            try await state.loadAutomations()
            dismiss()
        }
    }
}

struct FamilyMember: Decodable, Identifiable, Sendable { let id: String; let name: String }

struct RecordSharingView: View {
    @Environment(AppState.self) private var state
    let recordID: String
    @State private var members: [FamilyMember] = []
    @State private var grants: Set<String> = []
    @State private var me = ""
    @State private var error: String?
    @State private var busy = false
    var body: some View {
        List {
            Section { Text("只共享当前这条资料或记忆，不会开放你的其他数据。可随时撤回；秘密资料不能共享。").font(.caption) }
            if let error { Text(error).foregroundStyle(.red) }
            ForEach(members.filter { $0.id != me }) { member in
                HStack {
                    Text(member.name); Spacer()
                    Button(grants.contains(member.id) ? "撤回共享" : "共享") { Task { await change(member.id) } }.disabled(busy)
                }
            }
        }.navigationTitle("共享给家庭成员").task(id: state.connectionRevision) { members = []; grants = []; await load() }
    }
    func load() async {
        do {
            let namespace = try await state.api.syncNamespace()
            let owner = try await state.api.ownerIdentity(expectedNamespace: namespace)
            me = owner.userID
            members = try JSONDecoder().decode([FamilyMember].self, from: await state.api.request("GET", "/api/v1/members", expectedNamespace: namespace))
            struct Grants: Decodable { let grantee_ids: [String] }
            grants = Set(try JSONDecoder().decode(Grants.self, from: await state.api.request("GET", "/api/v1/data/" + recordID + "/grants", expectedNamespace: namespace)).grantee_ids)
            error = nil
        } catch { self.error = error.localizedDescription }
    }
    func change(_ member: String) async {
        busy = true; defer { busy = false }
        do {
            _ = try await state.api.request(grants.contains(member) ? "DELETE" : "PUT", "/api/v1/data/" + recordID + "/grants/" + member)
            await load()
        } catch { self.error = error.localizedDescription }
    }
}

struct MemberMemoryView: View {
    @Environment(AppState.self) private var state
    struct Candidate: Decodable, Identifiable { let id: String; let content: String }
    @State private var candidates: [Candidate] = []
    @State private var ownerID = ""
    @State private var source = ""
    @State private var content = ""
    @State private var error: String?
    @State private var busy = false
    var body: some View {
        List {
            Section { Text("同步的原始资料不会自动成为长期记忆。提交候选并确认后，才保存到你的记忆账本。").font(.caption) }
            if let error { Text(error).foregroundStyle(.red) }
            Section("已确认及共享给我的记忆") {
                ForEach(state.records.filter { $0.kind == "memory.fact" }) { record in
                    VStack(alignment: .leading) {
                        Text(record.payload["content"]?.description ?? record.title)
                        if record.owner_id == ownerID { NavigationLink("共享此条记忆") { RecordSharingView(recordID: record.id) } }
                        else { Text("成员共享给你的记忆").font(.caption) }
                    }
                }
            }
            Section("提交我的候选记忆") {
                Picker("来源资料", selection: $source) {
                    Text("请选择自己的资料").tag("")
                    ForEach(state.records.filter { $0.owner_id == ownerID && $0.sensitivity != "SECRET" }) { record in Text(record.title).tag(record.id) }
                }
                TextField("需要记住的内容", text: $content, axis: .vertical).lineLimit(3...8)
                Button("提交候选，等待确认") { Task { await submit() } }.disabled(busy || source.isEmpty || content.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            }
            Section("等待我确认") {
                ForEach(candidates) { candidate in
                    VStack(alignment: .leading) {
                        Text(candidate.content)
                        HStack {
                            Button("确认记住") { Task { await decide(candidate.id, "confirm") } }
                            Button("拒绝", role: .destructive) { Task { await decide(candidate.id, "reject") } }
                        }.disabled(busy)
                    }
                }
            }
        }.navigationTitle("成员记忆").task(id: state.connectionRevision) { candidates = []; await load() }.refreshable { await load() }
    }
    func load() async {
        do {
            let namespace = try await state.api.syncNamespace()
            ownerID = try await state.api.ownerIdentity(expectedNamespace: namespace).userID
            try await state.loadData()
            candidates = try JSONDecoder().decode([Candidate].self, from: await state.api.request("GET", "/api/v1/memory/candidates"))
            error = nil
        } catch { self.error = error.localizedDescription }
    }
    func submit() async {
        busy = true; defer { busy = false }
        do {
            _ = try await state.api.request("POST", "/api/v1/memory/candidates", body: JSONSerialization.data(withJSONObject: ["source_ids": [source], "content": content]))
            content = ""; await load()
        } catch { self.error = error.localizedDescription }
    }
    func decide(_ id: String, _ decision: String) async {
        busy = true; defer { busy = false }
        do { _ = try await state.api.request("POST", "/api/v1/memory/candidates/" + id + "/" + decision); await load() }
        catch { self.error = error.localizedDescription }
    }
}

private struct MemberRecordDetail: View {
    let record: DataEntry
    let isOwner: Bool
    var body: some View {
        List {
            LabeledContent("类型", value: record.kind)
            LabeledContent("敏感等级", value: record.sensitivity)
            if isOwner && record.sensitivity != "SECRET" {
                NavigationLink("共享给家庭成员") { RecordSharingView(recordID: record.id) }
            }
            if record.kind == "photo.selected" && record.sensitivity != "SECRET" {
                NavigationLink("使用本地模型分析照片") { PhotoAnalysisView(recordID: record.id) }
            }
            ForEach(record.payload.keys.filter { $0 != "content_base64" }.sorted(), id: \.self) { key in
                VStack(alignment: .leading) {
                    Text(key).foregroundStyle(.secondary)
                    Text(record.payload[key]?.description ?? "").textSelection(.enabled)
                }
            }
        }.navigationTitle(record.title)
    }
}
