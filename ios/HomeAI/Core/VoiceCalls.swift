import AVFoundation
@preconcurrency import CallKit
import Foundation
import Observation
@preconcurrency import PushKit

struct VoiceCallState: Decodable, Identifiable, Sendable {
    let id: String
    let status: String
    let direction: String
    let reason: String
    let expires_at: Double
    let greeting_turn_id: String?
}
struct VoiceCallPreferences: Codable, Sendable {
    var incoming_enabled = false
    var quiet_start = 22
    var quiet_end = 8
    var daily_limit = 4
    var timezone = TimeZone.current.identifier
    var speaker = "中文女"
}
struct VoiceCallSettings: Decodable, Sendable {
    struct Capability: Decodable, Sendable { let configured: Bool; let provider_id: String? }
    let preferences: VoiceCallPreferences
    let capabilities: [String: Capability]
    let apns_ready: Bool
    let device_registered: Bool
}
private struct VoiceTurnState: Decodable {
    let id: String
    let status: String
    let transcript: String
    let answer: String
    let audio_count: Int
    let error: String?
}

@MainActor @Observable
final class VoiceCalls: NSObject, @preconcurrency CXProviderDelegate, @preconcurrency PKPushRegistryDelegate {
    static let shared = VoiceCalls()
    var presented = false
    var status = "未通话"
    var error: String?
    var transcript = ""
    var answer = ""
    var muted = false
    var activeID: UUID?
    var isRinging: Bool { activeID != nil && !accepted }
    private let provider: CXProvider
    private let controller = CXCallController()
    private var registry: PKPushRegistry?
    private let api = AppServices.api
    private let audio = VoiceCallAudio()
    private var operation: Task<Void, Never>?
    private var monitor: Task<Void, Never>?
    private var namespace: String?
    private var accepted = false
    private var greetingTurnID: String?
    private var audioActivated = false
    private var audioLease: UUID?
    private var token: String?
    private var tokenNamespace: String?
    private var lifetime = UUID()
    private var starting = false
    private var connectionDeadline: Task<Void, Never>?
    private var traceEvents: [[String: String]] = []
    private func trace(_ stage: String) {
        traceEvents.append(["stage": stage, "time": ISO8601DateFormatter().string(from: Date())])
        traceEvents = Array(traceEvents.suffix(40))
        if let data = try? JSONSerialization.data(withJSONObject: traceEvents),
           let directory = FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask).first {
            try? data.write(to: directory.appendingPathComponent("HomeAIVoiceDiagnostics.json"), options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication])
        }
    }

    private override init() {
        let config = CXProviderConfiguration()
        config.supportsVideo = false
        config.maximumCallGroups = 1
        config.maximumCallsPerCallGroup = 1
        config.supportedHandleTypes = [.generic]
        config.includesCallsInRecents = false
        provider = CXProvider(configuration: config)
        super.init()
        provider.setDelegate(self, queue: .main)
        audio.onSegment = { [weak self] data in Task { @MainActor in self?.send(data) } }
    }

    func registerPushes() {
        guard registry == nil else { return }
        let registry = PKPushRegistry(queue: .main)
        registry.delegate = self
        registry.desiredPushTypes = [.voIP]
        self.registry = registry
    }

    func synchronize() async {
        registerPushes()
        guard let token else { return }
        do {
            await api.restoreConnectionIfNeeded()
            let scope = try await api.syncNamespace()
            guard tokenNamespace != scope else { return }
            let body = try JSONEncoder().encode(VoIPToken(token: token, environment: ClientNotifications.pushEnvironment))
            _ = try await api.request("PUT", "/api/v1/voice/device", body: body, expectedNamespace: scope)
            tokenNamespace = scope
        } catch { self.error = "来电注册未完成：" + error.localizedDescription }
    }

    func pushRegistry(_ registry: PKPushRegistry, didUpdate pushCredentials: PKPushCredentials, for type: PKPushType) {
        guard type == .voIP else { return }
        token = pushCredentials.token.map { String(format: "%02x", $0) }.joined(); tokenNamespace = nil
        Task { await synchronize() }
    }
    func pushRegistry(_ registry: PKPushRegistry, didInvalidatePushTokenFor type: PKPushType) {
        token = nil; tokenNamespace = nil
        Task { _ = try? await api.request("DELETE", "/api/v1/voice/device") }
    }
    func pushRegistry(_ registry: PKPushRegistry, didReceiveIncomingPushWith payload: PKPushPayload, for type: PKPushType, completion: @escaping () -> Void) {
        trace("push_received")
        // 先向系统报告来电，再做网络鉴权；不能等待P2P建连才报告PushKit。
        let id = (payload.dictionaryPayload["call_id"] as? String).flatMap(UUID.init(uuidString:)) ?? UUID()
        let update = CXCallUpdate()
        update.remoteHandle = CXHandle(type: .generic, value: "Home AI")
        update.localizedCallerName = "Home AI"
        update.hasVideo = false; update.supportsHolding = false; update.supportsGrouping = false; update.supportsUngrouping = false
        let completion = MainThreadNotificationCompletion(completion)
        provider.reportNewIncomingCall(with: id, update: update) { error in
            Task { @MainActor in
                completion.call()
                self.trace(error == nil ? "push_reported" : "push_report_failed")
                guard error == nil else { return }
                guard self.activeID == nil || self.activeID == id else { self.provider.reportCall(with: id, endedAt: Date(), reason: .unanswered); return }
                self.activeID = id; self.status = "Home AI 来电"
                do {
                    await self.api.restoreConnectionIfNeeded()
                    self.namespace = try await self.api.syncNamespace()
                    self.trace("incoming_namespace_ready")
                    let info = try await self.getCall(id)
                    self.trace("incoming_verified")
                    guard info.status == "RINGING" || info.status == "ACTIVE" else { throw APIClient.APIError.message("来电已过期") }
                    guard self.activeID == id else { return }
                    self.answer = info.reason
                    self.startMonitor(id)
                } catch { if self.activeID == id { self.fail(error) } }
            }
        }
    }

    func start() async {
        guard activeID == nil && !starting else { presented = true; return }
        starting = true; let attempt = UUID(); lifetime = attempt
        defer { if lifetime == attempt { starting = false } }
        presented = true; error = nil; status = "正在连接家庭服务器"
        do {
            guard await AVAudioApplication.requestRecordPermission() else { throw APIClient.APIError.message("请允许麦克风后开始语音通话") }
            namespace = try await api.syncNamespace()
            let data = try await api.request("POST", "/api/v1/voice/calls", body: JSONEncoder().encode(CallKey(client_key: UUID().uuidString)), expectedNamespace: namespace)
            let call = try JSONDecoder().decode(VoiceCallState.self, from: data)
            guard let id = UUID(uuidString: call.id) else { throw APIClient.APIError.message("通话标识无效") }
            guard lifetime == attempt else {
                _ = try? await api.request("POST", path(id) + "/end", expectedNamespace: namespace)
                return
            }
            activeID = id; transcript = ""; answer = ""
            try await controller.request(CXTransaction(action: CXStartCallAction(call: id, handle: CXHandle(type: .generic, value: "Home AI"))))
            startMonitor(id)
        } catch { if lifetime == attempt { fail(error) } }
    }

    func provider(_ provider: CXProvider, perform action: CXStartCallAction) {
        accepted = true
        do { try configureAudio(); action.fulfill(); provider.reportOutgoingCall(with: action.callUUID, connectedAt: Date()); beginIfReady() }
        catch { action.fail(); fail(error) }
    }
    func provider(_ provider: CXProvider, perform action: CXAnswerCallAction) {
        trace("answer_received")
        guard activeID == action.callUUID else { action.fail(); return }
        connectionDeadline?.cancel()
        connectionDeadline = Task {
            do { try await Task.sleep(for: .seconds(25)) } catch { return }
            guard activeID == action.callUUID, !accepted else { return }
            if !action.isComplete { action.fail() }
            fail(APIClient.APIError.message("未能直连家庭服务器，通话已结束。请检查家庭网络和协调平台连接后重试。"))
        }
        Task {
            do {
                guard await AVAudioApplication.requestRecordPermission() else { throw APIClient.APIError.message("麦克风未授权") }
                trace("microphone_authorized")
                await api.restoreConnectionIfNeeded()
                namespace = try await api.syncNamespace()
                let data = try await api.request("POST", path(action.callUUID) + "/accept", expectedNamespace: namespace)
                guard activeID == action.callUUID, !action.isComplete else { return }
                greetingTurnID = try JSONDecoder().decode(VoiceCallState.self, from: data).greeting_turn_id
                trace("server_accepted")
                connectionDeadline?.cancel(); connectionDeadline = nil
                accepted = true; presented = true; try configureAudio(); action.fulfill(); beginIfReady()
            } catch { if !action.isComplete { action.fail() }; if activeID == action.callUUID { fail(error) } }
        }
    }
    func provider(_ provider: CXProvider, perform action: CXEndCallAction) { cleanup(remote: true); action.fulfill() }
    func provider(_ provider: CXProvider, perform action: CXSetMutedCallAction) { muted = action.isMuted; audio.setListening(!muted && operation == nil); action.fulfill() }
    func provider(_ provider: CXProvider, timedOutPerforming action: CXAction) { action.fail(); cleanup(remote: true) }
    func providerDidReset(_ provider: CXProvider) { cleanup(remote: true) }
    func provider(_ provider: CXProvider, didActivate audioSession: AVAudioSession) { trace("audio_activated"); audioActivated = true; beginIfReady() }
    func provider(_ provider: CXProvider, didDeactivate audioSession: AVAudioSession) { audioActivated = false; audio.stop() }

    private func configureAudio() throws {
        let lease = UUID()
        try AppAudioLease.shared.claim(lease, kind: .call, onInterrupted: { [weak self] in Task { @MainActor in self?.end() } })
        audioLease = lease
    }
    private func beginIfReady() {
        guard accepted, audioActivated, activeID != nil else { return }
        do {
            try audio.start(); trace("capture_started")
            if let greeting = greetingTurnID, let id = activeID, let scope = namespace {
                greetingTurnID = nil; audio.setListening(false); status = "正在准备来电语音"
                operation = Task {
                    defer { operation = nil; if activeID == id { audio.setListening(!muted) } }
                    do { try await awaitAnswer(path(id) + "/turns/" + greeting, id: id, scope: scope) }
                    catch is CancellationError { }
                    catch { self.error = error.localizedDescription; status = "请说话" }
                }
            } else { audio.setListening(!muted); status = "请说话，停顿后自动发送" }
        }
        catch { fail(error) }
    }
    func toggleMute() {
        guard let id = activeID else { return }
        Task { try? await controller.request(CXTransaction(action: CXSetMutedCallAction(call: id, muted: !muted))) }
    }
    func answerIncoming() {
        guard let id = activeID, !accepted else { return }
        Task {
            do { try await controller.request(CXTransaction(action: CXAnswerCallAction(call: id))) }
            catch { fail(error) }
        }
    }
    func end() {
        guard let id = activeID else { cleanup(remote: false); presented = false; return }
        Task {
            do { try await controller.request(CXTransaction(action: CXEndCallAction(call: id))) }
            catch { cleanup(remote: true) }
        }
    }
    func identityChanged() { end(); tokenNamespace = nil }
    func interruptReply() { operation?.cancel(); status = "已停止朗读，可以继续说话" }

    private func send(_ data: Data) {
        guard let id = activeID, let scope = namespace, accepted, operation == nil, !muted else { return }
        operation = Task {
            defer { operation = nil; if activeID == id { audio.setListening(!muted) } }
            do {
                status = "正在识别和处理"
                let body = try JSONEncoder().encode(AudioTurn(client_key: UUID().uuidString, content_base64: data.base64EncodedString()))
                struct Created: Decodable { let id: String }
                let created = try JSONDecoder().decode(Created.self, from: await api.request("POST", path(id)+"/turns", body: body, expectedNamespace: scope))
                let turnPath = path(id)+"/turns/"+created.id
                try await awaitAnswer(turnPath, id: id, scope: scope)
            } catch is CancellationError { }
            catch { self.error = error.localizedDescription; status = "可以重新说话" }
        }
    }
    private func awaitAnswer(_ turnPath: String, id: UUID, scope: String) async throws {
                for _ in 0..<300 {
                    try Task.checkCancellation()
                    guard activeID == id else { return }
                    let turn = try JSONDecoder().decode(VoiceTurnState.self, from: await api.request("GET", turnPath, expectedNamespace: scope))
                    transcript = turn.transcript; answer = turn.answer
                    if turn.status == "FAILED" { throw APIClient.APIError.message(turn.error ?? "本段语音处理失败") }
                    if turn.status == "READY" || turn.status == "WAITING" {
                        status = turn.status == "WAITING" ? "请在聊天中处理请求卡片" : "正在回答"
                        for index in 0..<turn.audio_count {
                            try Task.checkCancellation()
                            let value = try JSONDecoder().decode(JSONValue.self, from: await api.request("GET", turnPath+"/audio/"+String(index), expectedNamespace: scope))
                            let wave = try SpeechPlayback.decodeAudio(value)
                            trace("playback_started")
                            try await audio.play(wave)
                            trace("playback_finished")
                        }
                        status = "请继续说话"; return
                    }
                    try await Task.sleep(for: .seconds(1))
                }
                throw APIClient.APIError.message("处理耗时较长，请在聊天中查看任务")
    }
    private func startMonitor(_ id: UUID) {
        monitor?.cancel()
        monitor = Task {
            var failures = 0
            while !Task.isCancelled, activeID == id {
                do {
                    let value = try await getCall(id)
                    failures = 0
                    if !["RINGING","ACTIVE"].contains(value.status) { cleanup(remote: false); return }
                } catch { failures += 1; if failures >= 5 { fail(error); return } }
                do { try await Task.sleep(for: .seconds(3)) } catch { return }
            }
        }
    }
    private func getCall(_ id: UUID) async throws -> VoiceCallState { try JSONDecoder().decode(VoiceCallState.self, from: await api.request("GET", path(id), expectedNamespace: namespace)) }
    private func path(_ id: UUID) -> String { "/api/v1/voice/calls/" + id.uuidString.lowercased() }
    private func fail(_ error: Error) { trace("failure_" + String((error as NSError).code)); self.error = error.localizedDescription; cleanup(remote: true) }
    private func cleanup(remote: Bool) {
        let id = activeID, scope = namespace
        lifetime = UUID(); starting = false
        connectionDeadline?.cancel(); connectionDeadline = nil
        activeID = nil; accepted = false; greetingTurnID = nil; audioActivated = false; muted = false
        operation?.cancel(); operation = nil; monitor?.cancel(); monitor = nil; audio.stop()
        if let lease = audioLease { AppAudioLease.shared.release(lease); audioLease = nil }
        if let id {
            provider.reportCall(with: id, endedAt: Date(), reason: remote ? .failed : .remoteEnded)
            if remote { Task { _ = try? await api.request("POST", path(id)+"/end", expectedNamespace: scope) } }
        }
        namespace = nil; status = "通话已结束"
    }
    private struct CallKey: Encodable { let client_key: String }
    private struct VoIPToken: Encodable { let token: String; let environment: String }
    private struct AudioTurn: Encodable { let client_key: String; let content_base64: String }
}
