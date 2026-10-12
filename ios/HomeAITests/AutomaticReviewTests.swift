import XCTest
@testable import HomeAI

@MainActor
final class AutomaticReviewTests: XCTestCase {
    private func configuration(_ enabled: Bool, scope: String = "current_device_tasks") throws -> Data {
        try JSONSerialization.data(withJSONObject: ["enabled": enabled, "device_id": "test-device", "scope": scope])
    }

    func testLivePairedDeviceDefaultIsDisabled() async throws {
        guard ProcessInfo.processInfo.environment["TEST_RUNNER_HOMEAI_AUTO_REVIEW_LIVE"] == "1" ||
              ProcessInfo.processInfo.environment["HOMEAI_AUTO_REVIEW_LIVE"] == "1" else {
            throw XCTSkip("仅显式运行已配对设备的只读自动审核状态检查")
        }
        let api = AppServices.api
        await api.restoreConnectionIfNeeded()
        let namespace = try await api.syncNamespace()
        let store = AutomaticReviewStore()
        await store.load(namespace: namespace) { method, body in
            XCTAssertEqual(method, "GET"); XCTAssertNil(body)
            return try await api.request(method, "/api/v1/devices/current/auto-review", expectedNamespace: namespace)
        }
        XCTAssertTrue(store.verified, store.message ?? "未验证")
        XCTAssertFalse(store.enabled, "本次部署不应替真实成员开启自动审核")
        print("AUTO_REVIEW_LIVE signed_device=true default_disabled=\(!store.enabled) read_only=true")
    }

    func testInitialStateIsOffAndConfirmationIsRequiredBeforeRequest() async {
        let store = AutomaticReviewStore()
        XCTAssertFalse(store.enabled)
        XCTAssertFalse(store.verified)
        await store.change(enabled: true, acknowledged: false, namespace: "test") { _, _ in
            XCTFail("未确认时不应发送开启请求"); return Data()
        }
        XCTAssertFalse(store.enabled)
        XCTAssertFalse(store.permitsCapture(namespace: "test"))
    }

    func testServerReceiptControlsEnableDisableAndNamespaceIsolation() async throws {
        let store = AutomaticReviewStore()
        await store.load(namespace: "test") { method, body in
            XCTAssertEqual(method, "GET"); XCTAssertNil(body)
            return try self.configuration(false)
        }
        await store.change(enabled: true, acknowledged: true, namespace: "test") { method, body in
            XCTAssertEqual(method, "PUT")
            XCTAssertFalse(store.enabled)
            XCTAssertFalse(store.permitsCapture(namespace: "test"))
            let json = try XCTUnwrap(try JSONSerialization.jsonObject(with: XCTUnwrap(body)) as? [String: Bool])
            XCTAssertEqual(json, ["enabled": true, "acknowledged": true])
            return try self.configuration(true)
        }
        XCTAssertTrue(store.permitsCapture(namespace: "test"))
        XCTAssertFalse(store.permitsCapture(namespace: "other-member"))
        await store.change(enabled: false, acknowledged: false, namespace: "test") { _, _ in
            XCTAssertTrue(store.enabled, "保存中仍显示已开启警告")
            XCTAssertFalse(store.permitsCapture(namespace: "test"))
            return try self.configuration(false)
        }
        XCTAssertFalse(store.enabled)
        XCTAssertTrue(store.verified)
    }

    func testFailedDisableNeverClaimsServerIsDisabledAndStopsLocalCapture() async throws {
        let store = AutomaticReviewStore()
        await store.load(namespace: "test") { _, _ in try self.configuration(true) }
        await store.change(enabled: false, acknowledged: false, namespace: "test") { _, _ in
            throw APIClient.APIError.message("连接断开")
        }
        XCTAssertTrue(store.enabled)
        XCTAssertFalse(store.verified)
        XCTAssertFalse(store.permitsCapture(namespace: "test"))
        XCTAssertNotNil(store.message)
    }

    func testLateEnableReceiptCannotAffectChangedIdentityOrBackground() async throws {
        let store = AutomaticReviewStore()
        await store.change(enabled: true, acknowledged: true, namespace: "test") { _, _ in
            store.suspend(clearIdentity: true)
            return try self.configuration(true)
        }
        XCTAssertFalse(store.enabled)
        XCTAssertNil(store.namespace)
        XCTAssertFalse(store.verified)
    }

    func testUnknownServerScopeAndMalformedReplyCannotEnableAutomaticCapture() async throws {
        let store = AutomaticReviewStore()
        await store.load(namespace: "test") { _, _ in try self.configuration(true, scope: "all_household_members") }
        XCTAssertFalse(store.enabled)
        XCTAssertFalse(store.verified)
        await store.load(namespace: "test") { _, _ in Data(#"{"enabled":true}"#.utf8) }
        XCTAssertFalse(store.permitsCapture(namespace: "test"))
    }

    func testCanceledCaptureLockCanResumeButFinishedAttemptDoesNotRepeat() {
        let attempts = AutomaticClientActionAttempts()
        XCTAssertTrue(attempts.begin(actionID: "one", namespace: "member", expiresAt: 200, now: 100))
        XCTAssertFalse(attempts.begin(actionID: "two", namespace: "member", expiresAt: 200, now: 100))
        attempts.finish(actionID: "one", namespace: "member", retryable: true)
        XCTAssertTrue(attempts.begin(actionID: "one", namespace: "member", expiresAt: 200, now: 100))
        attempts.finish(actionID: "one", namespace: "member")
        XCTAssertFalse(attempts.begin(actionID: "one", namespace: "member", expiresAt: 200, now: 100))
    }
}
