import SwiftUI

@main struct HomeAIApp: App {
    @State private var state = AppState()
    var body: some Scene {
        WindowGroup {
            RootView().environment(state).task { await state.restore() }
        }
    }
}

struct RootView: View {
    @Environment(AppState.self) private var state
    var body: some View {
        @Bindable var state = state
        TabView {
            Tab("AI", systemImage: "sparkles") { NavigationStack { ChatView() } }
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
