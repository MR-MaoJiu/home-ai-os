import Foundation
import Observation
@preconcurrency import WebRTC

struct AndroidWorkspaceEnvelope: Decodable, Sendable { let workspace: AndroidWorkspace? }
struct AndroidWorkspace: Decodable, Sendable, Identifiable {
    let id: String
    let label: String
    let backend: String
    let status: String
    let mode: String
    let epoch: Int
    let controller_device_id: String?
    let packages: [String]
    let last_sequence: Int?
    var active_task_id: String? = nil
    var active_task_status: String? = nil

    func controlled(by deviceID: String) -> Bool {
        mode == "human" && !deviceID.isEmpty && controller_device_id == deviceID
    }
    var modeLabel: String {
        switch mode { case "human": "人工操作"; case "ai": "AI 操作"; default: "已暂停" }
    }
}
struct AndroidScreenFrame: Decodable, Sendable {
    let frame_id: String
    let width: Int
    let height: Int
    let epoch: Int
    let mode: String
    let blocked_reason: String?
    var touch_available: Bool? = nil
    var touch_reason: String? = nil
    var audio_available: Bool? = nil
    var audio_reason: String? = nil
    var display_control_supported: Bool? = nil
    var device_screen_off: Bool? = nil
    var display_verified: Bool? = nil
    var display_reason: String? = nil
    var display_revision: Int? = nil

    var displayState: AndroidDisplayState? {
        guard let supported = display_control_supported, let revision = display_revision, revision >= 0 else { return nil }
        return AndroidDisplayState(display_control_supported: supported, device_screen_off: device_screen_off,
            display_verified: display_verified ?? false, display_reason: display_reason, display_revision: revision)
    }

    var isValid: Bool {
        !frame_id.isEmpty && frame_id.count <= 128 && (1...8192).contains(width) &&
        (1...8192).contains(height) && epoch >= 0 && ["paused", "human", "ai"].contains(mode)
    }
}

/// 关闭的是安卓实体面板；未经传感器确认的监督状态只能展示为“已请求”。
struct AndroidDisplayState: Decodable, Equatable, Sendable {
    let display_control_supported: Bool
    let device_screen_off: Bool?
    let display_verified: Bool
    let display_reason: String?
    let display_revision: Int

    var allowsChange: Bool { display_control_supported && device_screen_off != nil && display_revision >= 0 }
    var status: String {
        guard display_control_supported else { return display_reason ?? "此设备暂不支持控制实体屏幕" }
        guard let off = device_screen_off else { return display_reason ?? "实体屏幕状态尚未确认" }
        if display_verified { return off ? "安卓实体屏幕已关闭" : "安卓实体屏幕已开启" }
        return off ? "已请求关闭实体屏幕" : "实体屏幕跟随系统状态"
    }
    func supersedes(_ previous: AndroidDisplayState?) -> Bool {
        display_revision >= 0 && display_revision >= (previous?.display_revision ?? -1)
    }
}

/// 短期输入焦点凭据只用于当前一次文字提交，不保存到缓存。
struct AndroidInputFocus: Decodable, Sendable {
    let focus_token: String
    let expires_at: Double
    func isValid(at date: Date) -> Bool {
        (16...256).contains(focus_token.utf8.count) &&
        expires_at.isFinite && expires_at > date.timeIntervalSince1970
    }
}

/// 跨全屏页面实例串行修改控制权，退出清理不得覆盖后来的成员操作意图。
@MainActor
final class AndroidControlLifecycle {
    let gate = AsyncOperationGate()
    private var revisions: [String: Int] = [:]

    func revision(namespace: String, workspaceID: String) -> Int {
        revisions[namespace + ":" + workspaceID, default: 0]
    }
    @discardableResult func registerIntent(namespace: String, workspaceID: String) -> Int {
        let key = namespace + ":" + workspaceID
        revisions[key, default: 0] += 1
        return revisions[key, default: 0]
    }
    func canRelease(namespace: String, workspaceID: String, revision: Int) -> Bool {
        self.revision(namespace: namespace, workspaceID: workspaceID) == revision
    }
    static func shouldPauseLateResponse(_ response: AndroidWorkspace?, requestedMode: String,
                                       workspaceID: String, deviceID: String) -> Bool {
        guard let response, response.id == workspaceID, response.epoch > 0 else { return false }
        // 明确交还 AI 后，离开观看页不应中断家庭服务器任务。
        return requestedMode == "human" && response.controlled(by: deviceID)
    }
}

/// 画面只保存在内存。执行与控制权始终由家庭服务器决定。
@MainActor @Observable
final class AndroidWorkspaceStore {
    private static let controls = AndroidControlLifecycle()
    private(set) var workspace: AndroidWorkspace?
    private(set) var screen: AndroidScreenFrame?
    private(set) var track: RTCVideoTrack?
    private(set) var connecting = false
    private(set) var online = false
    private(set) var recovering = false
    private(set) var changingControl = false
    private(set) var sendingInput = false
    private(set) var touchActive = false
    private(set) var touchCancellationID = UUID()
    private(set) var error: String?
    private(set) var deviceID = ""
    private(set) var restoringTouch = false
    private var localTouchAvailable = false
    private var remoteTouchAvailable = true
    private var localTouchReason: String?
    private var remoteTouchReason: String?
    var onVideoTrack: ((RTCVideoTrack?) -> Void)?
    private(set) var audioSupported = false
    private(set) var audioAvailable = false
    private(set) var audioRequested = true
    private(set) var audioPlaying = false
    private(set) var displayState: AndroidDisplayState?
    private(set) var changingDisplay = false
    private(set) var displayError: String?
    private var displayRefreshTask: Task<Void, Never>?
    private var displayOperation = UUID()
    private var audioUnavailableReason: String?
    private var audioPlaybackReason: String?
    private var api: APIClient?
    private var namespace: String?
    private var transport: AndroidScreenTransport?
    private var streamID: String?
    private var generation = UUID()
    private var monitor: Task<Void, Never>?
    private var receivedAt = Date.distantPast
    private var streamFailure: String?
    private var sequence = 0
    private var touchPipeline: AndroidTouchPipeline?
    private var touchTimer: Task<Void, Never>?
    private var touchSourceID: UUID?

    var audioStatus: String {
        if !audioSupported || !audioAvailable { return audioUnavailableReason ?? "此设备暂不能传输系统声音" }
        if !audioRequested { return "声音已关闭" }
        if let audioPlaybackReason { return audioPlaybackReason }
        return audioPlaying ? "正在播放安卓系统声音" : "等待安卓系统声音"
    }
    func setAudioEnabled(_ enabled: Bool) {
        cancelTouches()
        audioRequested = enabled
        audioPlaybackReason = nil
        transport?.setAudioEnabled(enabled && audioAvailable)
    }

    var canChangeDisplay: Bool {
        online && !recovering && !connecting && !changingControl && !changingDisplay && !sendingInput &&
        workspace?.controlled(by: deviceID) == true && displayState?.allowsChange == true
    }
    var displayStatus: String {
        if let displayState { return displayState.status }
        if !online { return "连接后可读取实体屏幕状态" }
        return displayError == nil ? "正在读取实体屏幕状态" : "暂时无法确认实体屏幕状态"
    }

