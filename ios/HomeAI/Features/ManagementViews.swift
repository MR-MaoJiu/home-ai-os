import SwiftUI
import PhotosUI

struct ActivityView: View {
    @Environment(AppState.self) private var state
    var body: some View {
        List {
            if !state.taskEventStatus.isEmpty { Text(state.taskEventStatus).font(.caption).foregroundStyle(.secondary) }
            if !state.approvals.isEmpty {
                Section("等待你的确认") {
                    ForEach(state.approvals) { approval in
                        VStack(alignment: .leading, spacing: 10) {
                            Text(approval.capability).font(.headline)
                            if approval.capability == "web.search@v1" { Text("确认后，下列查询词将发送给外部搜索引擎。").font(.caption) }
                            ForEach(approval.arguments.keys.sorted(), id: \.self) { key in
                                Text("\(key)：\(approval.arguments[key]?.description ?? "")").font(.caption).textSelection(.enabled)
                            }
                            HStack {
                                Button("拒绝", role: .destructive) { decide(approval.id, "REJECTED") }
                                Spacer()
                                Button("确认执行") { decide(approval.id, "APPROVED") }
                            }.buttonStyle(.bordered)
                        }.padding(.vertical, 6)
                    }
                }
            }
            if !state.taskStates.isEmpty {
                Section("任务进度") {
                    ForEach(state.taskStates) { task in
                        NavigationLink { TaskProgressView(identifier: task.id) } label: {
                            VStack(alignment: .leading) {
                                Text(task.status)
                                Text(task.id.prefix(8)).font(.caption.monospaced()).foregroundStyle(.secondary)
                            }
                        }
                    }
                }
            }
            Section("操作记录") {
                ForEach(state.activity) { entry in
                    VStack(alignment: .leading) {
                        Text(entry.action)
                        Text(Date(timeIntervalSince1970: entry.created_at), style: .relative).font(.caption).foregroundStyle(.secondary)
                    }
                }
            }
        }.overlay { if state.activity.isEmpty && state.approvals.isEmpty && state.taskStates.isEmpty { ContentUnavailableView("暂无活动", systemImage: "clock") } }
        .navigationTitle("活动")
        .task { await reload() }.refreshable { await reload() }
    }
    func reload() async { guard state.connected else { return }; await state.perform { try await state.loadActivity() } }
    func decide(_ id: String, _ decision: String) {
        Task { await state.perform {
            _ = try await state.api.request("POST", "/api/v1/approvals/" + id, body: JSONSerialization.data(withJSONObject: ["decision": decision]))
            try await state.loadActivity()
        } }
    }
}

struct TaskProgressView: View {
    @Environment(AppState.self) private var state
    let identifier: String
    @State private var result: ChatView.TaskResult?
    @State private var error: String?
    var body: some View {
        List {
            if let error { Text(error).foregroundStyle(.red) }
            if let result {
                LabeledContent("状态", value: result.status)
                if let message = result.error { Text(message) }
                if let value = ChatView.answer(result.result) { Text(value).textSelection(.enabled) }
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
        .task(id: state.taskEventRevision) { await reload() }
        .refreshable { await reload() }
    }
    func reload() async {
        do {
            let namespace = try await state.api.syncNamespace()
            let data = try await state.api.request("GET", "/api/v1/tasks/" + identifier, expectedNamespace: namespace)
            guard !Task.isCancelled else { return }
            result = try JSONDecoder().decode(ChatView.TaskResult.self, from: data)
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
    var body: some View {
        List {
            if !state.syncStatus.isEmpty { Text(state.syncStatus).font(.caption).foregroundStyle(.secondary) }
            Section("已授权的数据") {
                ForEach(state.records) { record in
                    NavigationLink {
                        List {
                            LabeledContent("类型", value: record.kind)
                            LabeledContent("敏感等级", value: record.sensitivity)
                            ForEach(record.payload.keys.sorted(), id: \.self) { key in
                                VStack(alignment: .leading) { Text(key).foregroundStyle(.secondary); Text(record.payload[key]?.description ?? "").textSelection(.enabled) }
                            }
                        }.navigationTitle(record.title)
                    } label: {
                        Label { VStack(alignment: .leading) { Text(record.title); Text(record.kind).font(.caption).foregroundStyle(.secondary) } } icon: { Image(systemName: "doc.text") }
                    }
                    .swipeActions { Button("删除", role: .destructive) { Task { await remove(record.id) } } }
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
        .task { await reload() }.refreshable { await reload() }
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
