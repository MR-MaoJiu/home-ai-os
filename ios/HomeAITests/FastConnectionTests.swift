import XCTest
@testable import HomeAI

final class FastConnectionTests: XCTestCase {
    func testOnlySavedLocalHTTPSOriginsCanBeProbed() {
        XCTAssertEqual(FastConnection.localCandidates([
            "https://192.168.1.10:58443/", "https://192.168.1.10:58443",
            "http://10.0.0.1", "https://user:pass@10.0.0.1", "https://example.com",
            "https://localhost", "https://10.0.0.1/path", "https://172.16.0.1:443",
            "https://home.local", "https://10.0.0.1?token=value"
        ]), ["https://192.168.1.10:58443", "https://172.16.0.1:443", "https://home.local"])
    }
    @MainActor func testOutboxPersistsDeduplicatesAndSeparatesMembers() throws {
        let owner = "test-outbox-" + UUID().uuidString
        defer { try? DeviceIdentity.save(Data(), name: "chat.outbox:" + owner) }
        let queue = ChatOutbox()
        try queue.activate(owner: owner)
        let message = PendingChatSend(conversationID: "conversation", clientKey: "same-message", content: "公开测试", timezone: "Asia/Shanghai")
        try queue.enqueue(message); try queue.enqueue(message)
        XCTAssertEqual(queue.entries.count, 1)
        let restored = ChatOutbox(); try restored.activate(owner: owner)
        XCTAssertEqual(restored.entries.first?.message.content, "公开测试")
        try queue.activate(owner: owner + "-other")
        XCTAssertTrue(queue.entries.isEmpty)
        try restored.acknowledge(["same-message"])
        XCTAssertTrue(restored.entries.isEmpty)
    }
    @MainActor func testLiveAuthenticatedFastConnectionAndEncryptedRelay() async throws {
        guard ProcessInfo.processInfo.environment["HOMEAI_FAST_CONNECT_LIVE"] == "1" else { throw XCTSkip("需启用真实家庭连接验收") }
        let api = AppServices.api
        await api.restoreConnectionIfNeeded()
        let start = Date()
        _ = try await api.request("GET", "/api/v1/me")
        print("HOMEAI_FAST_READY_SECONDS=\(Date().timeIntervalSince(start))")
        let warm = Date()
        _ = try await api.request("GET", "/api/v1/me")
        print("HOMEAI_REUSED_REQUEST_SECONDS=\(Date().timeIntervalSince(warm))")
        let relay = try await api.verifyEncryptedRelayForAcceptance()
        print("HOMEAI_ENCRYPTED_RELAY_SECONDS=\(relay)")
        XCTAssertLessThan(relay, 20)
    }
}

final class RelayRecoveryTests: XCTestCase {
    private actor Replies {
        var outcomes: [Result<Data, Error>]
        var bodies: [Data] = []
        init(_ outcomes: [Result<Data, Error>]) { self.outcomes = outcomes }
        func call(_ body: Data = Data()) throws -> Data {
            bodies.append(body)
            return try outcomes.removeFirst().get()
        }
        func recordedBodies() -> [Data] { bodies }
    }

    func testBusySubmitRetriesOnlyTheSameEnvelope() async throws {
        let envelope = Data("相同密文及请求标识".utf8)
        let replies = Replies([.failure(RelayPlatformError(status: 429)), .failure(RelayPlatformError(status: 429)), .success(Data("accepted".utf8))])
        let result = try await RelayRecovery.submit(envelope, deadline: Date().addingTimeInterval(10), pause: { _ in }) {
            try await replies.call($0)
        }
        let bodies = await replies.recordedBodies()
        XCTAssertEqual(result, Data("accepted".utf8))
        XCTAssertEqual(bodies, [envelope, envelope, envelope])
    }

    func testUnknownSubmitFailureIsNeverRepeated() async {
        for failure: Error in [URLError(.timedOut), RelayPlatformError(status: 503), RelayPlatformError(status: 401)] {
            let replies = Replies([.failure(failure)])
            do {
                _ = try await RelayRecovery.submit(Data(), deadline: Date().addingTimeInterval(10), pause: { _ in }) { try await replies.call($0) }
                XCTFail("不明提交失败必须返回，不能再次提交")
            } catch { }
            let bodies = await replies.recordedBodies()
            XCTAssertEqual(bodies.count, 1)
        }
    }

    func testBusySubmitHasBoundedAttempts() async {
        let replies = Replies(Array(repeating: .failure(RelayPlatformError(status: 429)), count: 5))
        do {
            _ = try await RelayRecovery.submit(Data(), deadline: Date().addingTimeInterval(10), pause: { _ in }) { try await replies.call($0) }
            XCTFail("持续拥塞应结束重试")
        } catch { XCTAssertEqual((error as? RelayPlatformError)?.status, 429) }
        let bodies = await replies.recordedBodies()
        XCTAssertEqual(bodies.count, 5)
    }

    private actor Pauses {
        var values: [Double] = []
        func record(_ seconds: Double) { values.append(seconds) }
        func recorded() -> [Double] { values }
    }

