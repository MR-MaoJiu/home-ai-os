import SwiftUI

/// 滚动跟随只依赖内容/可视区尺寸；用户阅读历史时不因偏移变化反复被拉回。
struct ChatScrollMetrics: Equatable {
    let contentHeight: CGFloat
    let viewportHeight: CGFloat
    let bottomDistance: CGFloat
}

/// 回复保持原请求身份；并发任务按完成时间出现，时间相同时先展示问题。
struct ChatTimelineItem: Identifiable {
    let turn: StoredChatTurn
    let answer: Bool
    var id: String { turn.id + (answer ? ":answer" : ":request") }
    var date: Double { answer ? turn.completed_at ?? (turn.created_at ?? Double(turn.sequence)) + 0.001 : turn.created_at ?? Double(turn.sequence) }
    struct DisplayRevision: Equatable {
        let id: String
        let text: String
        let status: String
        let partCount: Int
    }
    var displayRevision: DisplayRevision {
        DisplayRevision(id: id, text: answer ? turn.assistant_text : turn.acknowledgement ?? "",
                        status: turn.status, partCount: turn.assistant_parts?.count ?? 0)
    }
    static func ordered(_ turns: [StoredChatTurn]) -> [Self] {
        var items: [Self] = []
        for turn in turns {
            items.append(Self(turn: turn, answer: false))
            if !turn.assistant_text.isEmpty || !turn.displayedAssistantParts.isEmpty {
                items.append(Self(turn: turn, answer: true))
            }
        }
        return items.sorted { left, right in
            if left.date != right.date { return left.date < right.date }
            if left.turn.sequence != right.turn.sequence { return left.turn.sequence < right.turn.sequence }
            if left.answer != right.answer { return !left.answer }
            return left.id < right.id
        }
    }
}

/// 仅展示服务器下发的操作；按钮仍由各自的鉴权与提交逻辑处理。
struct ChatInteractionCard<Content: View>: View {
    let title: String
    var subtitle: String? = nil
    let icon: String
    var status: String? = nil
    var tint: Color = .teal
    @ViewBuilder let content: () -> Content
    @Environment(\.dynamicTypeSize) private var typeSize

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack(alignment: .top, spacing: 12) {
                Image(systemName: icon).font(.system(size: 19, weight: .semibold))
                    .foregroundStyle(tint).frame(width: 40, height: 40)
                    .background(tint.opacity(0.1), in: RoundedRectangle(cornerRadius: 12))
                    .accessibilityHidden(true)
                VStack(alignment: .leading, spacing: 5) {
                    Text(verbatim: title).font(.headline).foregroundStyle(.primary)
                    if let subtitle, !subtitle.isEmpty {
                        Text(verbatim: subtitle).font(.caption).foregroundStyle(.secondary)
                    }
                    if typeSize.isAccessibilitySize, let status { statusBadge(status) }
                }.frame(maxWidth: .infinity, alignment: .leading)
                if !typeSize.isAccessibilitySize, let status { statusBadge(status) }
            }
            content()
        }
        .padding(16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color(uiColor: .secondarySystemGroupedBackground), in: RoundedRectangle(cornerRadius: 18))
        .overlay { RoundedRectangle(cornerRadius: 18).strokeBorder(.primary.opacity(0.08), lineWidth: 1).allowsHitTesting(false) }
    }

    private func statusBadge(_ text: String) -> some View {
        Text(verbatim: text).font(.caption.weight(.medium)).foregroundStyle(tint)
            .padding(.horizontal, 9).padding(.vertical, 5)
            .background(tint.opacity(0.08), in: Capsule())
            .fixedSize(horizontal: false, vertical: true)
    }
}

struct ChatSourceItem: Identifiable, Equatable {
    let id: String
    let title: String
    let domain: String
    let url: URL
}

enum ChatSourcePresentation {
    static func items(_ sources: [ChatWebSource]) -> [ChatSourceItem] {
        var seen = Set<String>()
        return sources.compactMap { source in
            guard let url = ChatMarkdownDocument.safeURL(source.url), seen.insert(url.absoluteString).inserted else { return nil }
            let title = source.title.trimmingCharacters(in: .whitespacesAndNewlines)
            return ChatSourceItem(id: url.absoluteString, title: title.isEmpty ? (url.host ?? url.absoluteString) : title,
                                  domain: url.host ?? "网页", url: url)
        }
    }
    static func summary(_ sources: [ChatSourceItem]) -> String {
        var seen = Set<String>()
        return sources.filter { seen.insert($0.domain).inserted }.prefix(3).map(\.domain).joined(separator: " · ")
    }
}

