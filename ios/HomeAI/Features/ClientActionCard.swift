import SwiftUI

struct ClientActionCard: View {
    @Environment(AppState.self) private var state
    @Environment(\.scenePhase) private var phase
    let action: ClientActionRequest
    @State private var presentation: ClientActionPresentation
    @State private var draft: MediaDraft
    @State private var text = ""
    @State private var selection: Set<String> = []
    @State private var records: [DataEntry] = []
    @State private var showRecords = false
    @State private var recordsLoading = false
    @State private var mediaBusy = true
    @State private var busy = false
    @State private var error: String?
    @State private var noReadableHealthData = false
    @State private var notice: String?
    @State private var detailsExpanded = false
    @State private var bootstrapped = false
    @State private var serverVerified = false
    @State private var unavailable = false
    @State private var visible = true
    @State private var namespace: String?
    @State private var connection: UUID?
    @State private var epoch = UUID()
    @State private var now = Date()
    @State private var operation: Task<Void, Never>?
    @State private var operationID: UUID?
    @State private var refreshTask: Task<Void, Never>?
    @State private var refreshID: UUID?
    @State private var refreshDirty = false
    @State private var recordLoadID: UUID?
    private var current: ClientActionRequest { presentation.current }
    private var pending: Bool { current.effectiveStatus(at: now) == "PENDING" }
    private var online: Bool { state.connected && state.serverReachable && phase == .active }
    private var canRespond: Bool { !busy && bootstrapped && serverVerified && !unavailable && online && connection == state.connectionRevision && namespace != nil && current.can_respond && pending }
    private var canRevoke: Bool { !busy && bootstrapped && serverVerified && !unavailable && online && connection == state.connectionRevision && namespace != nil && current.can_revoke && current.status == "RESPONDED" && current.expires_at > now.timeIntervalSince1970 }
    private var lifecycleKey: String { "\(action.id)-\(state.connectionRevision)-\(phase)-\(state.connected)" }
    private struct Scope {
        let epoch: UUID
        let connection: UUID
        let actionID: String
        let namespace: String
    }

