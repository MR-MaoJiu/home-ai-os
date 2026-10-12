import SwiftUI
import Observation
import UIKit

struct PrivacyReviewDocument: Codable, Equatable, Sendable {
    struct Provider: Codable, Equatable, Sendable {
        let id: String
        let model: String
        let endpoint: String
        var origin: String {
            guard let url = URLComponents(string: endpoint), let scheme = url.scheme, ["https", "http"].contains(scheme.lowercased()), let host = url.host else { return "未提供服务地址" }
            return scheme + "://" + host + (url.port.map { ":" + String($0) } ?? "")
        }
    }
    struct Block: Codable, Equatable, Identifiable, Sendable { let id: String; let label: String; let text: String; let editable: Bool }
    let review_id: String?
    let task_id: String
    let status: String
    let code: String
    let stage: String
    let reason: String
    let suggestion: String
    let retryable: Bool
    let overrideable: Bool
    let expires_at: Double?
    let provider: Provider?
    let blocks: [Block]
    let has_media: Bool
    let can_force: Bool
    let can_edit: Bool

    var mediaNotice: String? {
        has_media ? "本次还包含图像像素；修改文字不会改变图像，发送图像前会核对本人的披露授权。" : nil
    }
    var forceConfirmation: String {
        let target = provider.map { $0.model + "（" + $0.origin + "）" } ?? "本次目标模型"
        var message = "本次暂停原因：" + reason + "\n继续后将向 " + target + " 发送当前展示的副本。"
        if let mediaNotice { message += "\n" + mediaNotice }
        return message
    }

    func isExpired(at now: Date) -> Bool {
        guard review_id != nil else { return false }
        guard let expiry = expires_at else { return true }
        return !expiry.isFinite || expiry <= now.timeIntervalSince1970
    }
    var withoutPreview: Self {
        Self(review_id: nil, task_id: task_id, status: status, code: code, stage: stage, reason: reason, suggestion: suggestion,
             retryable: retryable, overrideable: false, expires_at: nil, provider: provider, blocks: [], has_media: has_media, can_force: false, can_edit: false)
    }
}

enum PrivacyReviewDecision: String, Codable, Sendable { case retry, force, edit }
struct PrivacyReviewSubmission: Codable, Equatable, Sendable {
    let review_id: String
    let decision: PrivacyReviewDecision
    let edits: [String: String]?
    let idempotency_key: String
}

/// 敏感预览、修改与幂等正文只在页面内存中保存，不接入聊天或页面磁盘缓存。
@MainActor @Observable
final class PrivacyReviewSession {
    private(set) var document: PrivacyReviewDocument?
    private(set) var namespace: String?
    private(set) var connectionID: UUID?
    private(set) var generation = UUID()
    private(set) var busy = false
    var drafts: [String: String] = [:]
    var message: String?
    private var pending: PrivacyReviewSubmission?

