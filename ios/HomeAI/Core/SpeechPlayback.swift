import AVFoundation
import Foundation
import Observation

@MainActor @Observable
final class SpeechPlayback {
    var voices: [String] = []
    var selectedVoice = ""
    var status = ""
    var busy = false
    var playing = false
    var error: String?
    private var audioLease: UUID?
    private var operation: Task<Void, Never>?
    private var generation = UUID()
    private var activeTask: String?
    private var namespace: String?
    private let api: APIClient

    init(api: APIClient) { self.api = api }

    func loadVoices(expectedNamespace: String? = nil) {
        start { namespace in
            let result = try await self.execute("speech.voices@v1", arguments: [:], namespace: namespace)
            guard case .object(let fields) = result, case .array(let values) = fields["voices"] else {
                throw APIClient.APIError.message("服务器音色列表格式无效")
            }
            let voices = values.compactMap { value -> String? in
                if case .string(let name) = value, !name.isEmpty, name.count <= 100 { return name }
                return nil
            }
            guard !voices.isEmpty, voices.count <= 50 else { throw APIClient.APIError.message("没有可用内置音色") }
            self.voices = voices
            if !voices.contains(self.selectedVoice) { self.selectedVoice = voices.first(where: { $0 == "中文女" }) ?? voices[0] }
            self.status = "音色来自家庭服务器"
        } expectedNamespace: { expectedNamespace }
    }

    func speak(_ text: String, expectedNamespace: String? = nil, sources: [RecordReference] = []) {
        guard voices.contains(selectedVoice), !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
              text.unicodeScalars.count <= 300 else { error = "请选择音色并输入最多 300 字符"; return }
        let speaker = selectedVoice
        start { namespace in
            try await self.validateSources(sources, namespace: namespace)
            let result = try await self.execute("speech.synthesize@v1", arguments: ["text": text, "speaker": speaker], namespace: namespace)
            try await self.validateSources(sources, namespace: namespace)
            let audio = try Self.decodeAudio(result)
            try Task.checkCancellation()
            guard namespace == (try await self.api.syncNamespace()) else { throw APIClient.APIError.message("家庭身份已变化") }
            let lease = UUID()
            self.audioLease = lease
            try await SpeechAudioSession.shared.play(audio, lease: lease)
            if Task.isCancelled {
                await SpeechAudioSession.shared.release(lease)
                throw CancellationError()
            }
            self.playing = true
            self.status = "正在播放"
            while await SpeechAudioSession.shared.isPlaying(lease) {
                try await Task.sleep(for: .milliseconds(200))
                try Task.checkCancellation()
            }
            self.releaseAudio()
            self.status = "播放完成"
        } expectedNamespace: { expectedNamespace }
    }

    func stop(cancelServer: Bool = false) {
        let task = activeTask, scope = namespace
        generation = UUID()
        operation?.cancel(); operation = nil
        activeTask = nil; busy = false
        releaseAudio()
        status = task == nil ? "已停止" : "已停止等待，服务端任务可在活动中查看"
        if cancelServer, let task, let scope {
            Task { _ = try? await api.request("POST", "/api/v1/tasks/" + task + "/cancel", expectedNamespace: scope) }
        }
    }

    private func releaseAudio() {
        playing = false
        if let lease = audioLease {
            audioLease = nil
            Task { await SpeechAudioSession.shared.release(lease) }
        }
    }

    private func start(_ action: @escaping @MainActor (String) async throws -> Void, expectedNamespace: () -> String?) {
        guard !busy else { return }
        stop(); error = nil; busy = true
        let run = UUID(); generation = run
        let expected = expectedNamespace()
        operation = Task {
            defer { if generation == run { busy = false; activeTask = nil; operation = nil } }
            do {
                let scope = try await api.syncNamespace()
                if let expected, expected != scope { throw APIClient.APIError.message("家庭身份已变化，请重新打开页面") }
                try Task.checkCancellation()
                guard generation == run else { return }
                namespace = scope
                try await action(scope)
            } catch is CancellationError { }
            catch {
                if generation == run { releaseAudio(); self.error = error.localizedDescription; status = "操作未完成" }
            }
        }
    }