    init(action: ClientActionRequest) {
        self.action = action
        _presentation = State(initialValue: ClientActionPresentation(request: action))
        _draft = State(initialValue: MediaDraft(scope: "action:" + action.id))
    }
    var body: some View {
        Group {
            if !bootstrapped || !state.connected || current.id != action.id || (connection != nil && connection != state.connectionRevision) {
                VStack(alignment: .leading, spacing: 6) {
                    if let notice {
                        Text(verbatim: notice).font(.caption).foregroundStyle(.secondary)
                        if online { Button("刷新状态") { Task { await bootstrap() } }.font(.caption) }
                    } else {
                        HStack(spacing: 8) { if online { ProgressView().controlSize(.small) }; Text(online ? "正在确认请求状态…" : "连接家庭服务器后查看请求状态").font(.caption).foregroundStyle(.secondary) }
                    }
                }.padding(12).frame(maxWidth: .infinity, alignment: .leading)
            } else if unavailable {
                Label(current.kind == "member.notify" ? "成员消息已不可访问" : (current.finished ? current.summaryLabel : (notice ?? "本次请求已不可访问")), systemImage: "lock.shield")
                    .font(.caption).foregroundStyle(.secondary)
            } else if current.kind == "member.notify" {
                ChatInteractionCard(title: "成员消息", subtitle: "来自：" + current.requested_by.name, icon: icon, tint: .teal) {
                    Text(verbatim: current.parameters["summary"]?.description ?? "").fixedSize(horizontal: false, vertical: true)
                    if let notice { Text(verbatim: notice).font(.caption).foregroundStyle(.secondary) }
                }
            } else if !pending {
                ClientActionTerminalSummary(summary: current.summaryLabel, requester: current.requested_by.name,
                    icon: terminalIcon, tint: current.status == "RESPONDED" ? .teal : .secondary,
                    notice: notice, expanded: $detailsExpanded) {
                        purpose
                        requestDetails
                        if let note = current.note { Text(verbatim: note).font(.caption).foregroundStyle(.secondary) }
                        if current.can_revoke {
                            Button(role: .destructive) { run(requiresPending: false) { snapshot, namespace in
                                try await ClientActionStore.revoke(snapshot, api: state.api, expectedNamespace: namespace)
                            } } label: { actionLabel("撤回本次任务授权", icon: "arrow.uturn.backward.circle") }
                                .buttonStyle(.bordered).disabled(!canRevoke)
                        }
                        if busy { ProgressView("正在提交…") }
                        if let error { Text(verbatim: error).font(.caption).foregroundStyle(.red) }
                        Button("刷新详情") { requestRefresh() }.font(.caption).disabled(!online || busy || refreshID != nil)
                    }
            } else {
                ChatInteractionCard(title: title, subtitle: current.isPersonalClarification ? nil : "来自：" + current.requested_by.name,
                                    icon: icon, status: current.statusLabel, tint: sensitive ? .orange : .teal) {
                    purpose
                    requestDetails
                    if let error {
                        Label { Text(verbatim: error).fixedSize(horizontal: false, vertical: true) } icon: { Image(systemName: "exclamationmark.triangle.fill") }
                            .font(.caption).foregroundStyle(.red).accessibilityLabel("操作未完成：" + error)
                    }
                    if let notice { Text(verbatim: notice).font(.caption).foregroundStyle(.secondary) }
                    if busy { ProgressView("正在提交…").font(.subheadline).tint(.teal) }
                    if current.can_respond {
                        controls
                        Button(role: .destructive) { run { snapshot, namespace in
                            try await ClientActionStore.deny(snapshot, api: state.api, expectedNamespace: namespace)
                        } } label: { actionLabel(current.kind == "android.assist" ? "取消此任务" : "本次不提供", icon: "xmark.circle") }
                            .buttonStyle(.bordered).disabled(!canRespond)
                        if let recipientNotice = ClientActionConsentCopy.recipientNotice(current) {
                            Label { Text(verbatim: recipientNotice).fixedSize(horizontal: false, vertical: true) } icon: { Image(systemName: "person.crop.circle") }
                                .font(.caption).foregroundStyle(.secondary)
                        }
                    }
                }
            }
        }
        .onAppear { visible = true }
        .onDisappear { visible = false; suspend() }
        .task(id: lifecycleKey) { await bootstrap() }
        .onChange(of: action) { _, value in
            if value.id != current.id { reset(value) }
            else { receive(value) }
            requestRefresh()
        }
        .onChange(of: state.taskEventRevision) { _, _ in requestRefresh() }
        .onChange(of: state.dataEventRevision) { _, _ in requestRefresh(); if showRecords { Task { await loadRecords(present: false) } } }
        .onChange(of: state.serverReachable) { _, reachable in
            if reachable { if bootstrapped { requestRefresh() } else { Task { await bootstrap() } } }
            else { serverVerified = false }
        }
        .task(id: "\(current.id)-\(current.status)-\(current.expires_at)-\(phase)") {
            now = Date()
            guard phase == .active, current.expires_at.isFinite, current.status == "PENDING" || current.can_revoke else { return }
            while !Task.isCancelled {
                let remaining = current.expires_at - Date().timeIntervalSince1970
                if remaining <= 0 { now = Date(); requestRefresh(); return }
                do { try await Task.sleep(for: .seconds(min(60, remaining))) } catch { return }
                now = Date()
            }
        }
        .sheet(isPresented: $showRecords) { recordPicker }
    }
    private var purpose: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(current.kind == "android.assist" ? "需要你处理" : "本次用途").font(.caption.weight(.semibold)).foregroundStyle(.secondary)
            Text(verbatim: current.kind == "android.assist" ? (current.parameters["reason"]?.description ?? current.purpose) : current.purpose).fixedSize(horizontal: false, vertical: true)
            if pending {
                Label { Text("有效至 \(Date(timeIntervalSince1970: current.expires_at).formatted(date: .abbreviated, time: .shortened))") } icon: { Image(systemName: "clock") }
                    .font(.caption).foregroundStyle(.secondary)
            }
        }
    }
    private var terminalIcon: String {
        ["RESPONDED": "checkmark.circle", "DENIED": "hand.raised", "REVOKED": "arrow.uturn.backward.circle", "EXPIRED": "clock", "CANCELED": "xmark.circle"][current.effectiveStatus(at: now)] ?? "checkmark.circle"
    }
    private var title: String {
        if current.isPersonalClarification { return "等待你的补充" }
        return ["android.assist": "协助安卓任务", "cloud.disclose": "确认本次云端披露", "capture_location": "提供位置", "read_health": "提供健康数据", "choose_files": "提供文件", "choose_photos": "提供照片", "provide_text": "补充信息", "choose_option": "请选择", "data.share": "资料授权", "authorize_records": "资料授权", "member.notify": "成员消息"][current.kind] ?? "需要你的操作"
    }
    private var sensitive: Bool {
        ["android.assist", "cloud.disclose", "capture_location", "read_health", "data.share", "authorize_records"].contains(current.kind)
    }
    private var icon: String {
        ["android.assist": "iphone.gen3", "cloud.disclose": "cloud", "capture_location": "location.circle", "read_health": "heart.text.clipboard",
         "choose_files": "doc.badge.plus", "choose_photos": "photo.badge.plus", "provide_text": "text.bubble", "choose_option": "list.bullet.circle",
         "data.share": "lock.shield", "authorize_records": "lock.shield", "member.notify": "person.crop.circle.badge.questionmark"][current.kind] ?? "hand.tap"
    }
    private func actionLabel(_ title: String, icon: String) -> some View {
        Label { Text(verbatim: title).multilineTextAlignment(.leading).fixedSize(horizontal: false, vertical: true) } icon: { Image(systemName: icon) }
            .font(.subheadline.weight(.semibold)).frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
            .contentShape(Rectangle())
    }
    private func detail(_ title: String, value: String) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(verbatim: title).font(.caption).foregroundStyle(.secondary)
            Text(verbatim: value).font(.subheadline).fixedSize(horizontal: false, vertical: true)
        }.frame(maxWidth: .infinity, alignment: .leading)
    }
    @ViewBuilder private var requestDetails: some View {
        switch current.kind {
        case "android.assist":
            if pending { Text("在安卓设备上处理完成后，回到此处继续。AI 会重新读取当前画面。").font(.caption).foregroundStyle(.secondary) }
        case "cloud.disclose":
            if let provider = current.parameters["provider_id"]?.description { detail("接收本次资料的云端服务", value: provider) }
            Text("本次会向云端发送以下资料版本与范围。").font(.caption).foregroundStyle(.secondary)
            if case .object(let versions) = current.parameters["record_versions"] {
                ForEach(versions.keys.sorted(), id: \.self) { identifier in
                    NavigationLink {
                        RecordSourceView(source: RecordReference(id: identifier, title: "本次披露资料", version: Int(versions[identifier]?.description ?? "")))
                    } label: { actionLabel("查看资料 · 版本 " + (versions[identifier]?.description ?? ""), icon: "doc.text.magnifyingglass") }
                        .buttonStyle(.bordered).tint(.teal)
                }
                if versions.isEmpty { detail("披露范围", value: "本次对话输入与服务器列出的最小上下文") }
            }
        case "provide_text":
            if let prompt = current.parameters["prompt"]?.description, !prompt.isEmpty { detail("需要补充", value: prompt) }
            Text("最多 \(current.integer("max_length", default: 4000)) 字").font(.caption).foregroundStyle(.secondary)
        case "choose_option":
            if let prompt = current.parameters["prompt"]?.description, !prompt.isEmpty { detail("选择内容", value: prompt) }
            if !current.can_respond || !current.pending, case .array(let values) = current.parameters["options"] {
                ForEach(Array(values.enumerated()), id: \.offset) { _, value in
                    if case .object(let option) = value, let label = option["label"]?.description {
                        Text(verbatim: label).font(.subheadline).fixedSize(horizontal: false, vertical: true)
                    }
                }
            }
        case "capture_location":
            detail("位置精度", value: current.parameters["precision"]?.description == "precise" ? "精确位置" : "大致位置")
            detail("位置时效", value: "采样距现在不超过 \(current.integer("max_age_seconds", default: 300)) 秒")
            Text("需要系统位置权限，采集可能需要少量时间。").font(.caption).foregroundStyle(.secondary)
        case "read_health":
            detail("健康指标", value: current.strings("types").map { HealthMetric(rawValue: $0)?.title ?? $0 }.joined(separator: "、"))
            detail("开始时间", value: current.parameters["start_at"]?.description ?? "")
            detail("结束时间", value: current.parameters["end_at"]?.description ?? "")
            if pending { Text("将读取上述指标和时间范围内的数据，返回本次任务。").font(.caption).foregroundStyle(.secondary) }
        case "choose_files", "choose_photos":
            detail("附件范围", value: "最多 \(current.integer("max_count", default: 5)) 项，每项不超过 \(ByteCountFormatter.string(fromByteCount: Int64(current.integer("max_bytes", default: 10 * 1024 * 1024)), countStyle: .file))")
            if !current.strings("accepted_mime_types").isEmpty {
                detail("允许的格式", value: current.strings("accepted_mime_types").joined(separator: "、"))
            }
        case "authorize_records", "data.share":
            detail("授权范围", value: current.strings("record_ids").isEmpty ? "由你选择的本人资料" : "指定的 \(current.strings("record_ids").count) 项本人资料，选择时可查看详情")
        default:
            EmptyView()
        }
    }
    @ViewBuilder private var controls: some View {
        switch current.kind {
        case "android.assist":
            Button { openAndroidAssistance() } label: { actionLabel("打开安卓处理", icon: "iphone.gen3") }
                .buttonStyle(.borderedProminent).tint(.teal).disabled(!canRespond)
            Button { run { snapshot, namespace in
                try await ClientActionStore.respond(snapshot, api: state.api, expectedNamespace: namespace)
            } } label: { actionLabel("处理完成，继续任务", icon: "play.circle") }
                .buttonStyle(.bordered).tint(.teal).disabled(!canRespond)
        case "cloud.disclose":
            Button { run { snapshot, namespace in
                try await ClientActionStore.respond(snapshot, api: state.api, expectedNamespace: namespace)
            } } label: { actionLabel("允许本次披露", icon: "checkmark.shield") }
                .buttonStyle(.borderedProminent).tint(.teal).disabled(!canRespond)
        case "provide_text":
            TextField("输入补充内容", text: $text, axis: .vertical).lineLimit(2...6).textFieldStyle(.roundedBorder)
                .disabled(busy).accessibilityLabel(current.parameters["prompt"]?.description ?? "补充说明")
            Button {
                let submittedText = text
                run { snapshot, namespace in try await ClientActionStore.respond(snapshot, text: submittedText, api: state.api, expectedNamespace: namespace) }
            } label: { actionLabel(current.isPersonalClarification ? "补充并继续" : "提交补充", icon: "arrow.up.circle") }
                .buttonStyle(.borderedProminent).tint(.teal)
                .disabled(!canRespond || text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || text.count > current.integer("max_length", default: 4000))
        case "choose_option":
            if current.isPersonalClarification { Text("选择后继续本次任务。").font(.caption).foregroundStyle(.secondary) }
            if case .array(let values) = current.parameters["options"] {
                ForEach(Array(values.enumerated()), id: \.offset) { _, value in
                    if case .object(let option) = value, let identifier = option["id"]?.description, let label = option["label"]?.description {
                        Button { run { snapshot, namespace in
                            try await ClientActionStore.respond(snapshot, text: identifier, api: state.api, expectedNamespace: namespace)
                        } } label: { actionLabel(label, icon: "checkmark.circle") }
                            .buttonStyle(.borderedProminent).tint(.teal).disabled(!canRespond)
                    }
                }
            }
        case "capture_location", "read_health":
            Button { run { snapshot, namespace in
                let ids = try await ClientActionStore.capture(snapshot, api: state.api, expectedNamespace: namespace)
                try Task.checkCancellation()
                return try await ClientActionStore.respond(snapshot, recordIDs: ids, api: state.api, expectedNamespace: namespace)
            } } label: { actionLabel(current.kind == "read_health" ? "读取并提供健康数据" : "允许并提供", icon: "checkmark.shield") }
                .buttonStyle(.borderedProminent).tint(.teal).disabled(!canRespond)
            if error != nil {
                Button { run { snapshot, namespace in
                    await ClientViewCache.shared.remove(key: "client-action-capture:" + snapshot.id, namespace: namespace)
                    await ClientViewCache.shared.remove(key: "client-action-response:" + snapshot.id, namespace: namespace)
                    try Task.checkCancellation()
                    let ids = try await ClientActionStore.capture(snapshot, api: state.api, expectedNamespace: namespace)
                    try Task.checkCancellation()
                    return try await ClientActionStore.respond(snapshot, recordIDs: ids, api: state.api, expectedNamespace: namespace)
                } } label: { actionLabel("重新采集", icon: "arrow.clockwise") }
                    .buttonStyle(.bordered).tint(.teal).disabled(!canRespond)
            }
            if current.kind == "read_health", noReadableHealthData {
                Button { run { snapshot, namespace in
                    let ids = try await ClientActionStore.capture(snapshot, api: state.api, expectedNamespace: namespace, allowEmptyHealth: true)
                    try Task.checkCancellation()
                    return try await ClientActionStore.respond(snapshot, recordIDs: ids, api: state.api, expectedNamespace: namespace)
                } } label: { actionLabel("告知暂无可读数据并继续任务", icon: "arrow.right.circle") }
                    .buttonStyle(.bordered).tint(.teal).disabled(!canRespond)
            }
        case "choose_files", "choose_photos":
            let attachmentDraft = draft
            let attachmentEpoch = epoch
            let attachmentActionID = current.id
            MediaComposer(draft: attachmentDraft, allowsFiles: current.kind == "choose_files", allowsPhotos: current.kind == "choose_photos", allowsVideos: false,
                          maxCount: current.integer("max_count", default: 5), maximumBytes: current.integer("max_bytes", default: 10 * 1024 * 1024),
                          acceptedMIMEs: current.strings("accepted_mime_types"), expectedNamespace: namespace,
                          disabled: busy, onBusyChanged: { value in
                              guard epoch == attachmentEpoch, current.id == attachmentActionID, draft === attachmentDraft else { return }
                              mediaBusy = value
                          })
            Button {
                let submittedDraft = draft
                let recordIDs = submittedDraft.items.compactMap { $0.asset?.record_id }
                let localIDs = submittedDraft.items.map(\.id)
                run(submittedDraft: submittedDraft, localIDs: localIDs) { snapshot, namespace in
                    try await ClientActionStore.respond(snapshot, recordIDs: recordIDs, api: state.api, expectedNamespace: namespace)
                }
            } label: { actionLabel("提供已选附件", icon: "arrow.up.doc") }
                .buttonStyle(.borderedProminent).tint(.teal).disabled(!canRespond || mediaBusy || !draft.ready || draft.namespace != namespace)
            Button { Task { await loadRecords() } } label: { actionLabel("选择已有资料", icon: "folder") }
                .buttonStyle(.bordered).tint(.teal).disabled(!canRespond || recordsLoading)
        case "authorize_records", "data.share":
            Button { Task { await loadRecords() } } label: { actionLabel("选择并检查我的资料", icon: "doc.text.magnifyingglass") }
                .buttonStyle(.borderedProminent).tint(.teal).disabled(!canRespond || recordsLoading)
        default:
            Text("当前客户端暂不支持此请求，请更新后重试。").font(.caption)
        }
    }
    private var recordPicker: some View {
        NavigationStack {
            List {
                Text(verbatim: ClientActionConsentCopy.pickerHint(current)).font(.caption)
                if recordsLoading { ProgressView("正在刷新资料…") }
                if let error { Text(verbatim: error).foregroundStyle(.red) }
                ForEach(records) { record in
                    HStack {
                        Button {
                            if selection.contains(record.id) { selection.remove(record.id) }
                            else if selection.count < current.integer("max_count", default: 20) { selection.insert(record.id) }
                        } label: {
                            Image(systemName: selection.contains(record.id) ? "checkmark.circle.fill" : "circle").frame(width: 44, height: 44).contentShape(Rectangle())
                        }.buttonStyle(.plain).tint(.teal).disabled(!canRespond)
                            .accessibilityLabel((selection.contains(record.id) ? "取消选择：" : "选择：") + record.title)
                        NavigationLink { MemberRecordDetail(record: record, isOwner: true) } label: { Text(verbatim: record.title).fixedSize(horizontal: false, vertical: true) }
                    }
                }
            }.navigationTitle("本次提供的资料")
                .toolbar {
                    ToolbarItem(placement: .cancellationAction) { Button("取消") { showRecords = false } }
                    ToolbarItem(placement: .confirmationAction) {
                        Button("提供") {
                            let recordIDs = Array(selection)
                            showRecords = false
                            run { snapshot, namespace in try await ClientActionStore.respond(snapshot, recordIDs: recordIDs, api: state.api, expectedNamespace: namespace) }
                        }.disabled(selection.isEmpty || !canRespond || recordsLoading)
                    }
                }
        }
    }
    private func openAndroidAssistance() {
        guard canRespond, current.kind == "android.assist", let namespace,
              case .string(let workspaceID) = current.parameters["workspace_id"] else { return }
        let scope = Scope(epoch: epoch, connection: state.connectionRevision, actionID: current.id, namespace: namespace)
        let id = UUID(); operationID = id; busy = true; error = nil
        operation = Task {
            defer { if operationID == id { operation = nil; operationID = nil; busy = false } }
            do {
                let raw = try await state.api.request("GET", "/api/v1/android/workspace", expectedNamespace: namespace)
                let envelope = try JSONDecoder().decode(AndroidWorkspaceEnvelope.self, from: raw)
                guard !Task.isCancelled, matches(scope), current.pending,
                      await state.api.cachedNamespace() == namespace else { return }
                guard envelope.workspace?.id == workspaceID else { throw APIClient.APIError.message("本次协助的安卓设备绑定已变化。") }
                state.androidPresentation.showFullScreen()
            } catch { if !Task.isCancelled && matches(scope) { self.error = error.localizedDescription } }
        }
    }
    private func matches(_ scope: Scope) -> Bool {
        visible && phase == .active && epoch == scope.epoch && state.connectionRevision == scope.connection &&
            current.id == scope.actionID && action.id == scope.actionID && namespace == scope.namespace && connection == scope.connection
    }
    private func clearForm() {
        text = ""; selection = []; records = []; showRecords = false; recordsLoading = false; recordLoadID = nil
        draft.pause(); draft = MediaDraft(scope: "action:" + current.id); mediaBusy = true
        noReadableHealthData = false
    }
    private func suspend() {
        epoch = UUID(); operation?.cancel(); operation = nil; operationID = nil; busy = false
        refreshTask?.cancel(); refreshTask = nil; refreshID = nil; refreshDirty = false
        recordLoadID = nil; recordsLoading = false; showRecords = false; records = []
        draft.pause(); serverVerified = false
    }
    private func reset(_ value: ClientActionRequest) {
        suspend(); presentation = ClientActionPresentation(request: value)
        namespace = nil; connection = nil; bootstrapped = false; unavailable = false
        detailsExpanded = false; error = nil; notice = nil; now = Date(); clearForm()
    }
    @discardableResult private func receive(_ value: ClientActionRequest) -> Bool {
        guard value.id == current.id else { return false }
        let previousStatus = current.status
        let accepted = presentation.receive(value)
        if accepted && current.finished && (previousStatus == "PENDING" || previousStatus != current.status) {
            clearForm(); detailsExpanded = false; error = nil
        }
        now = Date()
        return accepted
    }
    private func bootstrap() async {
        guard visible, !Task.isCancelled else { return }
        if current.id != action.id || (connection != nil && connection != state.connectionRevision) { reset(action) }
        if ClientActionLifecycle.shouldSuspend(phase: phase, connected: state.connected) {
            suspend(); if !state.connected { bootstrapped = false; namespace = nil }; return
        }
        // HealthKit/定位权限面板只会暂时 inactive，不能取消用户刚启动的采集。
        guard phase == .active else { return }
        let capturedEpoch = epoch, capturedConnection = state.connectionRevision, identifier = action.id
        guard UUID(uuidString: identifier) != nil else {
            bootstrapped = true; serverVerified = false; unavailable = true; notice = "请求标识无效"; return
        }
        do {
            let cached = await state.api.cachedNamespace()
            let target: String
            if let cached { target = cached } else { target = try await state.api.syncNamespace() }
            guard !Task.isCancelled, epoch == capturedEpoch, state.connectionRevision == capturedConnection, current.id == identifier else { return }
            namespace = target; connection = capturedConnection
            let receipt = await ClientActionStore.cachedReceipt(actionID: identifier, namespace: target)
            guard await state.api.cachedNamespace() == target, !Task.isCancelled, epoch == capturedEpoch, connection == capturedConnection, current.id == identifier else { return }
            if let receipt, presentation.acknowledge(receipt) { clearForm(); detailsExpanded = false }
            bootstrapped = true
            if online { requestRefresh() }
        } catch {
            guard epoch == capturedEpoch, !Task.isCancelled else { return }
            serverVerified = false; notice = "暂时无法确认请求状态，恢复连接后重试。"
        }
    }
    private func requestRefresh() {
        guard visible, phase == .active, online, bootstrapped, let namespace, connection == state.connectionRevision else { return }
        guard !busy, refreshID == nil else { refreshDirty = true; return }
        refreshDirty = false
        let identifier = UUID(); refreshID = identifier
        let scope = Scope(epoch: epoch, connection: state.connectionRevision, actionID: current.id, namespace: namespace)
        refreshTask = Task {
            defer {
                if refreshID == identifier {
                    refreshID = nil; refreshTask = nil
                    if refreshDirty { requestRefresh() }
                }
            }
            do {
                let raw = try await state.api.request("GET", "/api/v1/client-actions/" + scope.actionID, expectedNamespace: scope.namespace)
                let full = try JSONDecoder().decode(ClientActionRequest.self, from: raw)
                guard full.id == scope.actionID else { throw APIClient.APIError.message("服务器返回了不匹配的请求") }
                guard await state.api.cachedNamespace() == scope.namespace, !Task.isCancelled, matches(scope) else { return }
                if receive(full) { serverVerified = true; unavailable = false; notice = nil }
                if current.finished { error = nil }
                if ClientActionReceipt.handledStatuses.contains(current.status) {
                    try? await ClientActionStore.cacheReceipt(ClientActionReceipt(id: current.id, status: current.status, data_status: current.data_status, note: current.note), namespace: scope.namespace)
                }
                automaticallyProvideAllowedLocation()
            } catch {
                guard !Task.isCancelled, matches(scope) else { return }
                serverVerified = false
                if case APIClient.APIError.http(let code, _) = error, [401, 403, 404, 410].contains(code) { unavailable = true; records = []; selection = []; showRecords = false }
                notice = ClientActionReceipt.handledStatuses.contains(current.status) ? "已处理，详情待刷新" : (pending ? "暂时无法确认最新状态，请恢复连接后刷新。" : "请求已到期，详情待刷新")
            }
        }
    }
    private func automaticallyProvideAllowedLocation() {
        guard canRespond, error == nil, current.permitsAutomaticCoarseLocation(), let namespace,
              ClientContextSampler.shared.canAutomaticallyCaptureCoarseLocation,
              AutomaticClientActionAttempts.shared.begin(actionID: current.id, namespace: namespace, expiresAt: current.expires_at) else { return }
        run(automaticCapture: true) { snapshot, namespace in
            let ids = try await ClientActionStore.capture(snapshot, api: state.api, expectedNamespace: namespace, automatically: true)
            try Task.checkCancellation()
            guard ClientContextSampler.shared.canAutomaticallyCaptureCoarseLocation else {
                throw APIClient.APIError.message("位置授权已变化，请确认后重新提供。")
            }
            return try await ClientActionStore.respond(snapshot, recordIDs: ids, api: state.api, expectedNamespace: namespace)
        }
    }
    private func loadRecords(present: Bool = true) async {
        guard canRespond, let namespace else { return }
        let snapshot = current
        let scope = Scope(epoch: epoch, connection: state.connectionRevision, actionID: snapshot.id, namespace: namespace)
        let id = UUID(); recordLoadID = id; recordsLoading = true
        defer { if recordLoadID == id { recordLoadID = nil; recordsLoading = false } }
        do {
            let owner = try await state.api.ownerIdentity(expectedNamespace: namespace).userID
            try await state.loadData(force: true)
            guard await state.api.cachedNamespace() == namespace, !Task.isCancelled, matches(scope), recordLoadID == id, current.pending else { return }
            let allowed = Set(snapshot.strings("record_ids"))
            records = state.records.filter { record in
                record.owner_id == owner && record.sensitivity != "SECRET" && !record.kind.hasPrefix("memory.") &&
                (allowed.isEmpty || allowed.contains(record.id)) &&
                (snapshot.kind != "choose_photos" || ["photo.selected", "photo.file"].contains(record.kind)) &&
                (snapshot.kind != "choose_files" || ["document.file", "document.import"].contains(record.kind))
            }
            selection.formIntersection(Set(records.map(\.id)))
            if present { showRecords = true }
        } catch { if matches(scope), recordLoadID == id, !Task.isCancelled { self.error = error.localizedDescription } }
    }
    private func run(requiresPending: Bool = true, submittedDraft: MediaDraft? = nil, localIDs: [String]? = nil, automaticCapture: Bool = false,
                     _ work: @escaping @MainActor (ClientActionRequest, String) async throws -> ClientActionReceipt) {
        guard requiresPending ? canRespond : canRevoke, let namespace else { return }
        let snapshot = current
        let sourceDraft = submittedDraft ?? draft
        let queuedIDs = localIDs ?? sourceDraft.items.map(\.id)
        let scope = Scope(epoch: epoch, connection: state.connectionRevision, actionID: snapshot.id, namespace: namespace)
        let id = UUID(); operationID = id
        busy = true; error = nil; notice = nil
        sourceDraft.pause()
        operation = Task {
            defer {
                if automaticCapture { AutomaticClientActionAttempts.shared.finish(actionID: snapshot.id, namespace: namespace) }
                if operationID == id {
                    operationID = nil; operation = nil; busy = false
                    if refreshDirty { requestRefresh() }
                }
            }
            do {
                guard await state.api.cachedNamespace() == namespace, matches(scope), !Task.isCancelled,
                      requiresPending ? (current.can_respond && current.pending) : current.can_revoke else { throw CancellationError() }
                let receipt = try await work(snapshot, namespace)
                try await ClientActionStore.waitForForeground()
                guard receipt.id == snapshot.id, ClientActionReceipt.handledStatuses.contains(receipt.status) else { throw APIClient.APIError.message("服务器未返回可核对的处理回执，请刷新状态。") }
                guard await state.api.cachedNamespace() == namespace, matches(scope), !Task.isCancelled else { return }
                // 回执一到就结束交互；权限GET和草稿清理都不得延后这一展示确认。
                _ = presentation.acknowledge(receipt)
                serverVerified = false; detailsExpanded = false; error = nil; notice = nil
                clearForm(); now = Date(); state.taskEventRevision = UUID()
                refreshDirty = true
                // 使用提交时的身份、草稿实例和ID，不能清理新卡片或新身份的队列。
                sourceDraft.pause()
                for identifier in queuedIDs { try? await MediaUploadStore.shared.remove(id: identifier, namespace: namespace) }
            } catch {
                // 系统面板回调可能早于 scene 恢复；保留用户可理解的错误直到回到前台。
                if !Task.isCancelled { try? await ClientActionStore.waitForForeground() }
                guard !Task.isCancelled, matches(scope) else { return }
                noReadableHealthData = (error as? HealthReadFailure) == .noReadableData
                self.error = error.localizedDescription; serverVerified = false; refreshDirty = true
            }
        }
    }
}