    @discardableResult func beginLoad(connectionID: UUID) -> UUID {
        invalidate(message: nil)
        self.connectionID = connectionID; busy = true
        return generation
    }
    func bind(namespace: String, ticket: UUID) -> Bool {
        guard ticket == generation, busy else { return false }
        self.namespace = namespace; return true
    }
    @discardableResult func receive(_ value: PrivacyReviewDocument, ticket: UUID, namespace: String, now: Date = Date()) -> Bool {
        guard ticket == generation, self.namespace == namespace else { return false }
        guard !value.isExpired(at: now) else { invalidate(message: "本次预览已过期，请重新读取原因和内容。"); return false }
        let display = value.review_id == nil ? value.withoutPreview : value
        document = display
        drafts = Dictionary(display.blocks.filter(\.editable).map { ($0.id, $0.text) }, uniquingKeysWith: { _, newest in newest })
        busy = false; message = nil
        return true
    }
    @discardableResult func revalidate(_ value: PrivacyReviewDocument, ticket: UUID, namespace: String, now: Date = Date()) -> Bool {
        guard ticket == generation, self.namespace == namespace else { return false }
        let next = value.review_id == nil ? value.withoutPreview : value
        if next == document && !next.isExpired(at: now) { return true }
        let identity = connectionID
        invalidate(message: nil)
        connectionID = identity; self.namespace = namespace
        let accepted = receive(next, ticket: generation, namespace: namespace, now: now)
        if accepted { message = "本次复核已更新，请核对最新结果后处理。" }
        return accepted
    }
    func invalidate(message: String?) {
        generation = UUID(); document = nil; namespace = nil; connectionID = nil
        drafts = [:]; pending = nil; busy = false; self.message = message
    }
    func fail(_ message: String, ticket: UUID) {
        guard ticket == generation else { return }
        busy = false; self.message = message
    }
    func isVisible(connectionID: UUID, now: Date = Date()) -> Bool {
        guard self.connectionID == connectionID, let document, namespace != nil else { return false }
        return !document.isExpired(at: now)
    }
    var hasChanges: Bool {
        document?.blocks.contains { $0.editable && drafts[$0.id] != $0.text } == true
    }
    @discardableResult func updateDraft(_ text: String, blockID: String, ticket: UUID, now: Date = Date()) -> Bool {
        guard ticket == generation, !busy, let document, !document.isExpired(at: now), document.can_edit,
              document.blocks.contains(where: { $0.id == blockID && $0.editable }) else { return false }
        drafts[blockID] = text; return true
    }
    func resetEdits() {
        guard let document else { return }
        drafts = Dictionary(document.blocks.filter(\.editable).map { ($0.id, $0.text) }, uniquingKeysWith: { _, newest in newest })
        pending = nil
    }
    func canSubmit(_ decision: PrivacyReviewDecision, connectionID: UUID, now: Date = Date()) -> Bool {
        guard !busy, isVisible(connectionID: connectionID, now: now), let document else { return false }
        switch decision {
        case .retry:
            let statusAllows = document.review_id == nil ? ["pending", "expired", "invalidated", "unavailable"].contains(document.status) : document.status == "pending"
            return statusAllows && document.retryable && !hasChanges
        case .force: return document.status == "pending" && document.review_id != nil && document.can_force && document.overrideable && document.provider != nil && !document.blocks.isEmpty && !hasChanges
        case .edit: return document.status == "pending" && document.review_id != nil && document.can_edit && hasChanges
        }
    }
    func submission(_ decision: PrivacyReviewDecision, connectionID: UUID, now: Date = Date()) throws -> PrivacyReviewSubmission? {
        guard canSubmit(decision, connectionID: connectionID, now: now), let document else { throw APIClient.APIError.message("当前复核已失效或不允许此操作，请重新读取。") }
        guard let identifier = document.review_id else { return nil }
        let edits: [String: String]? = decision == .edit ? Dictionary(document.blocks.filter { $0.editable && drafts[$0.id] != $0.text }.map { ($0.id, drafts[$0.id] ?? $0.text) }, uniquingKeysWith: { _, newest in newest }) : nil
        if let pending, pending.review_id == identifier, pending.decision == decision, pending.edits == edits { return pending }
        let value = PrivacyReviewSubmission(review_id: identifier, decision: decision, edits: edits, idempotency_key: UUID().uuidString.lowercased())
        pending = value; return value
    }
    func beginSubmitting() { busy = true; message = nil }
}

struct PrivacyReviewView: View {
    @Environment(AppState.self) private var state
    @Environment(\.scenePhase) private var phase
    @Environment(\.dismiss) private var dismiss
    let taskID: String
    @State private var review = PrivacyReviewSession()
    @State private var operation: Task<Void, Never>?
    @State private var validation: Task<Void, Never>?
    @State private var validationID: UUID?
    @State private var validationDirty = false
    private var online: Bool { state.connected && state.serverReachable && phase == .active }
    private var environmentKey: String { "\(state.connectionRevision)-\(state.connected)-\(state.serverReachable)-\(phase)" }

