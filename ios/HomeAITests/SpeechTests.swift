import XCTest
@testable import HomeAI

final class SpeechTests: XCTestCase {
    @MainActor
    func testRealServerVoicesSynthesisAndPlayback() async throws {
        let environment = ProcessInfo.processInfo.environment
        guard let path = environment["HOMEAI_SPEECH_PAIR_FILE"] ?? environment["TEST_RUNNER_HOMEAI_SPEECH_PAIR_FILE"] else {
            throw XCTSkip("需要真实 CosyVoice 与隔离 HTTPS 服务配对文件")
        }
        struct Fixture: Decodable { let pairing: PairingCode }
        let fixture = try JSONDecoder().decode(Fixture.self, from: Data(contentsOf: URL(fileURLWithPath: path)))
        let api = APIClient(persistConnection: false)
        try await api.pair(fixture.pairing)
        let namespace = try await api.syncNamespace()
        let playback = SpeechPlayback(api: api)
        playback.loadVoices(expectedNamespace: namespace)
        let deadline = Date().addingTimeInterval(120)
        while playback.busy, Date() < deadline {
            _ = try await api.request("POST", "/_test/run-speech", expectedNamespace: namespace)
            try await Task.sleep(for: .milliseconds(200))
        }
        XCTAssertNil(playback.error)
        XCTAssertTrue(playback.voices.contains("中文女"))
        playback.speak("你好，欢迎使用家庭助手。", expectedNamespace: namespace)
        var observedPlayback = false
        while playback.busy, Date() < deadline {
            if playback.playing { observedPlayback = true; break }
            _ = try await api.request("POST", "/_test/run-speech", expectedNamespace: namespace)
            try await Task.sleep(for: .milliseconds(100))
        }
        XCTAssertNil(playback.error)
        XCTAssertTrue(observedPlayback, playback.status)
        playback.stop()
        XCTAssertFalse(playback.playing)
        XCTAssertFalse(playback.busy)
        // 等待期间离开页面后，即使服务端稍后完成，也不能恢复播放。
        playback.speak("停止后不要播放这段语音。", expectedNamespace: namespace)
        try await Task.sleep(for: .milliseconds(150))
        playback.stop()
        _ = try await api.request("POST", "/_test/run-speech", expectedNamespace: namespace)
        try await Task.sleep(for: .milliseconds(300))
        XCTAssertFalse(playback.playing)
        XCTAssertFalse(playback.busy)
        let recordBody = try JSONSerialization.data(withJSONObject: ["records": [["source": "speech-test", "source_id": UUID().uuidString, "kind": "document.parsed", "version": 1, "payload": ["markdown": "验收资料"]]]])
        struct Records: Decodable { let records: [DataEntry] }
        let records = try JSONDecoder().decode(Records.self, from: await api.request("POST", "/api/v1/data/sync", body: recordBody, expectedNamespace: namespace))
        let record = try XCTUnwrap(records.records.first)
        _ = try await api.request("DELETE", "/api/v1/data/" + record.id, expectedNamespace: namespace)
        playback.speak("已删除资料不应朗读。", expectedNamespace: namespace, sources: [RecordReference(id: record.id, title: "验收资料", version: 1)])
        for _ in 0..<20 where playback.busy { try await Task.sleep(for: .milliseconds(100)) }
        XCTAssertNotNil(playback.error)
        XCTAssertFalse(playback.playing)
        playback.loadVoices(expectedNamespace: "wrong-household")
        try await Task.sleep(for: .milliseconds(100))
        XCTAssertNotNil(playback.error)
        XCTAssertFalse(playback.busy)
    }
}
