import XCTest
@testable import HomeAI

@MainActor
final class AndroidTouchTests: XCTestCase {
    private let frameID = "public-frame-token-0000000000001"
    private func point(_ id: Int, _ x: Double = 0.5, _ y: Double = 0.5) -> AndroidTouchPoint { .init(id: id, x: x, y: y) }
    private func ack(_ packet: AndroidTouchPacket, status: String = "accepted") -> AndroidTouchAck {
        .init(gesture_id: packet.gesture_id, sequence: packet.sequence, status: status, reason: nil)
    }

    func testLivePairedGestureStreamInPreparedPublicTestActivity() async throws {
        guard ProcessInfo.processInfo.environment["HOMEAI_ANDROID_GESTURE_CHECK"] == "1" else {
            throw XCTSkip("仅在宿主已打开并核对第一方 TouchTestActivity 后启用；不启动任何应用")
        }
        // 宿主先核对第一方验收页再开启环境开关；客户端不搜索、打开或读取其他应用。
        let api = AppServices.api
        await api.restoreConnectionIfNeeded()
        let namespace = try await api.syncNamespace()
        let raw = try await api.request("GET", "/api/v1/android/workspace", expectedNamespace: namespace)
        let binding = try XCTUnwrap(JSONDecoder().decode(AndroidWorkspaceEnvelope.self, from: raw).workspace,
            "当前成员没有绑定安卓验收设备")
        let store = AndroidWorkspaceStore()
        await store.connect(api: api)
        guard store.online, store.workspace?.id == binding.id else {
            store.stop(); throw APIClient.APIError.message("验收设备屏幕未完成连接")
        }
        func check() throws {
            guard store.online, store.error == nil else {
                throw APIClient.APIError.message(store.error ?? "手势验收连接中断")
            }
        }
        func delay(_ milliseconds: Int) async throws {
            try await Task.sleep(for: .milliseconds(milliseconds))
            try check()
        }
        func send(_ phase: AndroidTouchPhase, changed: Int?, points: [AndroidTouchPoint], source: UUID) throws {
            try check()
            guard points.allSatisfy({ (0.3...0.7).contains($0.x) && (0.6...0.8).contains($0.y) }),
                  store.handleTouch(.init(phase: phase, changedID: changed, points: points), sourceID: source) else {
                throw APIClient.APIError.message("公开验收区域的手势未被接收")
            }
        }
        do {
            await store.setControl("human")
            let readyUntil = Date().addingTimeInterval(15)
            while !store.canControl && store.error == nil && Date() < readyUntil {
                try await Task.sleep(for: .milliseconds(100))
            }
            guard store.canControl else { throw APIClient.APIError.message(store.error ?? "未获得人工触控权") }

            let drag = UUID()
            try send(.down, changed: 0, points: [point(0, 0.4, 0.65)], source: drag)
            try await delay(120)
            for index in 1...4 {
                let offset = Double(index) / 4
                try send(.move, changed: nil, points: [point(0, 0.4 + 0.2 * offset, 0.65 + 0.1 * offset)], source: drag)
                try await delay(80)
            }
            try send(.up, changed: 0, points: [point(0, 0.6, 0.75)], source: drag)
            try await delay(500)

            let multi = UUID()
            try send(.down, changed: 0, points: [point(0, 0.42, 0.7)], source: multi)
            try await delay(100)
            try send(.pointer_down, changed: 1, points: [point(0, 0.42, 0.7), point(1, 0.58, 0.7)], source: multi)
            try await delay(100)
            for index in 1...4 {
                let offset = Double(index) / 4
                try send(.move, changed: nil,
                    points: [point(0, max(0.3, 0.42 - 0.12 * offset), max(0.6, 0.7 - 0.1 * offset)),
                             point(1, min(0.7, 0.58 + 0.12 * offset), min(0.8, 0.7 + 0.1 * offset))], source: multi)
                try await delay(100)
            }
            try send(.pointer_up, changed: 1, points: [point(0, 0.3, 0.6), point(1, 0.7, 0.8)], source: multi)
            try await delay(100)
            try send(.up, changed: 0, points: [point(0, 0.3, 0.6)], source: multi)
            try await delay(500)

            for index in 0..<2 {
                let tap = UUID()
                try send(.down, changed: 0, points: [point(0, 0.5, 0.7)], source: tap)
                try await delay(60)
                try send(.up, changed: 0, points: [point(0, 0.5, 0.7)], source: tap)
                if index == 0 { try await delay(100) } // 固定间隔，不等待前次最终 ACK。
            }
            try await delay(500)

            let held = UUID()
            try send(.down, changed: 0, points: [point(0, 0.5, 0.7)], source: held)
            try await delay(800) // 生产计时器应发送 keepalive；实际保持时长由安卓验收页核对。
            try send(.up, changed: 0, points: [point(0, 0.5, 0.7)], source: held)
            try await delay(600)

            let canceled = UUID()
            try send(.down, changed: 0, points: [point(0, 0.5, 0.7)], source: canceled)
            try await delay(200)
            XCTAssertTrue(store.touchActive)
            XCTAssertNil(store.error)
            store.stop() // 实际退出路径必须发送 CANCEL，不能把它转换为 UP。
            XCTAssertFalse(store.touchActive)
            XCTAssertFalse(store.online)

            var paused = false
            let pauseUntil = Date().addingTimeInterval(8)
            while Date() < pauseUntil {
                let state = try JSONDecoder().decode(AndroidWorkspaceEnvelope.self,
                    from: await api.request("GET", "/api/v1/android/workspace", expectedNamespace: namespace))
                if state.workspace?.mode == "paused" { paused = true; break }
                try await Task.sleep(for: .milliseconds(200))
            }
            XCTAssertTrue(paused, "退出后应释放人工操作权")
            print("ANDROID_IOS_GESTURE drag=true two_finger=true double_tap=true hold_800ms=true stopped_cancel=true paused=\(paused) launch=0 text=0")
        } catch {
            store.cancelTouches()
            await store.setControl("paused")
            store.stop()
            throw error
        }
    }

