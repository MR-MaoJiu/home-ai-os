import SwiftUI

struct ChatWebSource: Codable { let title: String; let url: String }
struct ChatRecordSource: Codable { let record_id: String; let title: String; let version: Int? }
struct StoredChatTurn: Codable, Identifiable {
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
    let etag: String?
}
struct StoredConversationUpdates: Decodable {
    let turns: [StoredChatTurn]
    let etag: String
    let reset: Bool
    let unchanged: Bool
}
struct ChatSnapshot: Codable {
    let conversationID: String
    let ownerNamespace: String
    let turns: [StoredChatTurn]
    let before: Int?
    let etag: String?
    let after: Int

    /// 更新相同轮次可替换处理中状态和已撤权的旧回答，不重复添加消息。
    static func merge(_ current: [StoredChatTurn], updates: [StoredChatTurn]) -> [StoredChatTurn] {
        var values = Dictionary(current.map { ($0.id, $0) }, uniquingKeysWith: { _, newest in newest })
        for turn in updates { values[turn.id] = turn }
        return values.values.sorted { $0.sequence < $1.sequence }
    }
    var bounded: ChatSnapshot {
        let recent = Array(turns.suffix(200))
        return ChatSnapshot(conversationID: conversationID, ownerNamespace: ownerNamespace, turns: recent,
                            before: recent.count < turns.count ? recent.first?.sequence : before,
                            etag: etag, after: after)
    }
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
    @State private var turns: [StoredChatTurn] = []
    @State private var pendingApprovals: [ApprovalEntry] = []
    @State private var olderRun: UUID?
    @State private var etag: String?
    @State private var after = 0
    @State private var defaultResolved = false
    @State private var notice: String?
    @State private var refreshRun: UUID?
    @State private var refreshRequested = false
    @State private var trailingRefresh: Task<Void, Never>?
    @State private var isVisible = true
    @State private var viewGeneration = UUID()
    @State private var bootstrapRun: UUID?
    @State private var sending = false
    @State private var error: String?
    @State private var before: Int?
    @State private var pollID = UUID()
    @State private var voice = VoiceCapture()
    @State private var router = IntentRouter.shared
    private var pendingKey: String { "chat.pending:" + ownerNamespace }
    private let cacheKey = "chat.default"
    private var loading: Bool { refreshRun != nil }
    private var loadingOlder: Bool { olderRun != nil }
    private var running: Bool { turns.contains { $0.pending } }