enum ClientActionConsentCopy {
    static func recipientNotice(_ action: ClientActionRequest) -> String? {
        guard ["capture_location", "read_health", "choose_files", "choose_photos", "authorize_records", "data.share", "provide_text", "choose_option"].contains(action.kind),
              action.requested_by.id != action.target_member_id else { return nil }
        return "本次提供的资料将供“" + action.requested_by.name + "”的任务使用。"
    }
    static func pickerHint(_ action: ClientActionRequest) -> String {
        switch action.kind {
        case "choose_photos": return "选择本次要提供的照片。"
        case "choose_files": return "选择本次要提供的文件。"
        default: return action.strings("record_ids").isEmpty ? "选择本次要授权使用的资料。" : "从本次请求的资料中选择要授权的项目。"
        }
    }
}

enum ClientActionLifecycle {
    static func shouldSuspend(phase: ScenePhase, connected: Bool) -> Bool {
        !connected || phase == .background
    }
}

/// 终态是单行状态，详情和撤权仍可展开；不包含网络或本地猜测的处理动作。
struct ClientActionTerminalSummary<Details: View>: View {
    let summary: String
    let requester: String?
    let icon: String
    let tint: Color
    let notice: String?
    @Binding var expanded: Bool
    @ViewBuilder let details: () -> Details
    var body: some View {
        DisclosureGroup(isExpanded: $expanded) {
            VStack(alignment: .leading, spacing: 12) { details() }.padding(.top, 10)
        } label: {
            HStack(alignment: .top, spacing: 9) {
                Image(systemName: icon).foregroundStyle(tint).frame(width: 22, height: 22)
                VStack(alignment: .leading, spacing: 3) {
                    Text(verbatim: summary).font(.subheadline.weight(.medium)).fixedSize(horizontal: false, vertical: true)
                    if let notice { Text(verbatim: notice).font(.caption).foregroundStyle(.secondary) }
                }.frame(maxWidth: .infinity, alignment: .leading)
                if let requester { Text(verbatim: requester).font(.caption).foregroundStyle(.secondary).lineLimit(1) }
            }.frame(minHeight: 30, alignment: .center)
        }.tint(tint).padding(12)
            .background(Color(uiColor: .secondarySystemGroupedBackground), in: RoundedRectangle(cornerRadius: 14))
            .overlay { RoundedRectangle(cornerRadius: 14).strokeBorder(.primary.opacity(0.07), lineWidth: 1).allowsHitTesting(false) }
    }
}

