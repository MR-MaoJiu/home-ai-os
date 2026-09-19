import XCTest
import AppIntents
@testable import HomeAI

final class IntentTests: XCTestCase {
    func testStandardTOTPVector() throws {
        let secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
        XCTAssertEqual(try AdminTOTP.code(secret, at: Date(timeIntervalSince1970: 59)), "287082")
        XCTAssertEqual(try AdminTOTP.code(secret, at: Date(timeIntervalSince1970: 1111111109)), "081804")
        XCTAssertThrowsError(try AdminTOTP.secret("not-a-valid-secret"))
    }

    @MainActor
    func testOpenIntentUsesSharedNavigation() async throws {
        IntentRouter.shared.destination = .ai
        _ = try await OpenHomeActivityIntent().perform()
        XCTAssertEqual(IntentRouter.shared.destination, .ai)
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
