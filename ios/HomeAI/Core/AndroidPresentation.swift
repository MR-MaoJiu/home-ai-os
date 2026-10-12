import Foundation
import Observation
import UIKit

/// 呈现模式变化只换视图，不产生新的设备会话，也不改变服务端 AI 任务。
enum AndroidPresentationMode: String, Sendable { case closed, inline, fullScreen }

struct AndroidPresentationPolicy {
    private(set) var mode: AndroidPresentationMode = .closed
    private(set) var activeTaskID: String?
    private var dismissedTasks: [String] = []
    private var presentedTaskID: String?
    mutating func show(_ mode: AndroidPresentationMode) { self.mode = mode }
    mutating func observe(taskID: String?, mode: String?) -> Bool {
        activeTaskID = taskID
        guard mode == "ai", let taskID else { return false }
        if self.mode != .closed { presentedTaskID = taskID; return false }
        guard !dismissedTasks.contains(taskID) else { return false }
        presentedTaskID = taskID
        self.mode = .inline
        return true
    }
    mutating func close() {
        if let taskID = presentedTaskID ?? activeTaskID, !dismissedTasks.contains(taskID) {
            dismissedTasks.append(taskID)
            if dismissedTasks.count > 64 { dismissedTasks.removeFirst() }
        }
        mode = .closed
    }
    mutating func reset() { self = AndroidPresentationPolicy() }
}

/// 只读观看只释放本人的人工租约；多个呈现事件共用一个待处理请求。
@MainActor
final class AndroidViewingControlRelease {
    private var operation: Task<Void, Never>?
    private var run: UUID?

    func request(isChanging: @escaping @MainActor () -> Bool,
                 isOwned: @escaping @MainActor () -> Bool,
                 release: @escaping @MainActor () async -> Void) {
        guard operation == nil, isChanging() || isOwned() else { return }
        let identifier = UUID(); run = identifier
        operation = Task { [weak self] in
            guard let self else { return }
            defer { if self.run == identifier { self.run = nil; self.operation = nil } }
            let deadline = Date().addingTimeInterval(15)
            while isChanging() && !Task.isCancelled && Date() < deadline {
                do { try await Task.sleep(for: .milliseconds(25)) } catch { return }
            }
            guard !Task.isCancelled, self.run == identifier, !isChanging(), isOwned() else { return }
            await release()
        }
    }
    func cancel() { run = nil; operation?.cancel(); operation = nil }
    func waitUntilIdle() async { await operation?.value }
}

@MainActor @Observable
final class AndroidPresentationCoordinator {
    let workspace: AndroidWorkspaceStore
    let player: AndroidPictureInPicturePlayer
    private(set) var policy = AndroidPresentationPolicy()
    private(set) var appForeground = true
    private(set) var chatVisible = false
    private var api: APIClient
    private var namespace: String?
    private var authorized = false
    private var taskSnapshotPrimed = false
    private var seenTaskUses: Set<String> = []
    private var requestedTaskIDs: Set<String> = []
    private var pendingTaskUse: (id: String, workspaceID: String?)?
    private var connectionTask: Task<Void, Never>?
    private var connectionRun: UUID?
    private var activityTask: Task<Void, Never>?
    private var activityRun: UUID?
    private var activityReading = false
    private var lastActivityRead = Date.distantPast
    private var lifecycle = UUID()
    private var backgroundTransition: Task<Void, Never>?
    private let viewingControlRelease = AndroidViewingControlRelease()
    var showChat: (() -> Void)?

    init(api: APIClient, workspace: AndroidWorkspaceStore = AndroidWorkspaceStore()) {
        self.api = api; self.workspace = workspace; player = AndroidPictureInPicturePlayer()
        workspace.onVideoTrack = { [weak player] track in player?.attach(track) }
        player.onRestore = { [weak self] in self?.showFullScreen() }
        player.onStopped = { [weak self] preserve in
            guard let self else { return }
            if !preserve { self.close() }
        }
        player.onActive = { [weak self] in
            self?.workspace.cancelTouches(); self?.releaseHumanForViewing()
        }
    }

