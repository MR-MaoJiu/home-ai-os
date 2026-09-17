import SwiftUI

@main struct HomeAIApp: App {
    @State private var state = AppState()
    @Environment(\.scenePhase) private var phase
    var body: some Scene {
        WindowGroup {
            RootView().environment(state).task { await state.resumeForeground() }
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
        .alert("操作未完成", isPresented: Binding(get: { state.error != nil }, set: { if !$0 { state.error = nil } })) {
            Button("知道了", role: .cancel) { state.error = nil }
        } message: { Text(state.error ?? "") }
    }
}
