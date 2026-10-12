import XCTest
import UIKit
@testable import HomeAI

@MainActor
final class AndroidPresentationTests: XCTestCase {
    func testClosedTaskDoesNotReopenButNextTaskDoes() {
        var policy = AndroidPresentationPolicy()
        XCTAssertTrue(policy.observe(taskID: "task-a", mode: "ai"))
        XCTAssertEqual(policy.mode, .inline)
        policy.close()
        XCTAssertFalse(policy.observe(taskID: "task-a", mode: "ai"))
        XCTAssertEqual(policy.mode, .closed)
        XCTAssertTrue(policy.observe(taskID: "task-b", mode: "ai"))
    }

    func testCompletionDoesNotRemovePictureAndCloseSuppressesSameTaskResume() {
        var policy = AndroidPresentationPolicy()
        XCTAssertTrue(policy.observe(taskID: "task-a", mode: "ai"))
        XCTAssertFalse(policy.observe(taskID: nil, mode: "paused"))
        XCTAssertEqual(policy.mode, .inline)
        policy.close()
        XCTAssertFalse(policy.observe(taskID: "task-a", mode: "ai"))
    }

    func testPresentationSwitchDoesNotChangeStoreOrStopConnection() {
        let api = APIClient(persistConnection: false)
        let workspace = AndroidWorkspaceStore()
        let session = AndroidPresentationCoordinator(api: api, workspace: workspace)
        session.showFullScreen()
        XCTAssertTrue(session.workspace === workspace)
        XCTAssertEqual(session.mode, .fullScreen)
        session.showInline()
        session.fullScreenDismissed()
        XCTAssertEqual(session.mode, .inline, "切到小窗导致的视图消失不能关闭会话")
        XCTAssertTrue(session.workspace === workspace)
        session.showFullScreen()
        session.fullScreenDismissed()
        XCTAssertEqual(session.mode, .fullScreen, "旧页面迟到关闭回调不能关闭新全屏")
        XCTAssertTrue(session.workspace === workspace)
        session.close()
        XCTAssertEqual(session.mode, .closed)
    }

    func testViewingEventsReleaseHumanOnlyOnceWithoutWaitingForPresentation() async throws {
        let handler = AndroidViewingControlRelease()
        let gate = AsyncOperationGate()
        try await gate.acquire()
        var mode = "human", calls = 0
        func request() {
            handler.request(isChanging: { false }, isOwned: { mode == "human" }, release: {
                calls += 1
                _ = try? await gate.withPermit { true }
                mode = "paused"
            })
        }
        request(); request(); request()
        for _ in 0..<100 {
            if calls == 1 { break }
            await Task.yield()
        }
        XCTAssertEqual(calls, 1)
        request() // 系统 PiP didStart 和 background 再次进入也不能并发提交暂停。
        XCTAssertEqual(calls, 1)
        await gate.release()
        await handler.waitUntilIdle()
        XCTAssertEqual(mode, "paused")
        request()
        await handler.waitUntilIdle()
        XCTAssertEqual(calls, 1)
    }

    func testViewingReleaseDoesNotPauseAIOrAnotherDeviceAndChecksLateOwnership() async throws {
        let handler = AndroidViewingControlRelease()
        var mode = "ai", owner = "self", changing = false, calls = 0
        func request() {
            handler.request(isChanging: { changing }, isOwned: { mode == "human" && owner == "self" }, release: {
                calls += 1; mode = "paused"
            })
        }
        request(); await handler.waitUntilIdle()
        XCTAssertEqual(calls, 0)
        mode = "human"; owner = "other"
        request(); await handler.waitUntilIdle()
        XCTAssertEqual(calls, 0)
        mode = "paused"; owner = "self"; changing = true
        request()
        mode = "ai"; changing = false
        await handler.waitUntilIdle()
        XCTAssertEqual(calls, 0, "等待中的交还 AI 成功后不能误暂停 AI")
        mode = "paused"; changing = true
        request()
        mode = "human"; changing = false
        await handler.waitUntilIdle()
        XCTAssertEqual(calls, 1, "进入小窗前尚未返回的接管成功，也应释放人工权")
        XCTAssertEqual(mode, "paused")
    }