    var mode: AndroidPresentationMode { policy.mode }
    var wantsFullScreen: Bool { mode == .fullScreen }

    func showFullScreen() {
        workspace.cancelTouches()
        policy.show(.fullScreen)
        player.stop(preservingPresentation: true)
        ensureConnection()
    }
    func showInline(startSystemPiP: Bool = false) {
        workspace.cancelTouches()
        policy.show(.inline)
        releaseHumanForViewing()
        showChat?()
        ensureConnection()
        if startSystemPiP { player.requestStart() }
    }
    func releaseHumanForViewing() {
        workspace.cancelTouches()
        let started = lifecycle
        viewingControlRelease.request(isChanging: { [weak self] in
            guard let self, self.lifecycle == started else { return false }
            return self.workspace.changingControl
        }, isOwned: { [weak self] in
            guard let self, self.lifecycle == started,
                  self.mode == .inline || self.player.active || !self.appForeground else { return false }
            return self.workspace.online && self.workspace.workspace?.controlled(by: self.workspace.deviceID) == true
        }, release: { [weak self] in
            guard let self, self.lifecycle == started else { return }
            // 只变成 paused；不替用户交还 AI，也不恢复任何旧任务。
            await self.workspace.setControl("paused")
        })
    }

    func fullScreenDismissed() {
        // UIKit 迟到的 dismiss 回调不能关闭已切换到小窗或新全屏的共享会话。
        workspace.cancelTouches()
    }
    func close() {
        lifecycle = UUID(); policy.close(); viewingControlRelease.cancel()
        connectionTask?.cancel(); connectionTask = nil; connectionRun = nil
        backgroundTransition?.cancel(); backgroundTransition = nil
        player.close(); workspace.stop()
        updateActivityMonitoring()
    }
    func identityChanged() {
        close(); policy.reset(); namespace = nil; authorized = false
        taskSnapshotPrimed = false; seenTaskUses.removeAll(); requestedTaskIDs.removeAll(); pendingTaskUse = nil
        activityTask?.cancel(); activityTask = nil; activityRun = nil
    }
    func setChatVisible(_ visible: Bool) {
        chatVisible = visible
        updateActivityMonitoring()
    }

    func foreground() {
        appForeground = true; authorized = true
        backgroundTransition?.cancel(); backgroundTransition = nil
        if mode == .inline || player.active { releaseHumanForViewing() }
        ensureConnection(); updateActivityMonitoring()
    }

    func background() {
        appForeground = false
        workspace.cancelTouches(); releaseHumanForViewing()
        activityTask?.cancel(); activityTask = nil; activityRun = nil
        if !player.active && mode != .closed && player.hasVideo { player.requestStart() }
        let started = lifecycle
        backgroundTransition?.cancel()
        backgroundTransition = Task { [weak self] in
            guard let self else { return }
            guard self.lifecycle == started, !Task.isCancelled else { return }
            if !self.player.active && self.mode != .closed && self.player.hasVideo {
                self.player.requestStart()
            }
            // 仅给系统完成真实 PiP 切换的短暂时间；失败后照常停止后台屏幕连接。
            let deadline = Date().addingTimeInterval(2)
            while !self.player.active && self.player.starting && Date() < deadline && !Task.isCancelled {
                try? await Task.sleep(for: .milliseconds(50))
            }
            guard self.lifecycle == started, !Task.isCancelled, !self.appForeground else { return }
            if !self.player.active {
                self.connectionTask?.cancel(); self.connectionTask = nil; self.connectionRun = nil
                self.workspace.stop(message: "后台未开启画中画，屏幕连接已暂停")
            }
        }
    }

