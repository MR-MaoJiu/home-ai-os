import AVFoundation
import Foundation
import Observation

/// 只追踪前台新答复；累计流式快照只消费新增完整句，最终快照只补未朗读的尾段。
struct ChatAutomaticSpeech {
    static let preferenceKey = "automaticallyReadChatAnswers"
    struct Segment {
        let turnID: String
        let taskID: String
        let messageID: String
        let kind: ChatSpeechMessage.Kind
        let text: String
        let sources: [RecordReference]
        let context: ChatSpeechValidation
    }
    private struct Cursor {
        var consumed = ""
        var sources = ""
        var finished = false
        var historical = false
        var waitsForFinal = false
        var completedTexts: Set<String> = []
    }
    private var cursors: [String: Cursor] = [:]
    private(set) var suppressed: Set<String> = []
    private(set) var suppressedMessages: Set<String> = []

    mutating func baseline(_ turns: [StoredChatTurn]) {
        for turn in turns {
            for message in ChatSpeechMessage.messages(in: turn) {
                cursors[message.id] = Cursor(consumed: message.rawText, sources: message.sourceIdentity,
                    finished: !message.isStreaming, historical: !turn.pending,
                    completedTexts: message.isStreaming ? [] : [message.rawText])
            }
            // 已完成的历史空答复随后得到缓存修复时，也不能被当成新回复播放。
            if !turn.pending, cursors[turn.id + ":answer"] == nil {
                cursors[turn.id + ":answer"] = Cursor(historical: true)
            }
        }
    }
    mutating func suppress(_ ids: Set<String>) { suppressed.formUnion(ids) }
    mutating func suppressMessages(_ ids: Set<String>) { suppressedMessages.formUnion(ids) }
    mutating func update(_ turns: [StoredChatTurn], enabled: Bool) -> [Segment] {
        var segments: [Segment] = []
        let ordered = turns.sorted {
            ($0.completed_at ?? $0.created_at ?? Double($0.sequence)) < ($1.completed_at ?? $1.created_at ?? Double($1.sequence))
        }
        for turn in ordered {
            for message in ChatSpeechMessage.messages(in: turn) {
                var cursor = cursors[message.id] ?? Cursor(sources: message.sourceIdentity)
                guard enabled, turn.result_redacted != true, !suppressed.contains(turn.id),
                      !suppressedMessages.contains(message.id), !cursor.historical else {
                    if !enabled {
                        cursor.consumed = message.rawText
                        cursor.sources = message.sourceIdentity
                        cursor.finished = !message.isStreaming
                        if cursor.finished { cursor.completedTexts.insert(message.rawText) }
                    }
                    cursors[message.id] = cursor
                    continue
                }
                if cursor.finished {
                    guard !cursor.completedTexts.contains(message.rawText) else { continue }
                    // 等待补充的提示和最终答复属于同一任务，但仍是两次独立的可见回复。
                    cursor.consumed = ""; cursor.sources = message.sourceIdentity; cursor.finished = false
                }
                if cursor.waitsForFinal {
                    guard !message.isStreaming else { continue }
                    cursor.consumed = ""; cursor.sources = message.sourceIdentity; cursor.waitsForFinal = false
                }
                guard message.rawText.hasPrefix(cursor.consumed), cursor.consumed.isEmpty
                    || ChatSpeechValidation.matchesSources(cursor.sources, current: message.sourceIdentity, allowingAdditions: true) else {
                    // 流式内容更正后先等最终版本；已完成的新版本可重新核验后完整朗读。
                    if message.isStreaming {
                        cursor.waitsForFinal = true; cursors[message.id] = cursor; continue
                    }
                    cursor.consumed = ""; cursor.sources = message.sourceIdentity
                    let text = Self.spokenText(message.rawText)
                    cursor.consumed = message.rawText; cursor.finished = true
                    cursor.completedTexts.insert(message.rawText); cursors[message.id] = cursor
                    if !text.isEmpty {
                        segments.append(Segment(turnID: turn.id, taskID: turn.task_id, messageID: message.id, kind: message.kind,
                            text: text, sources: message.sources, context: message.validation))
                    }
                    continue
                }
                let remaining = String(message.rawText.dropFirst(cursor.consumed.count))
                let stable = message.isStreaming ? Self.completePrefix(remaining) : remaining
                cursor.sources = message.sourceIdentity
                cursor.consumed += stable
                cursor.finished = !message.isStreaming
                if cursor.finished { cursor.completedTexts.insert(message.rawText) }
                cursors[message.id] = cursor
                let text = Self.spokenText(stable)
                if !text.isEmpty {
                    segments.append(Segment(turnID: turn.id, taskID: turn.task_id, messageID: message.id, kind: message.kind,
                        text: text, sources: message.sources, context: message.validation))
                }
            }
        }
        return segments
    }
    static func completePrefix(_ text: String) -> String {
        var end = text.startIndex
        var inFence = false
        var index = text.startIndex
        while index < text.endIndex {
            if text[index...].hasPrefix("```") || text[index...].hasPrefix("~~~") {
                inFence.toggle(); index = text.index(index, offsetBy: 3); continue
            }
            let next = text.index(after: index)
            let character = text[index]
            if !inFence && ("。！？；!?\n".contains(character)
                || (character == "." && next < text.endIndex && text[next].isWhitespace)) { end = next }
            index = next
        }
        return String(text[..<end])
    }
    static func spokenText(_ text: String) -> String {
        func spoken(_ blocks: [ChatMarkdownDocument.Block], includeCode: Bool) -> [String] {
            blocks.flatMap { block -> [String] in
                if !includeCode, let kind = block.kind, case .codeBlock = kind { return [] }
                let value = String(block.text.characters).trimmingCharacters(in: .whitespacesAndNewlines)
                return (value.isEmpty ? [] : [value]) + spoken(block.children, includeCode: includeCode)
            }
        }
        let blocks = ChatMarkdownDocument(text).blocks
        let prose = spoken(blocks, includeCode: false).joined(separator: "\n")
        if !prose.isEmpty { return prose }
        // 纯代码回复仍可朗读；混合回复默认朗读说明，避免逐字念大段程序影响使用。
        let code = spoken(blocks, includeCode: true).joined(separator: "\n")
        if !code.isEmpty { return "代码内容：\n" + code }
        return text.trimmingCharacters(in: .whitespacesAndNewlines)
    }

}

