import XCTest
import SwiftUI
import UIKit
@testable import HomeAI

@MainActor
final class PrivacyReviewTests: XCTestCase {
    private let now = Date(timeIntervalSince1970: 1_800_000_000)
    private func document(reviewID: String? = "review-one", status: String = "pending", force: Bool = true, edit: Bool = true, expires: Double? = 1_800_000_600, hasMedia: Bool = true) -> PrivacyReviewDocument {
        PrivacyReviewDocument(review_id: reviewID, task_id: "task-one", status: status, code: "review_unavailable", stage: "local_review",
            reason: "本地概率检测未完成", suggestion: "检查本地服务后重试", retryable: true, overrideable: force,
            expires_at: expires, provider: .init(id: "provider-one", model: "private-model", endpoint: "https://user:password@example.com/v1?token=secret"),
            blocks: [.init(id: "input", label: "本次输入", text: "原始内容", editable: true), .init(id: "readonly", label: "只读来源", text: "只读资料", editable: false)],
            has_media: hasMedia, can_force: force, can_edit: edit)
    }
    private func loaded(_ document: PrivacyReviewDocument? = nil) -> (PrivacyReviewSession, UUID) {
        let value = PrivacyReviewSession(), connection = UUID()
        let ticket = value.beginLoad(connectionID: connection)
        XCTAssertTrue(value.bind(namespace: "member-A", ticket: ticket))
        XCTAssertTrue(value.receive(document ?? self.document(), ticket: ticket, namespace: "member-A", now: now))
        return (value, connection)
    }

    func testExactDocumentFieldsAndTargetOriginDecodeWithoutCredentials() throws {
        let raw = try JSONEncoder().encode(document())
        let decoded = try JSONDecoder().decode(PrivacyReviewDocument.self, from: raw)
        XCTAssertEqual(decoded.reason, "本地概率检测未完成")
        XCTAssertEqual(decoded.blocks.count, 2)
        XCTAssertFalse(decoded.blocks[1].editable)
        XCTAssertTrue(decoded.has_media)
        XCTAssertEqual(decoded.provider?.origin, "https://example.com")
    }

    func testForceConfirmationUsesCurrentReasonAndOnlyCurrentMediaScope() throws {
        let textOnly = document(hasMedia: false)
        XCTAssertNil(textOnly.mediaNotice)
        XCTAssertTrue(textOnly.forceConfirmation.contains(textOnly.reason))
        XCTAssertTrue(textOnly.forceConfirmation.contains(try XCTUnwrap(textOnly.provider).model))
        XCTAssertTrue(textOnly.forceConfirmation.contains("https://example.com"))
        XCTAssertFalse(textOnly.forceConfirmation.contains("password"))
        XCTAssertFalse(textOnly.forceConfirmation.contains("token=secret"))
        let withImage = document(hasMedia: true)
        let mediaNotice = try XCTUnwrap(withImage.mediaNotice)
        XCTAssertTrue(withImage.forceConfirmation.contains(mediaNotice))
        XCTAssertFalse(textOnly.forceConfirmation.contains(mediaNotice))
    }

    func testForceEncodesOnlySnapshotDecisionAndReusesPendingIdempotencyKey() throws {
        let (value, connection) = loaded()
        let first = try XCTUnwrap(value.submission(.force, connectionID: connection, now: now))
        let second = try XCTUnwrap(value.submission(.force, connectionID: connection, now: now))
        XCTAssertEqual(first.idempotency_key, second.idempotency_key)
        let raw = try JSONSerialization.jsonObject(with: JSONEncoder().encode(first)) as? [String: Any]
        XCTAssertEqual(raw?["review_id"] as? String, "review-one")
        XCTAssertEqual(raw?["decision"] as? String, "force")
        XCTAssertNil(raw?["edits"])
        XCTAssertEqual(Set(raw?.keys.map { $0 } ?? []), Set(["review_id", "decision", "idempotency_key"]))
    }