    func reconnect() {
        guard appForeground, mode != .closed else { return }
        connectionTask?.cancel(); connectionTask = nil; connectionRun = nil
        workspace.stop()
        ensureConnection()
    }

    private func ensureConnection() {
        guard authorized, mode != .closed, appForeground || player.active,
              !workspace.online, !workspace.connecting, connectionTask == nil else { return }
        let run = UUID(); connectionRun = run
        connectionTask = Task { [weak self] in
            guard let self else { return }
            await self.workspace.connect(api: self.api)
            guard self.connectionRun == run else { return }
            self.connectionTask = nil; self.connectionRun = nil
            if self.mode == .inline || self.player.active { self.releaseHumanForViewing() }
        }
        updateActivityMonitoring()
    }

    func refreshActivity(force: Bool = false) async {
        guard authorized, appForeground, !activityReading, force || Date().timeIntervalSince(lastActivityRead) >= 1.5 else { return }
        activityReading = true; lastActivityRead = .now
        let started = lifecycle
        defer { activityReading = false }
        do {
            let current = try await api.syncNamespace()
            let raw = try await api.request("GET", "/api/v1/android/workspace", expectedNamespace: current)
            guard lifecycle == started, appForeground, !Task.isCancelled, await api.cachedNamespace() == current else { return }
            if let namespace, namespace != current { identityChanged(); return }
            namespace = current
            let value = try JSONDecoder().decode(AndroidWorkspaceEnvelope.self, from: raw).workspace
            var show = policy.observe(taskID: value?.active_task_id, mode: value?.mode)
            if let pendingTaskUse, let value, pendingTaskUse.workspaceID == nil || pendingTaskUse.workspaceID == value.id {
                show = policy.observe(taskID: pendingTaskUse.id, mode: "ai") || show
                self.pendingTaskUse = nil
            }
            if show { ensureConnection() }
        } catch { /* 状态探测失败不拆除仍健康的独立视频，也不猜测 AI 是否在操作。 */ }
    }

    func trackRequestedTask(_ id: String) {
        requestedTaskIDs.insert(id)
        // 首份任务快照可能先于发消息回执到达，回执需补处理已被历史基线记录的本次任务。
        if seenTaskUses.contains(id) {
            pendingTaskUse = (id, nil)
            Task { [weak self] in await self?.refreshActivity(force: true) }
        }
    }

    #if DEBUG
    var pendingTaskIDForAcceptance: String? { pendingTaskUse?.id }
    #endif

    func receiveTaskSnapshot(_ tasks: [TaskStateEvent.Item], activeID: String?) {
        let used = tasks.filter { $0.used_android == true }
        let newUse = used.first { task in
            let id = task.android_task_id ?? task.id
            return !seenTaskUses.contains(id) && (taskSnapshotPrimed || requestedTaskIDs.contains(id) || id == activeID)
        }
        for task in used { seenTaskUses.insert(task.android_task_id ?? task.id) }
        taskSnapshotPrimed = true
        if let newUse { pendingTaskUse = (newUse.android_task_id ?? newUse.id, newUse.android_workspace_id) }
        else if let activeID { pendingTaskUse = (activeID, nil) }
        if pendingTaskUse != nil { Task { [weak self] in await self?.refreshActivity(force: true) } }
    }

    private func updateActivityMonitoring() {
        guard authorized, appForeground, chatVisible || mode != .closed else {
            activityTask?.cancel(); activityTask = nil; activityRun = nil; return
        }
        guard activityTask == nil else { return }
        let run = UUID(); activityRun = run
        activityTask = Task { [weak self] in
            while !Task.isCancelled {
                guard let self, self.activityRun == run else { return }
                await self.refreshActivity()
                do { try await Task.sleep(for: .seconds(2)) } catch { return }
            }
        }
    }
}

#if DEBUG
@MainActor enum AndroidPresentationAcceptance {
    static weak var current: AndroidPresentationCoordinator?
}
#endif