@MainActor @Observable
final class SpeechPlayback {
    var voices: [String] = []
    var selectedVoice = ""
    var status = ""
    var busy = false
    var playing = false
    var error: String?
    private(set) var remainingAnswerText = ""
    private var audioLease: UUID?
    private var operation: Task<Void, Never>?
    private var generation = UUID()
    private var activeTask: String?
    private var namespace: String?
    private var voiceNamespace: String?
    private var voicesLoadedAt: Date?
    private var clips: [String: Data] = [:]
    private var clipOrder: [String] = []
    private var clipNamespace: String?
    private let api: APIClient

    init(api: APIClient) { self.api = api }

    func loadVoices(expectedNamespace: String? = nil) {
        start { namespace in
            try await self.fetchVoices(namespace: namespace)
        } expectedNamespace: { expectedNamespace }
    }

    private func fetchVoices(namespace: String) async throws {
        if voiceNamespace == namespace, let loaded = voicesLoadedAt, Date().timeIntervalSince(loaded) < 600,
           !voices.isEmpty, voices.contains(selectedVoice) { return }
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
        voiceNamespace = namespace; voicesLoadedAt = Date()
        if !voices.contains(self.selectedVoice) { self.selectedVoice = voices.first(where: { $0 == "中文女" }) ?? voices[0] }
        self.status = "音色来自家庭服务器"
    }

    /// 聊天朗读自动取得音色；分段仅处理播放长度，不改变服务端业务任务。
    func speakAnswer(_ text: String, expectedNamespace: String, sources: [RecordReference] = [], context: ChatSpeechValidation? = nil) {
        let parts: [String]
        do { parts = try Self.answerSegments(text) }
        catch { self.error = error.localizedDescription; return }
        remainingAnswerText = parts.joined()
        start { namespace in
            self.status = "正在准备语音"
            try await self.validatePlayback(sources, context: context, namespace: namespace)
            try await self.fetchVoices(namespace: namespace)
            try Task.checkCancellation()
            let speaker = self.selectedVoice
            var pending: Task<Data, Error>? = nil
            defer { pending?.cancel() }
            for (index, part) in parts.enumerated() {
                try Task.checkCancellation()
                let audio: Data
                if let prepared = pending { audio = try await prepared.value; pending = nil }
                else { audio = try await self.answerAudio(part, speaker: speaker, sources: sources, context: context, namespace: namespace) }
                try await self.validatePlayback(sources, context: context, namespace: namespace)
                try Task.checkCancellation()
                // 只提前准备一段；正在播放时合成下一段，停止时取消预取。
                if index + 1 < parts.count {
                    let following = parts[index + 1]
                    pending = Task { try await self.answerAudio(following, speaker: speaker, sources: sources, context: context, namespace: namespace) }
                }
                try await self.play(audio)
                try Task.checkCancellation()
                self.remainingAnswerText = parts.dropFirst(index + 1).joined()
            }
            self.status = "朗读完成"
        } expectedNamespace: { expectedNamespace }
    }

