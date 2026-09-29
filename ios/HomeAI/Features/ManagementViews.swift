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
    @State private var selectedPhoto: PhotosPickerItem?
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
        .toolbar {
            PhotosPicker(selection: $selectedPhoto, matching: .images) { Label("导入照片", systemImage: "photo") }
            Button("导入文件", systemImage: "plus") { importing = true }
        }
        .onChange(of: selectedPhoto) { _, photo in
            guard let photo else { return }
            Task { await state.perform {
                guard let data = try await photo.loadTransferable(type: Data.self) else { return }
                let prepared = try PhotoPreparation.jpeg(data)
                try await ConnectorSync(api: state.api).uploadRecord(source: "photos", sourceID: DeviceIdentity.hash(data), kind: "photo.selected", payload: ["name": .string("用户选择的照片"), "content_base64": .string(prepared.base64EncodedString())])
                try await state.loadData()
                selectedPhoto = nil
            } }
        }
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
    @State private var scope = "personal"
    var body: some View {
        List {
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
        .onChange(of: state.taskEventRevision) { _, _ in Task { await reload() } }
        .refreshable { await reload() }
    }
    func reload() async { guard state.connected else { return }; await state.perform { try await state.loadAutomations() } }
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

struct FamilyMember: Decodable, Identifiable, Sendable { let id: String; let name: String }

struct MemberMemoryView: View {
    @Environment(AppState.self) private var state
    struct Candidate: Decodable, Identifiable { let id: String; let content: String }
    @State private var entries: [DataEntry] = []
    @State private var candidates: [Candidate] = []
    @State private var deciding = false
    @State private var error: String?
    @State private var loading = false
    var body: some View {
        List {
            if let error { Text(error).foregroundStyle(.red) }
            if loading && entries.isEmpty { ProgressView("正在读取记忆…") }
            if !entries.isEmpty {
                Section("已记住") {
                    ForEach(entries) { record in
                        Text(record.payload["content"]?.description ?? record.title).textSelection(.enabled)
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
            .task(id: state.connectionRevision) { entries = []; candidates = []; await load() }
            .onChange(of: state.taskEventRevision) { _, _ in Task { await load() } }
            .refreshable { await load() }
    }
    private func load() async {
        guard !loading else { return }; loading = true; defer { loading = false }
        do {
            let namespace = try await state.api.syncNamespace()
            let bytes = try await state.api.request("GET", "/api/v1/memory/entries", expectedNamespace: namespace)
            let pending = try await state.api.request("GET", "/api/v1/memory/candidates", expectedNamespace: namespace)
            guard !Task.isCancelled else { return }
            entries = try JSONDecoder().decode([DataEntry].self, from: bytes)
            candidates = try JSONDecoder().decode([Candidate].self, from: pending); error = nil
        } catch { entries = []; candidates = []; self.error = error.localizedDescription }
    }
    private func decide(_ id: String, _ action: String) async {
        guard !deciding else { return }; deciding = true; defer { deciding = false }
        do {
            _ = try await state.api.request("POST", "/api/v1/memory/candidates/" + id + "/" + action)
            await load()
        } catch { self.error = error.localizedDescription }
    }
}

private struct MemberRecordDetail: View {
    @Environment(AppState.self) private var state
    let record: DataEntry
    let isOwner: Bool
    @State private var visibility = "personal"
    @State private var saving = false
    var body: some View {
        List {
            LabeledContent("类型", value: record.kind)
            if isOwner && record.sensitivity != "SECRET" {
                Picker("可见范围", selection: Binding(get: { visibility }, set: { selected in Task { await change(selected) } })) {
                    Text("仅自己").tag("personal")
                    Text("家庭所有成员").tag("family")
                }.disabled(saving)
            } else { LabeledContent("可见范围", value: record.isFamily ? "家庭所有成员" : "仅自己") }
            ForEach(record.payload.keys.filter { $0 != "content_base64" }.sorted(), id: \.self) { key in
                VStack(alignment: .leading) {
                    Text(key).foregroundStyle(.secondary)
                    Text(record.payload[key]?.description ?? "").textSelection(.enabled)
                }
            }
        }.navigationTitle(record.title).onAppear { visibility = record.visibility ?? "personal" }
    }
    private func change(_ value: String) async {
        guard !saving else { return }; saving = true; defer { saving = false }
        await state.perform {
            _ = try await state.api.request("PUT", "/api/v1/data/" + record.id + "/visibility", body: JSONSerialization.data(withJSONObject: ["visibility": value]))
            visibility = value; try await state.loadData()
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
                    if let conversation = item.conversation_id {
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