struct ChatSearchSources: View {
    let sources: [ChatWebSource]
    @State private var expanded: Bool
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    private var items: [ChatSourceItem] { ChatSourcePresentation.items(sources) }

    init(sources: [ChatWebSource], initiallyExpanded: Bool = false) {
        self.sources = sources
        _expanded = State(initialValue: initiallyExpanded)
    }

    var body: some View {
        let entries = items
        if !entries.isEmpty {
            VStack(alignment: .leading, spacing: 0) {
                Button { expanded.toggle() } label: {
                    HStack(spacing: 10) {
                        Image(systemName: "globe").foregroundStyle(.teal)
                            .frame(width: 30, height: 30).background(.teal.opacity(0.08), in: RoundedRectangle(cornerRadius: 9))
                        VStack(alignment: .leading, spacing: 3) {
                            Text("参考来源 · \(entries.count)").font(.subheadline.weight(.medium)).foregroundStyle(.primary)
                            Text(ChatSourcePresentation.summary(entries)).font(.caption).foregroundStyle(.secondary).lineLimit(1)
                        }.frame(maxWidth: .infinity, alignment: .leading)
                        Text(expanded ? "收起" : "展开").font(.caption).foregroundStyle(.secondary)
                        Image(systemName: expanded ? "chevron.up" : "chevron.down").font(.caption.weight(.semibold)).foregroundStyle(.secondary)
                    }.padding(12).frame(minHeight: 52)
                }
                .buttonStyle(.plain)
                .accessibilityLabel("参考来源，\(entries.count) 个")
                .accessibilityValue(expanded ? "已展开" : "已收起")
                .accessibilityHint(expanded ? "轻点收起来源列表" : "轻点查看来源链接")
                .accessibilityIdentifier("chatSourcesToggle")
                if expanded {
                    VStack(spacing: 0) {
                        ForEach(Array(entries.enumerated()), id: \.element.id) { index, item in
                            Divider().padding(.horizontal, 12)
                            Link(destination: item.url) {
                                HStack(alignment: .top, spacing: 10) {
                                    Text("\(index + 1)").font(.caption.monospacedDigit()).foregroundStyle(.secondary).frame(width: 20)
                                    VStack(alignment: .leading, spacing: 4) {
                                        Text(verbatim: item.title).font(.subheadline).foregroundStyle(.primary).fixedSize(horizontal: false, vertical: true)
                                        Text(verbatim: item.domain).font(.caption).foregroundStyle(.secondary)
                                    }.frame(maxWidth: .infinity, alignment: .leading)
                                    Image(systemName: "arrow.up.right").font(.caption).foregroundStyle(.teal)
                                }.padding(12).frame(minHeight: 44, alignment: .leading)
                            }.buttonStyle(.plain)
                        }
                    }.transition(.opacity)
                }
            }
            .background(Color(uiColor: .secondarySystemGroupedBackground), in: RoundedRectangle(cornerRadius: 14))
            .overlay { RoundedRectangle(cornerRadius: 14).strokeBorder(.primary.opacity(0.07), lineWidth: 1).allowsHitTesting(false) }
            .animation(reduceMotion ? nil : .easeInOut(duration: 0.18), value: expanded)
            .onChange(of: entries.map(\.id)) { _, _ in expanded = false }
        }
    }
}

struct ChatExecutionApprovalDetails: Equatable {
    let command: String
    let location: String
    let identity: String
    let source: String
    let constraints: [String]
    let resourceScope: [String]
    let summary: String
    let fileChanges: [String]
    let serviceChanges: [String]
    let uncertainties: [String]
    let proposalImpact: String

    init?(_ arguments: [String: JSONValue]) {
        guard case .object(let value) = arguments["approval_context"], value["version"] == .number(1),
              case .string(let command) = value["command"], !command.isEmpty,
              case .string(let location) = value["execution_location"], !location.isEmpty,
              case .string(let identity) = value["execution_identity"], !identity.isEmpty,
              case .string(let source) = value["source"],
              ["sandbox_policy", "local_administrator", "server_policy"].contains(source),
              case .object(let effects) = value["effects"],
              case .string(let summary) = effects["summary"], !summary.isEmpty else { return nil }
        func strings(_ entry: JSONValue?) -> [String]? {
            guard case .array(let values) = entry else { return nil }
            var result: [String] = []
            for item in values {
                guard case .string(let text) = item else { return nil }
                result.append(text)
            }
            return result
        }
        guard let constraints = strings(value["constraints"]),
              let fileChanges = strings(effects["file_changes"]),
              let serviceChanges = strings(effects["service_changes"]),
              let uncertainties = strings(effects["uncertainties"]) else { return nil }
        self.command = command; self.location = location; self.identity = identity
        self.source = source
        self.constraints = Self.relevantLines(constraints)
        self.summary = Self.summary(summary, source: source, arguments: arguments)
        self.resourceScope = Self.relevantLines(strings(value["resource_scope"]) ?? [])
        self.fileChanges = Self.relevantLines(fileChanges)
        self.serviceChanges = Self.relevantLines(serviceChanges)
        self.uncertainties = Self.relevantLines(uncertainties)
        if case .string(let proposal) = value["proposal_impact"] { self.proposalImpact = proposal }
        else { self.proposalImpact = "" }
    }

