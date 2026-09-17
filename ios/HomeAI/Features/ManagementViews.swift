import SwiftUI
import PhotosUI

struct ActivityView: View {
    @Environment(AppState.self) private var state
    var body: some View {
        List {
            if !state.approvals.isEmpty {
                Section("等待你的确认") {
                    ForEach(state.approvals) { approval in
                        VStack(alignment: .leading, spacing: 10) {
                            Text(approval.capability).font(.headline)
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
            Section("操作记录") {
                ForEach(state.activity) { entry in
                    VStack(alignment: .leading) {
                        Text(entry.action)
                        Text(Date(timeIntervalSince1970: entry.created_at), style: .relative).font(.caption).foregroundStyle(.secondary)
                    }
                }
            }
        }.overlay { if state.activity.isEmpty && state.approvals.isEmpty { ContentUnavailableView("暂无活动", systemImage: "clock") } }
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

struct DataView: View {
    @Environment(AppState.self) private var state
    @State private var importing = false
    var body: some View {
        List {
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
        .overlay { if state.records.isEmpty { ContentUnavailableView("数据由你掌控", systemImage: "externaldrive", description: Text("在设置中授权同步，或导入文件。")) } }
        .navigationTitle("数据")
        .toolbar { Button("导入文件", systemImage: "plus") { importing = true } }
        .fileImporter(isPresented: $importing, allowedContentTypes: [.data]) { result in
            Task { await state.perform {
                let url = try result.get()
                let access = url.startAccessingSecurityScopedResource()
                defer { if access { url.stopAccessingSecurityScopedResource() } }
                let bytes = try Data(contentsOf: url)
                guard bytes.count <= 20 * 1024 * 1024 else { throw APIClient.APIError.message("文件超过 20 MB") }
                try await ConnectorSync(api: state.api).uploadRecord(source: "files", sourceID: DeviceIdentity.hash(bytes), kind: "document.import", payload: ["name": .string(url.lastPathComponent), "content_base64": .string(bytes.base64EncodedString())])
                try await state.loadData()
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
                Text(item.cron).font(.caption.monospaced())
            }.swipeActions {
                Button("停用", role: .destructive) { Task { await state.perform {
                    _ = try await state.api.request("DELETE", "/api/v1/automations/" + item.id)
                    try await state.loadAutomations()
                } } }
            }
        }
        .overlay { if state.automations.isEmpty { ContentUnavailableView("让日常按时发生", systemImage: "bolt", description: Text("创建定时提醒，每次执行都遵守你的权限设置。")) } }
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
    var body: some View {
        NavigationStack {
            Form {
                TextField("提醒内容", text: $title)
                DatePicker("每天", selection: $time, displayedComponents: .hourAndMinute)
                Text("提醒保存到家庭服务器。推送功能尚未完成配置时，请在数据页面查看。").font(.caption).foregroundStyle(.secondary)
            }.navigationTitle("每天提醒")
                .toolbar {
                    ToolbarItem(placement: .cancellationAction) { Button("取消") { dismiss() } }
                    ToolbarItem(placement: .confirmationAction) { Button("保存") { Task { await save() } }.disabled(title.isEmpty) }
                }
        }
    }
    func save() async {
        await state.perform {
            let parts = Calendar.current.dateComponents([.hour, .minute], from: time)
            let body: [String: Any] = ["name": title, "cron": "\(parts.minute ?? 0) \(parts.hour ?? 0) * * *", "timezone": TimeZone.current.identifier, "enabled": true, "skill": ["name": title, "steps": [["capability": "reminder.create@v1", "arguments": ["title": title]]]]]
            _ = try await state.api.request("POST", "/api/v1/automations", body: JSONSerialization.data(withJSONObject: body))
            try await state.loadAutomations()
            dismiss()
        }
    }
}
