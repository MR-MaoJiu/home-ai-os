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
