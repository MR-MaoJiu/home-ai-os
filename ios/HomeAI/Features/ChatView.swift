import SwiftUI

struct StoredConversation: Decodable, Identifiable { let id: String; let title: String; let updated_at: Double }
struct ChatWebSource: Decodable { let title: String; let url: String }
struct ChatRecordSource: Decodable { let record_id: String; let title: String; let version: Int? }
struct StoredChatTurn: Decodable, Identifiable {
    let id: String
    let sequence: Int
    let client_key: String
    let user_text: String
    let task_id: String
    let status: String
    let assistant_text: String
    let error: String?
    let approvals: [ApprovalEntry]
    let web_sources: [ChatWebSource]
    let sources: [ChatRecordSource]
    var pending: Bool { !["SUCCEEDED", "FAILED", "CANCELED", "NEEDS_RECONCILIATION"].contains(status) }
}
struct StoredConversationPage: Decodable {
    let id: String
    let title: String
    let turns: [StoredChatTurn]
    let has_more: Bool
    let next_before: Int?
}
struct PendingChatSend: Codable {
    let conversationID: String
    let clientKey: String
    let content: String
    let timezone: String
}

struct ChatView: View {
    @Environment(AppState.self) private var state
    @Environment(\.scenePhase) private var scenePhase
    @State private var input = ""
    @State private var namespace: String?
    @State private var ownerNamespace = ""
    @State private var selected: String?
    @State private var conversations: [StoredConversation] = []
    @State private var turns: [StoredChatTurn] = []
    @State private var pendingApprovals: [ApprovalEntry] = []
    @State private var showHistory = false
    @State private var historyHasMore = false
    @State private var loadingHistory = false
    @State private var loading = false
    @State private var bootstrapRun: UUID?
    @State private var sending = false
    @State private var error: String?
    @State private var before: Int?
    @State private var pollID = UUID()
    @State private var voice = VoiceCapture()
    @State private var router = IntentRouter.shared
    private var pendingKey: String { "chat.pending:" + ownerNamespace }
    private var selectedKey: String { "chat.selected:" + ownerNamespace }
    private var running: Bool { turns.contains { $0.pending } }

