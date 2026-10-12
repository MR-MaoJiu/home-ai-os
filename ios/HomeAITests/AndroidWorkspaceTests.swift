import XCTest
@preconcurrency import WebRTC
@testable import HomeAI

@MainActor
final class AndroidWorkspaceTests: XCTestCase {

    func testFullscreenVideoFitsWithoutCroppingAndRejectsLetterboxTouches() throws {
        let video = CGSize(width: 1080, height: 2340)
        for container in [CGSize(width: 393, height: 852), CGSize(width: 852, height: 393)] {
            let rect = AndroidRemoteLayout.videoRect(video: video, container: container)
            XCTAssertEqual(rect.width / rect.height, video.width / video.height, accuracy: 0.000001)
            XCTAssertEqual(rect.midX, container.width / 2, accuracy: 0.001)
            XCTAssertEqual(rect.midY, container.height / 2, accuracy: 0.001)
            XCTAssertLessThanOrEqual(rect.maxX, container.width + 0.001)
            XCTAssertLessThanOrEqual(rect.maxY, container.height + 0.001)
            XCTAssertEqual(AndroidScreenPoint.normalize(rect.origin, in: rect), .init(x: 0, y: 0))
            let corner = try XCTUnwrap(AndroidScreenPoint.normalize(CGPoint(x: rect.maxX, y: rect.maxY), in: rect))
            XCTAssertEqual(corner.x, 1, accuracy: 0.000001)
            XCTAssertEqual(corner.y, 1, accuracy: 0.000001)
            XCTAssertNil(AndroidScreenPoint.normalize(CGPoint(x: rect.minX - 1, y: rect.midY), in: rect))
            XCTAssertNil(AndroidScreenPoint.normalize(CGPoint(x: rect.midX, y: rect.maxY + 1), in: rect))
        }
        XCTAssertEqual(AndroidRemoteLayout.videoRect(video: .zero, container: CGSize(width: 100, height: 100)), .zero)
    }

    func testFloatingPanelStaysReachableAfterDraggingAndRotation() {
        for container in [CGSize(width: 393, height: 780), CGSize(width: 760, height: 320)] {
            for expanded in [false, true] {
                let size = AndroidRemoteLayout.panelSize(expanded: expanded, container: container)
                for outside in [CGPoint(x: -1000, y: -1000), CGPoint(x: 2000, y: 2000)] {
                    let anchor = AndroidRemoteLayout.anchor(center: outside, size: size, container: container)
                    let center = AndroidRemoteLayout.panelCenter(anchor: anchor, size: size, container: container)
                    XCTAssertGreaterThanOrEqual(center.x - size.width / 2, 12)
                    XCTAssertLessThanOrEqual(center.x + size.width / 2, container.width - 12)
                    XCTAssertGreaterThanOrEqual(center.y - size.height / 2, 64)
                    XCTAssertLessThanOrEqual(center.y + size.height / 2, container.height - 12)
                }
            }
        }
    }