    var effectsSourceLabel: String {
        source == "local_administrator" ? "本机管理员登记的影响" : "服务端确定的处理方式"
    }

    // 只压缩已知的旧版固定说明，原审批参数和完整命令保持原样；自定义影响不得按关键词隐藏。
    private static func relevantLines(_ values: [String]) -> [String] {
        let replacements = [
            "只执行本机管理员登记的完整脚本；不接受模型追加参数": "执行本机管理员登记的脚本",
            "使用服务账户现有文件和系统权限，不能将工作目录视为隔离边界": "使用上述服务账户的宿主机权限",
            "脚本可访问服务账户有权访问的宿主机资源；登记的影响范围不是文件访问白名单": "可访问上述服务账户有权访问的宿主机资源",
            "只采集本次确认的项目，取消后停止后续处理": "",
            "仅执行服务端固定采集程序，不接受任意命令或文件路径": "",
            "通过 /bin/sh -s 执行以下完整指令": "",
            "不开放宿主机服务管理能力。": "",
            "无服务变更": "",
            "无文件变更": "",
            "影响说明由本机管理员登记，未对脚本进行完整静态证明；请结合完整指令确认。": "",
            "脚本的完整运行结果无法预先证明；模型说明仅供核对，不会减少审批要求。": "",
            "截屏可能包含私人画面；手机同意不替代服务器系统的屏幕录制权限": "截图包含服务器主显示器当前显示的内容。"
        ]
        var result: [String] = []
        for raw in values {
            let value = replacements[raw] ?? raw
            if !value.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && !result.contains(value) { result.append(value) }
        }
        return result
    }

    private static func summary(_ value: String, source: String, arguments: [String: JSONValue]) -> String {
        if source == "local_administrator" && value == "本机管理员尚未登记完整影响；脚本可能新增、修改或删除文件，也可能改变服务和软件。" {
            return "本机管理员尚未登记本次操作的影响，请核对完整指令。"
        }
        if source == "sandbox_policy" && value == "运行脚本并保存私有执行报告；声明的成果文件会作为当前成员的数据附件入库。" {
            if case .array(let outputs) = arguments["outputs"], !outputs.isEmpty {
                return "运行脚本，保存执行报告并返回下列成果文件。"
            }
            return "运行脚本并保存本次执行报告。"
        }
        return value
    }
}

enum ChatApprovalPresentation {
    struct Field: Identifiable, Equatable {
        let id: String
        let label: String
        let value: String
    }
    private static let titles = ["mail.send@v1": "确认发送邮件", "home.execute@v1": "确认家居操作", "model.generate@v1": "确认本次处理", "shell.run@v1": "确认执行 Shell", "host.execute@v1": "确认宿主管理", "host.inspect@v1": "允许读取服务器信息", "android.action@v1": "确认安卓界面操作"]
    static func title(_ capability: String) -> String {
        titles[capability] ?? "确认本次操作"
    }
    static func needsCapabilityDetail(_ capability: String) -> Bool { titles[capability] == nil }
    static func field(_ key: String) -> String {
        ["to": "收件人", "subject": "主题", "text": "正文", "title": "标题", "entity_id": "目标设备", "domain": "设备类型", "service": "执行动作", "data": "操作参数", "language": "执行语言", "script": "执行脚本", "inputs": "输入文件", "outputs": "返回文件", "timeout_seconds": "最长执行秒数", "action_id": "管理动作", "execution": "执行配置", "approval_context": "执行指令与影响", "impact_summary": "AI 预计影响（未经独立验证）", "targets": "本次读取范围", "purpose": "本次用途", "input": "设备操作参数"][key] ?? key
    }
    static func executionDetails(_ approval: ApprovalEntry) -> ChatExecutionApprovalDetails? {
        guard ["shell.run@v1", "host.execute@v1", "host.inspect@v1"].contains(approval.capability) else { return nil }
        return ChatExecutionApprovalDetails(approval.arguments)
    }