    func testTouchReadinessRequiresExplicitSupportedHandshake() throws {
        let decoder = JSONDecoder()
        let ready = try decoder.decode(AndroidTouchHandshake.self, from: Data(#"{"status":"ready","protocol":1,"channel_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","heartbeat_required":true}"#.utf8))
        XCTAssertTrue(ready.isReady)
        for value in [#"{"status":"ready"}"#, #"{"status":"ready","protocol":1}"#, #"{"status":"ready","protocol":2}"#,
                      #"{"status":"unavailable","protocol":1,"reason":"触控桥未就绪"}"#,
                      #"{"status":"accepted","protocol":1}"#] {
            XCTAssertFalse(try decoder.decode(AndroidTouchHandshake.self, from: Data(value.utf8)).isReady)
        }
        XCTAssertThrowsError(try decoder.decode(AndroidTouchAck.self, from: Data(#"{"status":"ready","protocol":1}"#.utf8)),
            "握手不能当作手势确认")
    }

    func testHeartbeatUsesFixedSchemaAndLowercaseHexNonce() throws {
        let channel = String(repeating: "a", count: 32), nonce = String(repeating: "1", count: 32)
        let valid: [String: Any] = ["status": "ping", "channel_id": channel, "nonce": nonce]
        let message = try XCTUnwrap(AndroidTouchHeartbeatMessage.decode(JSONSerialization.data(withJSONObject: valid)))
        let reply = try XCTUnwrap(JSONSerialization.jsonObject(with: message.reply()) as? [String: String])
        XCTAssertEqual(reply, ["type": "heartbeat", "channel_id": channel, "nonce": nonce])
        var extra = valid; extra["action"] = "down"
        XCTAssertNil(AndroidTouchHeartbeatMessage.decode(try JSONSerialization.data(withJSONObject: extra)))
        for value in [String(repeating: "A", count: 32), String(repeating: "g", count: 32), "short", nonce + "\n"] {
            var invalid = valid; invalid["nonce"] = value
            XCTAssertNil(AndroidTouchHeartbeatMessage.decode(try JSONSerialization.data(withJSONObject: invalid)))
        }
        var wrongType = valid; wrongType["nonce"] = true
        XCTAssertNil(AndroidTouchHeartbeatMessage.decode(try JSONSerialization.data(withJSONObject: wrongType)))
    }

    func testTouchLivenessRequiresAliveForAnActuallyRepliedNonce() {
        let channel = String(repeating: "a", count: 32), nonce = String(repeating: "1", count: 32)
        let ping = AndroidTouchHeartbeatMessage(status: "ping", channelID: channel, nonce: nonce)
        let alive = AndroidTouchHeartbeatMessage(status: "alive", channelID: channel, nonce: nonce)
        var liveness = AndroidTouchLiveness()
        liveness.begin(channelID: channel, at: 0)
        XCTAssertFalse(liveness.isReady(at: 0))
        XCTAssertFalse(liveness.accept(alive, at: 0.05), "没有回过的nonce不能开放触控")
        XCTAssertTrue(liveness.canReply(to: ping))
        liveness.didReply(to: ping, at: 0.1)
        XCTAssertFalse(liveness.isReady(at: 0.15), "sendData成功仍须等待服务端alive")
        XCTAssertTrue(liveness.accept(alive, at: 0.2))
        XCTAssertTrue(liveness.isReady(at: 0.3))
        XCTAssertFalse(liveness.accept(alive, at: 0.4), "旧alive不能重复延长存活期限")
        XCTAssertFalse(liveness.canReply(to: ping), "同一nonce不重复应答")
        XCTAssertTrue(liveness.isExpired(at: 1.71))
        XCTAssertFalse(liveness.isReady(at: 1.71))
    }

    func testTouchLivenessRejectsExpiredProofAndPreviousChannelAfterReset() {
        let old = String(repeating: "a", count: 32), current = String(repeating: "b", count: 32)
        let nonce = String(repeating: "2", count: 32)
        var liveness = AndroidTouchLiveness()
        liveness.begin(channelID: old, at: 0)
        liveness.didReply(to: .init(status: "ping", channelID: old, nonce: nonce), at: 0)
        XCTAssertFalse(liveness.accept(.init(status: "alive", channelID: old, nonce: nonce), at: 1.6))
        liveness.invalidate()
        XCTAssertFalse(liveness.canReply(to: .init(status: "ping", channelID: old, nonce: nonce)))
        liveness.begin(channelID: current, at: 2)
        XCTAssertFalse(liveness.canReply(to: .init(status: "ping", channelID: old, nonce: nonce)))
        XCTAssertFalse(liveness.accept(.init(status: "alive", channelID: old, nonce: nonce), at: 2.1))
        XCTAssertFalse(liveness.accept(.init(status: "alive", channelID: current, nonce: nonce), at: 2.1))
        liveness.didReply(to: .init(status: "ping", channelID: current, nonce: nonce), at: 2.2)
        XCTAssertTrue(liveness.accept(.init(status: "alive", channelID: current, nonce: nonce), at: 2.3))
        XCTAssertTrue(liveness.isReady(at: 2.4))
    }

    func testTouchWireBindsEachPacketToItsOwnChannelGeneration() throws {
        let packet = AndroidTouchPacket(gesture_id: UUID().uuidString.lowercased(), epoch: 1, sequence: 1,
            frame_id: frameID, phase: .down, changed_id: 0, points: [point(0)])
        let oldID = String(repeating: "a", count: 32), newID = String(repeating: "b", count: 32)
        let oldBytes = try AndroidTouchWire.encode(packet, channelID: oldID)
        let newBytes = try AndroidTouchWire.encode(packet, channelID: newID)
        let old = try XCTUnwrap(JSONSerialization.jsonObject(with: oldBytes) as? [String: Any])
        let current = try XCTUnwrap(JSONSerialization.jsonObject(with: newBytes) as? [String: Any])
        let business = try XCTUnwrap(JSONSerialization.jsonObject(with: JSONEncoder().encode(packet)) as? [String: Any])
        XCTAssertEqual(old["channel_id"] as? String, oldID, "旧缓冲包保留旧代次，不能伪装成重连后的新输入")
        XCTAssertEqual(current["channel_id"] as? String, newID)
        XCTAssertNil(business["channel_id"], "手势队列不保存传输握手凭据")
        XCTAssertThrowsError(try AndroidTouchWire.encode(packet, channelID: nil))
        XCTAssertThrowsError(try AndroidTouchWire.encode(packet, channelID: "short"))
        XCTAssertFalse(AndroidTouchWire.validChannelID(String(repeating: "a", count: 31) + "\n"))
    }

    func testDownAndMovesDoNotWaitForAckAndMovesStillCoalesce() {
        var time = 0.0, packets: [AndroidTouchPacket] = [], errors: [String] = []
        let pipeline = AndroidTouchPipeline(now: { time }, send: { packets.append($0); return true }, failure: { errors.append($0) })
        XCTAssertTrue(pipeline.enqueue(.init(phase: .down, changedID: 0, points: [point(0)]), epoch: 2, frameID: frameID))
        XCTAssertEqual(packets.map(\.phase), [.down])
        for position in 1...20 {
            _ = pipeline.enqueue(.init(phase: .move, changedID: nil, points: [point(0, Double(position) / 20)]), epoch: 2, frameID: "a-new-normal-frame")
        }
        XCTAssertEqual(packets.map(\.phase), [.down, .move], "第一条移动不等待 DOWN 确认")
        XCTAssertEqual(pipeline.pendingCount, 1)
        time = 0.1; pipeline.tick()
        XCTAssertEqual(packets.map(\.phase), [.down, .move, .move])
        XCTAssertEqual(packets.last?.points.first?.x, 1)
        XCTAssertEqual(packets.last?.frame_id, frameID, "手势固定起始画面，不随视频帧刷新而变化")
        pipeline.receive(ack(packets[0]))
        XCTAssertEqual(packets.count, 3)
        XCTAssertTrue(errors.isEmpty)
    }

    func testDelayedFirstAckDoesNotCompressUpAndSecondDownTiming() {
        var time = 0.0, packets: [AndroidTouchPacket] = [], sentTimes: [Double] = [], errors: [String] = []
        let pipeline = AndroidTouchPipeline(now: { time }, send: { packets.append($0); sentTimes.append(time); return true }, failure: { errors.append($0) })
        _ = pipeline.enqueue(.init(phase: .down, changedID: 0, points: [point(0)]), epoch: 1, frameID: frameID)
        time = 0.06
        _ = pipeline.enqueue(.init(phase: .up, changedID: 0, points: [point(0)]), epoch: 1, frameID: frameID)
        time = 0.16
        _ = pipeline.enqueue(.init(phase: .down, changedID: 0, points: [point(0)]), epoch: 1, frameID: frameID)
        time = 0.22
        _ = pipeline.enqueue(.init(phase: .up, changedID: 0, points: [point(0)]), epoch: 1, frameID: frameID)
        XCTAssertEqual(packets.map(\.phase), [.down, .up, .down, .up])
        XCTAssertEqual(sentTimes, [0, 0.06, 0.16, 0.22])
        XCTAssertEqual(sentTimes[2] - sentTimes[1], 0.1, accuracy: 0.000001)
        time = 0.3
        for packet in packets { pipeline.receive(ack(packet)) }
        XCTAssertEqual(packets.count, 4, "迟到 ACK 只确认已发送事件，不能补发或重放")
        XCTAssertTrue(errors.isEmpty)
        XCTAssertFalse(pipeline.hasPendingWork)
    }

    func testMoveThrottlePreservesImmediatePointerAndUpBoundaries() {
        var time = 0.0, packets: [AndroidTouchPacket] = []
        let pipeline = AndroidTouchPipeline(now: { time }, send: { packets.append($0); return true }, failure: { XCTFail($0) })
        _ = pipeline.enqueue(.init(phase: .down, changedID: 0, points: [point(0)]), epoch: 1, frameID: frameID)
        pipeline.receive(ack(packets[0]))
        _ = pipeline.enqueue(.init(phase: .move, changedID: nil, points: [point(0, 0.6)]), epoch: 1, frameID: frameID)
        time = 0.02
        _ = pipeline.enqueue(.init(phase: .move, changedID: nil, points: [point(0, 0.7)]), epoch: 1, frameID: frameID)
        XCTAssertEqual(packets.count, 2)
        _ = pipeline.enqueue(.init(phase: .up, changedID: 0, points: [point(0, 0.8)]), epoch: 1, frameID: frameID)
        XCTAssertEqual(packets.map(\.phase), [.down, .move, .up])
        XCTAssertEqual(packets.last?.points, [point(0, 0.8)])
        XCTAssertEqual(packets.map(\.sequence), [1, 2, 3])
        XCTAssertFalse(pipeline.isActive)
    }

    func testPinchRotationKeepsPointerIDsAndIncludesLiftedFinger() {
        var packets: [AndroidTouchPacket] = []
        let pipeline = AndroidTouchPipeline(send: { packets.append($0); return true }, failure: { XCTFail($0) })
        _ = pipeline.enqueue(.init(phase: .down, changedID: 0, points: [point(0, 0.2, 0.3)]), epoch: 4, frameID: frameID)
        pipeline.receive(ack(packets[0]))
        _ = pipeline.enqueue(.init(phase: .pointer_down, changedID: 1, points: [point(0, 0.2, 0.3), point(1, 0.8, 0.7)]), epoch: 4, frameID: frameID)
        _ = pipeline.enqueue(.init(phase: .move, changedID: nil, points: [point(0, 0.3, 0.8), point(1, 0.7, 0.2)]), epoch: 4, frameID: frameID)
        _ = pipeline.enqueue(.init(phase: .pointer_up, changedID: 0, points: [point(0, 0.3, 0.8), point(1, 0.7, 0.2)]), epoch: 4, frameID: frameID)
        _ = pipeline.enqueue(.init(phase: .up, changedID: 1, points: [point(1, 0.7, 0.2)]), epoch: 4, frameID: frameID)
        XCTAssertEqual(packets.map(\.phase), [.down, .pointer_down, .move, .pointer_up, .up])
        XCTAssertEqual(packets[3].changed_id, 0)
        XCTAssertEqual(packets[3].points.map(\.id), [0, 1])
        XCTAssertEqual(packets[4].points.map(\.id), [1])
    }

    func testLongPressKeepsRealDurationAndUsesEmptyKeepalivePoints() {
        var time = 0.0, packets: [AndroidTouchPacket] = []
        let pipeline = AndroidTouchPipeline(now: { time }, send: { packets.append($0); return true }, failure: { XCTFail($0) })
        _ = pipeline.enqueue(.init(phase: .down, changedID: 0, points: [point(0)]), epoch: 3, frameID: frameID)
        pipeline.receive(ack(packets[0]))
        time = 0.49; pipeline.tick()
        XCTAssertEqual(packets.count, 1)
        for seconds in [0.5, 1.0, 1.5, 2.0] {
            time = seconds; pipeline.tick()
            XCTAssertEqual(packets.last?.phase, .keepalive)
            XCTAssertEqual(packets.last?.points, [])
            pipeline.receive(ack(packets.last!))
        }
        XCTAssertFalse(packets.contains { $0.phase == .up })
        _ = pipeline.enqueue(.init(phase: .up, changedID: 0, points: [point(0)]), epoch: 3, frameID: frameID)
        XCTAssertEqual(packets.last?.phase, .up)
        XCTAssertEqual(packets.last?.sequence, 6)
    }

    func testDoubleTapSendsTwoGesturesInOrderWithoutWaitingForFinalAck() {
        var packets: [AndroidTouchPacket] = []
        let pipeline = AndroidTouchPipeline(send: { packets.append($0); return true }, failure: { XCTFail($0) })
        for _ in 0..<2 {
            _ = pipeline.enqueue(.init(phase: .down, changedID: 0, points: [point(0)]), epoch: 1, frameID: frameID)
            let down = packets.last!
            pipeline.receive(ack(down))
            _ = pipeline.enqueue(.init(phase: .up, changedID: 0, points: [point(0)]), epoch: 1, frameID: frameID)
        }
        XCTAssertEqual(packets.map(\.phase), [.down, .up, .down, .up])
        XCTAssertNotEqual(packets[0].gesture_id, packets[2].gesture_id)
        XCTAssertEqual(packets.map(\.sequence), [1, 2, 1, 2])
    }

    func testCancelDropsQueuedMovesAndDoesNotReplayAfterLateAck() {
        var packets: [AndroidTouchPacket] = []
        let pipeline = AndroidTouchPipeline(now: { 0 }, send: { packets.append($0); return true }, failure: { XCTFail($0) })
        _ = pipeline.enqueue(.init(phase: .down, changedID: 0, points: [point(0)]), epoch: 1, frameID: frameID)
        let down = packets[0]
        _ = pipeline.enqueue(.init(phase: .move, changedID: nil, points: [point(0, 0.6)]), epoch: 1, frameID: frameID)
        _ = pipeline.enqueue(.init(phase: .move, changedID: nil, points: [point(0, 0.9)]), epoch: 1, frameID: frameID)
        XCTAssertEqual(pipeline.pendingCount, 1)
        pipeline.cancel()
        XCTAssertEqual(packets.map(\.phase), [.down, .move, .cancel])
        XCTAssertEqual(packets.last?.points, [])
        pipeline.receive(ack(down)); pipeline.tick()
        XCTAssertEqual(packets.count, 3)
        XCTAssertFalse(packets.contains { $0.phase == .move && $0.points.first?.x == 0.9 })
        XCTAssertFalse(pipeline.hasPendingWork)
    }

    func testServerCancellationAtLastAcceptedSequenceIsNotIgnored() {
        for status in ["canceled", "rejected"] {
            var packets: [AndroidTouchPacket] = [], errors: [String] = []
            let pipeline = AndroidTouchPipeline(send: { packets.append($0); return true }, failure: { errors.append($0) })
            _ = pipeline.enqueue(.init(phase: .down, changedID: 0, points: [point(0)]), epoch: 1, frameID: frameID)
            let down = packets[0]
            pipeline.receive(ack(down))
            pipeline.receive(ack(down)) // 重复 accepted 仍应忽略。
            XCTAssertTrue(pipeline.isActive)
            XCTAssertTrue(errors.isEmpty)
            pipeline.receive(ack(down, status: status))
            XCTAssertFalse(pipeline.isActive)
            XCTAssertEqual(errors.count, 1)
            XCTAssertEqual(packets.map(\.phase), [.down, .cancel])
            pipeline.receive(ack(down, status: status))
            XCTAssertEqual(packets.count, 2, "重复取消不能重复发送")
        }
    }

    func testCompletedOldGestureCancellationDoesNotAffectNewGesture() {
        var packets: [AndroidTouchPacket] = [], errors: [String] = []
        let pipeline = AndroidTouchPipeline(send: { packets.append($0); return true }, failure: { errors.append($0) })
        _ = pipeline.enqueue(.init(phase: .down, changedID: 0, points: [point(0)]), epoch: 1, frameID: frameID)
        pipeline.receive(ack(packets[0]))
        _ = pipeline.enqueue(.init(phase: .up, changedID: 0, points: [point(0)]), epoch: 1, frameID: frameID)
        let oldUp = packets.last!
        pipeline.receive(ack(oldUp))
        _ = pipeline.enqueue(.init(phase: .down, changedID: 0, points: [point(0)]), epoch: 1, frameID: frameID)
        let newDown = packets.last!
        pipeline.receive(ack(oldUp, status: "canceled"))
        XCTAssertTrue(pipeline.isActive)
        XCTAssertTrue(errors.isEmpty)
        XCTAssertEqual(packets.count, 3)
        XCTAssertNotEqual(oldUp.gesture_id, newDown.gesture_id)
        pipeline.receive(ack(newDown))
        XCTAssertTrue(pipeline.isActive)
    }

    func testInitialAckTimeoutCancelsWithoutResendingDown() {
        var time = 0.0, packets: [AndroidTouchPacket] = [], errors: [String] = []
        let pipeline = AndroidTouchPipeline(now: { time }, send: { packets.append($0); return true }, failure: { errors.append($0) })
        _ = pipeline.enqueue(.init(phase: .down, changedID: 0, points: [point(0)]), epoch: 1, frameID: frameID)
        time = 2.9; pipeline.tick()
        XCTAssertTrue(errors.isEmpty)
        time = 3.01; pipeline.tick()
        XCTAssertEqual(errors.count, 1)
        XCTAssertEqual(packets.map(\.phase), [.down, .keepalive, .cancel])
        pipeline.tick()
        XCTAssertEqual(packets.count, 3)
    }

    func testSixthPointerAndEpochChangeCancelInsteadOfGuessing() {
        var packets: [AndroidTouchPacket] = [], errors: [String] = []
        let pipeline = AndroidTouchPipeline(send: { packets.append($0); return true }, failure: { errors.append($0) })
        _ = pipeline.enqueue(.init(phase: .down, changedID: 0, points: [point(0)]), epoch: 1, frameID: frameID)
        pipeline.receive(ack(packets[0]))
        XCTAssertFalse(pipeline.enqueue(.init(phase: .pointer_down, changedID: 5, points: (0...5).map { point($0) }), epoch: 1, frameID: frameID))
        XCTAssertEqual(packets.last?.phase, .cancel)
        XCTAssertEqual(errors.count, 1)
        XCTAssertFalse(AndroidTouchPoint(id: 0, x: .nan, y: 0).isValid)
    }

    func testEpochChangeAndRejectedAckCancelCurrentGesture() {
        var packets: [AndroidTouchPacket] = [], errors: [String] = []
        let pipeline = AndroidTouchPipeline(send: { packets.append($0); return true }, failure: { errors.append($0) })
        _ = pipeline.enqueue(.init(phase: .down, changedID: 0, points: [point(0)]), epoch: 1, frameID: frameID)
        pipeline.receive(ack(packets[0], status: "rejected"))
        XCTAssertEqual(packets.last?.phase, .cancel)
        XCTAssertEqual(errors.count, 1)
        XCTAssertFalse(pipeline.enqueue(.init(phase: .move, changedID: nil, points: [point(0)]), epoch: 2, frameID: frameID))
    }
    func testChangingEpochCancelsExistingPointersWithoutNewDown() {
        var packets: [AndroidTouchPacket] = [], errors: [String] = []
        let pipeline = AndroidTouchPipeline(send: { packets.append($0); return true }, failure: { errors.append($0) })
        _ = pipeline.enqueue(.init(phase: .down, changedID: 0, points: [point(0)]), epoch: 1, frameID: frameID)
        pipeline.receive(ack(packets[0]))
        XCTAssertFalse(pipeline.enqueue(.init(phase: .move, changedID: nil, points: [point(0)]), epoch: 2, frameID: frameID))
        XCTAssertEqual(packets.map(\.phase), [.down, .cancel])
        XCTAssertEqual(packets.last?.epoch, 1)
        XCTAssertEqual(errors.count, 1)
    }

}
