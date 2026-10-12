import XCTest
@testable import HomeAI

final class VoiceCallTests: XCTestCase {
    @MainActor func testCaptureEncodesActualPCMWithBoundedWAVHeader() throws {
        let samples = Data(repeating: 16, count: 6400)
        let audio = VoiceCallAudio.wav(samples)
        XCTAssertEqual(audio.count, samples.count + 44)
        XCTAssertEqual(audio.prefix(4), Data("RIFF".utf8))
        XCTAssertEqual(audio.subdata(in: 8..<12), Data("WAVE".utf8))
        XCTAssertEqual(audio.suffix(samples.count), samples)
        let value: JSONValue = .object(["audio_base64": .string(audio.base64EncodedString()), "format": .string("wav")])
        XCTAssertEqual(try SpeechPlayback.decodeAudio(value), audio)
    }
    func testCallPreferencesDefaultToNoAutonomousCalls() throws {
        let preferences = VoiceCallPreferences()
        XCTAssertFalse(preferences.incoming_enabled)
        XCTAssertEqual(preferences.quiet_start, 22)
        XCTAssertEqual(preferences.quiet_end, 8)
        let restored = try JSONDecoder().decode(VoiceCallPreferences.self, from: JSONEncoder().encode(preferences))
        XCTAssertEqual(restored.daily_limit, 4)
    }
    func testCallStateIncludesServerGreetingAndExpiry() throws {
        let value = Data(#"{"id":"4cbca263-8f08-4810-a655-f3777af66815","status":"ACTIVE","direction":"incoming","reason":"公开测试","expires_at":1000,"greeting_turn_id":"greeting"}"#.utf8)
        let state = try JSONDecoder().decode(VoiceCallState.self, from: value)
        XCTAssertEqual(state.greeting_turn_id, "greeting")
        XCTAssertEqual(state.expires_at, 1000)
    }
}

extension VoiceCallTests {
    @MainActor
    func testLivePairedDeviceVoIPRegistration() async throws {
        let env = ProcessInfo.processInfo.environment
        guard env["HOMEAI_VOICE_LIVE"] == "1" || env["TEST_RUNNER_HOMEAI_VOICE_LIVE"] == "1" else { throw XCTSkip("需要已配对真机与本机服务") }
        let api = AppServices.api
        await api.restoreConnectionIfNeeded()
        let connected = await api.isConnected()
        XCTAssertTrue(connected)
        VoiceCalls.shared.registerPushes()
        for _ in 0..<30 {
            await VoiceCalls.shared.synchronize()
            let data = try await api.request("GET", "/api/v1/voice/settings")
            let settings = try JSONDecoder().decode(VoiceCallSettings.self, from: data)
            if settings.device_registered {
                XCTAssertTrue(settings.apns_ready)
                XCTAssertTrue(settings.capabilities["transcription"]?.configured == true)
                XCTAssertTrue(settings.capabilities["synthesis"]?.configured == true)
                return
            }
            try await Task.sleep(for: .seconds(1))
        }
        XCTFail("VoIP令牌未登记：" + (VoiceCalls.shared.error ?? "系统尚未返回令牌"))
    }
}