    var body: some View {
        List {
            if !online {
                ContentUnavailableView("等待安全连接", systemImage: "lock.shield", description: Text("恢复前台连接后重新读取隐私原因与预览。"))
            } else if review.isVisible(connectionID: state.connectionRevision), let document = review.document {
                PrivacyReviewContent(document: document, review: review, connectionID: state.connectionRevision, online: online, onSubmit: submit)
            } else if review.busy { ProgressView("正在读取原因与本次内容…") }
            if online, let message = review.message {
                Section {
                    Text(verbatim: message).foregroundStyle(.red)
                    if !review.busy { Button("重新读取") { operation = Task { await load() } } }
                }
            }
        }
        .navigationTitle("隐私检查")
        .navigationBarTitleDisplayMode(.inline)
        .privacySensitive()
        .task(id: environmentKey) {
            clear(message: online ? nil : "连接恢复后需要重新读取。")
            if online { await load() }
        }
        .task(id: review.document?.expires_at) {
            guard let expiry = review.document?.expires_at, expiry.isFinite else { return }
            let ticket = review.generation
            while !Task.isCancelled && ticket == review.generation {
                let remaining = expiry - Date().timeIntervalSince1970
                if remaining <= 0 { clear(message: "本次预览已过期，请重新读取原因和内容。"); return }
                do { try await Task.sleep(for: .seconds(min(60, remaining))) } catch { return }
            }
        }
        .onChange(of: state.dataEventRevision) { _, _ in
            clear(message: "正在重新确认本次资料状态。")
            if online { operation = Task { await load() } }
        }
        .onChange(of: state.taskEventRevision) { _, _ in validateCurrentReview() }
        .onChange(of: review.busy) { _, busy in if !busy && validationDirty { validateCurrentReview() } }
        .onDisappear { clear(message: nil) }

    }

    private func clear(message: String?) {
        operation?.cancel(); operation = nil
        validation?.cancel(); validation = nil; validationID = nil; validationDirty = false
        review.invalidate(message: message)
    }
    private func load() async {
        guard online, UUID(uuidString: taskID) != nil else { return }
        let connectionID = state.connectionRevision
        let ticket = review.beginLoad(connectionID: connectionID)
        do {
            let namespace = try await state.api.syncNamespace()
            guard online, state.connectionRevision == connectionID, !Task.isCancelled, review.bind(namespace: namespace, ticket: ticket) else { return }
            let raw = try await state.api.request("GET", "/api/v1/tasks/" + taskID + "/privacy-review", expectedNamespace: namespace)
            let document = try JSONDecoder().decode(PrivacyReviewDocument.self, from: raw)
            guard online, !Task.isCancelled, state.connectionRevision == connectionID, await state.api.cachedNamespace() == namespace else { return }
            guard document.task_id == taskID else { throw APIClient.APIError.message("隐私复核与当前任务不匹配。") }
            review.receive(document, ticket: ticket, namespace: namespace)
        } catch {
            guard !Task.isCancelled, ticket == review.generation else { return }
            review.invalidate(message: error.localizedDescription)
        }
    }
    private func validateCurrentReview() {
        guard online, review.document != nil, let namespace = review.namespace else { return }
        guard !review.busy, validationID == nil else { validationDirty = true; return }
        validationDirty = false
        let id = UUID(); validationID = id
        let ticket = review.generation
        let connection = state.connectionRevision
        validation = Task {
            defer {
                if validationID == id {
                    validationID = nil; validation = nil
                    if validationDirty { validateCurrentReview() }
                }
            }
            do {
                let raw = try await state.api.request("GET", "/api/v1/tasks/" + taskID + "/privacy-review", expectedNamespace: namespace)
                let latest = try JSONDecoder().decode(PrivacyReviewDocument.self, from: raw)
                guard !Task.isCancelled, online, ticket == review.generation, state.connectionRevision == connection, await state.api.cachedNamespace() == namespace else { return }
                guard latest.task_id == taskID else { throw APIClient.APIError.message("隐私复核与当前任务不匹配。") }
                review.revalidate(latest, ticket: ticket, namespace: namespace)
            } catch {
                guard !Task.isCancelled, ticket == review.generation else { return }
                review.invalidate(message: "暂时无法确认当前复核权限，请重新读取。" + error.localizedDescription)
            }
        }
    }
    private func submit(_ decision: PrivacyReviewDecision) {
        guard online, !review.busy, let namespace = review.namespace else { return }
        do {
            let submission = try review.submission(decision, connectionID: state.connectionRevision)
            let ticket = review.generation
            let connectionID = state.connectionRevision
            review.beginSubmitting()
            operation = Task {
                do {
                    if let submission {
                        let encoder = JSONEncoder(); encoder.outputFormatting = [.sortedKeys]
                        _ = try await state.api.request("POST", "/api/v1/tasks/" + taskID + "/privacy-review", body: encoder.encode(submission), expectedNamespace: namespace)
                    } else {
                        _ = try await state.api.request("POST", "/api/v1/tasks/" + taskID + "/resume", expectedNamespace: namespace)
                    }
                    guard !Task.isCancelled, online, ticket == review.generation, connectionID == state.connectionRevision, await state.api.cachedNamespace() == namespace else { return }
                    review.invalidate(message: nil); state.taskEventRevision = UUID(); dismiss()
                } catch {
                    guard !Task.isCancelled, ticket == review.generation else { return }
                    if case APIClient.APIError.http(let code, _) = error, [401, 403, 404, 409, 410].contains(code) {
                        review.invalidate(message: error.localizedDescription)
                    } else { review.fail(error.localizedDescription, ticket: ticket) }
                }
            }
        } catch { review.message = error.localizedDescription }
    }
}