    func testLiveContinuousVideoDecodeRateWithoutInputs() async throws {
        guard ProcessInfo.processInfo.environment["HOMEAI_ANDROID_CONTINUOUS_CHECK"] == "1" else {
            throw XCTSkip("仅在宿主准备好公开动画页且未同时打开安卓全屏时启用")
        }
        let api = AppServices.api
        await api.restoreConnectionIfNeeded()
        let namespace = try await api.syncNamespace()
        let raw = try await api.request("GET", "/api/v1/android/workspace", expectedNamespace: namespace)
        let binding = try XCTUnwrap(JSONDecoder().decode(AndroidWorkspaceEnvelope.self, from: raw).workspace)
        guard binding.mode != "human" else { throw XCTSkip("只读帧率验收需先暂停人工控制") }
        let store = AndroidWorkspaceStore()
        await store.connect(api: api)
        defer { store.stop() }
        guard store.online else { throw APIClient.APIError.message(store.error ?? "屏幕连接未完成") }
        let firstFrameUntil = Date().addingTimeInterval(15)
        var initial: AndroidMediaStatistics?
        while Date() < firstFrameUntil {
            if let stats = await store.mediaStatisticsForAcceptance(), stats.videoFramesDecoded > 0 {
                initial = stats; break
            }
            guard store.online, store.error == nil else { throw APIClient.APIError.message(store.error ?? "屏幕连接中断") }
            try await Task.sleep(for: .milliseconds(100))
        }
        let first = try XCTUnwrap(initial, "必须先收到实际解码帧，不能用元数据代替视频")
        let started = ProcessInfo.processInfo.systemUptime
        for _ in 0..<8 {
            try await Task.sleep(for: .seconds(1))
            XCTAssertTrue(store.online)
            XCTAssertNil(store.error)
        }
        let latestStatistics = await store.mediaStatisticsForAcceptance()
        let last = try XCTUnwrap(latestStatistics)
        let elapsed = ProcessInfo.processInfo.systemUptime - started
        XCTAssertEqual(first.connectionID, last.connectionID, "采样期间不能重建屏幕连接")
        XCTAssertGreaterThanOrEqual(last.videoFramesDecoded, first.videoFramesDecoded)
        let frames = last.videoFramesDecoded >= first.videoFramesDecoded ? last.videoFramesDecoded - first.videoFramesDecoded : 0
        let fps = Double(frames) / elapsed
        XCTAssertGreaterThan(last.width, 0); XCTAssertGreaterThan(last.height, 0)
        XCTAssertGreaterThanOrEqual(fps, 15, "连续公开视频应至少达到15fps，目标30fps")
        if !store.audioSupported {
            XCTAssertFalse(store.audioAvailable)
            XCTAssertFalse(store.audioStatus.isEmpty, "不支持系统声音时必须有明确原因")
        }
        let audioPackets = last.audioPacketsReceived >= first.audioPacketsReceived ? last.audioPacketsReceived - first.audioPacketsReceived : 0
        print("ANDROID_CONTINUOUS_REAL decoded_frames=\(frames) elapsed_ms=\(Int(elapsed * 1000)) fps=\(String(format: "%.1f", fps)) width=\(last.width) height=\(last.height) audio_supported=\(store.audioSupported) audio_packets=\(audioPackets) inputs=0")
        // 音频包计数仅证明接收，不能当作实际听到了设备声音。
    }

    func testLivePairedAndroidVideoAndMetadataWithoutInputs() async throws {
        guard ProcessInfo.processInfo.environment["HOMEAI_ANDROID_LIVE_CHECK"] == "1" else {
            throw XCTSkip("仅在已授权的原配对手机上只读验证安卓画面，不发送控制或输入")
        }
        let api = AppServices.api
        await api.restoreConnectionIfNeeded()
        let namespace = try await api.syncNamespace()
        let data = try await api.request("GET", "/api/v1/android/workspace", expectedNamespace: namespace)
        let workspace = try XCTUnwrap(JSONDecoder().decode(AndroidWorkspaceEnvelope.self, from: data).workspace)
        XCTAssertFalse(workspace.id.isEmpty)
        struct Config: Decodable { let stun_urls: [String]; let transport_policy: String }
        let config = try JSONDecoder().decode(Config.self, from: await api.request("GET", "/api/v1/android/stream/config", expectedNamespace: namespace))
        XCTAssertEqual(config.transport_policy, "direct_only")
        let transport = try AndroidScreenTransport(stunURLs: config.stun_urls)
        let probe = AndroidFrameProbe()
        var remoteTrack: RTCVideoTrack?
        var metadata: AndroidScreenFrame?
        var blocked: String?
        var sessionID: String?
        transport.onTrack = { track in
            remoteTrack?.remove(probe)
            remoteTrack = track
            track.add(probe)
        }
        transport.onFrame = { metadata = $0 }
        transport.onBlocked = { blocked = $0 }
        do {
            let offer = try await transport.offer()
            let body = try JSONSerialization.data(withJSONObject: ["type": "offer", "sdp": offer])
            struct Answer: Decodable { let session_id: String; let type: String; let sdp: String }
            let answer = try JSONDecoder().decode(Answer.self, from: await api.request("POST", "/api/v1/android/stream/offer", body: body, expectedNamespace: namespace))
            sessionID = answer.session_id
            XCTAssertEqual(answer.type, "answer")
            try await transport.accept(answer.sdp)
            let deadline = Date().addingTimeInterval(20)
            while Date() < deadline && (probe.snapshot.count == 0 || metadata == nil) && blocked == nil {
                try await Task.sleep(for: .milliseconds(100))
            }
            XCTAssertNil(blocked, "屏幕采集被阻止")
            let frame = try XCTUnwrap(metadata, "未收到屏幕元数据")
            XCTAssertTrue(frame.isValid)
            let received = probe.snapshot
            XCTAssertGreaterThan(received.count, 0, "必须收到实际解码的视频帧，不能只凭元数据判定成功")
            XCTAssertEqual(received.width, frame.width)
            XCTAssertEqual(received.height, frame.height)
            print("ANDROID_IOS_LIVE decoded_frames=\(received.count) metadata=true inputs=0")
        } catch {
            remoteTrack?.remove(probe); transport.close()
            if let sessionID { _ = try? await api.request("POST", "/api/v1/android/stream/\(sessionID)/close", expectedNamespace: namespace) }
            throw error
        }
        remoteTrack?.remove(probe); transport.close()
        if let sessionID { _ = try await api.request("POST", "/api/v1/android/stream/\(sessionID)/close", expectedNamespace: namespace) }
    }

