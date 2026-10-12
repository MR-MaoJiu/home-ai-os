import SwiftUI

struct ChatWebSource: Codable { let title: String; let url: String }
struct ChatRecordSource: Codable { let record_id: String; let title: String; let version: Int?; var task_id: String? = nil }
struct StoredChatTurn: Codable, Identifiable {
    let id: String
    let sequence: Int
    let client_key: String
    let user_text: String
    let task_id: String
    var status: String
    let assistant_text: String
    let error: String?
    var approvals: [ApprovalEntry]
    let web_sources: [ChatWebSource]
    let sources: [ChatRecordSource]
    var user_parts: [ChatPart]? = nil
    var assistant_parts: [ChatPart]? = nil
    var mentions: [ChatMention]? = nil
    var references: [ChatDataReference]? = nil
    var client_actions: [ClientActionRequest]? = nil
    var requires_online_revalidation: Bool? = nil
    var result_redacted: Bool? = nil
    var budget_recoverable: Bool? = nil
    var interaction_kind: String? = nil
    var acknowledgement: String? = nil
    var created_at: Double? = nil
    var completed_at: Double? = nil
    var display_revision: Int? = nil
    var hasTaskDetails: Bool { interaction_kind == "task" || error != nil || !(client_actions ?? []).isEmpty || !approvals.isEmpty || ["WAITING_CLIENT", "WAITING_PRIVACY", "WAITING_BUDGET", "AWAITING_APPROVAL", "FAILED", "NEEDS_RECONCILIATION"].contains(status) }
    func acknowledgingApproval(_ id: String, status: String) -> Self {
        guard ["APPROVED", "CANCELED"].contains(status), approvals.contains(where: { $0.id == id }) else { return self }
        var value = self
        value.approvals.removeAll { $0.id == id }
        if value.status == "AWAITING_APPROVAL" { value.status = status }
        return value
    }
    /// 文本和卡片同时展示；只折叠与完整回答逐字相同的文本段，不吞掉不同内容。
    var displayedAssistantParts: [ChatPart] {
        let parts = assistant_parts ?? []
        guard !assistant_text.isEmpty else { return parts }
        if parts.contains(where: { $0.type == "text" && $0.text == assistant_text }) {
            var keptAnswer = false
            return parts.filter { part in
                guard part.type == "text", part.text == assistant_text else { return true }
                guard !keptAnswer else { return false }
                keptAnswer = true
                return true
            }
        }
        var answer = ChatPart(type: "text")
        answer.text = assistant_text
        return [answer] + parts
    }
    func cached(now: Date = Date()) -> StoredChatTurn {
        let temporary = sources.contains { $0.task_id != nil } || (assistant_parts ?? []).contains { ["image", "video", "file"].contains($0.type) && $0.task_id != nil }
        let consentExpired = (client_actions ?? []).contains { $0.kind == "cloud.disclose" && (($0.status == "RESPONDED" && $0.expires_at <= now.timeIntervalSince1970) || $0.status == "REVOKED") }
        guard temporary || consentExpired || requires_online_revalidation == true else { return self }
        return StoredChatTurn(id: id, sequence: sequence, client_key: client_key, user_text: user_text, task_id: task_id, status: status,
            assistant_text: "这轮内容需要联网重新确认授权。", error: nil, approvals: [], web_sources: [], sources: [], user_parts: user_parts,
            assistant_parts: [], mentions: mentions, references: nil, client_actions: [], requires_online_revalidation: true, budget_recoverable: budget_recoverable, interaction_kind: interaction_kind, acknowledgement: acknowledgement, created_at: created_at, completed_at: completed_at, display_revision: display_revision)
    }
    var needsLiveContentAccess: Bool { sources.contains { $0.task_id != nil } || (assistant_parts ?? []).contains { $0.task_id != nil } || (client_actions ?? []).contains { $0.kind == "cloud.disclose" && ["RESPONDED", "REVOKED"].contains($0.status) } }
    var contentExpiresAt: Double? { (client_actions ?? []).filter { $0.kind == "cloud.disclose" && $0.status == "RESPONDED" }.map(\.expires_at).min() }
    var blocksComposer: Bool { ["RECEIVED", "EXECUTING", "APPROVED"].contains(status) }
    var pending: Bool { !["SUCCEEDED", "FAILED", "CANCELED", "NEEDS_RECONCILIATION"].contains(status) }
    func awaitsPersonalClarification(at date: Date = Date()) -> Bool {
        status == "WAITING_CLIENT" && (client_actions ?? []).contains { $0.awaitsPersonalClarification(for: task_id, at: date) }
    }
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
    var snapshot: StoredConversationPage? = nil
    var reset_reason: String? = nil
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
        for turn in updates {
            // 发送回执、事件刷新和历史分页可能乱序返回；按服务端版本接收，不能按状态大小猜测。
            if let currentRevision = values[turn.id]?.display_revision,
               let incomingRevision = turn.display_revision, incomingRevision < currentRevision { continue }
            values[turn.id] = turn
        }
        return values.values.sorted { $0.sequence < $1.sequence }
    }
    var bounded: ChatSnapshot {
        let recent = Array(turns.suffix(200)).map { $0.cached() }
        return ChatSnapshot(conversationID: conversationID, ownerNamespace: ownerNamespace, turns: recent,
                            before: recent.count < turns.count ? recent.first?.sequence : before,
                            etag: recent.contains(where: { $0.requires_online_revalidation == true }) ? nil : etag, after: after)
    }
}
struct PendingChatSend: Codable {
    var conversationID: String
    let clientKey: String
    let content: String
    let timezone: String
    var schemaVersion: String? = nil
    var parts: [ChatPart]? = nil
    var mentions: [ChatMention]? = nil
    var references: [ChatDataReference]? = nil
    var mediaIDs: [String]? = nil
    var clientContext: ClientContextSnapshot? = nil
    var replyToTaskID: String? = nil
}

