import AVFoundation
import Foundation
import Observation

@MainActor @Observable
final class VoiceCapture {
    private var recorder: AVAudioRecorder?
    private var url: URL?
    var recording = false

    func start() async throws {
        guard await AVAudioApplication.requestRecordPermission() else { throw APIClient.APIError.message("麦克风未授权") }
        let session = AVAudioSession.sharedInstance()
        try session.setCategory(.record, mode: .measurement)
        try session.setActive(true)
        let file = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString + ".wav")
        url = file
        let audio = try AVAudioRecorder(url: file, settings: [AVFormatIDKey: kAudioFormatLinearPCM, AVSampleRateKey: 16000, AVNumberOfChannelsKey: 1, AVLinearPCMBitDepthKey: 16, AVLinearPCMIsFloatKey: false])
        recorder = audio
        guard audio.record(forDuration: 60) else { throw APIClient.APIError.message("录音启动失败") }
        recording = true
    }

    func finish() throws -> Data {
        recorder?.stop()
        recorder = nil
        recording = false
        try AVAudioSession.sharedInstance().setActive(false)
        guard let file = url else { throw APIClient.APIError.message("没有录音") }
        defer { try? FileManager.default.removeItem(at: file); url = nil }
        return try Data(contentsOf: file)
    }
}
