import SwiftUI
import EventKit

struct ReminderSyncSettings: View {
    @Environment(AppState.self) private var state
    @State private var store = EKEventStore()
    @State private var calendars: [EKCalendar] = []
    @State private var selected = ""
    @State private var namespace: String?
    @State private var accountNamespace: String?
    @State private var automatic = false
    @State private var preview: [DataEntry] = []
    @State private var confirming = false
    @State private var pending: ReminderSyncPreview?
    @State private var previewGeneration = UUID()
    @State private var result = ""

    private var target: EKCalendar? { calendars.first { $0.calendarIdentifier == selected } }
    var body: some View {
        Form {
            Section("写入系统提醒事项") {
                Text("只同步当前成员在家庭服务器创建的提醒。手机导入记录不会再次导出；双向修改冲突时不自动覆盖。")
                Button("授权并读取可写列表") { Task { await authorize() } }
                if !calendars.isEmpty {
                    Picker("目标列表", selection: Binding(get: { selected }, set: { selected = $0; automatic = false; pending = nil; preview = []; previewGeneration = UUID(); savePreference() })) {
                        Text("请选择").tag("")
                        ForEach(calendars, id: \.calendarIdentifier) { calendar in
                            Text(calendar.title + " · " + calendar.source.title).tag(calendar.calendarIdentifier)
                        }
                    }
                    if target?.source.sourceType == .local {
                        Toggle("前台自动同步到本机列表", isOn: $automatic)
                        Text("仅在前台在线同步后执行。不会在后台申请新权限。").font(.caption)
                    } else if target != nil {
                        Text("这个列表可能由 iCloud 或其他账号同步。每次写入前都需要预览并确认，不会自动导出私人内容。").font(.caption)
                    }
                    Button("预览本次同步") { Task { await state.perform {
                        guard let selectedCalendar = target else { return }
                        let generation = previewGeneration
                        let calendarID = selectedCalendar.calendarIdentifier
                        let sourceID = selectedCalendar.source.sourceIdentifier
                        let sourceType = selectedCalendar.source.sourceType.rawValue
                        let calendarName = selectedCalendar.title + " · " + selectedCalendar.source.title
                        let current = try await state.api.syncNamespace()
                        guard current == namespace else { throw APIClient.APIError.message("配对已切换，请重新选择列表") }
                        try await state.loadData()
                        struct Me: Decodable { let user_id: String }
                        let me = try JSONDecoder().decode(Me.self, from: await state.api.request("GET", "/api/v1/me", expectedNamespace: current))
                        var candidates: [DataEntry] = []
                        for item in state.records where item.kind == "reminder.item" && (item.source == nil || item.source == "core") {
                            let fresh = try JSONDecoder().decode(DataEntry.self, from: await state.api.request("GET", "/api/v1/data/" + item.id, expectedNamespace: current))
                            if fresh.source == "core" && fresh.owner_id == me.user_id && fresh.sensitivity != "SECRET" { candidates.append(fresh) }
                        }
                        guard generation == previewGeneration, selected == calendarID,
                              current == (try await state.api.syncNamespace()), !Task.isCancelled else {
                            throw APIClient.APIError.message("连接或目标列表已变化，请重新预览")
                        }
                        preview = candidates
                        pending = ReminderSyncPreview(namespace: current, calendarID: calendarID, sourceID: sourceID, sourceType: sourceType, calendarName: calendarName, records: candidates)
                        confirming = true
                    } } }.disabled(target == nil || state.busy)
                }
            }
            if !preview.isEmpty {
                Section("本次预览") { ForEach(preview) { record in Text(record.title) } }
            }
            if !result.isEmpty || !state.systemReminderStatus.isEmpty { Section("同步结果") { Text(result.isEmpty ? state.systemReminderStatus : result) } }
            Section("编辑与删除") {
                Text("系统中手动删除或移走的条目不会自动复活。服务器删除时，仅清除未被手动修改的 Home AI 条目；有冲突时保留内容等待核对。更换列表不会自动搬运旧条目。")
            }
        }.navigationTitle("系统提醒同步")
        .task(id: state.connectionRevision) { await restore() }
        .onChange(of: automatic) { _, _ in savePreference() }
        .confirmationDialog("同步到所选系统列表？", isPresented: $confirming, titleVisibility: .visible) {
            Button("确认本次同步") { Task { await sync() } }
            Button("取消", role: .cancel) { pending = nil }
        } message: {
            Text("目标：\(pending?.calendarName ?? "未选择")。本次会同步预览中的提醒，并核对以前由 Home AI 创建的条目。所选账号可能将内容上传至云端；不会覆盖已发现的双向冲突。")
        }
    }

    private func restore() async {
        let generation = UUID()
        previewGeneration = generation; pending = nil; confirming = false
        preview = []; result = ""; namespace = nil; accountNamespace = nil; calendars = []; selected = ""; automatic = false
        guard state.connected else { return }
        do {
            let current = try await state.api.syncNamespace()
            let owner = try await state.api.ownerIdentity(expectedNamespace: current)
            let available = EKEventStore.authorizationStatus(for: .reminder) == .fullAccess
                ? store.calendars(for: .reminder).filter(\.allowsContentModifications) : []
            guard generation == previewGeneration, !Task.isCancelled,
                  current == (try await state.api.syncNamespace()) else { return }
            namespace = current
            accountNamespace = owner.namespace
            calendars = available
            selected = UserDefaults.standard.string(forKey: SystemReminderSync.preferenceKey(owner.namespace)) ?? ""
            automatic = UserDefaults.standard.bool(forKey: SystemReminderSync.preferenceKey(owner.namespace) + ".automatic")
        } catch {
            if generation == previewGeneration && !Task.isCancelled { result = error.localizedDescription }
        }
    }
    private func authorize() async {
        await state.perform {
            // 旧 SDK 的 async 入口会跨隔离域传递 EKEventStore；回调只传递结果。
            let granted: Bool = try await withCheckedThrowingContinuation { continuation in
                store.requestFullAccessToReminders { granted, error in
                    if let error { continuation.resume(throwing: error) }
                    else { continuation.resume(returning: granted) }
                }
            }
            guard granted else { throw APIClient.APIError.message("提醒事项未授权") }
            await restore()
        }
    }
    private func savePreference() {
        guard let namespace = accountNamespace else { return }
        UserDefaults.standard.set(selected, forKey: SystemReminderSync.preferenceKey(namespace))
        UserDefaults.standard.set(automatic && target?.source.sourceType == .local, forKey: SystemReminderSync.preferenceKey(namespace) + ".automatic")
    }
    private func sync() async {
        guard let plan = pending else { return }
        pending = nil
        await state.perform {
            let current = try await state.api.syncNamespace()
            guard plan.matches(namespace: current, calendarID: selected),
                  store.calendar(withIdentifier: plan.calendarID)?.source.sourceIdentifier == plan.sourceID else {
                throw APIClient.APIError.message("连接或目标账号已变化，请重新预览")
            }
            let report = try await SystemReminderSync.shared.synchronize(records: plan.records, api: state.api,
                calendarID: plan.calendarID, expectedNamespace: plan.namespace, expectedSourceID: plan.sourceID, expectedSourceType: plan.sourceType, allowCloudExport: true, store: store)
            result = report.summary
        }
    }
}