    func setDeviceScreenOff(_ off: Bool) async {
        cancelTouches()
        guard canChangeDisplay, let api, let namespace, let streamID, let workspace else { return }
        let started = generation
        changingDisplay = true; displayError = nil; displayOperation = UUID()
        defer { if generation == started { changingDisplay = false } }
        do {
            let body = try JSONSerialization.data(withJSONObject: ["screen_off": off, "expected_epoch": workspace.epoch])
            let data = try await api.request("POST", "/api/v1/android/stream/\(streamID)/display", body: body, expectedNamespace: namespace)
            guard generation == started, self.streamID == streamID else { return }
            let result = try JSONDecoder().decode(AndroidDisplayState.self, from: data)
            guard result.display_revision >= 0 else { throw failure("实体屏幕状态版本无效") }
            applyDisplayState(result)
        } catch {
            guard generation == started, self.streamID == streamID else { return }
            displayError = error.localizedDescription
            await refreshDisplayState(started: started)
        }
    }

    private func applyDisplayState(_ value: AndroidDisplayState) {
        guard value.supersedes(displayState) else { return }
        if displayState != value { displayState = value }
    }
    private func refreshDisplayState(started: UUID) async {
        guard generation == started, let api, let namespace, let streamID else { return }
        let operation = displayOperation
        do {
            let data = try await api.request("GET", "/api/v1/android/stream/\(streamID)/display", expectedNamespace: namespace)
            guard generation == started, self.streamID == streamID, !Task.isCancelled else { return }
            let result = try JSONDecoder().decode(AndroidDisplayState.self, from: data)
            guard result.display_revision >= 0 else { throw failure("实体屏幕状态版本无效") }
            applyDisplayState(result)
        } catch {
            if generation == started, self.streamID == streamID, displayOperation == operation, !Task.isCancelled { displayError = error.localizedDescription }
        }
    }

    var touchAvailable: Bool { localTouchAvailable && remoteTouchAvailable }
    var touchStatus: String? { touchAvailable ? nil : (localTouchReason ?? remoteTouchReason ?? "触控暂不可用，可继续观看") }

    func restoreTouch() async {
        guard online, !restoringTouch, let transport else { return }
        cancelTouches(); restoringTouch = true
        let started = generation
        defer { if generation == started { restoringTouch = false } }
        do { try await transport.restoreTouchChannel() }
        catch { if generation == started { localTouchAvailable = false; localTouchReason = error.localizedDescription } }
    }

    private func installTouchPipeline(_ transport: AndroidScreenTransport, started: UUID) {
        touchPipeline?.cancel()
        touchPipeline = AndroidTouchPipeline(send: { [weak transport] packet in transport?.sendTouch(packet) == true },
            activity: { [weak self] active in
                guard let self, self.generation == started else { return }; self.touchActive = active
            }, failure: { [weak self] reason in
                guard let self, self.generation == started else { return }
                self.cancelTouches(); self.localTouchAvailable = false; self.localTouchReason = reason
                self.transport?.disableTouch(reason)
            })
    }

    var canControl: Bool {
        online && touchAvailable && !recovering && !connecting && !changingControl && !changingDisplay && !sendingInput &&
        workspace?.controlled(by: deviceID) == true && screen?.epoch == workspace?.epoch &&
        screen?.mode == "human" && screen?.blocked_reason == nil &&
        Date().timeIntervalSince(receivedAt) < 3
    }

    var canTouch: Bool {
        if !touchActive { return canControl }
        // 连续手势固定起始帧；画面正常刷新不取消已经按下的手指。
        return online && touchAvailable && !recovering && !connecting && !changingControl && !changingDisplay && !sendingInput &&
            workspace?.controlled(by: deviceID) == true && screen?.epoch == workspace?.epoch &&
            screen?.mode == "human" && screen?.blocked_reason == nil
    }

    @discardableResult
    func handleTouch(_ event: AndroidTouchEvent, sourceID: UUID) -> Bool {
        if event.phase == .cancel {
            if touchSourceID == sourceID { cancelTouches() }
            return true
        }
        guard canTouch, let workspace, let screen, let touchPipeline else { cancelTouches(); return false }
        if event.phase == .down {
            guard canControl else { return false }
            touchSourceID = sourceID
        } else if touchSourceID != sourceID { return false }
        return touchPipeline.enqueue(event, epoch: workspace.epoch, frameID: screen.frame_id)
    }

    func cancelTouches() {
        guard touchPipeline?.hasPendingWork == true else { return }
        touchPipeline?.cancel()
        touchActive = false; touchSourceID = nil; touchCancellationID = UUID()
    }

