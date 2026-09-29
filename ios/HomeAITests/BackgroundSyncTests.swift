import XCTest
@testable import HomeAI

final class BackgroundSyncTests: XCTestCase {
    func testCancelledWaiterDoesNotBlockTheNextOperation() async throws {
        let gate = AsyncOperationGate()
        try await gate.acquire()
        let waiting = Task { try await gate.acquire() }
        for _ in 0..<1000 {
            if await gate.waitingCount == 1 { break }
            await Task.yield()
        }
        let count = await gate.waitingCount
        XCTAssertEqual(count, 1)
        waiting.cancel()
        do { try await waiting.value; XCTFail("等待取消必须失败") }
        catch is CancellationError { }
        let remaining = await gate.waitingCount
        XCTAssertEqual(remaining, 0)
        await gate.release()
        let result = try await gate.withPermit { 42 }
        XCTAssertEqual(result, 42)
    }

    func testThrownOperationReleasesPermit() async throws {
        enum Expected: Error { case failure }
        let gate = AsyncOperationGate()
        do { let _: Int = try await gate.withPermit { throw Expected.failure }; XCTFail("应传播错误") }
        catch Expected.failure { }
        let result = try await gate.withPermit { "next" }
        XCTAssertEqual(result, "next")
    }

    func testBackgroundConfigurationIsPresent() throws {
        let identifiers = Bundle.main.object(forInfoDictionaryKey: "BGTaskSchedulerPermittedIdentifiers") as? [String]
        let modes = Bundle.main.object(forInfoDictionaryKey: "UIBackgroundModes") as? [String]
        XCTAssertTrue(identifiers?.contains("dev.homeai.sync.refresh") == true)
        XCTAssertTrue(modes?.contains("fetch") == true)
    }
}

import CryptoKit

private actor CacheRequestCounter {
    var value = 0
    func increment() { value += 1 }
}

final class ClientCacheTests: XCTestCase {
    func testBusinessDenialDoesNotInvalidateDeviceCredentials() {
        XCTAssertFalse(APIClient.invalidatesCredentials(status: 403))
        XCTAssertFalse(APIClient.invalidatesCredentials(status: 404))
        XCTAssertTrue(APIClient.invalidatesCredentials(status: 401))
    }

    func testViewSnapshotsAreEncryptedAndSeparatedByNamespace() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: directory) }
        let cache = ClientViewCache(directory: directory, key: SymmetricKey(data: Data(repeating: 9, count: 32)))
        let text = Data("private conversation text".utf8)
        try await cache.write(text, key: "chat", namespace: "first-member")
        let first = await cache.read(key: "chat", namespace: "first-member")
        let second = await cache.read(key: "chat", namespace: "second-member")
        XCTAssertEqual(first, text)
        XCTAssertNil(second)
        let folder = directory.appendingPathComponent(DeviceIdentity.hash(Data("first-member".utf8)))
        let file = try XCTUnwrap(FileManager.default.contentsOfDirectory(at: folder, includingPropertiesForKeys: nil).first)
        XCTAssertNil(try Data(contentsOf: file).range(of: text))
        try Data("corrupted".utf8).write(to: file)
        let damaged = await cache.read(key: "chat", namespace: "first-member")
        XCTAssertNil(damaged)
    }

    func testAuthorizationInvalidationPreventsLateCacheWrites() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: directory) }
        let cache = ClientViewCache(directory: directory, key: SymmetricKey(data: Data(repeating: 8, count: 32)))
        try await cache.write(Data("old".utf8), key: "memory", namespace: "member")
        await cache.invalidate(namespace: "member", revoked: true)
        do { try await cache.write(Data("late response".utf8), key: "memory", namespace: "member"); XCTFail("失权后不能把在途旧响应写回") }
        catch { }
        let value = await cache.read(key: "memory", namespace: "member")
        XCTAssertNil(value)
        await cache.authorize(namespace: "member")
        try await cache.write(Data("verified".utf8), key: "memory", namespace: "member")
        let restored = await cache.read(key: "memory", namespace: "member")
        XCTAssertEqual(restored, Data("verified".utf8))
    }

    func testConcurrentRequestsJoinOneOperation() async throws {
        let work = SharedClientOperation<Int>()
        let gate = AsyncOperationGate()
        let counter = CacheRequestCounter()
        try await gate.acquire()
        let jobs = (0..<8).map { _ in Task {
            try await work.run(key: "same-device") {
                await counter.increment()
                try await gate.acquire()
                await gate.release()
                return 42
            }
        } }
        for _ in 0..<10000 {
            if await work.waitingCount(key: "same-device") == jobs.count { break }
            await Task.yield()
        }
        let waiters = await work.waitingCount(key: "same-device")
        XCTAssertEqual(waiters, jobs.count)
        await gate.release()
        for job in jobs { let value = try await job.value; XCTAssertEqual(value, 42) }
        let count = await counter.value
        XCTAssertEqual(count, 1)
    }

    func testCancellingOnePageDoesNotCancelSharedRefresh() async throws {
        let work = SharedClientOperation<Int>()
        let gate = AsyncOperationGate()
        try await gate.acquire()
        let first = Task { try await work.run(key: "same") { try await gate.acquire(); await gate.release(); return 7 } }
        let second = Task { try await work.run(key: "same") { try await gate.acquire(); await gate.release(); return 7 } }
        for _ in 0..<10000 {
            if await work.waitingCount(key: "same") == 2 { break }
            await Task.yield()
        }
        first.cancel()
        do { _ = try await first.value; XCTFail("已取消的页面应该立即退出") } catch is CancellationError { }
        await gate.release()
        let remaining = try await second.value
        XCTAssertEqual(remaining, 7)
    }
}

extension ClientCacheTests {
    @MainActor
    func testPairedDeviceCacheReadTimingWithoutBusinessWrites() async throws {
        let env = ProcessInfo.processInfo.environment
        guard env["HOMEAI_READONLY_CACHE_CHECK"] == "1" || env["TEST_RUNNER_HOMEAI_READONLY_CACHE_CHECK"] == "1" else {
            throw XCTSkip("仅在已授权原配对真机上测量正常目录同步与本机缓存，不发送消息或上传资料")
        }
        let api = AppServices.api
        await api.restoreConnectionIfNeeded()
        let namespace = try await api.syncNamespace()
        _ = try await api.ownerIdentity(expectedNamespace: namespace)
        let sync = DeviceDataSync()
        _ = try await sync.synchronize(api: api)
        let cachedStarted = Date()
        let cached = await sync.cachedSnapshot(api: api)
        let cacheMilliseconds = Date().timeIntervalSince(cachedStarted) * 1000
        XCTAssertNotNil(cached)
        let identityStarted = Date()
        let owner = await api.cachedOwnerIdentity(expectedNamespace: namespace)
        let identityMilliseconds = Date().timeIntervalSince(identityStarted) * 1000
        XCTAssertNotNil(owner)
        let syncStarted = Date()
        let refreshed = try await sync.synchronize(api: api)
        let refreshMilliseconds = Date().timeIntervalSince(syncStarted) * 1000
        XCTAssertFalse(refreshed.offline)
        print("CLIENT_CACHE_REAL cache_ms=\(Int(cacheMilliseconds)) owner_ms=\(Int(identityMilliseconds)) delta_ms=\(Int(refreshMilliseconds)) records=\(refreshed.records.count)")
    }
}
