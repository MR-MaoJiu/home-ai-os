import XCTest
import SwiftUI
import UIKit
@testable import HomeAI

@MainActor
final class TaskRetryTests: XCTestCase {
    func testRetryAssessmentDecodesAllowedAndSafetyDenial() throws {
        let allowed = try JSONDecoder().decode(TaskRetryAssessment.self, from: Data(#"{"can_retry":true,"code":"safe_retry","reason":"已成功步骤保持不变","retry_step":1,"completed_steps":1}"#.utf8))
        XCTAssertTrue(allowed.can_retry)
        XCTAssertEqual(allowed.retry_step, 1)
        XCTAssertEqual(allowed.completed_steps, 1)
        let denied = try JSONDecoder().decode(TaskRetryAssessment.self, from: Data(#"{"can_retry":false,"code":"uncertain_effect","reason":"外部操作结果不明，不能重复执行","retry_step":null,"completed_steps":0}"#.utf8))
        XCTAssertFalse(denied.can_retry)
        XCTAssertNil(denied.retry_step)
        XCTAssertEqual(denied.reason, "外部操作结果不明，不能重复执行")
    }

    func testRetryReceiptRetainsReplayedStatusWithoutPretendingCompletion() throws {
        let receipt = try JSONDecoder().decode(TaskRetryReceipt.self, from: Data(#"{"id":"task-one","status":"AWAITING_APPROVAL","replayed":true}"#.utf8))
        XCTAssertEqual(receipt.id, "task-one")
        XCTAssertEqual(receipt.status, "AWAITING_APPROVAL")
        XCTAssertTrue(receipt.replayed)
    }

    func testBudgetRecoveryDecodesOldAndExplicitServerDecisions() throws {
        for allowed in [Optional<Bool>.none, false, true] {
            var task: [String: Any] = ["status": "WAITING_BUDGET", "error": "服务端明确的预算原因"]
            var turn: [String: Any] = ["id": "turn", "sequence": 1, "client_key": "message-key", "user_text": "提问", "task_id": "task",
                "status": "WAITING_BUDGET", "assistant_text": "", "approvals": [], "web_sources": [], "sources": []]
            if let allowed { task["budget_recoverable"] = allowed; turn["budget_recoverable"] = allowed }
            let decodedTask = try JSONDecoder().decode(TaskResult.self, from: JSONSerialization.data(withJSONObject: task))
            var decodedTurn = try JSONDecoder().decode(StoredChatTurn.self, from: JSONSerialization.data(withJSONObject: turn))
            XCTAssertEqual(decodedTask.budget_recoverable, allowed)
            XCTAssertEqual(decodedTurn.budget_recoverable, allowed)
            // 授权失效只移除敏感正文，不应重新显示服务端明确禁用的预算重查入口。
            decodedTurn.requires_online_revalidation = true
            XCTAssertEqual(decodedTurn.cached().budget_recoverable, allowed)
            let restored = try JSONDecoder().decode(StoredChatTurn.self, from: JSONEncoder().encode(decodedTurn.cached()))
            XCTAssertEqual(restored.budget_recoverable, allowed)
        }
    }

    func testOldAssetWithoutProvenanceRemainsCompatible() throws {
        let asset = try JSONDecoder().decode(MediaAsset.self, from: Data(#"{"record_id":"asset-one","version":1,"kind":"image","mime_type":"image/png","name":"图片.png","size":128,"sha256":"digest","processing":{"status":"failed"}}"#.utf8))
        XCTAssertNil(asset.provenance)
        XCTAssertEqual(asset.processing?.status, "failed")
        XCTAssertEqual(asset.kind, "image")
    }

    func testProvenanceOnlyLabelsKnownOriginsAndRejectsUnsafeLinks() throws {
        for (origin, expected) in [("generated", "AI生成"), ("edited", "AI编辑"), ("retrieved", "来源文件"), ("script_retrieved", "来源文件"), ("shell_created", "Shell处理文件")] {
            let value = MediaAssetProvenance(origin: origin, source_url: "https://example.com/source.png")
            XCTAssertEqual(value.label, expected)
            XCTAssertEqual(value.safeSourceURL?.host, "example.com")
        }
        for origin in ["vision", "uploaded", "unknown"] {
            XCTAssertNil(MediaAssetProvenance(origin: origin, source_url: nil).label)
        }
        for url in ["javascript:alert(1)", "data:text/html,test", "file:///tmp/private.png", "https://user:secret@example.com/source", "/relative-source"] {
            XCTAssertNil(MediaAssetProvenance(origin: "retrieved", source_url: url).safeSourceURL)
        }
        XCTAssertNotNil(MediaAssetProvenance(origin: "retrieved", source_url: "http://example.com/source").safeSourceURL)
    }

    func testPublicRetryCardsRenderWithoutTaskRequests() async throws {
        let allowed = TaskRetryAssessment(can_retry: true, code: "safe_retry", reason: "仅继续未完成部分；已成功步骤和已消耗预算保持不变。", retry_step: 1, completed_steps: 1)
        let denied = TaskRetryAssessment(can_retry: false, code: "uncertain_effect", reason: "邮件发送结果不明，不能重复执行。请先核对原操作结果。", retry_step: nil, completed_steps: 0)
        let content = NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 24) {
                    Text("暂时无法完成").font(.headline)
                    TaskRetryPresentation(assessment: allowed, online: true, loading: false, submitting: false, error: nil,
                        onRetry: { XCTFail("公开展示不能提交任务重试") }, onRefresh: { XCTFail("公开展示不能请求任务接口") })
                    Divider()
                    Text("需要先核对结果").font(.headline)
                    TaskRetryPresentation(assessment: denied, online: true, loading: false, submitting: false, error: nil,
                        onRetry: { XCTFail("安全拒绝不能提交任务重试") }, onRefresh: { XCTFail("公开展示不能请求任务接口") })
                }.padding()
            }.navigationTitle("任务处理").navigationBarTitleDisplayMode(.inline)
        }
        let controller = UIHostingController(rootView: content)
        let scene = try XCTUnwrap(UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }.first)
        let window = UIWindow(windowScene: scene)
        window.frame = CGRect(x: 0, y: 0, width: 390, height: 900)
        window.rootViewController = controller; window.isHidden = false
        defer { window.isHidden = true; window.rootViewController = nil }
        controller.view.frame = window.bounds
        for style in [UIUserInterfaceStyle.light, .dark] {
            controller.overrideUserInterfaceStyle = style
            controller.view.layoutIfNeeded()
            try await Task.sleep(for: .milliseconds(300))
            controller.view.layoutIfNeeded()
            let format = UIGraphicsImageRendererFormat(); format.scale = 2
            var rendered = false
            let image = UIGraphicsImageRenderer(bounds: controller.view.bounds, format: format).image { _ in
                rendered = controller.view.drawHierarchy(in: controller.view.bounds, afterScreenUpdates: true)
            }
            XCTAssertTrue(rendered)
            let attachment = XCTAttachment(image: image)
            attachment.name = style == .light ? "task-retry-light" : "task-retry-dark"
            attachment.lifetime = .keepAlways; add(attachment)
        }
    }
}