    func testEditsOnlyIncludeEditableBlocksAndDisableForceUntilRechecked() throws {
        let (value, connection) = loaded()
        value.drafts["input"] = "修改后的内容"
        value.drafts["readonly"] = "不能修改"
        value.drafts["unknown"] = "不能添加"
        XCTAssertFalse(value.canSubmit(.force, connectionID: connection, now: now))
        XCTAssertFalse(value.canSubmit(.retry, connectionID: connection, now: now))
        let first = try XCTUnwrap(value.submission(.edit, connectionID: connection, now: now))
        XCTAssertEqual(first.edits, ["input": "修改后的内容"])
        value.drafts["input"] = "再次修改"
        let second = try XCTUnwrap(value.submission(.edit, connectionID: connection, now: now))
        XCTAssertNotEqual(first.idempotency_key, second.idempotency_key)
        value.resetEdits()
        XCTAssertFalse(value.hasChanges)
        XCTAssertTrue(value.canSubmit(.force, connectionID: connection, now: now))
    }

    func testLegacyExpiredAndInvalidatedReasonOnlyTasksUseResumeWithoutSnapshot() throws {
        for status in ["pending", "expired", "invalidated", "unavailable"] {
            let (value, connection) = loaded(document(reviewID: nil, status: status, expires: now.timeIntervalSince1970 - 1))
            XCTAssertTrue(value.document?.blocks.isEmpty == true)
            XCTAssertTrue(value.drafts.isEmpty)
            XCTAssertFalse(value.canSubmit(.force, connectionID: connection, now: now))
            XCTAssertFalse(value.canSubmit(.edit, connectionID: connection, now: now))
            XCTAssertTrue(value.canSubmit(.retry, connectionID: connection, now: now))
            XCTAssertNil(try value.submission(.retry, connectionID: connection, now: now))
        }
    }

    func testExpiredOrMissingExpirySnapshotNeverDisplaysOrAllowsDecision() {
        for expiry in [Optional(now.timeIntervalSince1970), nil] {
            let value = PrivacyReviewSession(), connection = UUID()
            let ticket = value.beginLoad(connectionID: connection)
            XCTAssertTrue(value.bind(namespace: "member-A", ticket: ticket))
            XCTAssertFalse(value.receive(document(expires: expiry), ticket: ticket, namespace: "member-A", now: now))
            XCTAssertNil(value.document)
            XCTAssertFalse(value.isVisible(connectionID: connection, now: now))
            XCTAssertTrue(value.drafts.isEmpty)
        }
        let (value, connection) = loaded()
        let future = now.addingTimeInterval(600)
        XCTAssertFalse(value.isVisible(connectionID: connection, now: future))
        for decision in [PrivacyReviewDecision.retry, .force, .edit] { XCTAssertFalse(value.canSubmit(decision, connectionID: connection, now: future)) }
    }

    func testOfflineBackgroundAndIdentityChangesClearPreviewAndRejectLateResponses() throws {
        for reason in ["离线", "后台", "身份切换"] {
            let (value, connection) = loaded()
            let oldTicket = value.generation
            let previous = try XCTUnwrap(value.submission(.force, connectionID: connection, now: now))
            value.invalidate(message: reason)
            XCTAssertNil(value.document)
            XCTAssertNil(value.namespace)
            XCTAssertTrue(value.drafts.isEmpty)
            XCTAssertFalse(value.receive(document(), ticket: oldTicket, namespace: "member-A", now: now))
            XCTAssertFalse(value.updateDraft("迟到输入不能恢复预览", blockID: "input", ticket: oldTicket))
            XCTAssertTrue(value.drafts.isEmpty)
            let otherConnection = UUID()
            let newTicket = value.beginLoad(connectionID: otherConnection)
            XCTAssertTrue(value.bind(namespace: "member-B", ticket: newTicket))
            XCTAssertFalse(value.receive(document(), ticket: newTicket, namespace: "member-A", now: now))
            XCTAssertFalse(value.isVisible(connectionID: connection, now: now))
            XCTAssertTrue(value.receive(document(), ticket: newTicket, namespace: "member-B", now: now))
            let next = try XCTUnwrap(value.submission(.force, connectionID: otherConnection, now: now))
            XCTAssertNotEqual(previous.idempotency_key, next.idempotency_key)
        }
    }