    static func fields(_ approval: ApprovalEntry) -> [Field] {
        guard approval.capability == "android.action@v1", case .object(let input) = approval.arguments["input"],
              case .string(let action) = input["action"] else {
            return approval.arguments.keys.sorted().map { Field(id: $0, label: field($0), value: approval.arguments[$0]?.description ?? "") }
        }
        var fields: [Field] = []
        if let purpose = approval.arguments["purpose"]?.description, !purpose.isEmpty { fields.append(Field(id: "purpose", label: "本次用途", value: purpose)) }
        let actionName = ["tap": "点击", "swipe": "滑动", "text": "输入文字", "back": "返回上一页", "home": "返回主页", "launch": "打开应用"][action] ?? action
        fields.append(Field(id: "input.action", label: "本次操作", value: actionName))
        let labels = ["x": "起点横坐标", "y": "起点纵坐标", "x2": "终点横坐标", "y2": "终点纵坐标", "text": "输入内容", "package": "目标应用"]
        let internals: Set<String> = ["action", "epoch", "frame_id", "sequence", "focus_token"]
        for key in input.keys.sorted() where !internals.contains(key) {
            guard let value = input[key], value != .null else { continue }
            fields.append(Field(id: "input." + key, label: labels[key] ?? key, value: value.description))
        }
        for key in approval.arguments.keys.sorted() where key != "input" && key != "purpose" {
            guard let value = approval.arguments[key], value != .null, !value.description.isEmpty else { continue }
            fields.append(Field(id: key, label: field(key), value: value.description))
        }
        return fields
    }
}

private struct ChatExecutionApprovalContent: View {
    let details: ChatExecutionApprovalDetails

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            section("执行位置", text: details.location)
            section("执行身份", text: details.identity)
            VStack(alignment: .leading, spacing: 5) {
                Text("完整指令").font(.caption).foregroundStyle(.secondary)
                Text(verbatim: details.command).font(.caption.monospaced()).textSelection(.enabled)
                    .fixedSize(horizontal: false, vertical: true)
                    .frame(maxWidth: .infinity, alignment: .leading).padding(10)
                    .background(.primary.opacity(0.05), in: RoundedRectangle(cornerRadius: 8))
            }.accessibilityIdentifier("approvalFullCommand")
            lines("执行约束", values: details.constraints)
            lines("本次资源范围", values: details.resourceScope)
            section(details.effectsSourceLabel, text: details.summary)
            lines("文件影响", values: details.fileChanges)
            lines("服务与软件影响", values: details.serviceChanges)
            if !details.proposalImpact.isEmpty { section("AI 预计影响（未经独立验证）", text: details.proposalImpact) }
            lines("需知悉的影响", values: details.uncertainties)
        }
    }

    private func section(_ title: String, text: String) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(title).font(.caption).foregroundStyle(.secondary)
            Text(verbatim: text).font(.subheadline).textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
        }
    }

    @ViewBuilder private func lines(_ title: String, values: [String]) -> some View {
        if !values.isEmpty { section(title, text: values.joined(separator: "\n")) }
    }
}

struct ChatApprovalCard: View {
    let approval: ApprovalEntry
    let busy: Bool
    let onDecision: (String) -> Void

    var body: some View {
        ChatInteractionCard(title: ChatApprovalPresentation.title(approval.capability), subtitle: "核对以下内容后决定是否执行", icon: "checkmark.shield", status: "待确认", tint: .orange) {
            VStack(alignment: .leading, spacing: 12) {
                if ChatApprovalPresentation.needsCapabilityDetail(approval.capability) {
                    LabeledContent("操作类型", value: approval.capability).font(.caption).foregroundStyle(.secondary)
                }
                if let details = ChatApprovalPresentation.executionDetails(approval) {
                    ChatExecutionApprovalContent(details: details)
                } else {
                    ForEach(ChatApprovalPresentation.fields(approval)) { field in
                        VStack(alignment: .leading, spacing: 4) {
                            Text(verbatim: field.label).font(.caption).foregroundStyle(.secondary)
                            Text(verbatim: field.value).font(.subheadline).textSelection(.enabled)
                        }
                    }
                }
                Button { onDecision("APPROVED") } label: {
                    Label(approval.capability == "host.inspect@v1" ? "允许本次读取" : "同意并执行", systemImage: "checkmark").frame(maxWidth: .infinity, minHeight: 30)
                }.buttonStyle(.borderedProminent).tint(.teal)
                Button("拒绝本次操作", role: .destructive) { onDecision("REJECTED") }
                    .frame(maxWidth: .infinity, minHeight: 44)
            }.disabled(busy)
        }
    }
}
