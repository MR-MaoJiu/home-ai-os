import SwiftUI
import UIKit

@main struct HomeAIApp: App {
    init() {
        ProtectedRecordPreview.removeAbandonedFiles()
        // 重启时仅清理本应用上次未完成的选择器明文临时副本，已入队附件保持加密。
        try? FileManager.default.removeItem(at: FileManager.default.temporaryDirectory.appendingPathComponent("HomeAIMediaImport", isDirectory: true))
    }
    @State private var state = AppState()
    @UIApplicationDelegateAdaptor(HomeAINotificationDelegate.self) private var notifications
    @Environment(\.scenePhase) private var phase
    var body: some Scene {
        WindowGroup {
            RootView().environment(state).task { await state.resumeForeground() }
                .background(KeyboardDismissalInstaller())
                .onReceive(NotificationCenter.default.publisher(for: UIApplication.protectedDataDidBecomeAvailableNotification)) { _ in
                    Task { await state.resumeForeground() }
                }
        }
        .backgroundTask(.appRefresh(BackgroundSync.identifier)) {
            await state.refreshInBackground()
        }
        .onChange(of: phase) { _, phase in
            if phase == .background {
                Task { await state.stopForegroundEvents() }
                state.configureBackgroundSync(enabled: UserDefaults.standard.bool(forKey: BackgroundSync.preference))
            } else if phase == .active {
                Task { await state.resumeForeground() }
            }
        }
    }
}

private struct KeyboardDismissalInstaller: UIViewRepresentable {
    func makeUIView(context: Context) -> KeyboardDismissalView { KeyboardDismissalView() }
    func updateUIView(_ uiView: KeyboardDismissalView, context: Context) { }

    static func dismantleUIView(_ uiView: KeyboardDismissalView, coordinator: ()) {
        uiView.removeGesture()
    }
}

private final class KeyboardDismissalView: UIView, UIGestureRecognizerDelegate {
    private weak var installedWindow: UIWindow?
    private lazy var dismissGesture: UITapGestureRecognizer = {
        let gesture = UITapGestureRecognizer(target: self, action: #selector(dismissKeyboard))
        // 收起键盘时继续传递点击，避免吞掉按钮或导航操作。
        gesture.cancelsTouchesInView = false
        gesture.delegate = self
        return gesture
    }()

    override func didMoveToWindow() {
        super.didMoveToWindow()
        removeGesture()
        guard let window else { return }
        installedWindow = window
        window.addGestureRecognizer(dismissGesture)
    }

    func removeGesture() {
        installedWindow?.removeGestureRecognizer(dismissGesture)
        installedWindow = nil
    }

    @objc private func dismissKeyboard() {
        installedWindow?.endEditing(false)
    }

    func gestureRecognizer(_ gestureRecognizer: UIGestureRecognizer, shouldReceive touch: UITouch) -> Bool {
        // 输入框内部点击、光标定位和原生控件交互不触发收起。
        var view = touch.view
        while let current = view {
            if current is UITextField || current is UITextView || current is UIControl { return false }
            view = current.superview
        }
        return true
    }

    func gestureRecognizer(_ gestureRecognizer: UIGestureRecognizer, shouldRecognizeSimultaneouslyWith otherGestureRecognizer: UIGestureRecognizer) -> Bool {
        true
    }
}

struct RootView: View {
    @State private var navigation = IntentRouter.shared
    @State private var notifications = ClientNotifications.shared
    @Environment(AppState.self) private var state
    var body: some View {
        @Bindable var state = state
        TabView(selection: $navigation.destination) {
            Tab("AI", systemImage: "sparkles", value: HomeDestination.ai) { NavigationStack { ChatView().id(state.connectionRevision) } }
            Tab("记忆", systemImage: "brain", value: HomeDestination.memory) { NavigationStack { MemberMemoryView() }.id(state.connectionRevision) }
            Tab("自动化", systemImage: "bolt", value: HomeDestination.automations) { NavigationStack { AutomationsView() }.id(state.connectionRevision) }
            Tab("数据", systemImage: "externaldrive", value: HomeDestination.data) { NavigationStack { DataView() }.id(state.connectionRevision) }
            Tab("设置", systemImage: "gearshape", value: HomeDestination.settings) { NavigationStack { SettingsView() } }
        }
        .task(id: state.connectionRevision) {
            if state.connected {
                state.startForegroundEvents()
                await ClientNotifications.shared.synchronize(api: state.api)
            }
        }
        .onChange(of: state.connected) { _, connected in if connected { state.startForegroundEvents() } }
        .sheet(isPresented: Binding(get: { notifications.pendingNotificationID != nil }, set: { if !$0 { notifications.pendingNotificationID = nil } })) {
            NavigationStack { NotificationInboxView(highlightID: notifications.pendingNotificationID) }.environment(state)
        }
        .tint(.teal)
        .alert(state.pairingNotice ? (state.pairingFeedback == .success ? "连接成功" : "连接未完成") : "操作未完成", isPresented: Binding(get: { state.pairingNotice || state.error != nil }, set: { if !$0 { state.pairingNotice = false; state.error = nil } })) {
            Button("知道了", role: .cancel) { state.pairingNotice = false; state.error = nil }
        } message: { Text(state.pairingNotice ? state.pairingFeedback.message : (state.error ?? "")) }
    }
}
