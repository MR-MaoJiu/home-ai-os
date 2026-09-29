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

import UserNotifications
import Observation

struct ClientNotification: Decodable, Identifiable, Sendable {
    let id: String
    let kind: String
    let scope: String
    let created_at: Double
    let read_at: Double?
    let task_id: String?
    let conversation_id: String?
    let automation_id: String?
    let record_id: String?
    let status: String?
    var title: String {
        if kind == "system.test" { return "通知测试" }
        if kind == "reminder.due" { return scope == "family" ? "家庭提醒已到期" : "提醒已到期" }
        if status == "AWAITING_APPROVAL" { return "有操作需要确认" }
        return scope == "family" ? "家庭执行结果" : "我的执行结果"
    }
}

@MainActor @Observable
final class ClientNotifications {
    static let shared = ClientNotifications()
    var authorization = "unknown"
    var status = ""
    var pendingNotificationID: String?
    private var registration: String?
    private var syncing = false
    private var registrationRequested = false

    func requestPermission() async {
        do {
            _ = try await UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound, .badge])
            await synchronize(api: AppServices.api)
        } catch { status = "通知授权未完成：" + error.localizedDescription }
    }

    private static var pushEnvironment: String {
        // 导出后的签名配置可能改变环境，以应用自身的 provisioning profile 为准。
        if let url = Bundle.main.url(forResource: "embedded", withExtension: "mobileprovision"),
           let data = try? Data(contentsOf: url),
           let start = data.range(of: Data("<?xml".utf8)), let end = data.range(of: Data("</plist>".utf8)),
           start.lowerBound < end.upperBound,
           let plist = try? PropertyListSerialization.propertyList(from: data.subdata(in: start.lowerBound..<end.upperBound), format: nil) as? [String: Any],
           let entitlements = plist["Entitlements"] as? [String: Any],
           let environment = entitlements["aps-environment"] as? String {
            return environment == "production" ? "production" : "sandbox"
        }
        #if DEBUG
        return "sandbox"
        #else
        return "production"
        #endif
    }

    func registrationFailed() {
        registrationRequested = false
        status = "系统推送注册失败，仍可查看通知列表"
    }

    func didRegister(_ token: Data) async {
        let hex = token.map { String(format: "%02x", $0) }.joined()
        do { try DeviceIdentity.save(Data(hex.utf8), name: "push-device-token") }
        catch { status = "通知设备凭据保存失败"; return }
        await synchronize(api: AppServices.api)
    }

    func synchronize(api: APIClient) async {
        guard !syncing else { return }
        syncing = true; defer { syncing = false }
        let settings = await UNUserNotificationCenter.current().notificationSettings()
        switch settings.authorizationStatus {
        case .authorized, .ephemeral: authorization = "authorized"
        case .provisional: authorization = "provisional"
        case .denied: authorization = "denied"
        default: authorization = "notDetermined"
        }
        guard await api.isConnected() else { status = "配对后接收家庭服务器通知"; return }
        do {
            let namespace = try await api.syncNamespace()
            guard ["authorized", "provisional"].contains(authorization) else {
                if authorization == "denied" {
                    _ = try await api.request("DELETE", "/api/v1/devices/current/push", expectedNamespace: namespace)
                    registration = nil
                }
                status = authorization == "denied" ? "系统通知已关闭，仍可查看通知列表" : "允许通知后接收服务器提醒"
                return
            }
            if !registrationRequested {
                registrationRequested = true
                UIApplication.shared.registerForRemoteNotifications()
            }
            guard let raw = DeviceIdentity.read("push-device-token"), let token = String(data: raw, encoding: .utf8) else {
                status = "等待系统注册通知设备"; return
            }
            let environment = Self.pushEnvironment
            let identity = namespace + ":" + token + ":" + environment + ":" + authorization
            if identity != registration {
                let body = try JSONSerialization.data(withJSONObject: ["token": token, "environment": environment, "authorization": authorization])
                _ = try await api.request("PUT", "/api/v1/devices/current/push", body: body, expectedNamespace: namespace)
                registration = identity
            }
            struct Status: Decodable { let push_configured: Bool; let worker_online: Bool; let status: String }
            let result = try JSONDecoder().decode(Status.self, from: await api.request("GET", "/api/v1/notifications/status", expectedNamespace: namespace))
            switch result.status {
            case "disabled": status = "服务器已关闭系统推送，通知列表可用"
            case "invalid_configuration": status = "服务器推送配置无效，通知列表可用"
            case "not_configured": status = "服务器尚未配置系统推送，通知列表可用"
            case "ready": status = "服务器推送已配置"
            case "worker_offline": status = "推送已配置，等待服务器通知进程"
            default: status = "暂时无法确认服务器推送状态，通知列表可用"
            }
        } catch { status = "通知连接未完成，恢复网络后重试" }
    }
}

final class HomeAINotificationDelegate: NSObject, UIApplicationDelegate, UNUserNotificationCenterDelegate {
    func application(_ application: UIApplication, didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil) -> Bool {
        UNUserNotificationCenter.current().delegate = self
        return true
    }
    func application(_ application: UIApplication, didRegisterForRemoteNotificationsWithDeviceToken deviceToken: Data) {
        Task { @MainActor in await ClientNotifications.shared.didRegister(deviceToken) }
    }
    func application(_ application: UIApplication, didFailToRegisterForRemoteNotificationsWithError error: Error) {
        Task { @MainActor in ClientNotifications.shared.registrationFailed() }
    }
    nonisolated func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification,
                                            withCompletionHandler completionHandler: @escaping @Sendable (UNNotificationPresentationOptions) -> Void) {
        Task { @MainActor in completionHandler([.banner, .sound, .list]) }
    }
    nonisolated func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse,
                                            withCompletionHandler completionHandler: @escaping @Sendable () -> Void) {
        Self.finishNotificationResponse(identifier: response.notification.request.content.userInfo["notification_id"] as? String,
                                        completion: completionHandler)
    }

    nonisolated static func finishNotificationResponse(identifier: String?, completion: @escaping @Sendable () -> Void) {
        // 冷启动通知完成回调会触发 UIKit 恢复状态，必须与页面路由一起在主线程完成。
        // 不使用 async 代理桥接，避免 await 返回后系统 completion 落到工作线程。
        Task { @MainActor in
            defer { completion() }
            guard let identifier, let id = UUID(uuidString: identifier) else { return }
            // 推送仅携带通知标识，详情仍通过已配对身份向家庭服务器读取。
            ClientNotifications.shared.pendingNotificationID = id.uuidString.lowercased()
        }
    }
}