    func connect(api: APIClient) async {
        stop()
        let started = generation
        self.api = api; workspace = nil; deviceID = ""; connecting = true; error = nil; streamFailure = nil
        defer { if generation == started { connecting = false } }
        do {
            let namespace = try await api.syncNamespace()
            guard generation == started, !Task.isCancelled else { return }
            self.namespace = namespace
            struct Me: Decodable { let device_id: String }
            let identity = try await api.request("GET", "/api/v1/me", expectedNamespace: namespace)
            guard generation == started, !Task.isCancelled else { return }
            deviceID = try JSONDecoder().decode(Me.self, from: identity).device_id
            let data = try await api.request("GET", "/api/v1/android/workspace", expectedNamespace: namespace)
            guard generation == started, !Task.isCancelled else { return }
            workspace = try JSONDecoder().decode(AndroidWorkspaceEnvelope.self, from: data).workspace
            sequence = workspace?.last_sequence ?? 0
            guard workspace != nil else { return }
            struct Configuration: Decodable {
                let stun_urls: [String]; let transport_policy: String
                let audio_supported: Bool?; let audio_unavailable_reason: String?
            }
            let configuration = try JSONDecoder().decode(Configuration.self, from: await api.request("GET", "/api/v1/android/stream/config", expectedNamespace: namespace))
            guard generation == started, !Task.isCancelled else { return }
            guard configuration.transport_policy == "direct_only" else { throw failure("屏幕连接必须使用两端直连") }
            audioSupported = configuration.audio_supported == true
            audioAvailable = audioSupported
            audioUnavailableReason = configuration.audio_unavailable_reason
            audioPlaybackReason = nil
            for fullGathering in [false, true] {
                let transport = try AndroidScreenTransport(stunURLs: configuration.stun_urls, fullGathering: fullGathering, receiveAudio: audioSupported)
                self.transport = transport
                transport.onAudioPlayback = { [weak self] playing, reason in
                    guard let self, self.generation == started else { return }
                    self.audioPlaying = playing; self.audioPlaybackReason = reason
                }
                transport.setAudioEnabled(audioRequested && audioAvailable)
                installTouchPipeline(transport, started: started)
                transport.onTouchReset = { [weak self, weak transport] in
                    guard let self, let transport, self.generation == started else { return }
                    self.cancelTouches(); self.installTouchPipeline(transport, started: started)
                }
                transport.onTouchAvailable = { [weak self] available, reason in
                    guard let self, self.generation == started else { return }
                    if !available { self.cancelTouches() }
                    self.localTouchAvailable = available; self.localTouchReason = reason
                }
                transport.onTouchAck = { [weak self] ack in
                    guard let self, self.generation == started else { return }; self.touchPipeline?.receive(ack)
                }
                transport.onTrack = { [weak self] track in
                    guard let self, self.generation == started else { return }
                    self.track = track; self.onVideoTrack?(track)
                }
                transport.onFrame = { [weak self] frame in
                    guard let self, self.generation == started else { return }
                    if let old = self.screen, old.width != frame.width || old.height != frame.height || old.epoch != frame.epoch || old.mode != frame.mode {
                        self.cancelTouches()
                    }
                    if let available = frame.touch_available {
                        self.remoteTouchAvailable = available; self.remoteTouchReason = frame.touch_reason
                        if !available { self.cancelTouches() }
                    }
                    if let available = frame.audio_available {
                        let changed = self.audioAvailable != (self.audioSupported && available)
                        self.audioAvailable = self.audioSupported && available
                        self.audioUnavailableReason = frame.audio_reason ?? configuration.audio_unavailable_reason
                        if changed { self.transport?.setAudioEnabled(self.audioRequested && self.audioAvailable) }
                    }
                    if let display = frame.displayState { self.applyDisplayState(display) }
                    self.screen = frame; self.receivedAt = .now
                }
                transport.onBlocked = { [weak self] reason in
                    guard let self, self.generation == started else { return }
                    self.streamFailure = reason
                    self.stop(message: reason)
                }
                transport.onConnectivity = { [weak self] ready in
                    guard let self, self.generation == started else { return }
                    if !ready { self.cancelTouches() }
                    self.recovering = !ready
                }
                transport.onDisconnect = { [weak self] reason in
                    guard let self, self.generation == started else { return }
                    if self.connecting { self.streamFailure = reason; return }
                    self.stop(message: self.streamFailure ?? reason)
                }
                do {
                    let offer = try await transport.offer()
                    guard generation == started, !Task.isCancelled else { transport.close(); return }
                    let body = try JSONEncoder().encode(AndroidScreenOffer(sdp: offer, type: "offer", audio_enabled: audioSupported))
                    let answerData = try await api.request("POST", "/api/v1/android/stream/offer", body: body, expectedNamespace: namespace)
                    let answer = try JSONDecoder().decode(AndroidScreenAnswer.self, from: answerData)
                    guard generation == started, !Task.isCancelled else {
                        transport.close()
                        _ = try? await api.request("POST", "/api/v1/android/stream/\(answer.session_id)/close", expectedNamespace: namespace)
                        return
                    }
                    streamID = answer.session_id
                    guard answer.type == "answer" else { throw failure("屏幕连接响应无效") }
                    try await transport.accept(answer.sdp)
                    guard generation == started, !Task.isCancelled else { transport.close(); return }
                } catch {
                    guard generation == started, !Task.isCancelled else { transport.close(); return }
                    let authenticationFailure: Bool
                    if case APIClient.APIError.http(let status, _) = error { authenticationFailure = (400..<500).contains(status) }
                    else { authenticationFailure = false }
                    guard !fullGathering, transport.candidateSnapshotWasEarly, !authenticationFailure else { throw error }
                    // 已验证候选集合可能遗漏更慢的可用网络，只重试一次完整收集。
                    transport.close(); self.transport = nil
                    if let streamID {
                        _ = try? await api.request("POST", "/api/v1/android/stream/\(streamID)/close", expectedNamespace: namespace)
                    }
                    self.streamID = nil; track = nil; screen = nil; streamFailure = nil; recovering = false
                    displayState = nil; displayError = nil; displayOperation = UUID()
                    guard generation == started, !Task.isCancelled else { return }
                    #if DEBUG
                    print("ANDROID_SCREEN phase=retry_full_gather inputs_replayed=0")
                    #endif
                    continue
                }
                online = true; recovering = false; receivedAt = .now
                displayRefreshTask?.cancel()
                displayRefreshTask = Task { [weak self] in await self?.refreshDisplayState(started: started) }
                monitor = Task { [weak self] in await self?.monitorConnection(started: started) }
                touchTimer = Task { [weak self] in
                    while !Task.isCancelled {
                        do { try await Task.sleep(for: .milliseconds(25)) } catch { return }
                        guard let self, self.generation == started else { return }
                        self.touchPipeline?.tick()
                    }
                }
                break
            }
        } catch {
            guard generation == started else { return }
            stop(message: error.localizedDescription)
        }
    }

    func setControl(_ mode: String) async {
        cancelTouches()
        guard let api, let namespace, let workspace, online, !changingControl,
              !deviceID.isEmpty, ["human", "ai", "paused"].contains(mode) else { return }
        changingControl = true; error = nil
        let started = generation, originalDeviceID = deviceID, workspaceID = workspace.id
        let controls = Self.controls
        controls.registerIntent(namespace: namespace, workspaceID: workspaceID)
        defer { if generation == started { changingControl = false } }
        do {
            try await controls.gate.withPermit {
                try await self.changeControlUnderGate(mode, api: api, namespace: namespace,
                    workspace: workspace, deviceID: originalDeviceID, started: started)
            }
        } catch {
            guard generation == started else { return }
            self.error = error.localizedDescription
            await refreshWorkspace(started: started)
        }
    }

    private func changeControlUnderGate(_ mode: String, api: APIClient, namespace: String,
                                       workspace: AndroidWorkspace, deviceID: String, started: UUID) async throws {
        guard generation == started, !Task.isCancelled else { return }
        // 保留用户发起操作时的版本，排队期间发生接管必须由服务端拒绝旧意图。
        let body = try JSONSerialization.data(withJSONObject: ["mode": mode, "expected_epoch": workspace.epoch])
        let data = try await api.request("POST", "/api/v1/android/control", body: body, expectedNamespace: namespace)
        let response = try JSONDecoder().decode(AndroidWorkspaceEnvelope.self, from: data).workspace
        guard generation == started else {
            if AndroidControlLifecycle.shouldPauseLateResponse(response, requestedMode: mode,
                workspaceID: workspace.id, deviceID: deviceID), let response {
                // 仍持串行门，后一个页面尚不能接管；只能暂停这次响应的 epoch，不重试冲突。
                await Self.pauseOnce(api: api, namespace: namespace, epoch: response.epoch)
            }
            return
        }
        self.workspace = response
        sequence = response?.last_sequence ?? 0
    }

    private static func pauseOnce(api: APIClient, namespace: String, epoch: Int) async {
        // 退出可能取消原任务，使用一次独立清理任务，身份仍固定为原请求快照。
        await Task {
            guard let body = try? JSONSerialization.data(withJSONObject: ["mode": "paused", "expected_epoch": epoch]) else { return }
            _ = try? await api.request("POST", "/api/v1/android/control", body: body, expectedNamespace: namespace)
        }.value
    }

    func input(_ action: String, x: Double? = nil, y: Double? = nil,
               x2: Double? = nil, y2: Double? = nil, text: String? = nil, package: String? = nil) async {
        cancelTouches()
        guard canControl, let workspace, let screen, let api, let namespace else { return }
        let started = generation
        sendingInput = true; error = nil; sequence += 1
        defer { if generation == started { sendingInput = false } }
        var payload: [String: Any] = ["epoch": workspace.epoch, "sequence": sequence, "frame_id": screen.frame_id, "action": action]
        if let x { payload["x"] = x }; if let y { payload["y"] = y }
        if let x2 { payload["x2"] = x2 }; if let y2 { payload["y2"] = y2 }
        if let text { payload["text"] = text }; if let package { payload["package"] = package }
        do {
            if action == "text" {
                // 服务端将文字绑定到当前输入框，避免光标闪烁造成整张图片校验失效。
                let focusBody = try JSONSerialization.data(withJSONObject: ["epoch": workspace.epoch])
                let raw = try await api.request("POST", "/api/v1/android/input-focus", body: focusBody, expectedNamespace: namespace)
                let focus = try JSONDecoder().decode(AndroidInputFocus.self, from: raw)
                guard generation == started, !Task.isCancelled else { return }
                guard online, self.workspace?.epoch == workspace.epoch,
                      self.workspace?.controlled(by: deviceID) == true else {
                    throw failure("操作权已变化，文字未发送")
                }
                guard focus.isValid(at: .now) else { throw failure("输入焦点已过期，请核对输入框后重新发送") }
                payload["focus_token"] = focus.focus_token
            }
            let data = try await api.request("POST", "/api/v1/android/input", body: JSONSerialization.data(withJSONObject: payload), expectedNamespace: namespace)
            guard generation == started else { return }
            struct Result: Decodable { let status: String; let epoch: Int; let sequence: Int }
            let result = try JSONDecoder().decode(Result.self, from: data)
            guard result.status == "succeeded", result.epoch == workspace.epoch, result.sequence == sequence else { throw failure("操作响应不匹配，请核对当前画面") }
        } catch {
            guard generation == started else { return }
            // 输入可能已经执行，禁止自动重发坐标、文字或提交动作。
            self.error = "操作未确认：\(error.localizedDescription)。请核对画面后继续。"
            await refreshWorkspace(started: started, resetSequence: true)
        }
    }

