import SwiftUI

enum AutoReviewTapIntent: Equatable {
    case confirmEnable
    case disable
    case unavailable
}

/// 开关展示只使用服务端确认后的状态，不在点击或确认时提前切换。
struct AutoReviewControlState: Equatable {
    var enabled = false
    var updating = false
    var available = false

    var canChange: Bool { available && !updating }
    var tapIntent: AutoReviewTapIntent {
        guard canChange else { return .unavailable }
        return enabled ? .disable : .confirmEnable
    }
    var canConfirmEnable: Bool { canChange && !enabled }
    var accessibilityValue: String {
        if updating { return "正在保存" }
        if !available { return enabled ? "已开启，连接后可关闭" : "未连接，暂时无法修改" }
        return enabled ? "已开启" : "已关闭"
    }
}

/// 输入框内的高风险模式入口；弹窗确认只是提交意图，最终状态由上层服务端回执决定。
struct AutoReviewComposerButton: View {
    let state: AutoReviewControlState
    let onChange: (Bool) -> Void
    @State private var confirming = false

    var body: some View {
        Button {
            switch state.tapIntent {
            case .confirmEnable: confirming = true
            case .disable: onChange(false)
            case .unavailable: break
            }
        } label: {
            ViewThatFits(in: .horizontal) {
                badge(showTitle: true).fixedSize(horizontal: true, vertical: false)
                badge(showTitle: false)
            }
        }
        .buttonStyle(.plain)
        .disabled(!state.canChange)
        .accessibilityLabel("自动审核")
        .accessibilityValue(state.accessibilityValue)
        .accessibilityHint(state.enabled ? "关闭后，新的请求恢复手动审核" : "开启前会说明自动批准的范围")
        .accessibilityIdentifier("chatAutoReviewToggle")
        .alert("开启自动审核？", isPresented: $confirming) {
            Button("取消", role: .cancel) { }
            Button("开启自动审核", role: .destructive) {
                guard state.canConfirmEnable else { return }
                onChange(true)
            }
            .accessibilityIdentifier("chatAutoReviewConfirm")
        } message: {
            Text("家庭服务器会自动同意此设备任务当前及后续的可批准请求，包括执行命令、修改或删除文件、对外发送，以及披露你的私人资料。\n\n关闭 App 后仍然有效。关闭开关后新的请求恢复手动审核，已批准的操作可能继续执行。密码、系统权限和缺少的信息仍需要你处理。")
        }
        .onChange(of: state.canConfirmEnable) { _, allowed in
            if !allowed { confirming = false }
        }
    }

    private func badge(showTitle: Bool) -> some View {
        HStack(spacing: 5) {
            if state.updating {
                ProgressView().controlSize(.small)
                    .tint(state.enabled ? .orange : .secondary)
                    .frame(width: 19, height: 19)
            } else {
                Image(systemName: state.enabled ? "checkmark.shield.fill" : "shield")
                    .font(.system(size: 18, weight: .medium))
                    .frame(width: 19, height: 19)
            }
            if showTitle {
                Text("自动审核").font(.caption.weight(.semibold)).lineLimit(1)
            }
        }
        .foregroundStyle(state.enabled ? Color.orange : Color.secondary)
        .padding(.horizontal, showTitle ? 9 : 0)
        .frame(minWidth: 44, minHeight: 44)
        .background(state.enabled ? Color.orange.opacity(0.14) : Color.clear, in: Capsule())
        .overlay {
            Capsule().strokeBorder(state.enabled ? Color.orange.opacity(0.35) : Color.clear, lineWidth: 1)
        }
        .contentShape(Capsule())
    }
}