    var body: some View {
        VStack(spacing: 0) {
            if let error { Text(error).foregroundStyle(.red).font(.caption).padding() }
            if let notice { Text(notice).foregroundStyle(.secondary).font(.caption).padding(.horizontal) }
            if (loading || bootstrapRun != nil) && turns.isEmpty { ProgressView("正在读取服务端会话历史…").padding() }
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 18) {
                        if let before { Button(loadingOlder ? "正在加载…" : "加载更早消息") { Task { await loadOlder(before) } }.disabled(loadingOlder) }
                        if turns.isEmpty && !loading && bootstrapRun == nil { ContentUnavailableView("你的家庭助手", systemImage: "house.and.flag", description: Text("直接提问即可。服务端保存对话，并按需要检索资料、联网搜索和调用工具。")) }
                        ForEach(turns) { turn in turnCard(turn).id(turn.id) }
                        let visibleIDs = Set(turns.flatMap { $0.approvals.map(\.id) })
                        ForEach(pendingApprovals.filter { !visibleIDs.contains($0.id) }) { approval in approvalCard(approval) }
                    }.padding()
                }
                .onChange(of: turns.last?.id) { _, _ in if let last = turns.last { proxy.scrollTo(last.id, anchor: .bottom) } }
            }
            HStack(alignment: .bottom) {
                Button { Task { await voiceInput() } } label: { Image(systemName: voice.recording ? "stop.circle.fill" : "mic.circle").font(.title) }
                    .disabled(sending || namespace == nil || running)
                    .accessibilityLabel(voice.recording ? "结束录音" : "语音输入")
                TextField("问问 Home AI", text: $input, axis: .vertical).disabled(sending || voice.recording).lineLimit(1...6).padding(12).background(.quaternary, in: RoundedRectangle(cornerRadius: 18))
                Button { Task { await send() } } label: { Image(systemName: "arrow.up.circle.fill").font(.largeTitle) }
                    .disabled(sending || running || namespace == nil || ownerNamespace.isEmpty || input.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                    .accessibilityLabel("发送消息")
            }.padding()
        }
        .navigationTitle("Home AI")
        .onAppear { isVisible = true; viewGeneration = UUID() }
        .onDisappear {
            isVisible = false; viewGeneration = UUID(); bootstrapRun = nil
            refreshRequested = false; refreshRun = nil; olderRun = nil
            trailingRefresh?.cancel(); trailingRefresh = nil
        }
        .toolbar {
            ToolbarItem(placement: .topBarTrailing) {
                HStack(spacing: 6) {
                    Circle().fill(state.connected && state.serverReachable ? Color.green : Color.gray).frame(width: 7, height: 7)
                    Text(state.connected ? (state.serverReachable ? "在线" : "未连接") : "未配对").font(.caption)
                }.accessibilityElement(children: .combine).accessibilityIdentifier("serverConnectionStatus")
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
        .onChange(of: state.dataEventRevision) { _, _ in Task { await refresh() } }
        .onChange(of: router.conversationID) { _, id in
            guard id != nil else { return }
            handleConversationRoute()
            Task { await refresh() }
        }
        .onChange(of: state.serverReachable) { _, online in
            if online && bootstrapRun == nil { Task { await bootstrap() } }
        }
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
        guard state.connected else { clearView(); return }
        let run = UUID(); bootstrapRun = run; error = nil
        defer { if bootstrapRun == run { bootstrapRun = nil } }
        // 先读取与已配对身份绑定的加密快照，网络重连不会清空现有对话。
        if let local = await state.api.cachedNamespace() {
            guard !Task.isCancelled, bootstrapRun == run else { return }
            if namespace != local { clearView(); namespace = local; bootstrapRun = run }
            if turns.isEmpty, let bytes = await ClientViewCache.shared.read(key: cacheKey, namespace: local),
               let snapshot = try? JSONDecoder().decode(ChatSnapshot.self, from: bytes), !Task.isCancelled, bootstrapRun == run, namespace == local {
                selected = snapshot.conversationID; ownerNamespace = snapshot.ownerNamespace
                turns = snapshot.turns; before = snapshot.before; etag = snapshot.etag; after = snapshot.after
                restorePendingDraft()
            }
        }
        do {
            let current = try await state.api.syncNamespace()
            let owner = try await state.api.ownerIdentity(expectedNamespace: current)
            guard !Task.isCancelled, bootstrapRun == run else { return }
            if namespace != current || (!ownerNamespace.isEmpty && ownerNamespace != owner.namespace) { clearView(); bootstrapRun = run }
            namespace = current; ownerNamespace = owner.namespace
            let identifier = try await defaultConversation(namespace: current)
            guard !Task.isCancelled, namespace == current, bootstrapRun == run else { return }
            if selected != identifier { turns = []; before = nil; etag = nil; after = 0; input = "" }
            selected = identifier; defaultResolved = true
            handleConversationRoute(); restorePendingDraft()
            await refresh()
            if running { pollID = UUID() }
        } catch {
            guard !Task.isCancelled, bootstrapRun == run else { return }
            await handleFailure(error)
        }
    }
    private func clearView() {
        namespace = nil; ownerNamespace = ""; selected = nil; turns = []; pendingApprovals = []
        before = nil; etag = nil; after = 0; defaultResolved = false
        bootstrapRun = nil; input = ""; notice = nil
        refreshRun = nil; olderRun = nil; refreshRequested = false
        trailingRefresh?.cancel(); trailingRefresh = nil
    }
    private func defaultConversation(namespace: String) async throws -> String {
        struct Created: Decodable { let id: String }
        return try JSONDecoder().decode(Created.self, from: await state.api.request("POST", "/api/v1/conversations/default", expectedNamespace: namespace)).id
    }
    private func handleConversationRoute() {
        guard let requested = router.conversationID, let selected else { return }
        if requested != selected { notice = "这条通知关联的历史对话仍保存在服务器；当前继续使用你的固定对话。" }
        router.conversationID = nil
    }
    private func restorePendingDraft() {
        guard !ownerNamespace.isEmpty, let selected,
              let data = DeviceIdentity.read(pendingKey), let pending = try? JSONDecoder().decode(PendingChatSend.self, from: data), pending.conversationID == selected else { return }
        if turns.contains(where: { $0.client_key == pending.clientKey }) { try? DeviceIdentity.save(Data(), name: pendingKey) }
        else if input.isEmpty { input = pending.content }
    }
    private func saveCache(namespace: String) async {
        guard isVisible, !Task.isCancelled, state.connected, self.namespace == namespace, let selected, !ownerNamespace.isEmpty else { return }
        let revision = state.connectionRevision, generation = viewGeneration
        let snapshot = ChatSnapshot(conversationID: selected, ownerNamespace: ownerNamespace, turns: turns, before: before, etag: etag, after: after).bounded
        if let data = try? JSONEncoder().encode(snapshot) {
            try? await ClientViewCache.shared.write(data, key: cacheKey, namespace: namespace)
            if !isVisible || viewGeneration != generation || !state.connected || state.connectionRevision != revision || self.namespace != namespace {
                await ClientViewCache.shared.remove(key: cacheKey, namespace: namespace)
            }
        }
    }
    private func isCurrent(namespace: String, revision: UUID, generation: UUID) -> Bool {
        isVisible && viewGeneration == generation && state.connectionRevision == revision
            && self.namespace == namespace && state.connected && !Task.isCancelled
    }
    private func scheduleTrailingRefresh(namespace: String, revision: UUID, generation: UUID) {
        guard refreshRequested, !loading, !loadingOlder else { return }
        refreshRequested = false
        guard isVisible, viewGeneration == generation, state.connectionRevision == revision,
              self.namespace == namespace, state.connected else { return }
        trailingRefresh = Task {
            guard isCurrent(namespace: namespace, revision: revision, generation: generation) else { return }
            await refresh()
        }
    }
    private func fullPage(_ identifier: String, namespace: String) async throws {
        let revision = state.connectionRevision, generation = viewGeneration
        let data = try await state.api.request("GET", "/api/v1/conversations/" + identifier, expectedNamespace: namespace)
        let page = try JSONDecoder().decode(StoredConversationPage.self, from: data)
        guard isCurrent(namespace: namespace, revision: revision, generation: generation), selected == identifier else { return }
        turns = page.turns; before = page.has_more ? page.next_before : nil
        etag = page.etag; after = page.turns.last?.sequence ?? 0
    }
    private func refresh() async {
        guard isVisible, !Task.isCancelled, defaultResolved, let namespace, let identifier = selected else { return }
        if loading || loadingOlder { refreshRequested = true; return }
        let run = UUID(), revision = state.connectionRevision, generation = viewGeneration
        refreshRun = run
        defer {
            if refreshRun == run {
                refreshRun = nil
                scheduleTrailingRefresh(namespace: namespace, revision: revision, generation: generation)
            }
        }
        var readingConversation = true
        var needsCacheWrite = true
        do {
            if let etag {
                var components = URLComponents()
                components.path = "/api/v1/conversations/" + identifier + "/updates"
                components.queryItems = [URLQueryItem(name: "after", value: String(after)), URLQueryItem(name: "etag", value: etag)]
                let bytes = try await state.api.request("GET", components.string!, expectedNamespace: namespace)
                let update = try JSONDecoder().decode(StoredConversationUpdates.self, from: bytes)
                guard isCurrent(namespace: namespace, revision: revision, generation: generation), selected == identifier else { return }
                if update.reset {
                    // 来源权限变化时必须先清除旧答案，即使重新读取随后断网也不能继续显示。
                    turns = []; pendingApprovals = []; before = nil; self.etag = nil; after = 0
                    await ClientViewCache.shared.remove(key: cacheKey, namespace: namespace)
                    guard isCurrent(namespace: namespace, revision: revision, generation: generation) else { return }
                    try await fullPage(identifier, namespace: namespace)
                } else {
                    needsCacheWrite = update.etag != etag || !update.turns.isEmpty
                    turns = ChatSnapshot.merge(turns, updates: update.turns)
                    self.etag = update.etag; after = turns.last?.sequence ?? after
                }
            } else { try await fullPage(identifier, namespace: namespace) }
            guard isCurrent(namespace: namespace, revision: revision, generation: generation), selected == identifier else { return }
            restorePendingDraft()
            if needsCacheWrite { await saveCache(namespace: namespace) }
            readingConversation = false
            let pending = try await state.api.request("GET", "/api/v1/approvals", expectedNamespace: namespace)
            guard isCurrent(namespace: namespace, revision: revision, generation: generation) else { return }
            pendingApprovals = try JSONDecoder().decode([ApprovalEntry].self, from: pending)
            error = nil
        } catch {
            guard isCurrent(namespace: namespace, revision: revision, generation: generation) else { return }
            if readingConversation, case APIClient.APIError.http(404, _) = error {
                // 会话被服务器明确删除后重新获取固定入口，不在手机上生成另一会话。
                turns = []; before = nil; etag = nil; after = 0; pendingApprovals = []
                await ClientViewCache.shared.remove(key: cacheKey, namespace: namespace)
                guard isCurrent(namespace: namespace, revision: revision, generation: generation) else { return }
                do {
                    let replacement = try await defaultConversation(namespace: namespace)
                    guard isCurrent(namespace: namespace, revision: revision, generation: generation) else { return }
                    selected = replacement
                    try await fullPage(replacement, namespace: namespace)
                    guard isCurrent(namespace: namespace, revision: revision, generation: generation) else { return }
                    await saveCache(namespace: namespace)
                    self.error = nil
                } catch {
                    guard isCurrent(namespace: namespace, revision: revision, generation: generation) else { return }
                    await handleFailure(error)
                }
            } else { await handleFailure(error, conversationAccess: readingConversation) }
        }
    }
    private func handleFailure(_ failure: Error, conversationAccess: Bool = true) async {
        guard !Task.isCancelled else { return }
        if case APIClient.APIError.http(let status, _) = failure {
            if status == 401 {
                if let namespace { await ClientViewCache.shared.invalidate(namespace: namespace) }
                clearView()
            } else if status == 403 && conversationAccess {
                if let namespace { await ClientViewCache.shared.remove(key: cacheKey, namespace: namespace) }
                clearView()
            }
        }
        self.error = failure.localizedDescription
    }
    private func loadOlder(_ sequence: Int) async {
        guard isVisible, !Task.isCancelled, !loading, !loadingOlder, let selected, let namespace else { return }
        let run = UUID(), revision = state.connectionRevision, generation = viewGeneration
        olderRun = run; refreshRequested = true
        defer {
            if olderRun == run {
                olderRun = nil
                scheduleTrailingRefresh(namespace: namespace, revision: revision, generation: generation)
            }
        }
        do {
            let page = try JSONDecoder().decode(StoredConversationPage.self, from: await state.api.request("GET", "/api/v1/conversations/" + selected + "?before=" + String(sequence), expectedNamespace: namespace))
            guard isCurrent(namespace: namespace, revision: revision, generation: generation), self.selected == selected else { return }
            turns = ChatSnapshot.merge(turns, updates: page.turns)
            before = page.has_more ? page.next_before : nil
            await saveCache(namespace: namespace)
        } catch {
            guard isCurrent(namespace: namespace, revision: revision, generation: generation) else { return }
            await handleFailure(error)
        }
    }
    private func send() async {
        guard isVisible, !Task.isCancelled, let namespace, !ownerNamespace.isEmpty, !sending else { return }
        let revision = state.connectionRevision, generation = viewGeneration
        sending = true; defer { sending = false }
        let pendingStorage = pendingKey
        do {
            if !defaultResolved {
                let identifier = try await defaultConversation(namespace: namespace)
                guard isCurrent(namespace: namespace, revision: revision, generation: generation) else { return }
                if selected != identifier { turns = []; before = nil; etag = nil; after = 0 }
                selected = identifier; defaultResolved = true
            }
            guard let selected else { return }
            let previous = DeviceIdentity.read(pendingStorage).flatMap { try? JSONDecoder().decode(PendingChatSend.self, from: $0) }
            let pending = previous?.conversationID == selected && previous?.content == input ? previous! : PendingChatSend(conversationID: selected, clientKey: UUID().uuidString, content: input, timezone: TimeZone.current.identifier)
            try DeviceIdentity.save(JSONEncoder().encode(pending), name: pendingStorage)
            let body = try JSONSerialization.data(withJSONObject: ["client_key": pending.clientKey, "content": pending.content, "timezone": pending.timezone])
            let bytes = try await state.api.request("POST", "/api/v1/conversations/" + selected + "/messages", body: body, expectedNamespace: namespace)
            let received = try JSONDecoder().decode(StoredChatTurn.self, from: bytes)
            try DeviceIdentity.save(Data(), name: pendingStorage)
            guard isCurrent(namespace: namespace, revision: revision, generation: generation), self.selected == selected else { return }
            input = ""; turns = ChatSnapshot.merge(turns, updates: [received]); after = max(after, received.sequence); error = nil
            await saveCache(namespace: namespace); pollID = UUID()
        } catch {
            guard isCurrent(namespace: namespace, revision: revision, generation: generation) else { return }
            await handleFailure(error)
        }
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
