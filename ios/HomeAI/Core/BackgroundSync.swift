import BackgroundTasks
import UIKit

@MainActor
enum BackgroundSync {
    static let identifier = "dev.homeai.sync.refresh"
    static let preference = "backgroundSyncEnabled"

    static func schedule(enabled: Bool) -> String {
        BGTaskScheduler.shared.cancel(taskRequestWithIdentifier: identifier)
        guard enabled else { return "后台同步已关闭" }
        guard UIApplication.shared.backgroundRefreshStatus == .available else {
            return "系统未允许后台刷新，可在前台同步"
        }
        let request = BGAppRefreshTaskRequest(identifier: identifier)
        request.earliestBeginDate = Date(timeIntervalSinceNow: 15 * 60)
        do {
            try BGTaskScheduler.shared.submit(request)
            return "已向系统申请，执行时间由 iOS 决定"
        } catch {
            return "系统暂未接受后台申请，可在前台同步"
        }
    }
}