    func stop(message: String? = nil) {
        localTouchAvailable = false; remoteTouchAvailable = true; localTouchReason = nil; remoteTouchReason = nil; restoringTouch = false
        displayRefreshTask?.cancel(); displayRefreshTask = nil
        displayState = nil; changingDisplay = false; displayError = nil; displayOperation = UUID()
        cancelTouches(); touchTimer?.cancel(); touchTimer = nil; touchPipeline = nil
        touchActive = false; touchSourceID = nil; touchCancellationID = UUID()
        let api = self.api, namespace = self.namespace, streamID = self.streamID
        let owned = workspace?.controlled(by: deviceID) == true
        let epoch = workspace?.epoch, workspaceID = workspace?.id
        let controls = Self.controls
        let revision = namespace.flatMap { value in workspaceID.map { controls.revision(namespace: value, workspaceID: $0) } }
        generation = UUID(); monitor?.cancel(); monitor = nil
        transport?.close(); transport = nil; self.streamID = nil
        screen = nil; track = nil; onVideoTrack?(nil); audioPlaying = false; online = false; recovering = false; connecting = false
        changingControl = false; sendingInput = false; error = message
        self.namespace = nil
        // 后台无法保证请求送达，服务端仍必须依靠短期控制租约自动暂停。
        Task {
            guard let api, let namespace else { return }
            if owned, let epoch, let workspaceID, let revision {
                try? await controls.gate.withPermit {
                    guard await controls.canRelease(namespace: namespace, workspaceID: workspaceID, revision: revision) else { return }
                    await Self.pauseOnce(api: api, namespace: namespace, epoch: epoch)
                }
            }
            if let streamID { _ = try? await api.request("POST", "/api/v1/android/stream/\(streamID)/close", expectedNamespace: namespace) }
        }
    }

    private func refreshWorkspace(started: UUID, resetSequence: Bool = false) async {
        guard let api, let namespace else { return }
        do {
            let data = try await api.request("GET", "/api/v1/android/workspace", expectedNamespace: namespace)
            guard generation == started else { return }
            guard let updated = try JSONDecoder().decode(AndroidWorkspaceEnvelope.self, from: data).workspace else {
                workspace = nil
                stop(message: "安卓设备已解除绑定")
                return
            }
            if updated.epoch >= (workspace?.epoch ?? 0) {
                if updated.epoch != workspace?.epoch || updated.mode != workspace?.mode || updated.controller_device_id != workspace?.controller_device_id { cancelTouches() }
                if resetSequence || updated.epoch != workspace?.epoch { sequence = updated.last_sequence ?? 0 }
                else { sequence = max(sequence, updated.last_sequence ?? 0) }
                workspace = updated
            }
        } catch { if generation == started { stop(message: error.localizedDescription) } }
    }

    private func monitorConnection(started: UUID) async {
        var leaseRefresh = Date.now
        while !Task.isCancelled, generation == started {
            do { try await Task.sleep(for: .seconds(1)) } catch { return }
            guard generation == started else { return }
            if Date().timeIntervalSince(receivedAt) > 12 {
                stop(message: "画面更新已中断，操作已暂停。请重新连接。")
                return
            }
            await refreshWorkspace(started: started)
            guard generation == started, online else { return }
            if workspace?.controlled(by: deviceID) == true, Date().timeIntervalSince(leaseRefresh) >= 5 {
                leaseRefresh = .now
                await renewLease(started: started)
            }
        }
    }

    private func renewLease(started: UUID) async {
        guard generation == started, let api, let namespace, let workspace,
              workspace.controlled(by: deviceID) else { return }
        let originalDeviceID = deviceID
        do {
            try await Self.controls.gate.withPermit {
                try await self.renewLeaseUnderGate(api: api, namespace: namespace, workspace: workspace,
                    deviceID: originalDeviceID, started: started)
            }
        } catch { if generation == started { stop(message: "控制保活失败，操作已暂停。请重新连接。") } }
    }

    private func renewLeaseUnderGate(api: APIClient, namespace: String, workspace: AndroidWorkspace,
                                    deviceID: String, started: UUID) async throws {
        guard generation == started, !Task.isCancelled,
              self.workspace?.id == workspace.id, self.workspace?.epoch == workspace.epoch,
              self.workspace?.controlled(by: deviceID) == true else { return }
        let body = try JSONSerialization.data(withJSONObject: ["mode": "human", "expected_epoch": workspace.epoch])
        let data = try await api.request("POST", "/api/v1/android/control", body: body, expectedNamespace: namespace)
        let response = try JSONDecoder().decode(AndroidWorkspaceEnvelope.self, from: data).workspace
        guard generation != started else { return }
        if AndroidControlLifecycle.shouldPauseLateResponse(response, requestedMode: "human",
            workspaceID: workspace.id, deviceID: deviceID), let response {
            await Self.pauseOnce(api: api, namespace: namespace, epoch: response.epoch)
        }
    }
    #if DEBUG
    func mediaStatisticsForAcceptance() async -> AndroidMediaStatistics? {
        guard online, let transport else { return nil }
        return await transport.mediaStatisticsForAcceptance()
    }
    #endif
    private func failure(_ message: String) -> Error { APIClient.APIError.message(message) }
}

private struct AndroidScreenOffer: Encodable { let sdp: String; let type: String; let audio_enabled: Bool }
private struct AndroidScreenAnswer: Decodable { let session_id: String; let sdp: String; let type: String }

#if DEBUG
struct AndroidMediaStatistics: Sendable {
    let connectionID: UUID
    let videoFramesDecoded: UInt64
    let width: Int
    let height: Int
    let audioPacketsReceived: UInt64
}
#endif

/// 视频协商与业务数据连接分离；不采集麦克风或相机，也不配置 TURN。
@MainActor
final class AndroidScreenTransport: NSObject, RTCPeerConnectionDelegate, RTCDataChannelDelegate {
    private var factory: RTCPeerConnectionFactory!
    private let audioOutput = AndroidPlaybackAudioDevice()
    private let receiveAudio: Bool
    private var remoteAudioTrack: RTCAudioTrack?
    private var audioEnabled = false
    private let mediaConnectionID = UUID()
    private var peer: RTCPeerConnection!
    private var channel: RTCDataChannel?
    private var touchChannel: RTCDataChannel?
    private var touchBridgeReady = false
    private var touchChannelID: String?
    private var retiredTouchChannelIDs: [String] = []
    private var touchLiveness = AndroidTouchLiveness()
    private var heartbeatTask: Task<Void, Never>?
    private var closed = false
    private var recoveryTask: Task<Void, Never>?
    private var failureReason: String?
    private let startedAt = Date()
    private var receivedMetadata = false
    private let fullGathering: Bool
    private var firstPublicCandidateAt: Date?
    private(set) var candidateSnapshotWasEarly = false
    var onTrack: ((RTCVideoTrack) -> Void)?
    var onAudioPlayback: ((Bool, String?) -> Void)?
    var onFrame: ((AndroidScreenFrame) -> Void)?
    var onTouchAck: ((AndroidTouchAck) -> Void)?
    var onTouchReset: (() -> Void)?
    var onTouchAvailable: ((Bool, String?) -> Void)?
    private var touchFailureReason: String?
    var onDisconnect: ((String) -> Void)?
    var onConnectivity: ((Bool) -> Void)?
    var onBlocked: ((String) -> Void)?

