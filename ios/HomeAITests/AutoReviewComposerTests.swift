import XCTest
import SwiftUI
import UIKit
@testable import HomeAI

final class AutoReviewComposerTests: XCTestCase {
    func testDefaultNeverApprovesWithoutConnectionAndExplicitConfirmation() {
        var state = AutoReviewControlState()
        XCTAssertFalse(state.enabled)
        XCTAssertEqual(state.tapIntent, .unavailable)
        XCTAssertFalse(state.canConfirmEnable)

        state.available = true
        XCTAssertEqual(state.tapIntent, .confirmEnable)
        XCTAssertFalse(state.enabled, "打开确认框不能提前改变服务端审核状态")
        XCTAssertTrue(state.canConfirmEnable)
    }

    func testStaleConfirmationCannotEnableAfterDisconnectOrDuringSave() {
        var state = AutoReviewControlState(enabled: false, updating: false, available: true)
        XCTAssertTrue(state.canConfirmEnable)
        state.available = false
        XCTAssertFalse(state.canConfirmEnable)
        XCTAssertEqual(state.tapIntent, .unavailable)
        state.available = true
        state.updating = true
        XCTAssertFalse(state.canConfirmEnable)
        XCTAssertEqual(state.tapIntent, .unavailable)
        XCTAssertFalse(state.enabled)
    }

    func testTurningOffRetainsWarningUntilServerConfirms() {
        var state = AutoReviewControlState(enabled: true, updating: false, available: true)
        XCTAssertEqual(state.tapIntent, .disable)
        XCTAssertFalse(state.canConfirmEnable)
        state.updating = true
        XCTAssertTrue(state.enabled, "正在提交关闭请求时必须继续显示警告")
        XCTAssertEqual(state.accessibilityValue, "正在保存")
        state.updating = false
        state.available = false
        XCTAssertTrue(state.enabled, "关闭失败或离线时不能声称已关闭")
        XCTAssertEqual(state.accessibilityValue, "已开启，连接后可关闭")
    }

    @MainActor
    func testPublicComposerWarningScreenshotsInLightDarkAndNarrowLargeType() async throws {
        let scene = try XCTUnwrap(UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }.first)
        let cases: [(String, ColorScheme, DynamicTypeSize, CGFloat)] = [
            ("auto-review-light", .light, .large, 390),
            ("auto-review-dark", .dark, .large, 390),
            ("auto-review-narrow-accessibility", .dark, .accessibility2, 320)
        ]
        for (name, scheme, size, width) in cases {
            let controller = UIHostingController(rootView: AutoReviewPublicPage(scheme: scheme, size: size,
                onUnexpectedAction: { XCTFail("静态截图不能触发任何自动审核设置或业务操作") }))
            controller.overrideUserInterfaceStyle = scheme == .dark ? .dark : .light
            let window = UIWindow(windowScene: scene)
            window.frame = CGRect(x: 0, y: 0, width: width, height: 1000)
            window.rootViewController = controller
            window.isHidden = false
            defer { window.isHidden = true; window.rootViewController = nil }
            controller.view.frame = window.bounds
            controller.view.layoutIfNeeded()
            try await Task.sleep(for: .milliseconds(350))
            controller.view.layoutIfNeeded()
            let format = UIGraphicsImageRendererFormat(); format.scale = 2
            var rendered = false
            let image = UIGraphicsImageRenderer(bounds: controller.view.bounds, format: format).image { _ in
                rendered = controller.view.drawHierarchy(in: controller.view.bounds, afterScreenUpdates: true)
            }
            XCTAssertTrue(rendered)
            XCTAssertEqual(image.size.width, width, accuracy: 1)
            let attachment = XCTAttachment(image: image)
            attachment.name = name
            attachment.lifetime = .keepAlways
            add(attachment)
        }
    }
}

/// 只渲染公开样例，所有操作都会使测试失败，不能启用实际成员的自动审批。
private struct AutoReviewPublicPage: View {
    let scheme: ColorScheme
    let size: DynamicTypeSize
    let onUnexpectedAction: () -> Void

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                Text("自动审核").font(.title2.bold())
                Text("默认关闭").font(.caption).foregroundStyle(.secondary)
                composer(enabled: false, updating: false)
                Text("开启后的警告状态").font(.caption).foregroundStyle(.secondary)
                composer(enabled: true, updating: false)
                Text("等待服务器确认关闭").font(.caption).foregroundStyle(.secondary)
                composer(enabled: true, updating: true)
            }.padding(12)
        }
        .background(Color(uiColor: .systemGroupedBackground))
        .environment(\.colorScheme, scheme)
        .environment(\.dynamicTypeSize, size)
    }

    private func composer(enabled: Bool, updating: Bool) -> some View {
        ChatComposerSurface(input: .constant(""), mentions: [], references: [], replyTitle: nil,
            busy: false, canSend: false, voiceRecording: false, canRecord: true,
            canAddAttachments: true, canReferenceData: true, onAdd: { _ in onUnexpectedAction() },
            onMention: onUnexpectedAction, onReference: onUnexpectedAction, onInbox: onUnexpectedAction,
            onSend: onUnexpectedAction, onVoice: onUnexpectedAction, onRemoveMention: { _ in onUnexpectedAction() },
            onRemoveReference: { _ in onUnexpectedAction() }, onCancelReply: onUnexpectedAction,
            autoReviewEnabled: enabled, autoReviewUpdating: updating, autoReviewAvailable: true,
            onAutoReviewChange: { _ in onUnexpectedAction() }) { EmptyView() }
    }
}
