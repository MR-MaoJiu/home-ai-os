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
                state.configureBackgroundSync(enabled: UserDefaults.standard.bool(forKey: BackgroundSync.preference))
            } else if phase == .active {
                Task { await state.resumeForeground() }
            }
        }
    }
}

struct RootView: View {
    @Environment(AppState.self) private var state
    var body: some View {
        @Bindable var state = state
        TabView {
            Tab("AI", systemImage: "sparkles") { NavigationStack { ChatView().id(state.connectionRevision) } }
            Tab("活动", systemImage: "clock.arrow.circlepath") { NavigationStack { ActivityView() } }
            Tab("自动化", systemImage: "bolt") { NavigationStack { AutomationsView() } }
            Tab("数据", systemImage: "externaldrive") { NavigationStack { DataView() } }
            Tab("设置", systemImage: "gearshape") { NavigationStack { SettingsView() } }
        }
        .tint(.teal)
        .alert("操作未完成", isPresented: Binding(get: { state.error != nil }, set: { if !$0 { state.error = nil } })) {
            Button("知道了", role: .cancel) { state.error = nil }
        } message: { Text(state.error ?? "") }
    }
}