    private func execute(_ capability: String, arguments: [String: String], namespace: String) async throws -> JSONValue {
        try Task.checkCancellation()
        let body = try JSONSerialization.data(withJSONObject: ["capability": capability, "arguments": arguments,
            "idempotency_key": UUID().uuidString, "step_timeout_seconds": 300])
        let created = try JSONDecoder().decode(TaskCreated.self, from: await api.request("POST", "/api/v1/tasks", body: body, expectedNamespace: namespace))
        try Task.checkCancellation()
        activeTask = created.id
        let deadline = Date().addingTimeInterval(310)
        while Date() < deadline {
            try Task.checkCancellation()
            let task = try JSONDecoder().decode(TaskResult.self, from: await api.request("GET", "/api/v1/tasks/" + created.id, expectedNamespace: namespace))
            status = "服务器任务：" + task.status
            if task.status == "SUCCEEDED", let result = task.result {
                try Task.checkCancellation(); activeTask = nil; return result
            }
            if ["FAILED", "CANCELED", "NEEDS_RECONCILIATION", "AWAITING_APPROVAL"].contains(task.status) {
                throw APIClient.APIError.message(task.error ?? task.status)
            }
            try await Task.sleep(for: .seconds(1))
        }
        throw APIClient.APIError.message("任务仍在服务器运行，请到活动页面查看；没有重新提交")
    }

    private func validateSources(_ sources: [RecordReference], namespace: String) async throws {
        for source in sources {
            let data = try await api.request("GET", "/api/v1/data/" + source.id, expectedNamespace: namespace)
            let record = try JSONDecoder().decode(DataEntry.self, from: data)
            guard record.sensitivity != "SECRET", let version = source.version, version == record.version else {
                throw APIClient.APIError.message("回答来源已更新，请重新查询后朗读")
            }
        }
    }

    static func decodeAudio(_ result: JSONValue) throws -> Data {
        guard case .object(let fields) = result, case .string(let encoded) = fields["audio_base64"],
              case .string("wav") = fields["format"], encoded.utf8.count <= 3_000_000,
              let data = Data(base64Encoded: encoded), data.count >= 44,
              data.prefix(4) == Data("RIFF".utf8), data.dropFirst(8).prefix(4) == Data("WAVE".utf8) else {
            throw APIClient.APIError.message("服务器未返回有效 WAV 音频")
        }
        return data
    }
}


// 所有可变音频对象仅由这条串行队列访问，不向主线程传递 AVAudioPlayer。
// unchecked Sendable 的约束是 player/owner 永远不能在 queue 外读写。
private final class SpeechAudioSession: @unchecked Sendable {
    static let shared = SpeechAudioSession()
    private let queue = DispatchQueue(label: "dev.homeai.speech-session")
    private var owner: UUID?
    private var player: AVAudioPlayer?

    func play(_ data: Data, lease: UUID) async throws {
        try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
            queue.async { [self] in
                do {
                    let next = try AVAudioPlayer(data: data)
                    guard next.duration.isFinite, next.duration > 0, next.duration <= 45.1, next.numberOfChannels == 1 else {
                        throw APIClient.APIError.message("合成音频格式无效")
                    }
                    player?.stop()
                    let session = AVAudioSession.sharedInstance()
                    try session.setCategory(.playback, mode: .spokenAudio, options: [])
                    try session.setActive(true)
                    guard next.prepareToPlay(), next.play() else {
                        try? session.setActive(false, options: .notifyOthersOnDeactivation)
                        throw APIClient.APIError.message("无法播放合成音频")
                    }
                    player = next; owner = lease
                    continuation.resume()
                } catch { continuation.resume(throwing: error) }
            }
        }
    }

    func isPlaying(_ lease: UUID) async -> Bool {
        await withCheckedContinuation { continuation in
            queue.async { [self] in continuation.resume(returning: owner == lease && player?.isPlaying == true) }
        }
    }

    func release(_ lease: UUID) async {
        await withCheckedContinuation { (continuation: CheckedContinuation<Void, Never>) in
            queue.async { [self] in
                if owner == lease {
                    player?.stop(); player = nil; owner = nil
                    try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
                }
                continuation.resume()
            }
        }
    }
}