    var body: some View {
        VStack(spacing: 0) {
            if let error { Text(error).foregroundStyle(.red).font(.caption).padding() }
            if (loading || bootstrapRun != nil) && turns.isEmpty { ProgressView("正在读取服务端会话历史…").padding() }
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 18) {
                        if let before { Button("加载更早消息") { Task { await loadOlder(before) } } }
                        if turns.isEmpty && !loading && bootstrapRun == nil { ContentUnavailableView("你的家庭助手", systemImage: "house.and.flag", description: Text("直接提问即可。服务端保存对话，并按需要检索资料、联网搜索和调用工具。")) }
                        ForEach(turns) { turn in turnCard(turn).id(turn.id) }
                        let visibleIDs = Set(turns.flatMap { $0.approvals.map(\.id) })
                        ForEach(pendingApprovals.filter { !visibleIDs.contains($0.id) }) { approval in approvalCard(approval) }
                    }.padding()
                }
                .onChange(of: turns.count) { _, _ in if let last = turns.last { proxy.scrollTo(last.id, anchor: .bottom) } }
            }
            HStack(alignment: .bottom) {
                Button { Task { await voiceInput() } } label: { Image(systemName: voice.recording ? "stop.circle.fill" : "mic.circle").font(.title) }
                    .disabled(sending || namespace == nil || running)
                    .accessibilityLabel(voice.recording ? "结束录音" : "语音输入")
                TextField("问问 Home AI", text: $input, axis: .vertical).disabled(sending || voice.recording).lineLimit(1...6).padding(12).background(.quaternary, in: RoundedRectangle(cornerRadius: 18))
                Button { Task { await send() } } label: { Image(systemName: "arrow.up.circle.fill").font(.largeTitle) }
                    .disabled(sending || running || namespace == nil || input.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                    .accessibilityLabel("发送消息")
            }.padding()
        }
        .navigationTitle("Home AI")
        .toolbar {
            Button("历史对话", systemImage: "clock") { showHistory = true }.disabled(namespace == nil)
            Button("新对话", systemImage: "square.and.pencil") { Task { await startNew() } }.disabled(namespace == nil || sending)
        }
        .sheet(isPresented: $showHistory) {
            NavigationStack {
                List {
                    ForEach(conversations) { conversation in
                        Button(conversation.title) { showHistory = false; Task { await choose(conversation.id) } }
                    }
                    if historyHasMore { Button("加载更多对话") { Task { await loadMoreConversations() } }.disabled(loadingHistory) }
                }.navigationTitle("服务端历史对话")
                    .toolbar { ToolbarItem(placement: .cancellationAction) { Button("关闭") { showHistory = false } } }
            }
        }
        .task(id: "\(state.connected)-\(state.connectionRevision)") { await bootstrap() }
        .task(id: pollID) {
            while !Task.isCancelled && scenePhase == .active {
                await refresh()
                if !running { break }
                try? await Task.sleep(for: .seconds(1.5))
            }
        }
        .onChange(of: scenePhase) { _, phase in if phase == .active { Task { await bootstrap() } } }
        .onChange(of: state.taskEventRevision) { _, _ in Task { await refresh() } }
        .onChange(of: router.conversationID) { _, id in if let id { Task { await choose(id) } } }
    }

    @ViewBuilder private func turnCard(_ turn: StoredChatTurn) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack { Spacer(minLength: 24); Text(turn.user_text).padding().background(.teal.opacity(0.12), in: RoundedRectangle(cornerRadius: 16)) }
            if !turn.assistant_text.isEmpty {
                Text(turn.assistant_text).textSelection(.enabled)
                if let namespace {
                    NavigationLink("朗读回答") {
                        SpeechView(text: turn.assistant_text, expectedNamespace: namespace, sources: turn.sources.map { RecordReference(id: $0.record_id, title: $0.title, version: $0.version) })
                    }.font(.caption)
                }
            }
            ForEach(turn.sources, id: \.record_id) { source in
                NavigationLink(source.title) { RecordSourceView(source: RecordReference(id: source.record_id, title: source.title, version: source.version)) }.font(.caption)
            }
            if let problem = turn.error { Text(problem).foregroundStyle(.red).font(.caption) }
            if turn.pending { HStack { ProgressView(); Text(taskStatusLabel(turn.status)).font(.caption); Button("取消") { Task { await cancel(turn) } } } }
            ForEach(turn.approvals) { approval in approvalCard(approval) }
            ForEach(Array(turn.web_sources.enumerated()), id: \.offset) { _, source in
                if let url = URL(string: source.url), ["https", "http"].contains(url.scheme?.lowercased()), url.user == nil, url.password == nil {
                    Link(source.title.isEmpty ? source.url : source.title, destination: url).font(.caption)
                }
            }
        }
    }
    @ViewBuilder private func approvalCard(_ approval: ApprovalEntry) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("这项操作需要你确认").font(.headline)
            Text(approval.capability).font(.caption)
            ForEach(approval.arguments.keys.sorted(), id: \.self) { key in Text("\(key)：\(approval.arguments[key]?.description ?? "")").font(.caption) }
            HStack {
                Button("确认执行") { Task { await decide(approval.id, "APPROVED") } }
                Button("拒绝", role: .destructive) { Task { await decide(approval.id, "REJECTED") } }
            }.disabled(sending)
        }.padding().background(.quaternary, in: RoundedRectangle(cornerRadius: 12))
    }
    private func bootstrap() async {
        guard state.connected else { namespace = nil; turns = []; conversations = []; bootstrapRun = nil; return }
        let run = UUID(); bootstrapRun = run; error = nil
        defer { if bootstrapRun == run { bootstrapRun = nil } }
        do {
            let current = try await state.api.syncNamespace()
            let owner = try await state.api.ownerIdentity(expectedNamespace: current)
            guard !Task.isCancelled, bootstrapRun == run else { return }
            if ownerNamespace != owner.namespace { turns = []; selected = nil; pendingApprovals = [] }
            namespace = current; ownerNamespace = owner.namespace
            let list = try JSONDecoder().decode([StoredConversation].self, from: await state.api.request("GET", "/api/v1/conversations", expectedNamespace: current))
            guard !Task.isCancelled, namespace == current, bootstrapRun == run else { return }
            conversations = list; historyHasMore = list.count == 100
            let pending = DeviceIdentity.read(pendingKey).flatMap { try? JSONDecoder().decode(PendingChatSend.self, from: $0) }
            let saved = DeviceIdentity.read(selectedKey).flatMap { String(data: $0, encoding: .utf8) }
            let target = router.conversationID ?? pending?.conversationID ?? selected ?? saved ?? conversations.first?.id
            if let target { await choose(target) }
        } catch { if !Task.isCancelled { self.error = error.localizedDescription } }
    }
    private func choose(_ id: String) async {
        if selected != id { turns = []; before = nil; input = "" }
        selected = id
        try? DeviceIdentity.save(Data(id.utf8), name: selectedKey)
        await refresh(); pollID = UUID()
        if router.conversationID == id { router.conversationID = nil }
        if let data = DeviceIdentity.read(pendingKey), let pending = try? JSONDecoder().decode(PendingChatSend.self, from: data), pending.conversationID == id {
            if turns.contains(where: { $0.client_key == pending.clientKey }) { try? DeviceIdentity.save(Data(), name: pendingKey) }
            else { input = pending.content }
        }
    }
    private func refresh() async {
        guard !loading, let namespace else { return }
        loading = true; defer { loading = false }
        let identifier = selected
        do {
            if let identifier {
                let data = try await state.api.request("GET", "/api/v1/conversations/" + identifier, expectedNamespace: namespace)
                let page = try JSONDecoder().decode(StoredConversationPage.self, from: data)
                guard !Task.isCancelled, selected == identifier, self.namespace == namespace else { return }
                let recentIDs = Set(page.turns.map(\.id))
                turns = (turns.filter { !recentIDs.contains($0.id) && $0.sequence < (page.turns.first?.sequence ?? 0) } + page.turns).sorted { $0.sequence < $1.sequence }
                if turns.first?.sequence == page.turns.first?.sequence { before = page.has_more ? page.next_before : nil }
            }
            let pending = try await state.api.request("GET", "/api/v1/approvals", expectedNamespace: namespace)
            guard !Task.isCancelled, self.namespace == namespace else { return }
            pendingApprovals = try JSONDecoder().decode([ApprovalEntry].self, from: pending)
            error = nil
        } catch { if !Task.isCancelled { self.error = error.localizedDescription } }
    }
    private func loadOlder(_ sequence: Int) async {
        guard let selected, let namespace else { return }
        do {
            let page = try JSONDecoder().decode(StoredConversationPage.self, from: await state.api.request("GET", "/api/v1/conversations/" + selected + "?before=" + String(sequence), expectedNamespace: namespace))
            guard self.selected == selected, self.namespace == namespace, !Task.isCancelled else { return }
            let existing = Set(turns.map(\.id));turns = (page.turns.filter { !existing.contains($0.id) } + turns).sorted { $0.sequence < $1.sequence }
            before = page.has_more ? page.next_before : nil
        } catch { self.error = error.localizedDescription }
    }
    private func loadMoreConversations() async {
        guard let namespace, !loadingHistory else { return }
        loadingHistory = true; defer { loadingHistory = false }
        do {
            let page = try JSONDecoder().decode([StoredConversation].self, from: await state.api.request("GET", "/api/v1/conversations?offset=" + String(conversations.count), expectedNamespace: namespace))
            guard self.namespace == namespace else { return }
            let existing = Set(conversations.map(\.id))
            conversations += page.filter { !existing.contains($0.id) }
            historyHasMore = page.count == 100
        } catch { self.error = error.localizedDescription }
    }
    private func createConversation() async throws -> String {
        guard let namespace else { throw APIClient.APIError.message("请先连接家庭服务器") }
        struct Created: Decodable { let id: String }
        let key = "chat.creating:" + ownerNamespace
        let stored = DeviceIdentity.read(key).flatMap { String(data: $0, encoding: .utf8) }
        let identifier = stored.flatMap(UUID.init(uuidString:))?.uuidString ?? UUID().uuidString
        try DeviceIdentity.save(Data(identifier.utf8), name: key)
        let body = try JSONSerialization.data(withJSONObject: ["client_id": identifier])
        let result = try JSONDecoder().decode(Created.self, from: await state.api.request("POST", "/api/v1/conversations", body: body, expectedNamespace: namespace))
        try DeviceIdentity.save(Data(), name: key)
        guard self.namespace == namespace else { throw APIClient.APIError.message("连接已切换") }
        return result.id
    }
    private func startNew() async {
        do { let id = try await createConversation(); await bootstrap(); await choose(id); input = "" }
        catch { self.error = error.localizedDescription }
    }
    private func send() async {
        guard let namespace, !sending else { return }
        sending = true; defer { sending = false }
        let pendingStorage = pendingKey
        let selectionStorage = selectedKey
        do {
            if selected == nil { selected = try await createConversation() }
            guard let selected else { return }
            let previous = DeviceIdentity.read(pendingStorage).flatMap { try? JSONDecoder().decode(PendingChatSend.self, from: $0) }
            let pending = previous?.conversationID == selected && previous?.content == input ? previous! : PendingChatSend(conversationID: selected, clientKey: UUID().uuidString, content: input, timezone: TimeZone.current.identifier)
            try DeviceIdentity.save(JSONEncoder().encode(pending), name: pendingStorage)
            let body = try JSONSerialization.data(withJSONObject: ["client_key": pending.clientKey, "content": pending.content, "timezone": pending.timezone])
            _ = try await state.api.request("POST", "/api/v1/conversations/" + selected + "/messages", body: body, expectedNamespace: namespace)
            try DeviceIdentity.save(Data(), name: pendingStorage)
            try DeviceIdentity.save(Data(selected.utf8), name: selectionStorage)
            guard self.namespace == namespace, self.selected == selected else { return }
            input = ""; await refresh(); pollID = UUID()
            conversations = try JSONDecoder().decode([StoredConversation].self, from: await state.api.request("GET", "/api/v1/conversations", expectedNamespace: namespace))
        } catch { self.error = error.localizedDescription }
    }
    private func decide(_ id: String, _ decision: String) async {
        guard let namespace else { return }
        sending = true; defer { sending = false }
        do {
            _ = try await state.api.request("POST", "/api/v1/approvals/" + id, body: JSONSerialization.data(withJSONObject: ["decision": decision]), expectedNamespace: namespace)
            await refresh();pollID = UUID()
        } catch { self.error = error.localizedDescription }
    }
    private func cancel(_ turn: StoredChatTurn) async {
        guard let namespace else { return }
        do { _ = try await state.api.request("POST", "/api/v1/tasks/" + turn.task_id + "/cancel", expectedNamespace: namespace); await refresh() }
        catch { self.error = error.localizedDescription }
    }
    private func voiceInput() async {
        guard let namespace else { return }
        do {
            if !voice.recording { try await voice.start(); return }
            let audio = try voice.finish();sending = true
            defer { sending = false }
            let body = try JSONSerialization.data(withJSONObject: ["client_key": UUID().uuidString, "content_base64": audio.base64EncodedString()])
            let created = try JSONDecoder().decode(TaskCreated.self, from: await state.api.request("POST", "/api/v1/input/voice", body: body, expectedNamespace: namespace))
            let deadline = Date().addingTimeInterval(180)
            while Date() < deadline {
                let result = try JSONDecoder().decode(TaskResult.self, from: await state.api.request("GET", "/api/v1/tasks/" + created.id, expectedNamespace: namespace))
                if result.status == "SUCCEEDED" { input = Self.answer(result.result) ?? ""; return }
                if ["FAILED", "CANCELED"].contains(result.status) { throw APIClient.APIError.message(result.error ?? "语音输入未完成") }
                try await Task.sleep(for: .seconds(1))
            }
            throw APIClient.APIError.message("语音处理尚未完成，请稍后重试")
        } catch { self.error = error.localizedDescription }
    }
    static func sources(_ result: JSONValue?) -> [RecordReference] {
        guard case .object(let object) = result, case .array(let values) = object["sources"] else { return [] }
        return values.compactMap { value in
            guard case .object(let fields) = value, case .string(let identifier) = fields["record_id"], UUID(uuidString: identifier) != nil else { return nil }
            let version: Int?
            if case .number(let number) = fields["version"] { version = Int(exactly: number) } else { version = nil }
            return RecordReference(id: identifier, title: fields["title"]?.description ?? "资料", version: version)
        }
    }

    static func answer(_ result: JSONValue?) -> String? {
        if case .object(let fields) = result, case .string(let text) = fields["text"] { return text }
        if case .object(let fields) = result, fields["audio_base64"] != nil { return "语音已生成。请在语音朗读页面播放。" }
        if case .object(let object) = result,
           case .array(let choices) = object["choices"], case .object(let first) = choices.first,
           case .object(let message) = first["message"], case .string(let content) = message["content"] { return content }
        return result?.description
    }
}