/// 纯展示组件与真实页面共用；只有用户点处理按钮才交给页面的签名提交逻辑。
struct PrivacyReviewContent: View {
    let document: PrivacyReviewDocument
    let review: PrivacyReviewSession
    let connectionID: UUID
    let online: Bool
    let onSubmit: (PrivacyReviewDecision) -> Void
    @State private var confirmingForce = false
    @State private var forceTicket: UUID?
    private var forceExplanation: String {
        review.document?.forceConfirmation ?? "本次复核已失效，请重新读取。"
    }

    var body: some View {
        Group {
            if online && review.isVisible(connectionID: connectionID) {
                reasonSection(document)
                previewSection(document)
                actionsSection(document)
                    .confirmationDialog("强制本次继续？", isPresented: $confirmingForce, titleVisibility: .visible) {
                        Button("强制本次继续", role: .destructive) {
                            guard online, forceTicket == review.generation, review.canSubmit(.force, connectionID: connectionID) else { return }
                            onSubmit(.force)
                        }
                        Button("取消", role: .cancel) { forceTicket = nil }
                    } message: { Text(forceExplanation) }
            } else {
                Text("内容已隐藏，请重新读取本次隐私复核。")
            }
        }
        .onChange(of: review.generation) { _, _ in confirmingForce = false; forceTicket = nil }
        .onChange(of: online) { _, online in if !online { confirmingForce = false; forceTicket = nil } }
    }