    func testRetryAfterIsRespectedWithoutExtendingTheOriginalDeadline() async throws {
        for submitting in [true, false] {
            let pauses = Pauses()
            let replies = Replies([.failure(RelayPlatformError(status: 429, retryAfter: 5)), .success(Data())])
            if submitting {
                _ = try await RelayRecovery.submit(Data(), deadline: Date().addingTimeInterval(10), pause: { await pauses.record($0) }) { try await replies.call($0) }
            } else {
                _ = try await RelayRecovery.read(deadline: Date().addingTimeInterval(10), pause: { await pauses.record($0) }) { try await replies.call() }
            }
            let waited = await pauses.recorded()
            XCTAssertEqual(waited, [5])

            let limited = Replies([.failure(RelayPlatformError(status: 429, retryAfter: 5))])
            let forbiddenPause: @Sendable (Double) async throws -> Void = { _ in XCTFail("原期限不足时不能等待或延长请求") }
            do {
                if submitting {
                    _ = try await RelayRecovery.submit(Data(), deadline: Date().addingTimeInterval(1), pause: forbiddenPause) { try await limited.call($0) }
                } else {
                    _ = try await RelayRecovery.read(deadline: Date().addingTimeInterval(1), pause: forbiddenPause) { try await limited.call() }
                }
                XCTFail("超出原期限必须返回")
            } catch { XCTAssertEqual((error as? RelayPlatformError)?.status, 429) }
            let attempts = await limited.recordedBodies()
            XCTAssertEqual(attempts.count, 1)
        }
        for invalid in [Double.nan, Double.infinity, -1] {
            XCTAssertEqual(RelayRecovery.retryDelay(invalid, attempt: 0), 0.25)
        }
        XCTAssertEqual(RelayRecovery.retryDelay(nil, attempt: 20), 2)
    }

    func testAcceptedRequestReadsThroughTransientFailureWithoutResubmitting() async throws {
        let replies = Replies([.failure(URLError(.networkConnectionLost)), .failure(RelayPlatformError(status: 502)), .success(Data("result".utf8))])
        let result = try await RelayRecovery.read(deadline: Date().addingTimeInterval(10), pause: { _ in }) { try await replies.call() }
        let bodies = await replies.recordedBodies()
        XCTAssertEqual(result, Data("result".utf8))
        XCTAssertEqual(bodies.count, 3)
    }

    func testReadDoesNotRetryRevocationOrCancellation() async {
        for failure: Error in [RelayPlatformError(status: 403), URLError(.cancelled), CancellationError()] {
            let replies = Replies([.failure(failure)])
            do {
                _ = try await RelayRecovery.read(deadline: Date().addingTimeInterval(10), pause: { _ in }) { try await replies.call() }
                XCTFail("撤权或取消必须立即结束")
            } catch { }
            let bodies = await replies.recordedBodies()
            XCTAssertEqual(bodies.count, 1)
        }
        XCTAssertFalse(EncryptedRelay.shouldDiscardConnection(after: RelayPlatformError(status: 429)))
        XCTAssertFalse(EncryptedRelay.shouldDiscardConnection(after: CancellationError()))
        XCTAssertTrue(EncryptedRelay.shouldDiscardConnection(after: RelayPlatformError(status: 401)))
    }

    private actor HeldOperations {
        var active = 0
        var maximum = 0
        var entered = 0
        var open = false
        var held: [CheckedContinuation<Void, Never>] = []
        func run() async {
            active += 1; entered += 1; maximum = max(maximum, active)
            if !open { await withCheckedContinuation { held.append($0) } }
            active -= 1
        }
        func release() {
            open = true
            let values = held; held.removeAll()
            values.forEach { $0.resume() }
        }
        func counts() -> (Int, Int) { (entered, maximum) }
    }

    func testDeviceLimitWaiterCancellationAndPermitRecovery() async throws {
        let gate = RelayRequestGate(limit: 3), held = HeldOperations()
        let first = (0..<3).map { _ in Task { try await gate.withPermit(key: "device") { await held.run() } } }
        for _ in 0..<200 {
            if await held.counts().0 == 3 { break }
            try await Task.sleep(for: .milliseconds(5))
        }
        let canceled = Task { try await gate.withPermit(key: "device") { XCTFail("取消的等待者不能执行") } }
        for _ in 0..<200 {
            if await gate.waitingCount(key: "device") == 1 { break }
            try await Task.sleep(for: .milliseconds(5))
        }
        canceled.cancel()
        do { try await canceled.value; XCTFail("应收到取消") } catch is CancellationError { }
        let waiting = await gate.waitingCount(key: "device")
        XCTAssertEqual(waiting, 0)
        // 另一设备拥有独立额度，不被当前设备排队阻塞。
        let other = try await gate.withPermit(key: "other-device") { true }
        XCTAssertTrue(other)
        await held.release()
        for task in first { try await task.value }
        for _ in 0..<6 { try await gate.withPermit(key: "device") { await held.run() } }
        let counts = await held.counts()
        XCTAssertEqual(counts.0, 9)
        XCTAssertEqual(counts.1, 3)
    }
}