    init(stunURLs: [String], fullGathering: Bool = false, receiveAudio: Bool = false) throws {
        self.fullGathering = fullGathering; self.receiveAudio = receiveAudio
        guard stunURLs.count <= 8, stunURLs.allSatisfy({
            $0.hasPrefix("stun:") && $0.count <= 256 && !$0.contains("@") && !$0.contains("?") &&
            !$0.contains("#") && !$0.contains(where: { $0.isWhitespace })
        }) else { throw APIClient.APIError.message("屏幕通道只允许 STUN 直连") }
        super.init()
        audioOutput.setStatusHandler { [weak self] playing, reason in
            Task { @MainActor in
                guard let self, !self.closed else { return }; self.onAudioPlayback?(playing, reason)
            }
        }
        factory = RTCPeerConnectionFactory(encoderFactory: RTCVideoEncoderFactoryH264(),
            decoderFactory: RTCVideoDecoderFactoryH264(), audioDevice: audioOutput)
        let configuration = RTCConfiguration()
        configuration.iceServers = stunURLs.map { RTCIceServer(urlStrings: [$0]) }
        configuration.iceTransportPolicy = .all
        configuration.tcpCandidatePolicy = .disabled
        configuration.sdpSemantics = .unifiedPlan
        peer = factory.peerConnection(with: configuration, constraints: RTCMediaConstraints(mandatoryConstraints: nil, optionalConstraints: nil), delegate: self)
        guard peer != nil else { throw APIClient.APIError.message("无法创建屏幕连接") }
        let receive = RTCRtpTransceiverInit(); receive.direction = .recvOnly
        _ = peer.addTransceiver(of: .video, init: receive)
        if receiveAudio {
            let audio = RTCRtpTransceiverInit(); audio.direction = .recvOnly
            _ = peer.addTransceiver(of: .audio, init: audio)
        }
        let config = RTCDataChannelConfiguration(); config.isOrdered = true
        channel = peer.dataChannel(forLabel: "homeai.android.screen", configuration: config)
        channel?.delegate = self
        let touchConfig = RTCDataChannelConfiguration(); touchConfig.isOrdered = true
        touchChannel = peer.dataChannel(forLabel: "homeai.android.touch", configuration: touchConfig)
        touchChannel?.delegate = self
    }

    func setAudioEnabled(_ enabled: Bool) {
        audioEnabled = enabled && receiveAudio && !closed
        remoteAudioTrack?.isEnabled = audioEnabled
        audioOutput.setPlaybackEnabled(audioEnabled && remoteAudioTrack != nil)
    }

    #if DEBUG
    /// 验收只提取计数与视频尺寸，不返回 candidate、SDP、地址或媒体内容。
    func mediaStatisticsForAcceptance() async -> AndroidMediaStatistics? {
        guard !closed, let peer else { return nil }
        let identifier = mediaConnectionID
        return await withCheckedContinuation { continuation in
            peer.statistics { report in
                var decoded: UInt64 = 0, packets: UInt64 = 0, width = 0, height = 0
                for entry in report.statistics.values where entry.type == "inbound-rtp" {
                    let kind = (entry.values["kind"] as? String) ?? (entry.values["mediaType"] as? String)
                    if kind == "video" {
                        decoded += (entry.values["framesDecoded"] as? NSNumber)?.uint64Value ?? 0
                        width = max(width, (entry.values["frameWidth"] as? NSNumber)?.intValue ?? 0)
                        height = max(height, (entry.values["frameHeight"] as? NSNumber)?.intValue ?? 0)
                    } else if kind == "audio" {
                        packets += (entry.values["packetsReceived"] as? NSNumber)?.uint64Value ?? 0
                    }
                }
                continuation.resume(returning: AndroidMediaStatistics(connectionID: identifier,
                    videoFramesDecoded: decoded, width: width, height: height, audioPacketsReceived: packets))
            }
        }
    }
    #endif

    func disableTouch(_ reason: String) {
        guard !closed else { return }
        invalidateTouchReadiness(); touchFailureReason = reason
        onTouchReset?(); onTouchAvailable?(false, reason)
        touchChannel?.close()
    }

    func restoreTouchChannel() async throws {
        guard isReady else { throw APIClient.APIError.message("请先恢复视频连接") }
        let old = touchChannel
        old?.delegate = nil; old?.close()
        touchChannel = nil; invalidateTouchReadiness(); touchFailureReason = nil
        onTouchReset?(); onTouchAvailable?(false, "正在恢复触控连接…")
        let closingDeadline = Date().addingTimeInterval(3)
        while let old, old.readyState != .closed, Date() < closingDeadline {
            try await Task.sleep(for: .milliseconds(25))
        }
        guard !closed, isReady, old == nil || old?.readyState == .closed else { throw APIClient.APIError.message("旧触控通道尚未释放，请稍后重试") }
        let config = RTCDataChannelConfiguration(); config.isOrdered = true
        guard let channel = peer.dataChannel(forLabel: "homeai.android.touch", configuration: config) else {
            throw APIClient.APIError.message("无法创建触控通道")
        }
        touchChannel = channel; channel.delegate = self
        let identifier = ObjectIdentifier(channel), deadline = Date().addingTimeInterval(12)
        while !isTouchReady {
            guard !closed, touchChannel.map(ObjectIdentifier.init) == identifier else { throw APIClient.APIError.message("触控连接已切换") }
            if let touchFailureReason { throw APIClient.APIError.message(touchFailureReason) }
            guard Date() < deadline else { throw APIClient.APIError.message("触控恢复超时，可继续观看") }
            try await Task.sleep(for: .milliseconds(50))
        }
    }

    func sendTouch(_ packet: AndroidTouchPacket) -> Bool {
        guard isTouchReady, let touchChannel,
              touchChannel.readyState == .open,
              let data = try? AndroidTouchWire.encode(packet, channelID: touchChannelID), data.count <= 4096,
              touchChannel.bufferedAmount + UInt64(data.count) <= 65536 else { return false }
        return touchChannel.sendData(RTCDataBuffer(data: data, isBinary: false))
    }