    private func reasonSection(_ document: PrivacyReviewDocument) -> some View {
        Section("暂停原因") {
            Text(verbatim: document.reason)
            if !document.suggestion.isEmpty { Text(verbatim: document.suggestion).foregroundStyle(.secondary) }
            if let provider = document.provider {
                LabeledContent("目标模型", value: provider.model)
                LabeledContent("服务地址", value: provider.origin)
            }
            if let expiry = document.expires_at { LabeledContent("本次预览有效至") { Text(Date(timeIntervalSince1970: expiry), format: .dateTime.hour().minute().second()) } }
            DisclosureGroup("诊断信息") {
                LabeledContent("阶段", value: document.stage)
                LabeledContent("原因代码", value: document.code)
            }
        }
    }
    @ViewBuilder private func previewSection(_ document: PrivacyReviewDocument) -> some View {
        let ticket = review.generation
        if !document.blocks.isEmpty {
            Section("本次待发送文本") {
                ForEach(document.blocks) { block in
                    if block.editable {
                        VStack(alignment: .leading, spacing: 8) {
                            Text(verbatim: block.label).font(.headline)
                            blockText(block, document: document, ticket: ticket)
                            if document.can_edit { Text("修改这段待发送文本后，提交重新检测。").font(.caption).foregroundStyle(.secondary) }
                        }
                    } else {
                        DisclosureGroup {
                            blockText(block, document: document, ticket: ticket)
                        } label: { Text(verbatim: block.label).font(.headline) }
                    }
                }
            }
        }
        if let mediaNotice = document.mediaNotice {
            Section { Text(verbatim: mediaNotice).font(.caption) }
        }
    }
    private func blockText(_ block: PrivacyReviewDocument.Block, document: PrivacyReviewDocument, ticket: UUID) -> some View {
        PrivacyReviewText(text: Binding(get: { block.editable && document.can_edit ? (review.drafts[block.id] ?? block.text) : block.text }, set: { value in
            if block.editable && document.can_edit { review.updateDraft(value, blockID: block.id, ticket: ticket) }
        }), editable: block.editable && document.can_edit && !review.busy)
            .frame(minHeight: 130, maxHeight: 240)
            .id(review.generation.uuidString + ":" + block.id)
    }
    private func actionsSection(_ document: PrivacyReviewDocument) -> some View {
        Section("处理本次任务") {
            if review.busy { ProgressView("正在提交…") }
            Button("重新检查") { onSubmit(.retry) }
                .disabled(!online || !review.canSubmit(.retry, connectionID: connectionID))
            if document.can_edit {
                Button("修改并重新检测") { onSubmit(.edit) }
                    .disabled(!online || !review.canSubmit(.edit, connectionID: connectionID))
                if review.hasChanges { Button("撤销本页修改") { review.resetEdits() }.disabled(review.busy) }
            }
            if document.can_force && document.overrideable {
                Button("强制本次继续", role: .destructive) { forceTicket = review.generation; confirmingForce = true }
                    .disabled(!online || !review.canSubmit(.force, connectionID: connectionID))
            }
            if review.hasChanges { Text("文本已修改，请选择“修改并重新检测”。").font(.caption).foregroundStyle(.secondary) }
            if ["approved", "consumed"].contains(document.status) { Text("本次复核已处理，请返回任务查看执行状态。").font(.caption) }
        }
    }
}

/// 使用原生纯文本组件，不把待发送内容中的 Markdown、链接或 HTML 当成操作。
private struct PrivacyReviewText: UIViewRepresentable {
    @Binding var text: String
    let editable: Bool
    func makeCoordinator() -> Coordinator { Coordinator(text: $text) }
    func makeUIView(context: Context) -> UITextView {
        let view = UITextView()
        view.delegate = context.coordinator
        view.font = .preferredFont(forTextStyle: .body)
        view.adjustsFontForContentSizeCategory = true
        view.backgroundColor = .secondarySystemBackground
        view.layer.cornerRadius = 8
        view.textContainerInset = UIEdgeInsets(top: 10, left: 8, bottom: 10, right: 8)
        view.isSelectable = true
        view.dataDetectorTypes = []
        view.autocorrectionType = .no; view.autocapitalizationType = .none
        view.smartQuotesType = .no; view.smartDashesType = .no
        return view
    }
    func updateUIView(_ view: UITextView, context: Context) {
        context.coordinator.text = $text
        view.isEditable = editable
        if view.text != text { view.text = text; view.undoManager?.removeAllActions() }
    }
    static func dismantleUIView(_ view: UITextView, coordinator: Coordinator) {
        view.delegate = nil; coordinator.text = .constant("")
        view.resignFirstResponder(); view.text = ""; view.undoManager?.removeAllActions()
    }
    final class Coordinator: NSObject, UITextViewDelegate {
        var text: Binding<String>
        init(text: Binding<String>) { self.text = text }
        func textViewDidChange(_ textView: UITextView) { text.wrappedValue = textView.text }
    }
}
