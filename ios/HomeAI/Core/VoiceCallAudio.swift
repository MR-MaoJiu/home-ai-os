import AVFoundation
import Foundation

/// 音频回调仅处理PCM；业务请求与会话编排始终留在服务器。
final class VoiceCallAudio: @unchecked Sendable {
    private let lock = NSLock()
    private let engine = AVAudioEngine()
    private var player: AVAudioPlayer?
    private var pcm = Data()
    private var silence = 0
    private var voiced = 0
    private var listening = false
    private var installed = false
    var onSegment: (@Sendable (Data) -> Void)?

    func start() throws {
        stop()
        let input = engine.inputNode
        try input.setVoiceProcessingEnabled(true)
        let source = input.outputFormat(forBus: 0)
        guard let target = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: 16000, channels: 1, interleaved: false),
              let converter = AVAudioConverter(from: source, to: target) else { throw AudioError.format }
        input.installTap(onBus: 0, bufferSize: 2048, format: source) { [weak self] buffer, _ in
            let capacity = AVAudioFrameCount(Double(buffer.frameLength) * 16000 / source.sampleRate + 32)
            guard let output = AVAudioPCMBuffer(pcmFormat: target, frameCapacity: capacity) else { return }
            var supplied = false
            var error: NSError?
            converter.convert(to: output, error: &error) { _, status in
                if supplied { status.pointee = .noDataNow; return nil }
                supplied = true; status.pointee = .haveData; return buffer
            }
            guard error == nil, let samples = output.int16ChannelData?[0], output.frameLength > 0 else { return }
            self?.receive(Data(bytes: samples, count: Int(output.frameLength) * 2))
        }
        installed = true
        lock.lock(); listening = true; lock.unlock()
        engine.prepare(); try engine.start()
    }

    private func receive(_ bytes: Data) {
        lock.lock()
        guard listening else { lock.unlock(); return }
        let frames = bytes.count / 2
        let loud = bytes.withUnsafeBytes { raw -> Bool in
            let samples = raw.bindMemory(to: Int16.self)
            guard !samples.isEmpty else { return false }
            let energy = samples.reduce(0.0) { $0 + abs(Double($1)) } / Double(samples.count)
            return energy > 280
        }
        if loud { voiced += frames; silence = 0 }
        else { silence += frames }
        if voiced > 0 { pcm.append(bytes) }
        let ready = voiced >= 3200 && (silence >= 16000 || pcm.count >= 640000)
        if !ready {
            if silence > 32000 { pcm.removeAll(keepingCapacity: true); voiced = 0 }
            lock.unlock(); return
        }
        let data = Self.wav(pcm)
        pcm.removeAll(keepingCapacity: true); voiced = 0; silence = 0; listening = false
        lock.unlock()
        onSegment?(data)
    }

    func setListening(_ enabled: Bool) {
        lock.lock(); listening = enabled; pcm.removeAll(keepingCapacity: true); voiced = 0; silence = 0; lock.unlock()
    }

    @MainActor func play(_ data: Data) async throws {
        let player = try AVAudioPlayer(data: data)
        self.player = player
        guard player.play() else { throw AudioError.playback }
        while player.isPlaying {
            do { try await Task.sleep(for: .milliseconds(100)); try Task.checkCancellation() }
            catch { player.stop(); throw error }
        }
        self.player = nil
    }

    func stop() {
        setListening(false)
        if installed { engine.inputNode.removeTap(onBus: 0); installed = false }
        engine.stop(); player?.stop(); player = nil
    }

    static func wav(_ pcm: Data) -> Data {
        var data = Data("RIFF".utf8)
        func number<T: FixedWidthInteger>(_ value: T) { var little = value.littleEndian; withUnsafeBytes(of: &little) { data.append(contentsOf: $0) } }
        number(UInt32(pcm.count + 36)); data.append(Data("WAVEfmt ".utf8)); number(UInt32(16))
        number(UInt16(1)); number(UInt16(1)); number(UInt32(16000)); number(UInt32(32000)); number(UInt16(2)); number(UInt16(16))
        data.append(Data("data".utf8)); number(UInt32(pcm.count)); data.append(pcm)
        return data
    }
    private enum AudioError: LocalizedError {
        case format, playback
        var errorDescription: String? { self == .format ? "无法初始化通话录音格式" : "语音播放失败" }
    }
}