/// 几何观察回调只修改此对象的内部字段，不能在同一布局帧里写 SwiftUI State。
@MainActor
final class ChatScrollGeometryCoordinator {
    private var pending: Task<Void, Never>?
    private var generation = UUID()

    func schedule(_ action: @escaping @MainActor () -> Void) {
        cancel()
        let current = generation
        pending = Task {
            do { try await Task.sleep(for: .milliseconds(50)) } catch { return }
            guard !Task.isCancelled, current == generation else { return }
            action()
            if current == generation { pending = nil }
        }
    }

    func cancel() {
        generation = UUID()
        pending?.cancel(); pending = nil
    }
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
    @State private var hasLoadedHistory = false
    @State private var notice: String?
    @State private var refreshRun: UUID?
    @State private var refreshRequested = false
    @State private var trailingRefresh: Task<Void, Never>?
    @State private var isVisible = true
    @State private var viewGeneration = UUID()
    @State private var bootstrapRun: UUID?
    @State private var bootstrapConnectionRevision: UUID?
    @State private var sending = false
    @State private var error: String?
    @State private var before: Int?
    @State private var pollID = UUID()
    @State private var outbox = ChatOutbox.shared
    @State private var voice = VoiceCapture()
    @State private var answerPlayback = SpeechPlayback(api: AppServices.api)
    @State private var automaticReview = AutomaticReviewStore()
    @State private var automaticCapture = AutomaticReviewCapture()
    @State private var automaticReviewPollID = UUID()
    @State private var readingMessageID: String?
    @State private var readingContext: ChatSpeechValidation?
    @AppStorage(ChatAutomaticSpeech.preferenceKey) private var automaticReading = true
    @State private var automaticSpeech = ChatAutomaticSpeech()
    @State private var automaticQueue: [ChatAutomaticSpeech.Segment] = []
    @State private var automaticMessageID: String?
    @State private var speechPrimed = false
    @State private var speechRevalidating = false
    @State private var followLatest = true
    @State private var nearBottom = true
    @State private var scrollIsIdle = true
    @State private var scrollRevision = UUID()
    @State private var scrollGeometry = ChatScrollGeometryCoordinator()
    @State private var lastScrollLayout = CGSize.zero
    @State private var router = IntentRouter.shared
    @State private var draft = MediaDraft()
    @State private var mentions: [ChatMention] = []
    @State private var references: [ChatDataReference] = []
    @State private var dataPicker: ChatDataPickerRequest?
    @State private var directory = MemberDirectoryStore()
    @State private var clientActions = ClientActionStore()
    @State private var choosingMembers = false
    @State private var showingRequests = false
    @State private var replyToTaskID: String?
    @State private var lastApprovalRefresh = Date.distantPast
    @State private var handledApprovals: Set<String> = []
    private var pendingKey: String { "chat.pending:" + ownerNamespace }
    private let cacheKey = "chat.default"
    private var loading: Bool { refreshRun != nil }
    private var loadingOlder: Bool { olderRun != nil }
    private var running: Bool { turns.contains { $0.blocksComposer } }
    private var canSend: Bool {
        !sending && outbox.entries.count < 20 && !voice.recording && namespace != nil && !ownerNamespace.isEmpty &&
        (!input.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || !draft.items.isEmpty || !references.isEmpty) &&
        draft.items.allSatisfy { $0.asset != nil }
    }

