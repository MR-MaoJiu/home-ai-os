import AVFoundation
import Foundation
import Observation

@MainActor @Observable
final class VoiceCapture {
    private var recorder: AVAudioRecorder?
    private var url: URL?
    private var audioLease: UUID?
    var recording = false

    func start() async throws {
        guard await AVAudioApplication.requestRecordPermission() else { throw APIClient.APIError.message("麦克风未授权") }
        let lease = UUID()
        audioLease = lease
        do {
            try AppAudioLease.shared.claim(lease, kind: .recording, onInterrupted: { [weak self] in
                Task { @MainActor in
                    guard let self, self.audioLease == lease else { return }
                    self.recorder?.stop(); self.recording = false; self.audioLease = nil
                }
            })
            let file = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString + ".wav")
            url = file
            let audio = try AVAudioRecorder(url: file, settings: [AVFormatIDKey: kAudioFormatLinearPCM, AVSampleRateKey: 16000, AVNumberOfChannelsKey: 1, AVLinearPCMBitDepthKey: 16, AVLinearPCMIsFloatKey: false])
            recorder = audio
            guard audio.record(forDuration: 60) else { throw APIClient.APIError.message("录音启动失败") }
            recording = true
        } catch {
            recorder?.stop(); recorder = nil; recording = false; audioLease = nil
            AppAudioLease.shared.release(lease)
            if let url { try? FileManager.default.removeItem(at: url); self.url = nil }
            throw error
        }
    }

    func finish() throws -> Data {
        recorder?.stop()
        recorder = nil
        recording = false
        if let audioLease { self.audioLease = nil; AppAudioLease.shared.release(audioLease) }
        guard let file = url else { throw APIClient.APIError.message("没有录音") }
        defer { try? FileManager.default.removeItem(at: file); url = nil }
        return try Data(contentsOf: file)
    }
}