    private func answerAudio(_ text: String, speaker: String, sources: [RecordReference], context: ChatSpeechValidation?, namespace: String) async throws -> Data {
        try Task.checkCancellation()
        try await validatePlayback(sources, context: context, namespace: namespace)
        if clipNamespace != namespace { clips.removeAll(); clipOrder.removeAll(); clipNamespace = namespace }
        let key = DeviceIdentity.hash(Data((speaker + "\n" + text).utf8))
        if let data = clips[key] { return data }
        let result = try await execute("speech.synthesize@v1", arguments: ["text": text, "speaker": speaker], namespace: namespace)
        try Task.checkCancellation()
        guard namespace == (try await api.syncNamespace()) else { throw APIClient.APIError.message("家庭身份已变化") }
        let data = try Self.decodeAudio(result)
        clips[key] = data; clipOrder.removeAll { $0 == key }; clipOrder.append(key)
        while clips.values.reduce(0, { $0 + $1.count }) > 6 * 1024 * 1024, let oldest = clipOrder.first {
            clips.removeValue(forKey: oldest); clipOrder.removeFirst()
        }
        return data
    }

    nonisolated static func answerSegments(_ text: String) throws -> [String] {
        guard !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty, text.unicodeScalars.count <= 20000 else {
            throw APIClient.APIError.message("朗读内容为空或超过 20000 字符")
        }
        var parts: [String] = [], part = "", count = 0
        for character in text {
            let size = character.unicodeScalars.count
            guard size <= 120 else { throw APIClient.APIError.message("朗读内容含过长的组合字符") }
            let limit = parts.isEmpty ? 40 : 120
            if count + size > limit { parts.append(part); part = ""; count = 0 }
            part.append(character); count += size
            if count >= (parts.isEmpty ? 20 : 80) && "。！？；.!?;\n".contains(character) { parts.append(part); part = ""; count = 0 }
        }
        if !part.isEmpty { parts.append(part) }
        return parts.filter { !$0.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }
    }

    private func play(_ audio: Data) async throws {
        try Task.checkCancellation()
        let lease = UUID()
        audioLease = lease
        try await SpeechAudioSession.shared.play(audio, lease: lease)
        if Task.isCancelled { await SpeechAudioSession.shared.release(lease); throw CancellationError() }
        playing = true; status = "正在朗读"
        while await SpeechAudioSession.shared.isPlaying(lease) {
            try await Task.sleep(for: .milliseconds(200))
            try Task.checkCancellation()
        }
        releaseAudio()
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
        status = task == nil ? "已停止" : "已停止等待，服务端任务仍在继续处理"
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
            try Task.checkCancellation()
            status = capability == "speech.voices@v1" ? "正在准备音色" : "正在生成语音"
            if task.status == "SUCCEEDED", let result = task.result {
                try Task.checkCancellation(); activeTask = nil; return result
            }
            if ["FAILED", "CANCELED", "NEEDS_RECONCILIATION", "AWAITING_APPROVAL"].contains(task.status) {
                throw APIClient.APIError.message(task.error ?? task.status)
            }
            try await Task.sleep(for: .milliseconds(350))
        }
        throw APIClient.APIError.message("语音任务仍在服务器运行，请稍后查看；没有重新提交")
    }

    private func validatePlayback(_ sources: [RecordReference], context: ChatSpeechValidation?, namespace: String) async throws {
        guard let context else { try await validateSources(sources, namespace: namespace); return }
        guard let identifier = UUID(uuidString: context.taskID) else { throw APIClient.APIError.message("朗读消息的任务标识无效") }
        let data = try await api.request("GET", "/api/v1/tasks/" + identifier.uuidString.lowercased() + "/chat-speech", expectedNamespace: namespace)
        let current = try JSONDecoder().decode(StoredChatTurn.self, from: data)
        guard context.matches(current) else { throw APIClient.APIError.message("这条回复已更新或授权已变化，请刷新后重新朗读") }
        try Task.checkCancellation()
    }

    private func validateSources(_ sources: [RecordReference], namespace: String) async throws {
        for source in sources {
            let data: Data
            do {
                data = try await api.request("GET", ResourceRoutes.record(source.id, taskID: source.taskID), expectedNamespace: namespace)
            } catch APIClient.APIError.http(404, _) where source.taskID == nil {
                do { data = try await api.request("GET", "/api/v1/memory/entries/" + source.id, expectedNamespace: namespace) }
                catch APIClient.APIError.http(404, _) { throw APIClient.APIError.message("回答引用的资料已删除或不可访问，请重新查询后朗读") }
            }
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
                    player?.stop(); player = nil; owner = nil
                    try AppAudioLease.shared.claim(lease, kind: .speech, onInterrupted: { [weak self] in
                        guard let self else { return }
                        self.queue.async {
                            guard self.owner == lease else { return }
                            self.player?.stop(); self.player = nil; self.owner = nil
                        }
                    })
                    guard next.prepareToPlay(), next.play() else {
                        throw APIClient.APIError.message("无法播放合成音频")
                    }
                    player = next; owner = lease
                    continuation.resume()
                } catch { AppAudioLease.shared.release(lease); continuation.resume(throwing: error) }
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
                    AppAudioLease.shared.release(lease)
                }
                continuation.resume()
            }
        }
    }
}
