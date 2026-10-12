import XCTest
import SwiftUI
import HealthKit
import CoreLocation
@testable import HomeAI

/// 只验证授权状态机和数据契约，不打开权限面板、不读取或写入健康数据库。
final class HealthAuthorizationTests: XCTestCase {
    @MainActor
    func testLiveHealthConsentAndTaskResumeWithoutCloudAnalysis() async throws {
        guard ProcessInfo.processInfo.environment["HOMEAI_LIVE_HEALTH_CONSENT"] == "1" else { throw XCTSkip("需用户明确同意最近24小时步数验收，并亲自在手机处理健康权限") }
        let idle = UIApplication.shared.isIdleTimerDisabled
        UIApplication.shared.isIdleTimerDisabled = true
        defer { UIApplication.shared.isIdleTimerDisabled = idle }
        let api = AppServices.api
        await api.restoreConnectionIfNeeded()
        let namespace = try await api.syncNamespace()
        await ClientViewCache.shared.remove(key: "acceptance.health.last24h.steps", namespace: namespace)
        let cacheKey = "acceptance.health.last24h.steps.v2"
        let request: Data
        if let saved = await ClientViewCache.shared.read(key: cacheKey, namespace: namespace) { request = saved }
        else {
            let end = Date(), start = end.addingTimeInterval(-86400)
            request = try JSONSerialization.data(withJSONObject: ["idempotency_key": "health-acceptance-" + UUID().uuidString,
                "capability": "client.request@v1", "arguments": ["target_member_id": "self", "kind": "read_health",
                    "purpose": "健康授权验收：仅读取最近24小时步数，在家庭服务器本地使用，不发送云端分析。",
                    "parameters": ["types": ["steps"], "start_at": start.ISO8601Format(), "end_at": end.ISO8601Format()]]])
            try await ClientViewCache.shared.write(request, key: cacheKey, namespace: namespace)
        }
        let created = try JSONDecoder().decode(TaskCreated.self, from: await api.request("POST", "/api/v1/tasks", body: request, expectedNamespace: namespace))
        print("HEALTH_FLOW_READY recent24h_steps=true user_action_required=true")
        let deadline = Date().addingTimeInterval(240)
        while Date() < deadline {
            let task = try JSONDecoder().decode(TaskResult.self, from: await api.request("GET", "/api/v1/tasks/" + created.id, expectedNamespace: namespace))
            if task.status == "SUCCEEDED" {
                let raw = try await api.request("GET", "/api/v1/tasks/" + created.id + "/steps", expectedNamespace: namespace)
                let steps = try XCTUnwrap(JSONSerialization.jsonObject(with: raw) as? [[String: Any]])
                let result = try XCTUnwrap(steps.first?["result"] as? [String: Any])
                let status = result["data_status"] as? String ?? "unknown"
                let health = result["health"] as? [String: Any] ?? [:]
                XCTAssertTrue(["available", "partial", "no_data"].contains(status))
                print("HEALTH_FLOW_REAL resumed=true data_status=\(status) sample_count=\(health["sample_count"] as? Int ?? 0) cloud_analysis=false")
                await ClientViewCache.shared.remove(key: cacheKey, namespace: namespace)
                return
            }
            if ["FAILED", "CANCELED"].contains(task.status) {
                throw APIClient.APIError.message("健康验收任务未完成；请查看手机请求卡片中的状态")
            }
            try await Task.sleep(for: .seconds(1))
        }
        throw APIClient.APIError.message("等待用户处理健康卡片超时；请求仍保留在原任务中")
    }

    private let parameters: [String: JSONValue] = [
        "types": .array([.string("steps"), .string("sleep")]),
        "start_at": .string("2026-10-01T00:00:00+08:00"),
        "end_at": .string("2026-10-02T00:00:00+08:00")
    ]

    func testSystemPermissionPanelDoesNotCancelPendingCapture() {
        XCTAssertFalse(ClientActionLifecycle.shouldSuspend(phase: .active, connected: true))
        XCTAssertFalse(ClientActionLifecycle.shouldSuspend(phase: .inactive, connected: true))
        XCTAssertTrue(ClientActionLifecycle.shouldSuspend(phase: .background, connected: true))
        XCTAssertTrue(ClientActionLifecycle.shouldSuspend(phase: .inactive, connected: false))
    }

    func testHealthScopePreservesRequestedMetricsTimezoneAndRange() throws {
        let scope = try HealthReadScope(parameters: parameters)
        XCTAssertEqual(scope.types, ["steps", "sleep"])
        XCTAssertEqual(scope.end.timeIntervalSince(scope.start), 86400)
        let payload = try scope.payload(samples: [], allowEmpty: true)
        XCTAssertEqual(payload["start_at"], parameters["start_at"])
        XCTAssertEqual(payload["end_at"], parameters["end_at"])
        XCTAssertEqual(payload["types"], parameters["types"])
    }