    private static func sdpSections(_ sdp: String) throws -> (session: [String], media: [[String]]) {
        guard sdp.utf8.count <= 65536 else { throw APIClient.APIError.message("屏幕通道描述超过限制") }
        let lines = sdp.components(separatedBy: .newlines).map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }.filter { !$0.isEmpty }
        var session: [String] = [], media: [[String]] = []
        for line in lines {
            if line.hasPrefix("m=") { media.append([line]) }
            else if media.isEmpty { session.append(line) }
            else { media[media.count - 1].append(line) }
        }
        guard session.first == "v=0", (2...3).contains(media.count),
              media.filter({ $0.first?.hasPrefix("m=video ") == true }).count == 1,
              media.filter({ $0.first?.hasPrefix("m=application ") == true }).count == 1,
              media.filter({ $0.first?.hasPrefix("m=audio ") == true }).count == media.count - 2 else {
            throw APIClient.APIError.message("屏幕通道只允许一路视频、可选音频和加密数据传输")
        }
        return (session, media)
    }

    /// 完成收集后，每个媒体段都必须标记结束；BUNDLE 仅在同一加密传输下复用已收集候选。
    static func completedGatheringSDP(_ sdp: String, allowAudio: Bool = false) throws -> String {
        var parts = try sdpSections(sdp)
        let session = parts.session
        let bundle = parts.session.first(where: { $0.hasPrefix("a=group:BUNDLE ") })?
            .split(separator: " ").dropFirst().map { String($0) } ?? []
        func value(_ prefix: String, in section: [String]) -> String? {
            section.first(where: { $0.hasPrefix(prefix) }) ?? session.first(where: { $0.hasPrefix(prefix) })
        }
        for index in parts.media.indices {
            if !parts.media[index].contains(where: { $0.hasPrefix("a=candidate:") }),
               let mid = value("a=mid:", in: parts.media[index]).map({ String($0.dropFirst(6)) }), bundle.contains(mid) {
                let target = parts.media[index]
                let signature = ["a=ice-ufrag:", "a=ice-pwd:", "a=fingerprint:"].compactMap { value($0, in: target) }
                if signature.count == 3, let source = parts.media.first(where: { candidate in
                    guard let sourceMID = value("a=mid:", in: candidate).map({ String($0.dropFirst(6)) }), bundle.contains(sourceMID) else { return false }
                    return candidate.contains(where: { $0.hasPrefix("a=candidate:") }) &&
                        ["a=ice-ufrag:", "a=ice-pwd:", "a=fingerprint:"].compactMap { value($0, in: candidate) } == signature
                }) {
                    parts.media[index].append(contentsOf: source.filter { $0.hasPrefix("a=candidate:") })
                }
            }
            if !parts.media[index].contains("a=end-of-candidates") { parts.media[index].append("a=end-of-candidates") }
        }
        let completed = (parts.session + parts.media.flatMap { $0 }).joined(separator: "\r\n") + "\r\n"
        try validateSDP(completed, allowAudio: allowAudio)
        return completed
    }

    static func validateSDP(_ sdp: String, expectedVideoDirection: String = "recvonly", allowAudio: Bool = false) throws {
        let parts = try sdpSections(sdp)
        guard allowAudio || parts.media.count == 2 else { throw APIClient.APIError.message("当前会话未请求安卓声音") }
        for media in parts.media {
            let header = media[0].split(separator: " ")
            let video = header[0] == "m=video", audio = header[0] == "m=audio"
            let expectedProfile = video || audio ? "UDP/TLS/RTP/SAVPF" : "UDP/DTLS/SCTP"
            let directions = media.filter { ["a=recvonly", "a=sendonly", "a=sendrecv", "a=inactive"].contains($0) }
            if audio && expectedVideoDirection == "sendonly" && header.count >= 4 &&
                header[2] == expectedProfile && directions == ["a=inactive"] {
                // 拒收音频段可以没有候选，但仍不允许夹带中继或非 UDP 地址。
                for candidate in media.filter({ $0.hasPrefix("a=candidate:") }) {
                    let fields = candidate.split(separator: " ")
                    guard fields.count >= 8, fields[1] == "1", fields[2].lowercased() == "udp", fields[6] == "typ",
                          ["host", "srflx"].contains(String(fields[7])) else {
                        throw APIClient.APIError.message("音频段包含不允许的候选")
                    }
                }
                continue
            }
            guard header.count >= 4, header[1] != "0", header[2] == expectedProfile,
                  (media + parts.session).contains(where: { $0.hasPrefix("a=fingerprint:sha-256 ") }),
                  (media + parts.session).contains(where: { ["a=setup:actpass", "a=setup:active", "a=setup:passive"].contains($0) }),
                  media.contains("a=end-of-candidates") else {
                throw APIClient.APIError.message("屏幕通道需要完整的加密媒体描述")
            }
            if video || audio {
                let codec = video ? "h264/90000" : "opus/48000"
                guard directions == ["a=" + expectedVideoDirection],
                      media.contains(where: { $0.hasPrefix("a=rtpmap:") && $0.lowercased().contains(codec) }) else {
                    throw APIClient.APIError.message("屏幕媒体方向或编码不匹配")
                }
            }
            let candidates = media.filter { $0.hasPrefix("a=candidate:") }
            guard !candidates.isEmpty, candidates.count <= 32 else { throw APIClient.APIError.message("屏幕通道缺少完整直连地址") }
            for candidate in candidates {
                let fields = candidate.split(separator: " ")
                guard fields.count >= 8, fields[1] == "1", fields[2].lowercased() == "udp", fields[6] == "typ",
                      ["host", "srflx"].contains(String(fields[7])),
                      let port = Int(fields[5]), (1...65535).contains(port) else {
                    throw APIClient.APIError.message("屏幕通道拒绝中继与非 UDP 候选")
                }
            }
        }
    }

    /// 两段均已获得真实公网候选，且保留短暂候选汇集窗口后才能提前取快照。
    static func canUseEarlyCandidateSnapshot(_ sdp: String?, elapsed: TimeInterval, publicCandidateAge: TimeInterval?) -> Bool {
        guard elapsed >= 1, let publicCandidateAge, publicCandidateAge >= 0.25,
              let sdp, let parts = try? sdpSections(sdp) else { return false }
        return parts.media.allSatisfy { media in
            media.contains { line in
                guard line.hasPrefix("a=candidate:") else { return false }
                let fields = line.split(separator: " ")
                return fields.count >= 8 && fields[1] == "1" && fields[2].lowercased() == "udp" && fields[6] == "typ" && fields[7] == "srflx"
            }
        }
    }

    /// 诊断只返回媒体种类、方向及候选数量，不包含地址、端口、指纹或 ICE 凭据。
    static func diagnosticSummary(_ sdp: String) throws -> String {
        let parts = try sdpSections(sdp)
        return parts.media.map { media in
            let header = media[0].split(separator: " ")
            let kind = header[0] == "m=video" ? "video" : header[0] == "m=audio" ? "audio" : "data"
            let candidates = media.filter { $0.hasPrefix("a=candidate:") }
            let host = candidates.filter { $0.contains(" typ host") }.count
            let srflx = candidates.filter { $0.contains(" typ srflx") }.count
            let relay = candidates.filter { $0.contains(" typ relay") }.count
            let direction = ["recvonly", "sendonly", "sendrecv", "inactive"].first { media.contains("a=" + $0) } ?? "none"
            let profile = header.count > 2 && ["UDP/TLS/RTP/SAVPF", "UDP/DTLS/SCTP"].contains(String(header[2])) ? String(header[2]) : "unsupported"
            return "\(kind):profile=\(profile),direction=\(direction),host=\(host),srflx=\(srflx),relay=\(relay),complete=\(media.contains("a=end-of-candidates"))"
        }.joined(separator: ";")
    }

    private func diagnostic(_ phase: String, state: Int? = nil) {
        #if DEBUG
        print("ANDROID_SCREEN phase=\(phase) state=\(state.map { String($0) } ?? "none") elapsed_ms=\(Int(Date().timeIntervalSince(startedAt) * 1000))")
        #endif
    }

    func offer() async throws -> String {
        diagnostic("offer_start")
        let started = Date()
        let sdp: String = try await withCheckedThrowingContinuation { continuation in
            peer.offer(for: RTCMediaConstraints(mandatoryConstraints: nil, optionalConstraints: nil)) { description, error in
                if let error { continuation.resume(throwing: error) }
                else if let description { continuation.resume(returning: description.sdp) }
                else { continuation.resume(throwing: APIClient.APIError.message("未生成屏幕连接请求")) }
            }
        }
        try await setDescription(sdp, local: true)
        try await waitUntil({
            if self.peer.iceGatheringState == .complete { return true }
            guard !self.fullGathering else { return false }
            return Self.canUseEarlyCandidateSnapshot(self.peer.localDescription?.sdp,
                elapsed: Date().timeIntervalSince(started),
                publicCandidateAge: self.firstPublicCandidateAt.map { Date().timeIntervalSince($0) })
        }, timeout: 40)
        candidateSnapshotWasEarly = peer.iceGatheringState != .complete
        guard let gathered = peer.localDescription?.sdp else { throw APIClient.APIError.message("屏幕直连地址缺失") }
        let completed = try Self.completedGatheringSDP(gathered, allowAudio: receiveAudio)
        #if DEBUG
        print("ANDROID_SCREEN offer=\(try Self.diagnosticSummary(completed))")
        #endif
        diagnostic(candidateSnapshotWasEarly ? "offer_early_ready" : "offer_full_ready")
        return completed
    }

    func accept(_ sdp: String) async throws {
        try Self.validateSDP(sdp, expectedVideoDirection: "sendonly", allowAudio: receiveAudio)
        #if DEBUG
        print("ANDROID_SCREEN answer=\(try Self.diagnosticSummary(sdp))")
        #endif
        diagnostic("answer_received")
        try await setDescription(sdp, local: false)
        diagnostic("answer_accepted")
        try await waitUntil({ self.isReady }, timeout: 20)
        if let video = peer.receivers.compactMap({ $0.track as? RTCVideoTrack }).first { onTrack?(video) }
        diagnostic("ready")
    }

    private func setDescription(_ sdp: String, local: Bool) async throws {
        guard !closed else { throw APIClient.APIError.message(failureReason ?? "屏幕连接已关闭") }
        try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
            let completion: @Sendable (Error?) -> Void = { error in
                if let error { continuation.resume(throwing: error) } else { continuation.resume() }
            }
            let description = RTCSessionDescription(type: local ? .offer : .answer, sdp: sdp)
            if local { peer.setLocalDescription(description, completionHandler: completion) }
            else { peer.setRemoteDescription(description, completionHandler: completion) }
        }
    }
    private func waitUntil(_ condition: () -> Bool, timeout: TimeInterval) async throws {
        let deadline = Date().addingTimeInterval(timeout)
        while !condition() {
            guard !closed else { throw APIClient.APIError.message(failureReason ?? "屏幕连接已关闭") }
            guard Date() < deadline else {
                diagnostic("wait_timeout_ice", state: Int(peer.iceConnectionState.rawValue))
                diagnostic("wait_timeout_connection", state: Int(peer.connectionState.rawValue))
                diagnostic("wait_timeout_metadata", state: channel.map { Int($0.readyState.rawValue) })
                diagnostic("wait_timeout_touch_ready", state: touchBridgeReady ? 1 : 0)
                if peer.connectionState == .connected && channel?.readyState == .open && !touchBridgeReady {
                    throw APIClient.APIError.message("安卓触控桥未完成预热，请重新连接")
                }
                throw APIClient.APIError.message("当前网络无法建立屏幕直连，不会回退中继")
            }
            try await Task.sleep(for: .milliseconds(50))
        }
        try Task.checkCancellation()
        guard !closed else { throw APIClient.APIError.message(failureReason ?? "屏幕连接已关闭") }
    }
    func close() {
        guard !closed else { return }; closed = true
        diagnostic("close")
        touchBridgeReady = false; touchChannelID = nil; touchLiveness.invalidate()
        heartbeatTask?.cancel(); heartbeatTask = nil
        recoveryTask?.cancel(); recoveryTask = nil
        channel?.delegate = nil; channel?.close()
        touchChannel?.delegate = nil; touchChannel?.close(); peer?.close()
        remoteAudioTrack?.isEnabled = false; remoteAudioTrack = nil; audioOutput.shutdown()
        onAudioPlayback = nil
        onTrack = nil; onFrame = nil; onTouchAck = nil; onTouchReset = nil; onTouchAvailable = nil; onDisconnect = nil; onBlocked = nil; onConnectivity = nil
    }
    private func disconnected(_ reason: String) {
        guard !closed else { return }
        failureReason = failureReason ?? reason
        let callback = onDisconnect; close(); callback?(failureReason ?? reason)
    }
    private func invalidateTouchReadiness() {
        if let channelID = touchChannelID, !retiredTouchChannelIDs.contains(channelID) {
            retiredTouchChannelIDs.append(channelID)
            if retiredTouchChannelIDs.count > 32 { retiredTouchChannelIDs.removeFirst() }
        }
        touchBridgeReady = false; touchChannelID = nil; touchLiveness.invalidate()
    }

    private func monitorTouchLiveness() {
        guard heartbeatTask == nil else { return }
        heartbeatTask = Task { [weak self] in
            while !Task.isCancelled {
                do { try await Task.sleep(for: .milliseconds(100)) } catch { return }
                guard let self, !self.closed else { return }
                if self.touchLiveness.isExpired(at: ProcessInfo.processInfo.systemUptime) {
                    self.invalidateTouchReadiness()
                    self.onTouchReset?(); self.onTouchAvailable?(false, "触控保活中断，可继续观看")
                    self.diagnostic("touch_alive_expired")
                }
            }
        }
    }

    private func handleHeartbeat(_ data: Data) {
        guard let message = AndroidTouchHeartbeatMessage.decode(data), message.channelID == touchChannelID,
              !closed, peer.connectionState == .connected, let touchChannel, touchChannel.readyState == .open else {
            diagnostic("touch_heartbeat_ignored"); return
        }
        let time = ProcessInfo.processInfo.systemUptime
        if message.status == "ping" {
            guard touchLiveness.canReply(to: message), let reply = try? message.reply(),
                  touchChannel.bufferedAmount + UInt64(reply.count) <= 65536 else { return }
            if touchChannel.sendData(RTCDataBuffer(data: reply, isBinary: false)) {
                touchLiveness.didReply(to: message, at: time)
            }
        } else if touchLiveness.accept(message, at: time) {
            let first = !touchBridgeReady
            touchBridgeReady = true
            if first { diagnostic("touch_alive_confirmed") }
            publishReadiness()
        }
    }

    private var isReady: Bool {
        !closed && peer.connectionState == .connected && channel?.readyState == .open
    }
    private var isTouchReady: Bool {
        isReady && touchBridgeReady && touchLiveness.isReady(at: ProcessInfo.processInfo.systemUptime) &&
        touchChannelID != nil && touchChannel?.readyState == .open
    }
    private func publishReadiness() {
        if isReady { recoveryTask?.cancel(); recoveryTask = nil }
        onConnectivity?(isReady)
        onTouchAvailable?(isTouchReady, isTouchReady ? nil : (touchFailureReason ?? "触控连接确认中，可继续观看"))
    }
    private func allowBriefRecovery() {
        guard !closed, recoveryTask == nil else { return }
        invalidateTouchReadiness()
        onConnectivity?(false); onTouchAvailable?(false, "网络中断，触控已暂停")
        diagnostic("recover_start")
        recoveryTask = Task { [weak self] in
            do { try await Task.sleep(for: .seconds(2)) } catch { return }
            guard let self, !self.closed else { return }
            self.recoveryTask = nil
            if self.isReady {
                self.diagnostic("recovered"); self.publishReadiness()
            } else {
                self.disconnected("屏幕直连中断后未恢复，操作已暂停。请重新连接。")
            }
        }
    }
    nonisolated func dataChannelDidChangeState(_ dataChannel: RTCDataChannel) {
        let state = dataChannel.readyState
        let touch = dataChannel.label == "homeai.android.touch", source = ObjectIdentifier(dataChannel)
        Task { @MainActor in
            guard !closed else { return }
            if touch && touchChannel.map(ObjectIdentifier.init) != source { return }
            diagnostic(touch ? "touch_channel" : "metadata_channel", state: Int(state.rawValue))
            if state == .closed {
                if touch { disableTouch("触控通道已关闭，可恢复触控或继续观看") }
                else { disconnected("屏幕元数据通道已关闭，请重新连接。") }
            }
            else { publishReadiness() }
        }
    }
    nonisolated func dataChannel(_ dataChannel: RTCDataChannel, didReceiveMessageWith buffer: RTCDataBuffer) {
        let data = buffer.data
        let touch = dataChannel.label == "homeai.android.touch", source = ObjectIdentifier(dataChannel)
        Task { @MainActor in
            guard !closed else { return }
            if touch {
                guard touchChannel.map(ObjectIdentifier.init) == source else { return }
                guard data.count <= 4096 else { disableTouch("触控消息超过大小限制"); return }
                if let handshake = try? JSONDecoder().decode(AndroidTouchHandshake.self, from: data) {
                    if handshake.status == "ping" || handshake.status == "alive" { handleHeartbeat(data); return }
                    if handshake.status == "ready" {
                        guard handshake.isReady, let channelID = handshake.channelID else {
                            disableTouch("安卓触控协议版本或会话标识不匹配"); return
                        }
                        guard !retiredTouchChannelIDs.contains(channelID) else {
                            diagnostic("touch_stale_ready_ignored"); return
                        }
                        if let previous = touchChannelID, previous != channelID {
                            invalidateTouchReadiness()
                            onTouchReset?()
                            diagnostic("touch_channel_generation_changed")
                        }
                        if touchBridgeReady { onTouchReset?() }
                        touchChannelID = channelID; touchBridgeReady = false; touchFailureReason = nil
                        touchLiveness.begin(channelID: channelID, at: ProcessInfo.processInfo.systemUptime)
                        monitorTouchLiveness()
                        diagnostic("touch_bridge_ready_waiting_alive")
                        publishReadiness(); return
                    }
                    if handshake.status == "unavailable" {
                        invalidateTouchReadiness()
                        diagnostic("touch_bridge_unavailable")
                        disableTouch(handshake.reason.map { String($0.prefix(200)) } ?? "安卓触控桥暂不可用")
                        return
                    }
                }
                guard let ack = try? JSONDecoder().decode(AndroidTouchAck.self, from: data), ack.isValid else {
                    disableTouch("触控确认不符合契约，操作已暂停"); return
                }
                if touchBridgeReady { onTouchAck?(ack) }
                else { diagnostic("touch_ack_stale_ignored") }
                return
            }
            guard data.count <= 4096 else { diagnostic("metadata_oversize"); disconnected("屏幕元数据超过大小限制"); return }
            if let frame = try? JSONDecoder().decode(AndroidScreenFrame.self, from: data) {
                guard frame.isValid else { diagnostic("metadata_frame_invalid"); disconnected("屏幕元数据尺寸或状态无效"); return }
                if !receivedMetadata { receivedMetadata = true; diagnostic("metadata_first_frame") }
                onFrame?(frame); return
            }
            struct Blocked: Decodable { let blocked_reason: String }
            if let blocked = try? JSONDecoder().decode(Blocked.self, from: data), !blocked.blocked_reason.isEmpty {
                let reason = String(blocked.blocked_reason.prefix(300))
                failureReason = reason
                diagnostic("capture_blocked")
                onBlocked?(reason)
                disconnected(reason)
            } else { diagnostic("metadata_invalid"); disconnected("屏幕元数据不符合契约") }
        }
    }
    nonisolated func peerConnection(_ peerConnection: RTCPeerConnection, didChange stateChanged: RTCSignalingState) {
        Task { @MainActor in guard !closed else { return }; diagnostic("signalling", state: Int(stateChanged.rawValue)) }
    }
    nonisolated func peerConnection(_ peerConnection: RTCPeerConnection, didAdd stream: RTCMediaStream) {}
    nonisolated func peerConnection(_ peerConnection: RTCPeerConnection, didRemove stream: RTCMediaStream) {}
    nonisolated func peerConnectionShouldNegotiate(_ peerConnection: RTCPeerConnection) {}
    nonisolated func peerConnection(_ peerConnection: RTCPeerConnection, didChange newState: RTCIceConnectionState) {
        Task { @MainActor in
            guard !closed else { return }
            diagnostic("ice", state: Int(newState.rawValue))
            if newState == .disconnected { allowBriefRecovery() }
            else if newState == .failed { disconnected("屏幕网络直连失败，不会回退中继。请检查两端网络。") }
            else if newState == .closed { disconnected("屏幕网络通道已关闭，请重新连接。") }
            else if newState == .connected || newState == .completed { publishReadiness() }
        }
    }
    nonisolated func peerConnection(_ peerConnection: RTCPeerConnection, didChange newState: RTCPeerConnectionState) {
        Task { @MainActor in
            guard !closed else { return }
            diagnostic("connection", state: Int(newState.rawValue))
            if newState == .disconnected { allowBriefRecovery() }
            else if newState == .failed { disconnected("屏幕加密连接协商失败，请重新连接。") }
            else if newState == .closed { disconnected("屏幕连接已关闭，请重新连接。") }
            else if newState == .connected { publishReadiness() }
        }
    }
    nonisolated func peerConnection(_ peerConnection: RTCPeerConnection, didChange newState: RTCIceGatheringState) {
        Task { @MainActor in guard !closed else { return }; diagnostic("gathering", state: Int(newState.rawValue)) }
    }
    nonisolated func peerConnection(_ peerConnection: RTCPeerConnection, didGenerate candidate: RTCIceCandidate) {
        guard candidate.sdp.contains(" typ srflx") else { return }
        Task { @MainActor in
            guard !closed else { return }
            if firstPublicCandidateAt == nil { firstPublicCandidateAt = .now }
        }
    }
    nonisolated func peerConnection(_ peerConnection: RTCPeerConnection, didRemove candidates: [RTCIceCandidate]) {}
    nonisolated func peerConnection(_ peerConnection: RTCPeerConnection, didOpen dataChannel: RTCDataChannel) {
        dataChannel.close()
        Task { @MainActor in guard !closed else { return }; diagnostic("unexpected_remote_channel_rejected") }
    }
    nonisolated func peerConnection(_ peerConnection: RTCPeerConnection, didAdd rtpReceiver: RTCRtpReceiver, streams: [RTCMediaStream]) {
        if let track = rtpReceiver.track as? RTCVideoTrack {
            Task { @MainActor in guard !closed else { return }; diagnostic("video_track_received"); onTrack?(track) }
        } else if let track = rtpReceiver.track as? RTCAudioTrack {
            Task { @MainActor in
                guard !closed, receiveAudio else { track.isEnabled = false; return }
                remoteAudioTrack = track; setAudioEnabled(audioEnabled); diagnostic("audio_track_received")
            }
        }
    }
}