    var body: some View {
        VStack(spacing: 0) {
            if let error { Text(error).foregroundStyle(.red).font(.caption).padding() }
            if let notice { Text(notice).foregroundStyle(.secondary).font(.caption).padding(.horizontal) }
            if !outbox.entries.isEmpty {
                ScrollView(.horizontal) {
                    HStack {
                        ForEach(outbox.entries) { entry in
                            VStack(alignment: .leading, spacing: 4) {
                                Text(entry.message.content.isEmpty ? "附件消息" : entry.message.content).lineLimit(2)
                                Text(outbox.sendingID == entry.id ? "正在发送" : entry.error ?? "已保存，等待发送").font(.caption).foregroundStyle(.secondary)
                                HStack {
                                    if entry.blocked { Button("重试") { try? outbox.retry(entry.id) } }
                                    Button(entry.uncertain ? "停止重试" : "移除待发项") { try? outbox.remove(entry.id) }.disabled(outbox.sendingID == entry.id)
                                }.font(.caption)
                                if entry.uncertain { Text("若已送达，服务端任务仍会继续。").font(.caption2).foregroundStyle(.secondary) }
                            }.padding(10).frame(width: 240, alignment: .leading).background(.teal.opacity(0.08), in: RoundedRectangle(cornerRadius: 12))
                        }
                    }.padding(.horizontal)
                }.frame(maxHeight: 140)
            }
            if !hasLoadedHistory && (loading || bootstrapRun != nil) && turns.isEmpty { ProgressView("正在读取服务端会话历史…").padding() }
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 18) {
                        if let before { Button(loadingOlder ? "正在加载…" : "加载更早消息") { Task { await loadOlder(before) } }.disabled(loadingOlder) }
                        if turns.isEmpty && (hasLoadedHistory || (!loading && bootstrapRun == nil)) { ContentUnavailableView("你的家庭助手", systemImage: "house.and.flag", description: Text("直接提问即可。服务端保存对话，并按需要检索资料、联网搜索和调用工具。")) }
                        ForEach(timeline) { item in
                            if item.answer { answerBubble(item.turn).id(item.id) }
                            else { turnCard(item.turn).id(item.turn.id) }
                        }
                        let visibleIDs = Set(turns.flatMap { $0.approvals.map(\.id) })
                        let otherApprovals = pendingApprovals.filter { !visibleIDs.contains($0.id) && !handledApprovals.contains($0.id) }
                        let visibleActions = Set(turns.flatMap { ($0.client_actions ?? []).map(\.id) })
                        let otherActions = clientActions.items.filter { $0.pending && !visibleActions.contains($0.id) }
                        if !otherApprovals.isEmpty || !otherActions.isEmpty {
                            DisclosureGroup("有任务需要你处理") {
                                ForEach(otherApprovals) { approval in approvalCard(approval) }
                                ForEach(otherActions) { ClientActionCard(action: $0).id($0.id) }
                            }
                        }
                        Color.clear.frame(height: 1).id("chat-bottom")
                    }.padding([.top, .horizontal])
                }.scrollDismissesKeyboard(.interactively)
                .defaultScrollAnchor(.bottom, for: .initialOffset)
                .onScrollGeometryChange(for: ChatScrollMetrics.self) { geometry in
                    ChatScrollMetrics(contentHeight: geometry.contentSize.height.rounded(), viewportHeight: geometry.containerSize.height.rounded(),
                        bottomDistance: max(0, geometry.contentSize.height + geometry.contentInsets.bottom - geometry.visibleRect.maxY).rounded())
                } action: { _, new in
                    scheduleScrollGeometry(new)
                }
                .onScrollPhaseChange { _, phase in
                    scrollIsIdle = phase == .idle
                    if phase == .interacting { followLatest = false }
                    else if phase == .idle && nearBottom { followLatest = true }
                }
                .onChange(of: timeline.map(\.displayRevision)) { _, _ in
                    if followLatest && !loadingOlder { scrollRevision = UUID() }
                }
                .task(id: scrollRevision) {
                    guard followLatest, !loadingOlder else { return }
                    // 等待流式 Markdown 与输入框完成布局，再滚到专用尾部锚点。
                    do {
                        try await Task.sleep(for: .milliseconds(40))
                        guard followLatest else { return }
                        proxy.scrollTo("chat-bottom", anchor: .bottom)
                    } catch { }
                }
                .overlay(alignment: .bottomTrailing) {
                    if !followLatest && !nearBottom {
                        Button { followLatest = true; scrollRevision = UUID() } label: { Label("最新消息", systemImage: "arrow.down") }
                            .font(.caption).buttonStyle(.borderedProminent).tint(.teal).padding(12)
                            .accessibilityIdentifier("chatScrollToLatest")
                    }
                }
            }
            ChatComposerBar(input: $input, draft: draft,
                mentions: mentions.map { ChatComposerMention(id: $0.member_id, name: directory.name($0.member_id)) },
                references: references,
                replyTitle: replyToTaskID == nil ? nil : "关联任务继续对话",
                busy: sending, canSend: canSend, voiceRecording: voice.recording,
                canRecord: !sending && namespace != nil, expectedNamespace: namespace,
                onMention: { choosingMembers = true },
                onReference: { if let namespace { dataPicker = ChatDataPickerRequest(namespace: namespace, revision: state.connectionRevision) } },
                onInbox: { showingRequests = true },
                onSend: { Task { await send() } }, onVoice: { Task { await voiceInput() } },
                onRemoveMention: { id in mentions.removeAll { $0.member_id == id } },
                onRemoveReference: { id in references.removeAll { $0.id == id } },
                onCancelReply: { replyToTaskID = nil },
                autoReviewEnabled: automaticReview.enabled,
                autoReviewUpdating: automaticReview.updating,
                autoReviewAvailable: automaticReviewOnline && automaticReview.verified,
                onAutoReviewChange: changeAutomaticReview)
                .padding(.horizontal, 14).padding(.top, 8).padding(.bottom, 10)
            if let reviewMessage = automaticReview.message ?? automaticCapture.message {
                Text(verbatim: reviewMessage).font(.caption).foregroundStyle(.orange)
                    .frame(maxWidth: .infinity, alignment: .leading).padding(.horizontal, 20).padding(.bottom, 4)
            }
        }
        .navigationTitle("Home AI")
        .overlay(alignment: .topTrailing) {
            if state.androidPresentation.mode == .inline {
                AndroidMiniPlayer(session: state.androidPresentation).padding(.top, 8).padding(.trailing, 12)
            }
        }
        .sheet(isPresented: $choosingMembers) { MemberDirectoryView(selected: $mentions, directory: directory) }
        .sheet(item: $dataPicker) { request in
            ChatDataReferencePicker(selected: $references, request: request, attachmentCount: draft.items.count)
        }
        .navigationDestination(isPresented: $showingRequests) { ClientActionInbox(notificationID: nil) }
        .task(id: state.connectionRevision) { await directory.load(api: state.api); await clientActions.load(api: state.api) }
        .onAppear { isVisible = true; speechPrimed = false; followLatest = true; scrollRevision = UUID(); viewGeneration = UUID(); state.androidPresentation.setChatVisible(true) }
        .task(id: "auto-review-\(namespace ?? "")-\(state.connectionRevision)-\(automaticReviewOnline)-\(automaticReviewPollID)") {
            automaticCapture.stop()
            automaticReview.suspend(clearIdentity: automaticReview.namespace != namespace)
            guard automaticReviewOnline, let namespace else { return }
            while !Task.isCancelled && automaticReviewOnline && self.namespace == namespace {
                await automaticReview.load(namespace: namespace) { method, body in
                    try await state.api.request(method, "/api/v1/devices/current/auto-review", body: body, expectedNamespace: namespace)
                }
                guard !Task.isCancelled else { return }
                if automaticReview.permitsCapture(namespace: namespace) {
                    await clientActions.load(api: state.api)
                    processAutomaticCaptures()
                }
                do { try await Task.sleep(for: .seconds(automaticReview.enabled ? 3 : 15)) } catch { return }
            }
        }
        .onChange(of: clientActions.items) { _, _ in processAutomaticCaptures() }
        .onDisappear {
            automaticCapture.stop(); automaticReview.suspend()
            state.androidPresentation.setChatVisible(false)
            isVisible = false; viewGeneration = UUID(); bootstrapRun = nil
            speechPrimed = false
            scrollGeometry.cancel()
            refreshRequested = false; refreshRun = nil; olderRun = nil
            trailingRefresh?.cancel(); trailingRefresh = nil
            hideTransientContent()
        }
        .toolbar {
            ToolbarItem(placement: .topBarTrailing) {
                Button { Task { await VoiceCalls.shared.start() } } label: { Image(systemName: "phone.fill") }
                    .disabled(!state.connected).accessibilityLabel("与 Home AI 语音通话")
            }
            ToolbarItem(placement: .topBarTrailing) {
                Button { state.androidPresentation.showInline() } label: {
                    Image(systemName: "pip").frame(width: 44, height: 44)
                }.disabled(!state.connected).accessibilityLabel("查看安卓小窗")
            }
            ToolbarItem(placement: .topBarTrailing) {
                HStack(spacing: 6) {
                    Circle().fill(state.connected && state.serverReachable ? Color.green : Color.gray).frame(width: 7, height: 7)
                    Text(state.connected ? (state.serverReachable ? "在线" : "未连接") : "未配对").font(.caption)
                }.accessibilityElement(children: .combine).accessibilityIdentifier("serverConnectionStatus")
            }
        }
        .task(id: "\(state.connected)-\(state.connectionRevision)") { await bootstrap() }
        .task(id: "\(pollID)-\(running)-\(scenePhase)-\(isVisible)") {
            // 服务端恢复等待中的任务时，也要继续读取流式片段，不能只依赖发送按钮启动轮询。
            while !Task.isCancelled && scenePhase == .active && isVisible {
                await refresh()
                if !running { break }
                try? await Task.sleep(for: .milliseconds(350))
            }
        }
        .task(id: "outbox-\(ownerNamespace)-\(outbox.entries.isEmpty)-\(scenePhase)") {
            while !Task.isCancelled && scenePhase == .active && !outbox.entries.isEmpty {
                await drainOutbox()
                do { try await Task.sleep(for: .seconds(2)) } catch { return }
            }
        }
        .onChange(of: scenePhase) { _, phase in
            if phase != .active { stopReading(); speechPrimed = false }
            if phase == .active { Task { await bootstrap() } }
            else if phase == .background { hideTransientContent() }
        }
        .onChange(of: state.taskEventRevision) { _, _ in Task { await refresh(); await clientActions.load(api: state.api) } }
        .onChange(of: state.dataEventRevision) { _, _ in pauseReadingForSourceRefresh(); Task { await refresh() } }
        .onChange(of: state.memoryEventRevision) { _, _ in pauseReadingForSourceRefresh(); Task { await refresh() } }
        .onChange(of: automaticReading) { _, enabled in
            if !enabled { stopReading() }
            automaticSpeech.baseline(turns)
        }
        .onChange(of: VoiceCalls.shared.activeID) { _, id in if id != nil { stopReading() } }
        .onChange(of: answerPlayback.busy) { old, busy in
            if old && !busy {
                if let id = automaticMessageID, answerPlayback.error != nil {
                    automaticSpeech.suppressMessages([id]); automaticQueue.removeAll { $0.messageID == id }
                }
                automaticMessageID = nil
                startNextAutomaticReading()
            }
        }
        .onChange(of: router.conversationID) { _, id in
            guard id != nil else { return }
            handleConversationRoute()
            Task { await refresh() }
        }
        .onChange(of: state.serverReachable) { _, online in
            if online && bootstrapRun == nil {
                Task { if defaultResolved { await refresh() } else { await bootstrap() } }
            } else if !online { hideTransientContent() }
        }
    }

    private var automaticReviewOnline: Bool {
        isVisible && scenePhase == .active && state.connected && state.serverReachable && namespace != nil
    }
    private func changeAutomaticReview(_ enabled: Bool) {
        guard automaticReviewOnline, let namespace, !automaticReview.updating else { return }
        automaticCapture.stop(clearAttempts: true)
        Task {
            await automaticReview.change(enabled: enabled, acknowledged: enabled, namespace: namespace) { method, body in
                try await state.api.request(method, "/api/v1/devices/current/auto-review", body: body, expectedNamespace: namespace)
            }
            guard self.namespace == namespace, automaticReviewOnline else { return }
            automaticReviewPollID = UUID()
            await refresh(); await clientActions.load(api: state.api)
        }
    }
    private func processAutomaticCaptures() {
        guard automaticReviewOnline, let namespace, automaticReview.permitsCapture(namespace: namespace) else { return }
        automaticCapture.process(clientActions.items, namespace: namespace, store: automaticReview, api: state.api,
            isActive: { automaticReviewOnline && self.namespace == namespace },
            didRespond: { state.taskEventRevision = UUID(); await refresh(); await clientActions.load(api: state.api) })
    }

    private var timeline: [ChatTimelineItem] { ChatTimelineItem.ordered(turns) }

    private func scheduleScrollGeometry(_ metrics: ChatScrollMetrics) {
        // 几何回调中不直接改布局状态：合并同一帧及 Markdown 布局产生的多次回调。
        scrollGeometry.schedule {
            guard isVisible else { return }
            let nextNearBottom = metrics.bottomDistance < 80
            if nearBottom != nextNearBottom { nearBottom = nextNearBottom }
            if nextNearBottom && scrollIsIdle && !followLatest { followLatest = true }
            let layout = CGSize(width: metrics.contentHeight, height: metrics.viewportHeight)
            guard followLatest, !loadingOlder, metrics.bottomDistance > 2, lastScrollLayout != layout else { return }
            lastScrollLayout = layout
            scrollRevision = UUID()
        }
    }

    @ViewBuilder private func turnCard(_ turn: StoredChatTurn) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            if !turn.user_text.isEmpty { HStack { Spacer(minLength: 24); ChatMarkdownText(turn.user_text).padding().background(.teal.opacity(0.12), in: RoundedRectangle(cornerRadius: 16)) } }
            if let mentions = turn.mentions, !mentions.isEmpty { Text(mentions.map { "@" + directory.name($0.member_id) }.joined(separator: " ")).font(.caption).foregroundStyle(.teal) }
            if let references = turn.references, !references.isEmpty {
                ForEach(references) { reference in
                    NavigationLink { RecordSourceView(source: reference.source) } label: {
                        Label(reference.title, systemImage: ChatDataReferenceRules.icon(reference.kind))
                            .font(.caption).lineLimit(2)
                    }.buttonStyle(.plain).foregroundStyle(.blue)
                }
            }
            ForEach(Array((turn.user_parts ?? []).filter { $0.type != "text" }.enumerated()), id: \.offset) { _, part in ChatPartView(part: part) }
            if let acknowledgement = turn.acknowledgement, !acknowledgement.isEmpty {
                VStack(alignment: .leading, spacing: 8) {
                    ChatMarkdownText(acknowledgement)
                    if let message = ChatSpeechMessage.messages(in: turn).first(where: { $0.kind == .acknowledgement }) {
                        speechControls(message)
                    }
                }.padding(12).background(.quaternary, in: RoundedRectangle(cornerRadius: 16))
            } else if turn.pending && !turn.hasTaskDetails && turn.assistant_text.isEmpty {
                Text("正在回复…").font(.caption).foregroundStyle(.secondary)
            }
            if turn.hasTaskDetails {
                DisclosureGroup {
                    taskDetails(turn)
                } label: {
                    Label("任务 · " + taskStatusLabel(turn.status), systemImage: "checklist").font(.caption)
                }.padding(12).background(.quaternary, in: RoundedRectangle(cornerRadius: 14))
            }
        }
    }
    @ViewBuilder private func speechControls(_ message: ChatSpeechMessage) -> some View {
        if let namespace {
            let current = readingMessageID == message.id
            let busy = current && answerPlayback.busy
            VStack(alignment: .leading, spacing: 4) {
                HStack(spacing: 8) {
                    Button {
                        if busy { stopReading() }
                        else {
                            stopReading()
                            readingMessageID = message.id; readingContext = message.validation
                            answerPlayback.speakAnswer(message.text, expectedNamespace: namespace,
                                sources: message.sources, context: message.validation)
                        }
                    } label: {
                        Label(busy ? (answerPlayback.playing ? "停止朗读" : "取消朗读") : "朗读",
                            systemImage: busy ? "stop.circle" : "speaker.wave.2")
                    }.buttonStyle(.plain).foregroundStyle(.teal).frame(minHeight: 44)
                        .accessibilityIdentifier("chatRead-" + message.id)
                        .disabled(!busy && (!state.serverReachable || speechRevalidating || voice.recording || VoiceCalls.shared.activeID != nil))
                    if busy {
                        if !answerPlayback.playing { ProgressView().controlSize(.small) }
                        Text(answerPlayback.playing ? "正在朗读" : "正在生成语音").foregroundStyle(.secondary)
                    }
                }.font(.caption)
                if current, let error = answerPlayback.error { Text(error).font(.caption).foregroundStyle(.red) }
            }
        }
    }
    @ViewBuilder private func answerBubble(_ turn: StoredChatTurn) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            ForEach(Array(turn.displayedAssistantParts.filter { !["task", "approval", "choice", "data_request"].contains($0.type) && $0.action_id == nil }.enumerated()), id: \.offset) { _, part in
                ChatPartView(part: part, actions: turn.client_actions ?? [], validationTaskID: turn.needsLiveContentAccess ? turn.task_id : nil, expiresAt: turn.contentExpiresAt)
            }
            if let message = ChatSpeechMessage.messages(in: turn).first(where: { $0.kind == .answer }) {
                speechControls(message)
            }
            ForEach(turn.sources, id: \.record_id) { source in
                NavigationLink(source.title) { RecordSourceView(source: RecordReference(id: source.record_id, title: source.title, version: source.version, taskID: source.task_id)) }.font(.caption)
            }
            if turn.pending { Text("正在生成…").font(.caption).foregroundStyle(.secondary) }
            ChatSearchSources(sources: turn.web_sources)
        }.padding(12).background(.quaternary, in: RoundedRectangle(cornerRadius: 16))
    }
    @ViewBuilder private func taskDetails(_ turn: StoredChatTurn) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            if turn.pending || turn.error != nil || turn.status == "FAILED" {
                let needsClarification = turn.awaitsPersonalClarification()
                ChatInteractionCard(title: needsClarification ? "等待你的补充" : taskStatusLabel(turn.status),
                    icon: needsClarification ? "text.bubble" : turn.status == "WAITING_PRIVACY" ? "hand.raised.fill" : turn.error != nil ? "exclamationmark.circle" : "sparkles",
                    tint: turn.status == "FAILED" ? .red : turn.blocksComposer ? .teal : .orange) {
                    if let problem = turn.error { Text(verbatim: problem).font(.subheadline).textSelection(.enabled) }
                    if needsClarification { Text("在本次请求卡片中填写或选择后，原任务会继续。").font(.subheadline) }
                    if turn.blocksComposer { ProgressView("正在处理你的请求…").font(.subheadline).tint(.teal) }
                    if turn.status == "FAILED" {
                        TaskRetryControl(identifier: turn.task_id) { await refresh(); pollID = UUID() }
                    } else if turn.status == "WAITING_PRIVACY" {
                        NavigationLink { PrivacyReviewView(taskID: turn.task_id) } label: {
                            Label("查看原因与处理", systemImage: "hand.raised").frame(maxWidth: .infinity, minHeight: 30)
                        }.buttonStyle(.borderedProminent).tint(.teal)
                    } else if turn.status == "WAITING_MEDIA" || (turn.status == "WAITING_BUDGET" && turn.budget_recoverable != false) {
                        Button("重新检查", systemImage: "arrow.clockwise") { Task { await resume(turn) } }
                            .buttonStyle(.borderedProminent).tint(.teal).frame(minHeight: 44)
                    }
                    if turn.pending {
                        if !turn.blocksComposer && !needsClarification {
                            Button("就此继续对话", systemImage: "bubble.left") { replyToTaskID = turn.task_id }
                                .buttonStyle(.bordered).tint(.teal).frame(minHeight: 44)
                        }
                        Button("取消本次任务", role: .destructive) { Task { await cancel(turn) } }.frame(minHeight: 44)
                    }
                }
            }
            ForEach(Array((turn.assistant_parts ?? []).filter { $0.type == "task" }.enumerated()), id: \.offset) { _, part in ChatPartView(part: part) }
            ForEach(turn.client_actions ?? []) { ClientActionCard(action: $0).id($0.id) }
            ForEach(turn.approvals.filter { !handledApprovals.contains($0.id) }) { approval in approvalCard(approval) }
            ChatSearchSources(sources: turn.web_sources)
        }
    }
    @ViewBuilder private func approvalCard(_ approval: ApprovalEntry) -> some View {
        ChatApprovalCard(approval: approval, busy: sending) { decision in
            Task { await decide(approval.id, decision) }
        }
    }
    private func bootstrap() async {
        guard state.connected else { clearView(); return }
        if bootstrapRun != nil && bootstrapConnectionRevision == state.connectionRevision {
            refreshRequested = true
            return
        }
        let run = UUID(); bootstrapRun = run; bootstrapConnectionRevision = state.connectionRevision; error = nil
        defer { if bootstrapRun == run { bootstrapRun = nil } }
        // 先读取与已配对身份绑定的加密快照，网络重连不会清空现有对话。
        if let local = await state.api.cachedNamespace() {
            guard !Task.isCancelled, bootstrapRun == run else { return }
            if namespace != local { clearView(); namespace = local; bootstrapRun = run }
            if turns.isEmpty, let bytes = await ClientViewCache.shared.read(key: cacheKey, namespace: local),
               let stored = try? JSONDecoder().decode(ChatSnapshot.self, from: bytes), !Task.isCancelled, bootstrapRun == run, namespace == local {
                let snapshot = stored.bounded
                selected = snapshot.conversationID; ownerNamespace = snapshot.ownerNamespace
                turns = snapshot.turns; hasLoadedHistory = true; before = snapshot.before; etag = snapshot.etag; after = snapshot.after
                try? outbox.activate(owner: ownerNamespace)
                restorePendingDraft()
            }
        }
        do {
            let current = try await state.api.syncNamespace()
            let owner = try await state.api.ownerIdentity(expectedNamespace: current)
            guard !Task.isCancelled, bootstrapRun == run else { return }
            if namespace != current || (!ownerNamespace.isEmpty && ownerNamespace != owner.namespace) { clearView(); bootstrapRun = run }
            namespace = current; ownerNamespace = owner.namespace
            try outbox.activate(owner: ownerNamespace)
            let identifier: String
            if let selected { identifier = selected } else { identifier = try await defaultConversation(namespace: current) }
            guard !Task.isCancelled, namespace == current, bootstrapRun == run else { return }
            if selected != identifier { turns = []; hasLoadedHistory = false; before = nil; etag = nil; after = 0; input = "" }
            selected = identifier; defaultResolved = true
            handleConversationRoute(); restorePendingDraft()
            await refresh()
            if running { pollID = UUID() }
        } catch {
            guard !Task.isCancelled, bootstrapRun == run else { return }
            await handleFailure(error)
        }
    }
    private func hideTransientContent() {
        stopReading()
        let safe = turns.map { $0.cached() }
        if safe.contains(where: { $0.requires_online_revalidation == true }) { turns = safe; etag = nil }
    }
    private func clearView() {
        automaticCapture.stop(clearAttempts: true); automaticReview.suspend(clearIdentity: true)
        stopReading()
        automaticSpeech = ChatAutomaticSpeech(); speechPrimed = false; speechRevalidating = false
        outbox.clearIdentity()
        namespace = nil; ownerNamespace = ""; selected = nil; turns = []; pendingApprovals = []
        before = nil; etag = nil; after = 0; defaultResolved = false; hasLoadedHistory = false
        bootstrapRun = nil; bootstrapConnectionRevision = nil; input = ""; notice = nil
        refreshRun = nil; olderRun = nil; refreshRequested = false
        trailingRefresh?.cancel(); trailingRefresh = nil
        draft.pause(); mentions = []; references = []; dataPicker = nil; replyToTaskID = nil; clientActions.items = []
        handledApprovals = []
        choosingMembers = false; showingRequests = false
    }
    private func defaultConversation(namespace: String) async throws -> String {
        struct Created: Decodable { let id: String }
        return try JSONDecoder().decode(Created.self, from: await state.api.request("POST", "/api/v1/conversations/default", expectedNamespace: namespace)).id
    }
    private func pauseReadingForSourceRefresh() {
        speechRevalidating = true
        // 保留未播放的尾段；刷新后再按任务正文与来源校验，不能把新回复重置成历史。
        if let messageID = automaticMessageID, let context = readingContext,
           !answerPlayback.remainingAnswerText.isEmpty {
            automaticQueue.insert(ChatAutomaticSpeech.Segment(turnID: context.turnID, taskID: context.taskID,
                messageID: messageID, kind: context.kind, text: answerPlayback.remainingAnswerText,
                sources: [], context: context), at: 0)
        }
        stopReading(clearQueue: false, suppressCurrent: false)
    }
    private func stopReading(clearQueue: Bool = true, suppressCurrent: Bool = true) {
        var stopped = Set(automaticMessageID.map { [$0] } ?? [])
        if let readingMessageID { stopped.insert(readingMessageID) }
        if clearQueue { stopped.formUnion(automaticQueue.map(\.messageID)); automaticQueue = [] }
        if suppressCurrent { automaticSpeech.suppressMessages(stopped) }
        automaticMessageID = nil
        answerPlayback.stop(cancelServer: true)
        readingMessageID = nil; readingContext = nil
    }
    private var allowsAutomaticReading: Bool {
        automaticReading && isVisible && scenePhase == .active && state.serverReachable && !voice.recording
            && VoiceCalls.shared.activeID == nil
    }
    private func consumeAutomaticReading() {
        guard speechPrimed else { automaticSpeech.baseline(turns); speechPrimed = true; return }
        let segments = automaticSpeech.update(turns, enabled: allowsAutomaticReading)
        automaticQueue.removeAll { segment in
            automaticSpeech.suppressedMessages.contains(segment.messageID) ||
                !turns.contains(where: { $0.task_id == segment.taskID && segment.context.matches($0) })
        }
        if let context = readingContext, !turns.contains(where: { context.matches($0) }) {
            stopReading(clearQueue: false, suppressCurrent: false)
        }
        guard allowsAutomaticReading else { if automaticMessageID != nil { stopReading() }; return }
        automaticQueue.append(contentsOf: segments.filter { !automaticSpeech.suppressedMessages.contains($0.messageID) })
        startNextAutomaticReading()
    }
    private func startNextAutomaticReading() {
        guard allowsAutomaticReading, !speechRevalidating, !answerPlayback.busy, let namespace, !automaticQueue.isEmpty else { return }
        while !automaticQueue.isEmpty {
            let segment = automaticQueue.removeFirst()
            if automaticSpeech.suppressedMessages.contains(segment.messageID) { continue }
            var text = segment.text
            var context = segment.context
            // 同一条气泡的相邻句可以合并，接话与任务结果始终使用各自的验证上下文。
            while let following = automaticQueue.first, following.messageID == segment.messageID,
                  text.unicodeScalars.count + following.text.unicodeScalars.count < 1000 {
                text += "\n" + following.text
                context = following.context
                automaticQueue.removeFirst()
            }
            automaticMessageID = segment.messageID; readingMessageID = segment.messageID; readingContext = context
            answerPlayback.speakAnswer(text, expectedNamespace: namespace, sources: segment.sources, context: context)
            if answerPlayback.busy { return }
            automaticSpeech.suppressMessages([segment.messageID]); automaticMessageID = nil
        }
    }
    private func handleConversationRoute() {
        guard let requested = router.conversationID, let selected else { return }
        if requested != selected { notice = "这条通知关联的历史对话仍保存在服务器；当前继续使用你的固定对话。" }
        router.conversationID = nil
    }
    private func restorePendingDraft() {
        guard !ownerNamespace.isEmpty else { return }
        do { try outbox.activate(owner: ownerNamespace); try outbox.acknowledge(Set(turns.map(\.client_key))) }
        catch { self.error = "待发消息读取失败，原记录已保留" }
    }
    private func drainOutbox() async {
        guard isVisible, scenePhase == .active, state.serverReachable, let namespace, !ownerNamespace.isEmpty else { return }
        let revision = state.connectionRevision, generation = viewGeneration
        if let turn = await outbox.sendNext(api: state.api, namespace: namespace), isCurrent(namespace: namespace, revision: revision, generation: generation) {
            if !speechPrimed { automaticSpeech.baseline(turns); speechPrimed = true }
            state.androidPresentation.trackRequestedTask(turn.task_id)
            turns = ChatSnapshot.merge(turns, updates: [turn]); after = max(after, turn.sequence)
            consumeAutomaticReading()
            await saveCache(namespace: namespace); pollID = UUID()
        }
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
        try applyPage(page, identifier: identifier)
    }
    private func applyPage(_ page: StoredConversationPage, identifier: String) throws {
        turns = try ChatRefreshProjection.replace(turns, with: page, conversationID: identifier)
        hasLoadedHistory = true
        before = page.has_more ? page.next_before : nil
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
                    pauseReadingForSourceRefresh()
                    pendingApprovals = []
                    if let snapshot = update.snapshot {
                        // 服务端已在同一边界内重验权限，直接替换，页面不经历空数组。
                        do { try applyPage(snapshot, identifier: identifier) }
                        catch {
                            turns = ChatRefreshProjection.awaitingValidation(turns)
                            self.etag = nil
                            throw error
                        }
                    } else {
                        // 兼容旧服务：保留消息位置与本人问题，只遮蔽待重新校验的回答。
                        turns = ChatRefreshProjection.awaitingValidation(turns)
                        self.etag = nil
                        await ClientViewCache.shared.remove(key: cacheKey, namespace: namespace)
                        guard isCurrent(namespace: namespace, revision: revision, generation: generation) else { return }
                        try await fullPage(identifier, namespace: namespace)
                    }
                } else {
                    needsCacheWrite = update.etag != etag || !update.turns.isEmpty
                    turns = ChatSnapshot.merge(turns, updates: update.turns)
                    self.etag = update.etag; after = turns.last?.sequence ?? after
                }
            } else { try await fullPage(identifier, namespace: namespace) }
            guard isCurrent(namespace: namespace, revision: revision, generation: generation), selected == identifier else { return }
            speechRevalidating = false
            consumeAutomaticReading()
            restorePendingDraft()
            if needsCacheWrite { await saveCache(namespace: namespace) }
            readingConversation = false
            if !running && Date().timeIntervalSince(lastApprovalRefresh) > 15 {
                let pending = try await state.api.request("GET", "/api/v1/approvals", expectedNamespace: namespace)
                guard isCurrent(namespace: namespace, revision: revision, generation: generation) else { return }
                pendingApprovals = try JSONDecoder().decode([ApprovalEntry].self, from: pending)
                lastApprovalRefresh = Date()
            }
            error = nil
        } catch {
            guard isCurrent(namespace: namespace, revision: revision, generation: generation) else { return }
            if readingConversation, case APIClient.APIError.http(404, _) = error {
                // 会话被服务器明确删除后重新获取固定入口，不在手机上生成另一会话。
                turns = []; hasLoadedHistory = false; before = nil; etag = nil; after = 0; pendingApprovals = []
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
            automaticSpeech.baseline(page.turns)
            before = page.has_more ? page.next_before : nil
            await saveCache(namespace: namespace)
        } catch {
            guard isCurrent(namespace: namespace, revision: revision, generation: generation) else { return }
            await handleFailure(error)
        }
    }
    private func send() async {
        guard isVisible, !Task.isCancelled, namespace != nil, !ownerNamespace.isEmpty, !sending, !voice.recording else { return }
        sending = true; defer { sending = false }
        do {
            let parts = draft.items.compactMap { $0.asset.map(ChatPart.init(asset:)) }
            guard parts.count == draft.items.count else { throw APIClient.APIError.message("附件尚未上传完成") }
            guard Set(parts.compactMap(\.record_id) + references.map(\.record_id)).count <= ChatDataReferenceRules.maximum else { throw APIClient.APIError.message("附件和数据引用合计最多20项") }
            let pending = PendingChatSend(conversationID: selected ?? "", clientKey: UUID().uuidString, content: input, timezone: TimeZone.current.identifier,
                schemaVersion: "2.0", parts: parts, mentions: mentions, references: references, mediaIDs: draft.items.map(\.id), clientContext: ClientContextSampler.shared.snapshot(), replyToTaskID: replyToTaskID)
            try outbox.activate(owner: ownerNamespace)
            try outbox.enqueue(pending)
            followLatest = true; scrollRevision = UUID()
            input = ""; mentions = []; references = []; replyToTaskID = nil; error = nil
            await draft.acknowledge(ids: pending.mediaIDs ?? [])
        } catch { self.error = error.localizedDescription }
    }
    private func decide(_ id: String, _ decision: String) async {
        guard let namespace else { return }
        guard !sending, !handledApprovals.contains(id) else { return }
        let revision = state.connectionRevision
        sending = true; defer { sending = false }
        do {
            struct Receipt: Decodable { let status: String }
            let raw = try await state.api.request("POST", "/api/v1/approvals/" + id, body: JSONSerialization.data(withJSONObject: ["decision": decision]), expectedNamespace: namespace)
            let receipt = try JSONDecoder().decode(Receipt.self, from: raw)
            guard ["APPROVED", "CANCELED"].contains(receipt.status) else { throw APIClient.APIError.message("审批回执无法核对，请刷新状态") }
            guard !Task.isCancelled, revision == state.connectionRevision, self.namespace == namespace else { return }
            handledApprovals.insert(id)
            pendingApprovals.removeAll { $0.id == id }
            turns = turns.map { $0.acknowledgingApproval(id, status: receipt.status) }
            await saveCache(namespace: namespace)
            state.taskEventRevision = UUID()
            await refresh(); pollID = UUID()
        } catch {
            guard revision == state.connectionRevision, self.namespace == namespace else { return }
            if case APIClient.APIError.http(let code, _) = error, code == 409 {
                // 已处理或已失效的审批不继续开放旧按钮；真实任务状态仍从服务器重新读取。
                handledApprovals.insert(id)
                pendingApprovals.removeAll { $0.id == id }
                etag = nil
                await refresh()
            }
            self.error = error.localizedDescription
        }
    }
    private func resume(_ turn: StoredChatTurn) async {
        guard isVisible, !Task.isCancelled, let namespace else { return }
        guard turn.status != "WAITING_BUDGET" || turn.budget_recoverable != false else { return }
        let revision = state.connectionRevision, generation = viewGeneration
        do {
            _ = try await state.api.request("POST", "/api/v1/tasks/" + turn.task_id + "/resume", expectedNamespace: namespace)
            guard isCurrent(namespace: namespace, revision: revision, generation: generation) else { return }
            await refresh()
            guard isCurrent(namespace: namespace, revision: revision, generation: generation) else { return }
            pollID = UUID()
        } catch {
            guard isCurrent(namespace: namespace, revision: revision, generation: generation) else { return }
            self.error = error.localizedDescription
        }
    }
    private func cancel(_ turn: StoredChatTurn) async {
        guard let namespace else { return }
        do { _ = try await state.api.request("POST", "/api/v1/tasks/" + turn.task_id + "/cancel", expectedNamespace: namespace); await refresh() }
        catch { self.error = error.localizedDescription }
    }
    private func voiceInput() async {
        guard let namespace else { return }
        do {
            if !voice.recording { stopReading(); try await voice.start(); return }
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
            return RecordReference(id: identifier, title: fields["title"]?.description ?? "资料", version: version, taskID: fields["task_id"]?.description)
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