    func testUnrelatedTaskUpdatesKeepDraftButChangedReviewClearsIt() throws {
        let (value, connection) = loaded()
        let ticket = value.generation
        XCTAssertTrue(value.updateDraft("正在输入的内容", blockID: "input", ticket: ticket, now: now))
        XCTAssertTrue(value.revalidate(document(), ticket: ticket, namespace: "member-A", now: now))
        XCTAssertEqual(value.generation, ticket)
        XCTAssertEqual(value.drafts["input"], "正在输入的内容")
        XCTAssertTrue(value.revalidate(document(reviewID: nil, status: "invalidated"), ticket: ticket, namespace: "member-A", now: now))
        XCTAssertNotEqual(value.generation, ticket)
        XCTAssertTrue(value.drafts.isEmpty)
        XCTAssertFalse(value.canSubmit(.force, connectionID: connection, now: now))
        XCTAssertFalse(value.updateDraft("旧编辑回调", blockID: "input", ticket: ticket))
        XCTAssertFalse(value.revalidate(document(), ticket: ticket, namespace: "member-A", now: now))
    }

    func testBusyDeniedAndConsumedReviewsDoNotOfferUnsafeActions() {
        let (value, connection) = loaded(document(force: false, edit: false))
        XCTAssertFalse(value.canSubmit(.force, connectionID: connection, now: now))
        value.drafts["input"] = "修改"
        XCTAssertFalse(value.canSubmit(.edit, connectionID: connection, now: now))
        value.resetEdits(); value.beginSubmitting()
        XCTAssertFalse(value.canSubmit(.retry, connectionID: connection, now: now))
        for status in ["approved", "consumed"] {
            let (handled, identity) = loaded(document(status: status))
            XCTAssertFalse(handled.canSubmit(.force, connectionID: identity, now: now))
            XCTAssertFalse(handled.canSubmit(.retry, connectionID: identity, now: now))
        }
    }
}


extension PrivacyReviewTests {
    func testPublicPrivacyReviewContentScreenshotWithoutRequests() async throws {
        let value = document(expires: Date().timeIntervalSince1970 + 600)
        let session = PrivacyReviewSession(), connection = UUID()
        let ticket = session.beginLoad(connectionID: connection)
        XCTAssertTrue(session.bind(namespace: "public-ui-fixture", ticket: ticket))
        XCTAssertTrue(session.receive(value, ticket: ticket, namespace: "public-ui-fixture"))
        let content = NavigationStack {
            List {
                PrivacyReviewContent(document: value, review: session, connectionID: connection, online: true, onSubmit: { _ in
                    XCTFail("截图展示不能提交任何决定")
                })
            }.navigationTitle("隐私检查").navigationBarTitleDisplayMode(.inline)
        }
        let controller = UIHostingController(rootView: content)
        controller.overrideUserInterfaceStyle = .light
        let scene = try XCTUnwrap(UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }.first)
        let window = UIWindow(windowScene: scene)
        window.frame = CGRect(x: 0, y: 0, width: 390, height: 1500)
        window.rootViewController = controller; window.isHidden = false
        defer { session.invalidate(message: nil); window.isHidden = true; window.rootViewController = nil }
        controller.view.frame = window.bounds; controller.view.layoutIfNeeded()
        try await Task.sleep(for: .milliseconds(600))
        controller.view.layoutIfNeeded()
        let format = UIGraphicsImageRendererFormat(); format.scale = 2
        var rendered = false
        let image = UIGraphicsImageRenderer(bounds: controller.view.bounds, format: format).image { _ in
            rendered = controller.view.drawHierarchy(in: controller.view.bounds, afterScreenUpdates: true)
        }
        XCTAssertTrue(rendered)
        let attachment = XCTAttachment(image: image)
        attachment.name = "privacy-review-public-fixture"
        attachment.lifetime = .keepAlways
        add(attachment)
    }
}
