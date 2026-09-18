import SwiftUI

struct ChatMessage: Identifiable { let id = UUID(); let text: String; let mine: Bool; var sources: [RecordReference] = [] }
struct ChatView: View {
    @Environment(AppState.self) private var state
    @State private var input = ""
    @State private var conversationNamespace: String?
    @State private var conversationIdentity: String?
    @State private var messages: [ChatMessage] = []
    @State private var currentTask: String?
    @State private var status = ""
    @State private var sending = false
    @State private var voice = VoiceCapture()

    var body: some View {
        VStack(spacing: 0) {
            if messages.isEmpty {
                ContentUnavailableView("你的家庭助手", systemImage: "house.and.flag", description: Text("连接家庭服务器，查询资料、管理日程。个人数据默认留在本地。"))
            } else {
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 16) {
                        ForEach(messages) { message in
                            HStack {
                                if message.mine { Spacer(minLength: 32) }
                                VStack(alignment: .leading, spacing: 8) {
                                    Text(message.text).textSelection(.enabled)
                                    if !message.mine, let namespace = conversationNamespace {
                                        NavigationLink { SpeechView(text: message.text, expectedNamespace: namespace, sources: message.sources) } label: { Label("朗读", systemImage: "speaker.wave.2").font(.caption) }
                                    }
                                    ForEach(message.sources) { source in
                                        NavigationLink { RecordSourceView(source: source) } label: { Label(source.title, systemImage: "doc.text.magnifyingglass").font(.caption) }
                                    }
                                }.padding().background(message.mine ? Color.teal.opacity(0.12) : Color.secondary.opacity(0.08), in: RoundedRectangle(cornerRadius: 18))
                                if !message.mine { Spacer(minLength: 32) }
                            }
                        }
                    }.padding()
                }
            }
            if sending { HStack { ProgressView(); Text(status).font(.caption); Spacer(); Button("取消") { Task { await cancel() } } }.padding() }
            HStack(alignment: .bottom) {
                Button {
                    Task { await state.perform {
                        guard let namespace = conversationNamespace else { throw APIClient.APIError.message("请先连接家庭服务器") }
                        if voice.recording {
                            let audio = try voice.finish()
                            let body = try JSONSerialization.data(withJSONObject: ["idempotency_key": UUID().uuidString, "capability": "speech.transcribe@v1", "arguments": ["content_base64": audio.base64EncodedString()]])
                            let data = try await state.api.request("POST", "/api/v1/tasks", body: body, expectedNamespace: namespace)
                            let task = try JSONDecoder().decode(TaskCreated.self, from: data)
                            let result = try await waitForTask(task.id, namespace: namespace, seconds: 120)
                            if result.status == "SUCCEEDED", case .object(let object) = result.result {
                                if namespace == conversationNamespace { input = object["text"]?.description ?? "" }
                            } else { throw APIClient.APIError.message(result.error ?? result.status) }
                        } else { try await voice.start() }
                    } }
                } label: { Image(systemName: voice.recording ? "stop.circle.fill" : "mic.circle").font(.title) }
                .disabled(!state.connected || conversationNamespace == nil || sending || state.busy)
                .accessibilityLabel(voice.recording ? "结束录音并转写" : "开始录音")
                TextField("问问 Home AI", text: $input, axis: .vertical).lineLimit(1...6).padding(12).background(.quaternary, in: RoundedRectangle(cornerRadius: 18))
                Button { Task { await send() } } label: { Image(systemName: "arrow.up.circle.fill").font(.largeTitle) }
                    .disabled(sending || input.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || !state.connected || conversationNamespace == nil)
                    .accessibilityLabel("发送")
            }.padding()
        }
        .navigationTitle("Home AI")
        .task(id: "\(state.connected)-\(state.connectionRevision)") {
            let identity = "\(state.connected)-\(state.connectionRevision)"
            if conversationIdentity != identity {
                conversationIdentity = identity
                conversationNamespace = nil; messages = []; input = ""
                if voice.recording { _ = try? voice.finish() }
            }
            let namespace = state.connected ? (try? await state.api.syncNamespace()) : nil
            guard !Task.isCancelled else { return }
            conversationNamespace = namespace
        }
        .toolbar {
            Text(state.connected ? "家庭服务器" : "未配对").font(.caption).foregroundStyle(.secondary)
            if state.connected && conversationNamespace == nil {
                Button("重试连接") { Task { conversationNamespace = try? await state.api.syncNamespace() } }
            }
        }
    }

    private func send() async {
        guard let namespace = conversationNamespace else { return }
        let text = input
        input = ""
        messages.append(ChatMessage(text: text, mine: true))
        sending = true
        defer { sending = false; currentTask = nil }
        await state.perform {
            let body = try JSONSerialization.data(withJSONObject: ["message": text, "idempotency_key": UUID().uuidString, "mode": "local", "timezone": TimeZone.current.identifier])
            let data = try await state.api.request("POST", "/api/v1/tasks", body: body, expectedNamespace: namespace)
            let created = try JSONDecoder().decode(TaskCreated.self, from: data)
            currentTask = created.id
            let task = try await waitForTask(created.id, namespace: namespace, seconds: 300)
            guard namespace == conversationNamespace else { return }
            messages.append(ChatMessage(text: task.error ?? Self.answer(task.result) ?? (task.status == "AWAITING_APPROVAL" ? "请在活动页面确认操作。" : task.status), mine: false, sources: Self.sources(task.result)))
        }
    }
    private func waitForTask(_ identifier: String, namespace: String, seconds: Double) async throws -> TaskResult {
        let deadline = Date().addingTimeInterval(seconds)
        for attempt in 0..<4 {
            try Task.checkCancellation()
            guard UIApplication.shared.applicationState == .active else {
                throw APIClient.APIError.message("任务继续在服务器运行，回到活动页面可查看")
            }
            do {
                let subscription = try await state.api.taskEvents(taskID: identifier, expectedNamespace: namespace)
                defer { Task { await state.api.closeTaskEvents(subscription.id) } }
                for try await event in subscription.events {
                    try Task.checkCancellation()
                    guard Date() < deadline else { throw APIClient.APIError.message("任务仍在服务端运行，可在活动中查看进度") }
                    guard let task = event.tasks.first(where: { $0.id == identifier }) else { continue }
                    status = task.status
                    if ["SUCCEEDED", "FAILED", "CANCELED", "NEEDS_RECONCILIATION", "AWAITING_APPROVAL"].contains(task.status) {
                        let raw = try await state.api.request("GET", "/api/v1/tasks/" + identifier, expectedNamespace: namespace)
                        return try JSONDecoder().decode(TaskResult.self, from: raw)
                    }
                }
            } catch is CancellationError { throw CancellationError() }
            catch {
                guard attempt < 3, Date() < deadline, UIApplication.shared.applicationState == .active else { throw error }
                status = "连接中断，正在恢复任务状态"
                // 会话刷新、设备撤销和服务器身份检查继续走正常请求入口。
                do { _ = try await state.api.request("GET", "/api/v1/me", expectedNamespace: namespace) }
                catch let APIClient.APIError.http(code, message) where code == 401 || code == 403 { throw APIClient.APIError.http(code, message) }
                catch let APIClient.APIError.message(message) { throw APIClient.APIError.message(message) }
                catch { }
                try await Task.sleep(for: .seconds(1 << attempt))
            }
        }
        throw APIClient.APIError.message("任务状态连接中断，请在活动页面重试")
    }

    private func cancel() async {
        guard let currentTask, let namespace = conversationNamespace else { return }
        await state.perform { _ = try await state.api.request("POST", "/api/v1/tasks/" + currentTask + "/cancel", expectedNamespace: namespace) }
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
        if case .object(let object) = result,
           case .array(let choices) = object["choices"], case .object(let first) = choices.first,
           case .object(let message) = first["message"], case .string(let content) = message["content"] { return content }
        return result?.description
    }
}