    func testNoReadableSamplesRequireExplicitContinueAndNeverClaimAuthorization() throws {
        let scope = try HealthReadScope(parameters: parameters)
        XCTAssertThrowsError(try scope.payload(samples: [], allowEmpty: false)) { error in
            XCTAssertEqual(error as? HealthReadFailure, .noReadableData)
            XCTAssertTrue(error.localizedDescription.contains("没有记录"))
            XCTAssertTrue(error.localizedDescription.contains("尚未允许"))
        }
        XCTAssertEqual(try scope.payload(samples: [], allowEmpty: true)["status"], .string("no_data"))
    }

    @MainActor
    func testAutomaticHealthReadNeverRequestsUndecidedSystemPermission() {
        XCTAssertTrue(HealthCapture.mayReadWithoutPermissionPrompt(.unnecessary))
        XCTAssertFalse(HealthCapture.mayReadWithoutPermissionPrompt(.shouldRequest))
        XCTAssertFalse(HealthCapture.mayReadWithoutPermissionPrompt(.unknown))
    }

    func testAutomaticCaptureRequiresServerPermissionAndMatchingMember() throws {
        XCTAssertFalse(action().permitsAutomaticallyReviewedCapture(memberID: "member"))
        var allowed = action()
        allowed.automatic_capture_allowed = true
        XCTAssertTrue(allowed.permitsAutomaticallyReviewedCapture(memberID: "member"))
        XCTAssertFalse(allowed.permitsAutomaticallyReviewedCapture(memberID: "other-member"))
        XCTAssertFalse(allowed.permitsAutomaticallyReviewedCapture(memberID: "member", at: Date(timeIntervalSince1970: 4_000_000_001)))
        XCTAssertFalse(allowed.applyingReceipt(.init(id: "action", status: "RESPONDED")).permitsAutomaticallyReviewedCapture(memberID: "member"))
        let encoded = try JSONEncoder().encode(allowed)
        var object = try XCTUnwrap(JSONSerialization.jsonObject(with: encoded) as? [String: Any])
        for kind in ["provide_text", "choose_option", "select_file", "cloud.disclose", "android.assist"] {
            object["kind"] = kind
            let candidate = try JSONDecoder().decode(ClientActionRequest.self, from: JSONSerialization.data(withJSONObject: object))
            XCTAssertFalse(candidate.permitsAutomaticallyReviewedCapture(memberID: "member"))
        }
        object["kind"] = "read_health"
        object["requested_by"] = ["id": "someone-else", "name": "其他成员"]
        let other = try JSONDecoder().decode(ClientActionRequest.self, from: JSONSerialization.data(withJSONObject: object))
        XCTAssertFalse(other.permitsAutomaticallyReviewedCapture(memberID: "member"))
    }

    @MainActor
    func testAutomaticPreciseLocationRequiresFullSystemAccuracy() {
        XCTAssertTrue(ClientContextSampler.permitsReviewedCapture(precision: "coarse", authorization: .authorizedWhenInUse,
            accuracyAuthorization: .reducedAccuracy, applicationState: .active))
        XCTAssertTrue(ClientContextSampler.permitsReviewedCapture(precision: "precise", authorization: .authorizedAlways,
            accuracyAuthorization: .fullAccuracy, applicationState: .active))
        XCTAssertFalse(ClientContextSampler.permitsReviewedCapture(precision: "precise", authorization: .authorizedWhenInUse,
            accuracyAuthorization: .reducedAccuracy, applicationState: .active))
        for permission in [CLAuthorizationStatus.notDetermined, .denied, .restricted] {
            XCTAssertFalse(ClientContextSampler.permitsReviewedCapture(precision: "coarse", authorization: permission,
                accuracyAuthorization: .fullAccuracy, applicationState: .active))
        }
        for state in [UIApplication.State.inactive, .background] {
            XCTAssertFalse(ClientContextSampler.permitsReviewedCapture(precision: "coarse", authorization: .authorizedWhenInUse,
                accuracyAuthorization: .fullAccuracy, applicationState: state))
        }
    }

    func testHealthScopeRejectsMalformedTypesInsteadOfSilentlyDroppingThem() {
        for types: JSONValue in [.array([]), .array([.string("steps"), .number(3)]),
                                 .array([.string("steps"), .string("steps")]), .array([.string("unsupported")])] {
            var value = parameters; value["types"] = types
            XCTAssertThrowsError(try HealthReadScope(parameters: value))
        }
    }

    func testHealthScopeRejectsInvalidReversedAndExcessiveTimeRanges() {
        for end in ["invalid", "2026-09-30T00:00:00+08:00", "2026-10-01T00:00:00+08:00", "2026-11-02T00:00:00+08:00"] {
            var value = parameters; value["end_at"] = .string(end)
            XCTAssertThrowsError(try HealthReadScope(parameters: value))
        }
    }

