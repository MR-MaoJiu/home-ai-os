import SwiftUI

struct PhotoAnalysisView: View {
    @Environment(AppState.self) private var state
    @Environment(\.scenePhase) private var scenePhase
    let recordID: String
    @State private var namespace: String?
    @State private var preview: UIImage?
    @State private var question = "请用中文描述这张照片的可见内容。"
    @State private var answer = ""
    @State private var taskID: String?
    @State private var busy = false
    @State private var error: String?
    @State private var operation: Task<Void, Never>?

    var body: some View {
        Form {
            Section("本地照片分析") {
                if let preview { Image(uiImage: preview).resizable().scaledToFit().frame(maxHeight: 260).accessibilityLabel("当前照片") }
                Text("只分析已授权的非秘密照片，不猜测人物身份。模型可能看错细节，请自行核对。").font(.caption).foregroundStyle(.secondary)
                TextField("想了解什么", text: $question, axis: .vertical).lineLimit(2...5).disabled(busy)
                Button("分析照片") {
                    guard !busy else { return }
                    busy = true; operation = Task { await analyze() }
                }
                    .disabled(busy || namespace == nil || !state.connected || question.isEmpty || question.unicodeScalars.count > 1000)
                if busy { ProgressView("家庭服务器正在分析") }
                if let taskID { NavigationLink("查看任务状态") { TaskProgressView(identifier: taskID) } }
                if let error { Text(error).foregroundStyle(.red) }
            }
            if !answer.isEmpty { Section("模型分析结果") { Text(answer).textSelection(.enabled) } }
        }.navigationTitle("照片分析")
        .task {
            let revision = state.connectionRevision
            do {
                let value = try await state.api.syncNamespace()
                let record = try JSONDecoder().decode(DataEntry.self, from: await state.api.request("GET", "/api/v1/data/" + recordID, expectedNamespace: value))
                guard record.kind == "photo.selected", record.sensitivity != "SECRET",
                      case .string(let encoded) = record.payload["content_base64"], let data = Data(base64Encoded: encoded) else {
                    throw APIClient.APIError.message("照片不可用于分析")
                }
                let prepared = try await Task.detached { try PhotoPreparation.jpeg(data) }.value
                guard !Task.isCancelled, revision == state.connectionRevision, state.connected else { return }
                preview = UIImage(data: prepared); namespace = value
            } catch { if !Task.isCancelled { self.error = error.localizedDescription } }
        }
        .onDisappear { operation?.cancel() }
        .onChange(of: scenePhase) { _, phase in if phase != .active { operation?.cancel() } }
        .onChange(of: state.connectionRevision) { _, _ in operation?.cancel(); namespace = nil; answer = ""; preview = nil }
        .onChange(of: state.connected) { _, connected in if !connected { operation?.cancel(); namespace = nil; answer = ""; preview = nil } }
    }

    private func analyze() async {
        defer { busy = false }
        guard let namespace else { return }
        error = nil; answer = ""
        do {
            try Task.checkCancellation()
            let body = try JSONSerialization.data(withJSONObject: ["idempotency_key": UUID().uuidString,
                "capability": "photo.analyze@v1", "step_timeout_seconds": 180,
                "arguments": ["record_id": recordID, "question": question]])
            let created = try JSONDecoder().decode(TaskCreated.self, from: await state.api.request("POST", "/api/v1/tasks", body: body, expectedNamespace: namespace))
            try Task.checkCancellation()
            taskID = created.id
            for _ in 0..<190 {
                try Task.checkCancellation()
                let task = try JSONDecoder().decode(TaskResult.self, from: await state.api.request("GET", "/api/v1/tasks/" + created.id, expectedNamespace: namespace))
                if task.status == "SUCCEEDED", case .object(let fields) = task.result, case .string(let text) = fields["text"] {
                    try Task.checkCancellation()
                    guard namespace == self.namespace else { return }
                    answer = text; return
                }
                if ["FAILED", "CANCELED", "NEEDS_RECONCILIATION"].contains(task.status) {
                    throw APIClient.APIError.message(task.error ?? task.status)
                }
                try await Task.sleep(for: .seconds(1))
            }
            throw APIClient.APIError.message("任务仍在服务器运行，请查看任务状态；没有重新提交")
        } catch is CancellationError { }
        catch { if namespace == self.namespace { self.error = error.localizedDescription } }
    }
}
