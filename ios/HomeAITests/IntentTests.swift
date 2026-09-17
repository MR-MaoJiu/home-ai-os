import XCTest
import AppIntents
@testable import HomeAI

final class IntentTests: XCTestCase {
    @MainActor
    func testOpenIntentUsesSharedNavigation() async throws {
        IntentRouter.shared.destination = .ai
        _ = try await OpenHomeActivityIntent().perform()
        XCTAssertEqual(IntentRouter.shared.destination, .activity)
        XCTAssertEqual(CreateHomeReminderIntent.authenticationPolicy, .requiresLocalDeviceAuthentication)
        XCTAssertTrue(CreateHomeReminderIntent.openAppWhenRun)
        XCTAssertTrue(Bundle.main.localizations.contains("zh-Hans"))
    }

    func testEmptyReminderIsRejectedBeforeNetwork() async throws {
        do {
            _ = try await ReminderIntentService(storageKey: "intent-invalid-test").prepare(title: " \n ", namespace: "test")
            XCTFail("空内容不能提交")
        } catch APIClient.APIError.message { }
    }
}
