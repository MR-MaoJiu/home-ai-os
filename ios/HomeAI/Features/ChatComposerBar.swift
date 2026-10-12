import SwiftUI

struct ChatComposerMention: Identifiable, Equatable, Sendable {
    let id: String
    let name: String
}

enum ChatComposerRules {
    static func canSend(baseAllowed: Bool, busy: Bool, recording: Bool, mediaBusy: Bool, pickerPending: Bool,
                        expectedNamespace: String?, draftNamespace: String?, items: [QueuedMedia]) -> Bool {
        guard baseAllowed, !busy, !recording, !mediaBusy, !pickerPending, let expectedNamespace else { return false }
        return draftNamespace == expectedNamespace && items.allSatisfy { $0.asset != nil }
    }
}

/// 运行时只把真实附件队列接进展示面板，聊天提交和语音仍由上层原有逻辑处理。
struct ChatComposerBar: View {
    @Binding var input: String
    let draft: MediaDraft
    let mentions: [ChatComposerMention]
    let references: [ChatDataReference]
    let replyTitle: String?
    let busy: Bool
    let canSend: Bool
    let voiceRecording: Bool
    let canRecord: Bool
    let expectedNamespace: String?
    let onMention: () -> Void
    let onReference: () -> Void
    let onInbox: () -> Void
    let onSend: () -> Void
    let onVoice: () -> Void
    let onRemoveMention: (String) -> Void
    let onRemoveReference: (String) -> Void
    let onCancelReply: () -> Void
    var autoReviewEnabled = false
    var autoReviewUpdating = false
    var autoReviewAvailable = false
    var onAutoReviewChange: (Bool) -> Void = { _ in }
    @State private var pickerRequest: MediaPickerRequest?
    @State private var mediaBusy = true
    private var sendAllowed: Bool {
        ChatComposerRules.canSend(baseAllowed: canSend, busy: busy, recording: voiceRecording, mediaBusy: mediaBusy,
            pickerPending: pickerRequest != nil, expectedNamespace: expectedNamespace, draftNamespace: draft.namespace, items: draft.items)
    }
    var body: some View {
        ChatComposerSurface(input: $input, mentions: mentions, references: references, replyTitle: replyTitle, busy: busy,
            canSend: sendAllowed, voiceRecording: voiceRecording, canRecord: canRecord && !mediaBusy,
            canAddAttachments: !mediaBusy && expectedNamespace != nil && draft.namespace == expectedNamespace && draft.items.count < 5,
            canReferenceData: !mediaBusy && expectedNamespace != nil && draft.namespace == expectedNamespace,
            onAdd: { kind in guard !busy && !voiceRecording && !mediaBusy else { return }; pickerRequest = MediaPickerRequest(kind) },
            onMention: onMention, onReference: onReference, onInbox: onInbox,
            onSend: { guard sendAllowed else { return }; onSend() }, onVoice: onVoice,
            onRemoveMention: onRemoveMention, onRemoveReference: onRemoveReference, onCancelReply: onCancelReply,
            autoReviewEnabled: autoReviewEnabled, autoReviewUpdating: autoReviewUpdating,
            autoReviewAvailable: autoReviewAvailable, onAutoReviewChange: onAutoReviewChange) {
                MediaComposer(draft: draft, compact: true, pickerRequest: $pickerRequest, expectedNamespace: expectedNamespace,
                              disabled: busy || voiceRecording, onBusyChanged: { mediaBusy = $0 })
            }
            .id(expectedNamespace)
            .onChange(of: expectedNamespace) { _, _ in pickerRequest = nil; mediaBusy = true }
    }
}