    @MainActor
    func testLargeHealthCaptureChunksAreCompleteAndStable() throws {
        let scope = try HealthReadScope(parameters: parameters)
        let samples = Array(repeating: JSONValue.object(["type": .string("steps")]), count: 2501)
        let chunks = try HealthCapture.chunks(samples: samples, scope: scope, allowEmpty: false, captureID: "capture")
        XCTAssertEqual(chunks.count, 3)
        for (index, item) in chunks.enumerated() {
            XCTAssertEqual(item["chunk_index"], .number(Double(index)))
            XCTAssertEqual(item["chunk_count"], .number(3))
            XCTAssertEqual(item["capture_id"], .string("capture"))
            XCTAssertEqual(item["query_complete"], .bool(true))
            XCTAssertEqual(item["truncated"], .bool(false))
        }
        XCTAssertEqual(chunks.compactMap { if case .array(let rows) = $0["samples"] { return rows.count }; return nil }, [1000, 1000, 501])
        XCTAssertThrowsError(try HealthCapture.chunks(samples: Array(repeating: .null, count: 20001), scope: scope, allowEmpty: false))
    }

    @MainActor
    func testOvernightSleepIsClippedButOriginalBoundariesRemain() throws {
        let scope = try HealthReadScope(parameters: parameters)
        let sample = HKCategorySample(type: HKObjectType.categoryType(forIdentifier: .sleepAnalysis)!,
            value: HKCategoryValueSleepAnalysis.asleepDeep.rawValue,
            start: scope.start.addingTimeInterval(-3600), end: scope.start.addingTimeInterval(7200))
        guard case .object(let payload) = try HealthCapture.serialize(sample, metric: .sleep, scope: scope) else { return XCTFail("未生成样本") }
        XCTAssertEqual(payload["start_at"], .string(scope.start.ISO8601Format()))
        XCTAssertEqual(payload["original_start_at"], .string(sample.startDate.ISO8601Format()))
        XCTAssertEqual(payload["value"], .number(4))
        XCTAssertNotNil(payload["source_bundle"])
    }

    @MainActor
    func testAdditionalMetricsUseExplicitUnitsAndPercentScale() throws {
        var requested = parameters
        requested["types"] = .array(HealthMetric.allCases.map { .string($0.rawValue) })
        let scope = try HealthReadScope(parameters: requested)
        XCTAssertEqual(scope.types.count, 14)
        let sample = HKQuantitySample(type: HKObjectType.quantityType(forIdentifier: .oxygenSaturation)!,
            quantity: HKQuantity(unit: .percent(), doubleValue: 0.97), start: scope.start, end: scope.start)
        guard case .object(let payload) = try HealthCapture.serialize(sample, metric: .oxygen_saturation, scope: scope) else { return XCTFail("未生成样本") }
        XCTAssertEqual(payload["value"], .number(97))
        XCTAssertEqual(payload["unit"], .string("%"))
        XCTAssertEqual(HealthMetric.heart_rate_variability.measurement.name, "ms")
    }

    func testHealthErrorsDistinguishCancelUnavailableRestrictedAndLocked() {
        let errors: [(HKError.Code, HealthReadFailure)] = [
            (.errorUserCanceled, .canceled), (.errorHealthDataUnavailable, .unavailable),
            (.errorHealthDataRestricted, .restricted), (.errorDatabaseInaccessible, .locked)
        ]
        for (code, expected) in errors {
            XCTAssertEqual(HealthReadFailure.readable(NSError(domain: HKErrorDomain, code: code.rawValue)) as? HealthReadFailure, expected)
        }
        let unknown = NSError(domain: "HealthTest", code: 9)
        XCTAssertEqual(HealthReadFailure.readable(unknown) as NSError, unknown)
    }

    func testHealthReceiptRoundTripPreservesPartialAndNoDataOutcomes() throws {
        for (status, summary) in [("no_data", "已告知暂无可读健康数据"), ("partial", "已提供部分健康数据"), ("available", "健康数据已提供")] {
            let receipt = ClientActionReceipt(id: "action", status: "RESPONDED", data_status: status, note: "实际读取结果")
            let cached = try JSONDecoder().decode(ClientActionReceipt.self, from: JSONEncoder().encode(receipt))
            var presentation = ClientActionPresentation(request: action())
            XCTAssertTrue(presentation.acknowledge(cached))
            XCTAssertEqual(presentation.current.summaryLabel, summary)
            XCTAssertEqual(presentation.current.note, "实际读取结果")
            XCTAssertFalse(presentation.receive(action()))
        }
    }

    func testLegacyReceiptDoesNotImplyGrantedHealthPermission() throws {
        let receipt = try JSONDecoder().decode(ClientActionReceipt.self, from: Data(#"{"id":"action","status":"RESPONDED"}"#.utf8))
        XCTAssertEqual(action().applyingReceipt(receipt).summaryLabel, "健康读取结果已提交")
        let encoded = try JSONEncoder().encode(action())
        let decoded = try JSONDecoder().decode(ClientActionRequest.self, from: encoded)
        XCTAssertNil(decoded.data_status)
        XCTAssertNil(decoded.note)
    }

    private func action() -> ClientActionRequest {
        ClientActionRequest(id: "action", kind: "read_health", status: "PENDING", purpose: "健康请求契约验证",
            requested_by: .init(id: "member", name: "测试成员"), target_member_id: "member", scope: "personal",
            created_at: 0, expires_at: 4_000_000_000, parameters: parameters, task_id: "task", conversation_id: nil,
            notification_id: nil, can_respond: true, can_revoke: false)
    }
}
