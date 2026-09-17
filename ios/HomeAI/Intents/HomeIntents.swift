import AppIntents
import Foundation
import Observation

enum HomeDestination: String, Hashable {
    case ai, activity, automations, data, settings
}

@MainActor @Observable
final class IntentRouter {
    static let shared = IntentRouter()
    var destination: HomeDestination = .ai
}

struct CreateHomeReminderIntent: AppIntent {
    static let title: LocalizedStringResource = "创建家庭提醒"
    static let description = IntentDescription("向已配对的家庭服务器提交提醒任务；完成状态在活动页面查看。")
    static let openAppWhenRun = true
    static let authenticationPolicy: IntentAuthenticationPolicy = .requiresLocalDeviceAuthentication
    @Parameter(title: "提醒内容") var text: String
    static var parameterSummary: some ParameterSummary { Summary("创建家庭提醒：\(\.$text)") }

    func perform() async throws -> some IntentResult & ReturnsValue<String> & ProvidesDialog {
        let identifier = try await ReminderIntentService.shared.submit(title: text)
        await MainActor.run { IntentRouter.shared.destination = .activity }
        return .result(value: identifier, dialog: "家庭提醒任务已提交，请在活动页面查看执行结果。")
    }
}

struct OpenHomeActivityIntent: AppIntent {
    static let title: LocalizedStringResource = "查看家庭任务与审批"
    static let description = IntentDescription("打开 Home AI 的活动页面，查看任务进度和待确认操作。")
    static let openAppWhenRun = true
    static let authenticationPolicy: IntentAuthenticationPolicy = .requiresLocalDeviceAuthentication
    func perform() async throws -> some IntentResult {
        await MainActor.run { IntentRouter.shared.destination = .activity }
        return .result()
    }
}

struct HomeShortcuts: AppShortcutsProvider {
    static var appShortcuts: [AppShortcut] {
        AppShortcut(intent: CreateHomeReminderIntent(), phrases: ["用\(.applicationName)创建家庭提醒"], shortTitle: "创建家庭提醒", systemImageName: "checklist")
        AppShortcut(intent: OpenHomeActivityIntent(), phrases: ["查看\(.applicationName)的任务"], shortTitle: "查看任务与审批", systemImageName: "clock.arrow.circlepath")
    }
}