    func testFastAndroidTaskSnapshotBeforeSendReceiptIsNotLost() {
        let workspace = AndroidWorkspaceStore()
        let session = AndroidPresentationCoordinator(api: APIClient(persistConnection: false), workspace: workspace)
        let task = TaskStateEvent.Item(id: "fast-task", status: "SUCCEEDED", used_android: true,
            android_workspace_id: "workspace", android_task_id: "fast-task")
        session.receiveTaskSnapshot([task], activeID: nil)
        XCTAssertNil(session.pendingTaskIDForAcceptance, "首份历史快照只建立基线")
        session.trackRequestedTask("fast-task")
        XCTAssertEqual(session.pendingTaskIDForAcceptance, "fast-task", "迟到的本次发送回执必须补处理安卓使用记录")
        XCTAssertEqual(session.mode, .closed, "验证绑定之前不能凭任务标识直接打开设备")
        XCTAssertTrue(session.workspace === workspace, "补处理不能创建另一台观看会话")
        session.close()
    }

    func testServerTaskUsageAndActiveWorkspaceFieldsAreDecoded() throws {
        let data = Data(#"{"type":"task.snapshot","tasks":[{"id":"task","status":"SUCCEEDED","used_android":true,"android_workspace_id":"workspace","android_task_id":"task"}],"has_more":false,"android_revision":12,"android_active_task_id":null}"#.utf8)
        let event = try JSONDecoder().decode(TaskStateEvent.self, from: data)
        XCTAssertEqual(event.tasks.first?.used_android, true)
        XCTAssertEqual(event.tasks.first?.android_workspace_id, "workspace")
        XCTAssertEqual(event.android_revision, 12)
    }

    func testLiveSystemPiPUsesSameStreamAfterOpeningIOSSettings() async throws {
        guard ProcessInfo.processInfo.environment["HOMEAI_ANDROID_PIP_CHECK"] == "1" else {
            throw XCTSkip("仅在准备好公开动画页时验收真实系统画中画；不发送安卓输入")
        }
        let registrationUntil = Date().addingTimeInterval(5)
        while AndroidPresentationAcceptance.current == nil && Date() < registrationUntil {
            try await Task.sleep(for: .milliseconds(50))
        }
        let session = try XCTUnwrap(AndroidPresentationAcceptance.current, "需要实际 App 的共享观看协调器")
        guard session.mode == .closed else { throw XCTSkip("请先关闭用户正在使用的安卓画面，避免测试抢占") }
        defer { session.close() }
        session.foreground(); session.showInline()
        let videoUntil = Date().addingTimeInterval(100)
        while !session.workspace.online || !session.player.hasVideo {
            guard Date() < videoUntil else { throw APIClient.APIError.message(session.workspace.error ?? "未收到真实视频") }
            try await Task.sleep(for: .milliseconds(100))
        }
        guard session.workspace.workspace?.mode != "human" else { throw XCTSkip("画中画只读验收前请暂停人工控制") }
        let initialStats = await session.workspace.mediaStatisticsForAcceptance()
        let first = try XCTUnwrap(initialStats)
        session.showFullScreen()
        try await Task.sleep(for: .milliseconds(500))
        XCTAssertNotNil(session.player.displayLayer.superlayer, "真实视频层必须挂在可见页面")
        XCTAssertGreaterThan(session.player.displayLayer.bounds.width, 0)
        session.showInline(startSystemPiP: true)
        let startUntil = Date().addingTimeInterval(15)
        while !session.player.active && Date() < startUntil {
            try await Task.sleep(for: .milliseconds(100))
        }
        guard session.player.active else { throw APIClient.APIError.message(session.player.error ?? "系统未启动画中画") }
        let settings = URL(string: UIApplication.openSettingsURLString)!
        guard await UIApplication.shared.open(settings) else { throw APIClient.APIError.message("未能切换到 iOS 设置验证后台画中画") }
        let backgroundUntil = Date().addingTimeInterval(5)
        while UIApplication.shared.applicationState != .background && Date() < backgroundUntil {
            try await Task.sleep(for: .milliseconds(100))
        }
        XCTAssertEqual(UIApplication.shared.applicationState, .background)
        for _ in 0..<5 {
            try await Task.sleep(for: .seconds(1))
            XCTAssertTrue(session.player.active)
            XCTAssertTrue(session.workspace.online)
        }
        let finalStats = await session.workspace.mediaStatisticsForAcceptance()
        let last = try XCTUnwrap(finalStats)
        XCTAssertEqual(first.connectionID, last.connectionID, "小窗、全屏、系统画中画必须复用同一连接")
        XCTAssertGreaterThan(last.videoFramesDecoded, first.videoFramesDecoded)
        print("ANDROID_PIP_REAL active=true background=true same_connection=true decoded_delta=\(last.videoFramesDecoded - first.videoFramesDecoded) android_inputs=0")
    }
}