/// 无网络副作用的共用面板；公开UI样例可提供静态附件视图，真实页面提供MediaComposer。
struct ChatComposerSurface<Attachments: View>: View {
    @Binding var input: String
    let mentions: [ChatComposerMention]
    let references: [ChatDataReference]
    let replyTitle: String?
    let busy: Bool
    let canSend: Bool
    let voiceRecording: Bool
    let canRecord: Bool
    let canAddAttachments: Bool
    let canReferenceData: Bool
    let onAdd: (MediaPickerRequest.Kind) -> Void
    let onMention: () -> Void
    let onReference: () -> Void
    let onInbox: () -> Void
    let onSend: () -> Void
    let onVoice: () -> Void
    let onRemoveMention: (String) -> Void
    let onRemoveReference: (String) -> Void
    let onCancelReply: () -> Void
    var autoReviewEnabled = false
    var autoReviewUpdating = false
    var autoReviewAvailable = false
    var onAutoReviewChange: (Bool) -> Void = { _ in }
    @ViewBuilder let attachments: () -> Attachments
    private var editingDisabled: Bool { busy || voiceRecording }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            if let replyTitle {
                HStack(spacing: 8) {
                    Image(systemName: "arrowshape.turn.up.left").foregroundStyle(.secondary)
                    Text(replyTitle).font(.caption).lineLimit(2)
                    Spacer(minLength: 4)
                    Button(action: onCancelReply) { Image(systemName: "xmark").font(.caption.weight(.semibold)) }
                        .frame(width: 44, height: 44).contentShape(Rectangle()).buttonStyle(.plain)
                        .disabled(editingDisabled).accessibilityLabel("取消关联任务")
                }.padding(.leading, 8)
            }
            if !mentions.isEmpty {
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(spacing: 6) {
                        ForEach(mentions) { mention in
                            Button { onRemoveMention(mention.id) } label: {
                                HStack(spacing: 6) {
                                    Text("@" + mention.name).font(.caption.weight(.semibold)).lineLimit(1)
                                    Image(systemName: "xmark").font(.caption2.weight(.semibold))
                                }.padding(.horizontal, 10).padding(.vertical, 6)
                                    .background(.teal.opacity(0.1), in: Capsule())
                            }.buttonStyle(.plain).foregroundStyle(.teal)
                                .frame(minHeight: 44).disabled(editingDisabled)
                                .accessibilityLabel("移除提及：" + mention.name)
                        }
                    }
                }
            }
            attachments()
            if !references.isEmpty {
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(spacing: 8) {
                        ForEach(references) { reference in
                            HStack(spacing: 4) {
                                NavigationLink { RecordSourceView(source: reference.source) } label: {
                                    Label(reference.title, systemImage: ChatDataReferenceRules.icon(reference.kind))
                                        .font(.caption.weight(.semibold)).lineLimit(1).frame(maxWidth: 180)
                                }.frame(minHeight: 44).accessibilityLabel("查看引用数据：" + reference.title)
                                Button { onRemoveReference(reference.id) } label: {
                                    Image(systemName: "xmark").font(.caption2.weight(.semibold)).frame(width: 44, height: 44)
                                }.accessibilityLabel("移除数据引用：" + reference.title)
                            }.padding(.leading, 10).background(.blue.opacity(0.08), in: Capsule())
                                .buttonStyle(.plain).foregroundStyle(.blue).disabled(editingDisabled)
                        }
                    }
                }
            }
            TextField("问问 Home AI", text: $input, axis: .vertical)
                .font(.body).lineLimit(1...6).padding(.horizontal, 8).padding(.vertical, 7)
                .disabled(editingDisabled).accessibilityIdentifier("chatComposerInput")
            HStack(spacing: 0) {
                Menu {
                    Button("照片", systemImage: "photo") { onAdd(.photos) }.disabled(!canAddAttachments)
                    Button("视频", systemImage: "video") { onAdd(.videos) }.disabled(!canAddAttachments)
                    Button("文件", systemImage: "doc") { onAdd(.files) }.disabled(!canAddAttachments)
                    Divider()
                    Button("成员请求与消息", systemImage: "person.crop.circle.badge.questionmark", action: onInbox)
                } label: { controlIcon("plus") }
                    .disabled(editingDisabled).accessibilityLabel("添加附件或查看成员请求")
                Button(action: onMention) { controlIcon("at") }
                    .disabled(editingDisabled).accessibilityLabel("提及家庭成员")
                Button(action: onReference) { controlIcon("doc.badge.plus") }
                    .disabled(editingDisabled || !canReferenceData).accessibilityLabel("引用已有数据")
                    .accessibilityIdentifier("chatReferenceData")
                AutoReviewComposerButton(state: AutoReviewControlState(enabled: autoReviewEnabled,
                    updating: autoReviewUpdating, available: autoReviewAvailable), onChange: onAutoReviewChange)
                Spacer(minLength: 0)
                Button(action: onVoice) { controlIcon(voiceRecording ? "stop.fill" : "mic") }
                    .foregroundStyle(voiceRecording ? Color.red : Color.primary)
                    .disabled(!voiceRecording && (busy || !canRecord))
                    .accessibilityLabel(voiceRecording ? "结束录音" : "语音输入")
                Button { guard canSend && !busy && !voiceRecording else { return }; onSend() } label: {
                    Image(systemName: busy ? "ellipsis" : "arrow.up")
                        .font(.system(size: 19, weight: .semibold))
                        .foregroundStyle(canSend && !busy && !voiceRecording ? Color.white : Color.secondary)
                        .frame(width: 44, height: 44)
                        .background(canSend && !busy && !voiceRecording ? Color.teal : Color(uiColor: .tertiarySystemFill), in: Circle())
                }.buttonStyle(.plain).disabled(!canSend || busy || voiceRecording).accessibilityLabel("发送消息")
            }.buttonStyle(.plain)
        }.padding(10)
            .background(Color(uiColor: .secondarySystemBackground), in: RoundedRectangle(cornerRadius: 24))
            .overlay { RoundedRectangle(cornerRadius: 24).strokeBorder(.secondary.opacity(0.16), lineWidth: 1).allowsHitTesting(false) }
            .tint(.teal)
    }
    private func controlIcon(_ name: String) -> some View {
        Image(systemName: name).font(.system(size: 19, weight: .medium))
            .frame(width: 44, height: 44).contentShape(Rectangle())
    }
}