    func testLiveHumanControlCanOpenAndroidSettingsAndPause() async throws {
        guard ProcessInfo.processInfo.environment["HOMEAI_ANDROID_CONTROL_CHECK"] == "1" else {
            throw XCTSkip("仅在授权的安卓验收设备上接管并打开系统设置，不点击或输入文字")
        }
        let api = AppServices.api
        await api.restoreConnectionIfNeeded()
        let namespace = try await api.syncNamespace()
        let raw = try await api.request("GET", "/api/v1/android/workspace", expectedNamespace: namespace)
        let binding = try XCTUnwrap(JSONDecoder().decode(AndroidWorkspaceEnvelope.self, from: raw).workspace)
        guard binding.packages.contains("com.android.settings") else { throw XCTSkip("验收设备尚未登记系统设置应用") }
        let store = AndroidWorkspaceStore()
        await store.connect(api: api)
        guard store.online else { XCTFail(store.error ?? "屏幕直连未建立"); store.stop(); return }
        await store.setControl("human")
        let deadline = Date().addingTimeInterval(15)
        while !store.canControl && store.error == nil && Date() < deadline {
            try? await Task.sleep(for: .milliseconds(100))
        }
        if store.canControl {
            await store.input("launch", package: "com.android.settings")
            XCTAssertNil(store.error, "系统设置启动请求应正常完成")
            print("ANDROID_IOS_CONTROL launch_settings=\(store.error == nil) text=0 taps=0")
        } else {
            XCTFail(store.error ?? "接管后未获得可操作的新画面")
        }
        // 无论上述断言是否通过，都交还暂停状态，不能遗留人工控制或自动恢复 AI。
        await store.setControl("paused")
        XCTAssertEqual(store.workspace?.mode, "paused")
        store.stop()
    }

