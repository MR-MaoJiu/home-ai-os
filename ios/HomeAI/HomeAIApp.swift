import SwiftUI
import UIKit

@main struct HomeAIApp: App {
    @State private var state = AppState()
    @State private var reminderSource: RecordReference?
    @Environment(\.scenePhase) private var phase
    var body: some Scene {
        WindowGroup {
            RootView().environment(state).task { await state.resumeForeground() }
                .background(KeyboardDismissalInstaller())
                .onOpenURL { url in
                    guard url.scheme == SystemReminderSync.markerScheme, url.query == nil, url.fragment == nil,
                          url.pathComponents.count == 2, let identifier = UUID(uuidString: String(url.path.dropFirst())) else { return }
                    Task { await state.perform {
                        await state.api.restoreConnectionIfNeeded()
                        let namespace = try await state.api.syncNamespace()
                        let owner = try await state.api.ownerIdentity(expectedNamespace: namespace)
                        guard owner.namespace == url.host else { throw APIClient.APIError.message("此提醒属于另一服务器或成员，请先核对配对") }
                        let recordID = identifier.uuidString.lowercased()
                        let raw = try await state.api.request("GET", "/api/v1/data/" + recordID, expectedNamespace: namespace)
                        let record = try JSONDecoder().decode(DataEntry.self, from: raw)
                        guard record.kind == "reminder.item", record.owner_id == owner.userID else { throw APIClient.APIError.message("此链接不是当前成员的家庭提醒") }
                        reminderSource = RecordReference(id: recordID, title: "家庭提醒来源", version: nil)
                    } }
                }
                .sheet(item: $reminderSource) { source in NavigationStack { RecordSourceView(source: source) }.environment(state) }
                .onChange(of: state.connectionRevision) { _, _ in reminderSource = nil }
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
    @Environment(AppState.self) private var state
    var body: some View {
        @Bindable var state = state
        TabView(selection: $navigation.destination) {
            Tab("AI", systemImage: "sparkles", value: HomeDestination.ai) { NavigationStack { ChatView().id(state.connectionRevision) } }
            Tab("活动", systemImage: "clock.arrow.circlepath", value: HomeDestination.activity) { NavigationStack { ActivityView().id(state.connectionRevision) } }
            Tab("自动化", systemImage: "bolt", value: HomeDestination.automations) { NavigationStack { AutomationsView() } }
            Tab("数据", systemImage: "externaldrive", value: HomeDestination.data) { NavigationStack { DataView() } }
            Tab("设置", systemImage: "gearshape", value: HomeDestination.settings) { NavigationStack { SettingsView() } }
        }
        .task(id: state.connectionRevision) { if state.connected { state.startForegroundEvents() } }
        .onChange(of: state.connected) { _, connected in if connected { state.startForegroundEvents() } }
        .tint(.teal)
        .alert(state.pairingNotice ? (state.pairingFeedback == .success ? "连接成功" : "连接未完成") : "操作未完成", isPresented: Binding(get: { state.pairingNotice || state.error != nil }, set: { if !$0 { state.pairingNotice = false; state.error = nil } })) {
            Button("知道了", role: .cancel) { state.pairingNotice = false; state.error = nil }
        } message: { Text(state.pairingNotice ? state.pairingFeedback.message : (state.error ?? "")) }
    }
}