struct ClientActionInbox: View {
    @Environment(AppState.self) private var state
    @Environment(\.scenePhase) private var phase
    let notificationID: String?
    @State private var store = ClientActionStore()
    var body: some View {
        ScrollView { LazyVStack(spacing: 12) {
            if let error = store.error { Text(error).foregroundStyle(.red) }
            ForEach(store.items) { ClientActionCard(action: $0).id($0.id) }
            if store.hasMore { Button("加载更早请求") { Task { await store.load(api: state.api, notificationID: notificationID, older: true) } }.disabled(store.loading) }
            if store.items.isEmpty && !store.loading { ContentUnavailableView("暂无成员请求", systemImage: "person.crop.circle.badge.questionmark") }
        }.padding() }.navigationTitle("成员请求")
            .task(id: state.connectionRevision) { await store.load(api: state.api, notificationID: notificationID) }
            .onChange(of: state.taskEventRevision) { _, _ in Task { await store.load(api: state.api, notificationID: notificationID) } }
            .onChange(of: state.dataEventRevision) { _, _ in Task { await store.load(api: state.api, notificationID: notificationID) } }
            .onChange(of: phase) { _, value in if value == .active { Task { await store.load(api: state.api, notificationID: notificationID) } } }
            .refreshable { await store.load(api: state.api, notificationID: notificationID) }
    }
}