    func testOnlyCurrentDeviceCanControlHumanSession() throws {
        let raw = Data(#"{"workspace":{"id":"workspace","label":"测试设备","backend":"adb","status":"online","mode":"human","epoch":5,"controller_device_id":"phone-a","packages":["com.android.settings"]}}"#.utf8)
        let workspace = try XCTUnwrap(JSONDecoder().decode(AndroidWorkspaceEnvelope.self, from: raw).workspace)
        XCTAssertTrue(workspace.controlled(by: "phone-a"))
        XCTAssertFalse(workspace.controlled(by: "phone-b"))
        XCTAssertFalse(workspace.controlled(by: ""))
        XCTAssertEqual(workspace.modeLabel, "人工操作")
    }

    func testLateControlCleanupOnlyReleasesOriginalWorkspaceAndDevice() {
        func response(_ mode: String, device: String? = nil, id: String = "workspace", epoch: Int = 8) -> AndroidWorkspace {
            AndroidWorkspace(id: id, label: "公开测试设备", backend: "emulator", status: "online",
                mode: mode, epoch: epoch, controller_device_id: device, packages: [], last_sequence: nil)
        }
        func release(_ value: AndroidWorkspace?, requested: String = "human") -> Bool {
            AndroidControlLifecycle.shouldPauseLateResponse(value, requestedMode: requested,
                workspaceID: "workspace", deviceID: "original-phone")
        }
        XCTAssertTrue(release(response("human", device: "original-phone")))
        XCTAssertFalse(release(response("ai"), requested: "ai"), "明确交还 AI 后退出页面不能因响应晚到而暂停服务器任务")
        XCTAssertFalse(release(response("human", device: "new-phone")))
        XCTAssertFalse(release(response("human", device: "original-phone", id: "new-workspace")))
        XCTAssertFalse(release(response("human", device: "original-phone", epoch: 0)))
        XCTAssertFalse(release(response("paused"), requested: "paused"))
        XCTAssertFalse(release(response("ai")))
        XCTAssertFalse(release(response("human", device: "new-phone"), requested: "ai"))
        XCTAssertFalse(release(nil))
    }

    func testExitCleanupCannotOverrideNewControlIntentInSameWorkspace() {
        let lifecycle = AndroidControlLifecycle()
        let old = lifecycle.registerIntent(namespace: "paired-phone", workspaceID: "workspace")
        XCTAssertTrue(lifecycle.canRelease(namespace: "paired-phone", workspaceID: "workspace", revision: old))
        lifecycle.registerIntent(namespace: "another-phone", workspaceID: "workspace")
        XCTAssertTrue(lifecycle.canRelease(namespace: "paired-phone", workspaceID: "workspace", revision: old))
        lifecycle.registerIntent(namespace: "paired-phone", workspaceID: "another-workspace")
        XCTAssertTrue(lifecycle.canRelease(namespace: "paired-phone", workspaceID: "workspace", revision: old))
        lifecycle.registerIntent(namespace: "paired-phone", workspaceID: "workspace")
        XCTAssertFalse(lifecycle.canRelease(namespace: "paired-phone", workspaceID: "workspace", revision: old))
    }

    func testLateCleanupGateFinishesBeforeNewPageTakesControl() async throws {
        let lifecycle = AndroidControlLifecycle()
        let order = AndroidControlOrderProbe()
        try await lifecycle.gate.acquire()
        let takeover = Task {
            try await lifecycle.gate.withPermit { await order.append("new_page_control") }
        }
        for _ in 0..<100 {
            if await lifecycle.gate.waitingCount == 1 { break }
            try await Task.sleep(for: .milliseconds(5))
        }
        let waiting = await lifecycle.gate.waitingCount
        XCTAssertEqual(waiting, 1)
        await order.append("old_epoch_paused")
        await lifecycle.gate.release()
        try await takeover.value
        let result = await order.values
        XCTAssertEqual(result, ["old_epoch_paused", "new_page_control"])
    }

    func testInputFocusRejectsExpiredEmptyOrNonFiniteClaims() throws {
        let now = Date(timeIntervalSince1970: 1_800_000_000)
        let raw = Data(#"{"focus_token":"single-use-focus-token","expires_at":1800000010}"#.utf8)
        let focus = try JSONDecoder().decode(AndroidInputFocus.self, from: raw)
        XCTAssertTrue(focus.isValid(at: now))
        XCTAssertFalse(focus.isValid(at: now.addingTimeInterval(10)))
        XCTAssertFalse(AndroidInputFocus(focus_token: "", expires_at: now.timeIntervalSince1970 + 10).isValid(at: now))
        XCTAssertFalse(AndroidInputFocus(focus_token: String(repeating: "a", count: 257), expires_at: now.timeIntervalSince1970 + 10).isValid(at: now))
        XCTAssertFalse(AndroidInputFocus(focus_token: "single-use-focus-token", expires_at: .infinity).isValid(at: now))
    }

    func testCoordinatesRespectRotationAndRejectOutsideContent() {
        XCTAssertEqual(AndroidScreenPoint.normalize(CGPoint(x: 50, y: 100), in: CGSize(width: 100, height: 200)), .init(x: 0.5, y: 0.5))
        XCTAssertEqual(AndroidScreenPoint.normalize(CGPoint(x: 200, y: 50), in: CGSize(width: 200, height: 100)), .init(x: 1, y: 0.5))
        XCTAssertNil(AndroidScreenPoint.normalize(CGPoint(x: -1, y: 20), in: CGSize(width: 100, height: 200)))
        XCTAssertNil(AndroidScreenPoint.normalize(CGPoint(x: 101, y: 20), in: CGSize(width: 100, height: 200)))
        XCTAssertNil(AndroidScreenPoint.normalize(.zero, in: CGSize.zero))
    }

    func testFrameValidationRejectsUnknownModesAndInvalidDimensions() throws {
        let valid = AndroidScreenFrame(frame_id: "frame-1", width: 720, height: 1280, epoch: 1, mode: "human", blocked_reason: nil)
        XCTAssertTrue(valid.isValid)
        XCTAssertFalse(AndroidScreenFrame(frame_id: "frame-2", width: 0, height: 1280, epoch: 1, mode: "human", blocked_reason: nil).isValid)
        XCTAssertFalse(AndroidScreenFrame(frame_id: "frame-2", width: 720, height: 1280, epoch: 1, mode: "admin", blocked_reason: nil).isValid)
        XCTAssertTrue(AndroidScreenFrame(frame_id: "frame-3", width: 720, height: 1280, epoch: 2, mode: "human", blocked_reason: "当前画面禁止捕获").isValid)
    }

    private var screenOffer: String {
        """
        v=0
        a=group:BUNDLE 0 1
        m=video 9 UDP/TLS/RTP/SAVPF 102
        a=mid:0
        a=recvonly
        a=ice-ufrag:shared
        a=ice-pwd:shared-password
        a=rtpmap:102 H264/90000
        a=fingerprint:sha-256 12:34
        a=setup:actpass
        a=candidate:1 1 udp 2122260223 192.0.2.1 50000 typ host
        m=application 9 UDP/DTLS/SCTP webrtc-datachannel
        a=mid:1
        a=ice-ufrag:shared
        a=ice-pwd:shared-password
        a=fingerprint:sha-256 12:34
        a=setup:actpass
        a=candidate:1 1 udp 2122260223 192.0.2.1 50000 typ host
        """
    }

    func testScreenSDPCompletesEveryMediaSectionOnlyOnce() throws {
        let completed = try AndroidScreenTransport.completedGatheringSDP(screenOffer)
        let sections = completed.components(separatedBy: "m=").dropFirst()
        XCTAssertEqual(sections.count, 2)
        XCTAssertTrue(sections.allSatisfy { $0.components(separatedBy: "a=end-of-candidates").count == 2 })
        XCTAssertEqual(try AndroidScreenTransport.completedGatheringSDP(completed), completed)
        XCTAssertThrowsError(try AndroidScreenTransport.validateSDP(screenOffer + "\na=end-of-candidates"))
    }

    func testBundledCandidateReuseRequiresMatchingICEAndFingerprint() throws {
        let boundary = "m=application"
        let pieces = screenOffer.components(separatedBy: boundary)
        let candidateLess = pieces[1].components(separatedBy: "\n").filter { !$0.hasPrefix("a=candidate:") }.joined(separator: "\n")
        let bundled = pieces[0] + boundary + candidateLess
        let completed = try AndroidScreenTransport.completedGatheringSDP(bundled)
        XCTAssertEqual(completed.components(separatedBy: "a=candidate:").count - 1, 2)
        XCTAssertThrowsError(try AndroidScreenTransport.completedGatheringSDP(pieces[0] + boundary + candidateLess.replacingOccurrences(of: "a=ice-ufrag:shared", with: "a=ice-ufrag:other")))
        XCTAssertThrowsError(try AndroidScreenTransport.completedGatheringSDP(pieces[0] + boundary + candidateLess.replacingOccurrences(of: "12:34", with: "56:78")))
        XCTAssertThrowsError(try AndroidScreenTransport.completedGatheringSDP(bundled.replacingOccurrences(of: "a=group:BUNDLE 0 1", with: "a=group:BUNDLE 0")))
    }

    func testScreenSDPRejectsRelayTCPAudioAndWrongDirection() throws {
        let sdp = try AndroidScreenTransport.completedGatheringSDP(screenOffer)
        XCTAssertNoThrow(try AndroidScreenTransport.validateSDP(sdp))
        XCTAssertThrowsError(try AndroidScreenTransport.validateSDP(sdp.replacingOccurrences(of: "typ host", with: "typ relay")))
        XCTAssertThrowsError(try AndroidScreenTransport.validateSDP(sdp.replacingOccurrences(of: "1 udp", with: "1 tcp")))
        XCTAssertThrowsError(try AndroidScreenTransport.validateSDP(sdp + "\nm=audio 9 UDP/TLS/RTP/SAVPF 111"))
        XCTAssertThrowsError(try AndroidScreenTransport.validateSDP(sdp.replacingOccurrences(of: "H264/90000", with: "VP8/90000")))
        XCTAssertThrowsError(try AndroidScreenTransport.validateSDP(sdp.replacingOccurrences(of: "UDP/DTLS/SCTP", with: "DTLS/SCTP")))
        XCTAssertThrowsError(try AndroidScreenTransport.validateSDP(sdp.replacingOccurrences(of: "a=recvonly", with: "a=sendrecv")))
        XCTAssertNoThrow(try AndroidScreenTransport.validateSDP(sdp.replacingOccurrences(of: "a=recvonly", with: "a=sendonly"), expectedVideoDirection: "sendonly"))
    }

    func testEarlyScreenCandidatesRequireBothMediaAndMinimumCollectionWindow() {
        let both = screenOffer.replacingOccurrences(of: "typ host", with: "typ srflx")
        XCTAssertTrue(AndroidScreenTransport.canUseEarlyCandidateSnapshot(both, elapsed: 1, publicCandidateAge: 0.25))
        XCTAssertFalse(AndroidScreenTransport.canUseEarlyCandidateSnapshot(both, elapsed: 0.99, publicCandidateAge: 0.5))
        XCTAssertFalse(AndroidScreenTransport.canUseEarlyCandidateSnapshot(both, elapsed: 2, publicCandidateAge: 0.24))
        XCTAssertFalse(AndroidScreenTransport.canUseEarlyCandidateSnapshot(both, elapsed: 2, publicCandidateAge: nil))
        XCTAssertFalse(AndroidScreenTransport.canUseEarlyCandidateSnapshot(screenOffer, elapsed: 2, publicCandidateAge: 1))
        let halves = both.components(separatedBy: "m=application")
        let onlyVideo = halves[0] + "m=application" + halves[1].replacingOccurrences(of: "typ srflx", with: "typ host")
        XCTAssertFalse(AndroidScreenTransport.canUseEarlyCandidateSnapshot(onlyVideo, elapsed: 2, publicCandidateAge: 1))
        XCTAssertFalse(AndroidScreenTransport.canUseEarlyCandidateSnapshot(both.replacingOccurrences(of: "1 udp", with: "1 tcp"), elapsed: 2, publicCandidateAge: 1))
        XCTAssertFalse(AndroidScreenTransport.canUseEarlyCandidateSnapshot(both.replacingOccurrences(of: "typ srflx", with: "typ relay"), elapsed: 2, publicCandidateAge: 1))
    }

    func testScreenDiagnosticsExcludeAddressesCredentialsAndFingerprint() throws {
        let offer = try AndroidScreenTransport.completedGatheringSDP(screenOffer)
        let summary = try AndroidScreenTransport.diagnosticSummary(offer)
        XCTAssertTrue(summary.contains("video:profile=UDP/TLS/RTP/SAVPF,direction=recvonly,host=1"))
        XCTAssertTrue(summary.contains("data:profile=UDP/DTLS/SCTP"))
        for secret in ["192.0.2.1", "50000", "12:34", "shared-password", "a=ice", "a=candidate"] {
            XCTAssertFalse(summary.contains(secret))
        }
    }

    func testOptionalAndroidAudioOnlyNegotiatesReceiveDirection() throws {
        let audio = """

        m=audio 9 UDP/TLS/RTP/SAVPF 111
        a=mid:2
        a=recvonly
        a=ice-ufrag:shared
        a=ice-pwd:shared-password
        a=rtpmap:111 opus/48000/2
        a=fingerprint:sha-256 12:34
        a=setup:actpass
        a=candidate:1 1 udp 2122260223 192.0.2.1 50000 typ host
        """
        let offer = try AndroidScreenTransport.completedGatheringSDP(screenOffer + audio, allowAudio: true)
        XCTAssertNoThrow(try AndroidScreenTransport.validateSDP(offer, allowAudio: true))
        XCTAssertThrowsError(try AndroidScreenTransport.validateSDP(offer), "未请求声音的会话必须拒绝额外音频段")
        XCTAssertThrowsError(try AndroidScreenTransport.completedGatheringSDP(screenOffer + audio.replacingOccurrences(of: "a=recvonly", with: "a=sendrecv"), allowAudio: true))
        let answer = offer.replacingOccurrences(of: "a=recvonly", with: "a=sendonly")
        XCTAssertNoThrow(try AndroidScreenTransport.validateSDP(answer, expectedVideoDirection: "sendonly", allowAudio: true))
        let sections = answer.components(separatedBy: "m=audio")
        let inactive = sections[0] + "m=audio" + sections[1].replacingOccurrences(of: "a=sendonly", with: "a=inactive")
        XCTAssertNoThrow(try AndroidScreenTransport.validateSDP(inactive, expectedVideoDirection: "sendonly", allowAudio: true))
        let relay = sections[0] + "m=audio" + sections[1].replacingOccurrences(of: "a=sendonly", with: "a=inactive").replacingOccurrences(of: "typ host", with: "typ relay")
        XCTAssertThrowsError(try AndroidScreenTransport.validateSDP(relay, expectedVideoDirection: "sendonly", allowAudio: true))
    }

    func testUnavailableSystemAudioDoesNotInvalidateVideoMetadata() throws {
        let raw = Data(#"{"frame_id":"public-frame-token","width":640,"height":360,"epoch":2,"mode":"human","audio_available":false,"audio_reason":"此设备Android版本暂不支持系统声音（需Android11及以上）"}"#.utf8)
        let frame = try JSONDecoder().decode(AndroidScreenFrame.self, from: raw)
        XCTAssertTrue(frame.isValid)
        XCTAssertEqual(frame.audio_available, false)
        XCTAssertEqual(frame.audio_reason, "此设备Android版本暂不支持系统声音（需Android11及以上）")
    }

    func testRemoteAudioDeviceNeverInitializesRecording() {
        let output = AndroidPlaybackAudioDevice()
        XCTAssertFalse(output.isRecording)
        XCTAssertFalse(output.isRecordingInitialized)
        XCTAssertFalse(output.initializeRecording())
        XCTAssertFalse(output.startRecording())
        output.setPlaybackEnabled(true)
        XCTAssertFalse(output.isPlaying, "未收到播放请求时不应启动音频硬件")
        XCTAssertFalse(output.isRecording)
        output.shutdown()
    }

    func testDeviceDisplayStateUsesRequestWordingUntilVerified() throws {
        let raw = Data(#"{"display_control_supported":true,"device_screen_off":true,"display_verified":false,"display_reason":null,"display_revision":8}"#.utf8)
        let state = try JSONDecoder().decode(AndroidDisplayState.self, from: raw)
        XCTAssertTrue(state.allowsChange)
        XCTAssertEqual(state.status, "已请求关闭实体屏幕")
        let verified = AndroidDisplayState(display_control_supported: true, device_screen_off: true,
            display_verified: true, display_reason: nil, display_revision: 9)
        XCTAssertEqual(verified.status, "安卓实体屏幕已关闭")
        let on = AndroidDisplayState(display_control_supported: true, device_screen_off: false,
            display_verified: false, display_reason: nil, display_revision: 10)
        XCTAssertEqual(on.status, "实体屏幕跟随系统状态")
    }

    func testUnsupportedOrUnknownDeviceDisplayCannotBeChanged() {
        let unavailable = AndroidDisplayState(display_control_supported: false, device_screen_off: nil,
            display_verified: false, display_reason: "当前设备不支持控制实体面板", display_revision: 1)
        XCTAssertFalse(unavailable.allowsChange)
        XCTAssertEqual(unavailable.status, "当前设备不支持控制实体面板")
        let unknown = AndroidDisplayState(display_control_supported: true, device_screen_off: nil,
            display_verified: false, display_reason: "实体面板状态无法读取", display_revision: 2)
        XCTAssertFalse(unknown.allowsChange)
        XCTAssertEqual(unknown.status, "实体面板状态无法读取")
        let invalid = AndroidDisplayState(display_control_supported: true, device_screen_off: false,
            display_verified: true, display_reason: nil, display_revision: -1)
        XCTAssertFalse(invalid.allowsChange)
        XCTAssertFalse(invalid.supersedes(nil))
    }

    func testOlderDisplayMetadataCannotOverwriteAcknowledgedState() throws {
        let confirmed = AndroidDisplayState(display_control_supported: true, device_screen_off: true,
            display_verified: false, display_reason: nil, display_revision: 8)
        let raw = Data(#"{"frame_id":"public-frame-token","width":640,"height":360,"epoch":2,"mode":"human","display_control_supported":true,"device_screen_off":false,"display_verified":false,"display_revision":7}"#.utf8)
        let frame = try JSONDecoder().decode(AndroidScreenFrame.self, from: raw)
        let stale = try XCTUnwrap(frame.displayState)
        XCTAssertTrue(frame.isValid, "显示状态不应改变视频合法性")
        XCTAssertFalse(stale.supersedes(confirmed))
        XCTAssertTrue(confirmed.supersedes(stale))
        XCTAssertTrue(confirmed.supersedes(confirmed))
        let unknownFrame = AndroidScreenFrame(frame_id: "public-frame-token", width: 640, height: 360, epoch: 2, mode: "human", blocked_reason: nil)
        XCTAssertNil(unknownFrame.displayState, "旧服务没有状态字段时不能默认允许关屏")
    }

    func testScreenTransportRejectsTURNConfiguration() {
        XCTAssertThrowsError(try AndroidScreenTransport(stunURLs: ["turn:example.invalid:3478"]))
        XCTAssertThrowsError(try AndroidScreenTransport(stunURLs: ["stun:user@example.invalid:3478"]))
        XCTAssertThrowsError(try AndroidScreenTransport(stunURLs: ["stun:example.invalid:3478\n"]))
    }
}

/// 只统计解码帧数量及尺寸，不保存、输出或分析设备屏幕内容。
private final class AndroidFrameProbe: NSObject, RTCVideoRenderer, @unchecked Sendable {
    private let lock = NSLock()
    private var received = (count: 0, width: 0, height: 0)
    var snapshot: (count: Int, width: Int, height: Int) { lock.withLock { received } }
    func setSize(_ size: CGSize) {}
    func renderFrame(_ frame: RTCVideoFrame?) {
        guard let frame else { return }
        lock.withLock { received = (received.count + 1, Int(frame.width), Int(frame.height)) }
    }
}

private actor AndroidControlOrderProbe {
    private(set) var values: [String] = []
    func append(_ value: String) { values.append(value) }
}
