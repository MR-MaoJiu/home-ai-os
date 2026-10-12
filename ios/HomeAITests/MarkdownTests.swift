import XCTest
import Foundation
import SwiftUI
import UIKit
import WebKit
@testable import HomeAI

final class MarkdownTests: XCTestCase {
    private func flatten(_ blocks: [ChatMarkdownDocument.Block]) -> [ChatMarkdownDocument.Block] {
        blocks.flatMap { [$0] + flatten($0.children) }
    }

    private func text(_ document: ChatMarkdownDocument) -> String {
        flatten(document.blocks).map { String($0.text.characters) }.joined(separator: "\n")
    }

    func testHeadingsNestedListsQuotesCodeAndTablesPreserveNativeBlocks() {
        let document = ChatMarkdownDocument("""
        # 家庭计划

        1. 第一项
           - 嵌套项目
        2. 第二项

        > 引用内容

        ```swift
        let literal = "**不应加粗**"
        ```

        | 项目 | 状态 |
        | --- | --- |
        | 家庭服务 | 在线 |
        """)
        let kinds = flatten(document.blocks).compactMap(\.kind)
        XCTAssertTrue(kinds.contains { if case .header = $0 { return true }; return false })
        XCTAssertTrue(kinds.contains { if case .orderedList = $0 { return true }; return false })
        XCTAssertTrue(kinds.contains { if case .unorderedList = $0 { return true }; return false })
        XCTAssertTrue(kinds.contains { if case .blockQuote = $0 { return true }; return false })
        XCTAssertTrue(kinds.contains { if case .codeBlock = $0 { return true }; return false })
        XCTAssertTrue(kinds.contains { if case .table = $0 { return true }; return false })
        XCTAssertTrue(text(document).contains("**不应加粗**"))
        XCTAssertTrue(text(document).contains("家庭服务"))
        XCTAssertTrue(text(document).contains("嵌套项目"))
    }

    func testInlineEmphasisAndCodeRemainAttributed() {
        let document = ChatMarkdownDocument("**重点**，*强调*，`原生代码`。")
        let attributes = flatten(document.blocks).flatMap { Array($0.text.runs).compactMap(\.inlinePresentationIntent) }
        XCTAssertTrue(attributes.contains { $0.contains(.stronglyEmphasized) })
        XCTAssertTrue(attributes.contains { $0.contains(.emphasized) })
        XCTAssertTrue(attributes.contains { $0.contains(.code) })
    }

    func testSafeURLsRequireWebSchemeHostAndNoCredentials() throws {
        for value in ["https://example.com/说明?q=1", "http://example.com/path"] {
            XCTAssertNotNil(ChatMarkdownDocument.safeURL(try XCTUnwrap(URL(string: value))))
        }
        for value in ["javascript:alert(1)", "data:text/html,test", "file:///private/data", "https://user:password@example.com/", "https://user@example.com/", "/relative/path", "https:relative"] {
            XCTAssertNil(ChatMarkdownDocument.safeURL(try XCTUnwrap(URL(string: value))), value)
        }
    }

    func testUnsafeLinksAndReferenceImagesNeverExposeImageLoadingAttributes() {
        let document = ChatMarkdownDocument("""
        [安全网页](https://example.com/docs)
        [危险脚本](javascript:alert(1))
        [本机文件](file:///private/data)
        ![架构图](https://example.com/architecture.png)
        ![引用图片][image]
        ![危险图片](data:image/png;base64,AA)

        [image]: https://example.com/reference.png
        """)
        let blocks = flatten(document.blocks)
        let runs = blocks.flatMap { Array($0.text.runs) }
        XCTAssertTrue(runs.allSatisfy { $0.imageURL == nil })
        let links = runs.compactMap(\.link)
        XCTAssertTrue(links.allSatisfy { ChatMarkdownDocument.safeURL($0) != nil })
        XCTAssertTrue(links.contains { $0.absoluteString == "https://example.com/docs" })
        XCTAssertTrue(links.contains { $0.absoluteString == "https://example.com/architecture.png" })
        XCTAssertTrue(links.contains { $0.absoluteString == "https://example.com/reference.png" })
        XCTAssertTrue(text(document).contains("架构图"))
        XCTAssertTrue(text(document).contains("引用图片"))
    }

    func testHTMLRemainsReadableInertText() {
        let source = "<script>alert('内容')</script>\n<img src=\"https://example.com/pixel\">"
        let document = ChatMarkdownDocument(source)
        let visible = text(document)
        XCTAssertTrue(visible.contains("<script>"))
        XCTAssertTrue(visible.contains("alert('内容')"))
        XCTAssertTrue(visible.contains("<img"))
        XCTAssertTrue(flatten(document.blocks).flatMap { Array($0.text.runs) }.allSatisfy { $0.imageURL == nil })
    }

    func testOnlyExplicitHTMLCodeBlocksOfferWebpagePreview() throws {
        let source = "<main><h1>完整 HTML 源码</h1></main>"
        for language in ["html", "HTM", "xhtml", " HTML "] {
            let preview = try XCTUnwrap(ChatHTMLCodePreview.content(language: language, text: source))
            XCTAssertTrue(preview.isHTML)
            XCTAssertEqual(preview.text, source)
        }
        for language: String? in [nil, "", "text", "swift", "svg", "html+javascript", "javascript"] {
            XCTAssertNil(ChatHTMLCodePreview.content(language: language, text: source))
        }
        let document = ChatMarkdownDocument("```html\n" + source + "\n```")
        let code = try XCTUnwrap(flatten(document.blocks).first { if case .codeBlock? = $0.kind { return true }; return false })
        guard case .codeBlock(let language)? = code.kind else { return XCTFail("HTML 围栏应保留代码块语言") }
        XCTAssertNotNil(ChatHTMLCodePreview.content(language: language, text: String(code.text.characters)))
    }

    @MainActor
    func testH5CardOffersSourceSwitchAndKeepsItsLocalInteractions() async throws {
        let html = "<div id='h5-title'>原始内容</div><script>document.getElementById('h5-title').textContent='本地交互生效';</script>"
        let state = AppState(api: APIClient(persistConnection: false))
        let controller = UIHostingController(rootView: IsolatedH5View(title: "H5 卡片", html: html).environment(state))
        let scene = try XCTUnwrap(UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }.first)
        let window = UIWindow(windowScene: scene); window.rootViewController = controller; window.isHidden = false
        defer { window.isHidden = true; window.rootViewController = nil }
        func find<T: UIView>(_ type: T.Type, in view: UIView) -> T? {
            if let matched = view as? T { return matched }
            return view.subviews.compactMap { find(type, in: $0) }.first
        }
        var result: String?
        for _ in 0..<80 {
            controller.view.layoutIfNeeded()
            if let web = find(WKWebView.self, in: controller.view) {
                result = try? await web.evaluateJavaScript("document.getElementById('h5-title')?.textContent") as? String
                if result == "本地交互生效" { break }
            }
            try await Task.sleep(for: .milliseconds(100))
        }
        XCTAssertEqual(result, "本地交互生效")
        let web = try XCTUnwrap(find(WKWebView.self, in: controller.view))
        XCTAssertTrue(web.configuration.defaultWebpagePreferences.allowsContentJavaScript)
        XCTAssertFalse(web.configuration.websiteDataStore.isPersistent)
        let picker = try XCTUnwrap(find(UISegmentedControl.self, in: controller.view))
        XCTAssertEqual(picker.selectedSegmentIndex, 1)
        picker.selectedSegmentIndex = 0; picker.sendActions(for: .valueChanged)
        for _ in 0..<40 {
            controller.view.layoutIfNeeded()
            if find(UITextView.self, in: controller.view)?.text == html { break }
            try await Task.sleep(for: .milliseconds(100))
        }
        XCTAssertEqual(find(UITextView.self, in: controller.view)?.text, html)
        XCTAssertNil(find(WKWebView.self, in: controller.view))
    }

    @MainActor
    func testTaskH5CannotExposeSourceBeforeItsAccessCheck() async throws {
        let state = AppState(api: APIClient(persistConnection: false))
        let controller = UIHostingController(rootView: IsolatedH5View(title: "任务内容", html: "<h1>待授权内容</h1>", taskID: UUID().uuidString).environment(state))
        let scene = try XCTUnwrap(UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }.first)
        let window = UIWindow(windowScene: scene); window.rootViewController = controller; window.isHidden = false
        defer { window.isHidden = true; window.rootViewController = nil }
        try await Task.sleep(for: .milliseconds(300)); controller.view.layoutIfNeeded()
        func containsPreview(_ view: UIView) -> Bool {
            view is UISegmentedControl || view is UITextView || view is WKWebView || view.subviews.contains(where: containsPreview)
        }
        XCTAssertFalse(containsPreview(controller.view))
    }

    func testChangedSourceAndEmptySourceDoNotRetainOldContent() {
        let old = ChatMarkdownDocument("# 旧标题\n\n旧正文")
        let updated = ChatMarkdownDocument("## 新标题\n\n新正文")
        XCTAssertTrue(text(old).contains("旧标题"))
        XCTAssertTrue(text(updated).contains("新标题"))
        XCTAssertFalse(text(updated).contains("旧标题"))
        XCTAssertTrue(text(ChatMarkdownDocument("")).trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
    }
}

extension MarkdownTests {
    private func turn(answer: String, parts: [ChatPart]?) -> StoredChatTurn {
        StoredChatTurn(id: "display-turn", sequence: 1, client_key: "display-key", user_text: "提问", task_id: "display-task", status: "SUCCEEDED",
                       assistant_text: answer, error: nil, approvals: [], web_sources: [], sources: [], assistant_parts: parts)
    }

    private func part(_ type: String, text: String? = nil) -> ChatPart {
        var value = ChatPart(type: type); value.text = text; return value
    }

    func testAnswerStillDisplaysWithEmptyPartsAndOnlyRichCards() {
        for parts: [ChatPart]? in [nil, [], [part("h5")]] {
            let rendered = turn(answer: "**完整回答**", parts: parts).displayedAssistantParts
            XCTAssertEqual(rendered.first?.type, "text")
            XCTAssertEqual(rendered.first?.text, "**完整回答**")
            XCTAssertEqual(rendered.count, 1 + (parts?.count ?? 0))
        }
    }

    func testDuplicateMatchingAnswerFoldsOnceWithoutDroppingCardsOrOtherText() {
        let parts = [part("h5"), part("text", text: "完整回答"), part("text", text: "额外说明"), part("text", text: "完整回答"), part("image")]
        let rendered = turn(answer: "完整回答", parts: parts).displayedAssistantParts
        XCTAssertEqual(rendered.map(\.type), ["h5", "text", "text", "image"])
        XCTAssertEqual(rendered.compactMap(\.text), ["完整回答", "额外说明"])
    }

    func testDifferentTextAndWhitespaceArePreservedExactly() {
        let rendered = turn(answer: "原文", parts: [part("text", text: " 原文 "), part("text", text: "另一段")]).displayedAssistantParts
        XCTAssertEqual(rendered.compactMap(\.text), ["原文", " 原文 ", "另一段"])
        XCTAssertTrue(turn(answer: "", parts: []).displayedAssistantParts.isEmpty)
        XCTAssertEqual(turn(answer: "", parts: [part("h5")]).displayedAssistantParts.map(\.type), ["h5"])
    }
}


extension MarkdownTests {
    func testRevokedCachedTurnCannotRenderOldMarkdownOrRichCards() {
        var original = turn(answer: "# 临时私人正文", parts: [part("text", text: "# 临时私人正文"), part("h5")])
        original.requires_online_revalidation = true
        let sanitized = original.cached()
        let displayed = sanitized.displayedAssistantParts
        XCTAssertEqual(sanitized.requires_online_revalidation, true)
        XCTAssertTrue(displayed.allSatisfy { $0.type == "text" })
        XCTAssertFalse(displayed.compactMap(\.text).joined().contains("临时私人正文"))
        XCTAssertFalse(text(ChatMarkdownDocument(displayed.compactMap(\.text).joined())).contains("临时私人正文"))
    }

    @MainActor
    func testNativeMarkdownScreenshotsForLightDarkAndLargeType() async throws {
        let scene = try XCTUnwrap(UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }.first)
        let samples: [(String, ColorScheme, DynamicTypeSize)] = [
            ("markdown-light", .light, .large),
            ("markdown-dark", .dark, .large),
            ("markdown-dark-accessibility", .dark, .accessibility2)
        ]
        for (name, scheme, size) in samples {
            let controller = UIHostingController(rootView: MarkdownSamplePage(scheme: scheme, size: size))
            controller.overrideUserInterfaceStyle = scheme == .dark ? .dark : .light
            let window = UIWindow(windowScene: scene)
            window.frame = CGRect(x: 0, y: 0, width: 390, height: 1180)
            window.rootViewController = controller
            window.isHidden = false
            defer { window.isHidden = true; window.rootViewController = nil }
            controller.view.frame = window.bounds
            controller.view.layoutIfNeeded()
            try await Task.sleep(for: .milliseconds(600))
            controller.view.layoutIfNeeded()
            let format = UIGraphicsImageRendererFormat(); format.scale = 2
            var rendered = false
            let image = UIGraphicsImageRenderer(bounds: controller.view.bounds, format: format).image { _ in
                rendered = controller.view.drawHierarchy(in: controller.view.bounds, afterScreenUpdates: true)
            }
            XCTAssertTrue(rendered)
            XCTAssertEqual(image.size.width, 390, accuracy: 1)
            let attachment = XCTAttachment(image: image)
            attachment.name = name
            attachment.lifetime = .keepAlways
            add(attachment)
        }
    }
}

private struct MarkdownSamplePage: View {
    let scheme: ColorScheme
    let size: DynamicTypeSize
    var sourceOverride: String? = nil
    private let sample = """
    # 家庭服务周报

    这是一段 **加粗内容**、*斜体说明* 和 `行内代码`，支持选择与复制。

    ## 今日计划

    1. 检查服务状态
       - 阅读共享资料
       - 查看执行结果
    2. 整理聊天记忆

    > 客户端展示内容，任务由家庭服务器执行。

    ### 代码示例

    ```swift
    let message = "这段很长的代码可以在区域内部横向滚动，不应撑宽整条消息或溢出屏幕"
    print(message)
    ```

    | 模块 | 状态 | 说明 |
    | :--- | :---: | ---: |
    | 家庭服务 | 在线 | 资料仅按授权可见 |
    | 客户端 | 已连接 | 支持动态字体与深色模式 |

    [查看说明](https://example.com/docs) · ![架构示意图](https://example.com/diagram.png)
    """
    private let accessibilitySample = """
    # 大字体 Markdown

    **重要内容**仍可清楚阅读，代码与表格在各自区域横向滚动。

    ```swift
    let message = "这条长代码应在代码框内横向滚动，不撑宽页面"
    print(message)
    ```

    | 项目 | 状态 | 说明 |
    | --- | --- | --- |
    | 服务 | 在线 | 已授权 |
    | 文件 | 完整 | 可阅读 |

    [安全网页链接](https://example.com/docs)
    """
    var body: some View {
        ScrollView {
            ChatMarkdownText(sourceOverride ?? (size.isAccessibilitySize ? accessibilitySample : sample))
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(18)
        }
        .background(Color(uiColor: .systemBackground))
        .environment(\.colorScheme, scheme)
        .environment(\.dynamicTypeSize, size)
    }
}


extension MarkdownTests {
    @MainActor
    func testLiteralAndEscapedCheckboxMarkersPreserveVisibleCharacters() async throws {
        let source = #"""
        ## 字面标记
        - `[x] literal code`
        - \[x\] escaped literal
        - [ ] ordinary marker
        """#
        let document = ChatMarkdownDocument(source)
        let visible = text(document)
        XCTAssertTrue(visible.contains("[x] literal code"))
        XCTAssertTrue(visible.contains("[x] escaped literal"))
        XCTAssertTrue(visible.contains("[ ] ordinary marker"))
        let codeRuns = flatten(document.blocks).flatMap { Array($0.text.runs) }.filter { $0.inlinePresentationIntent?.contains(.code) == true }
        XCTAssertFalse(codeRuns.isEmpty)
        // 同时保留真实视图截图，覆盖列表渲染阶段误裁字面前缀的问题。
        let scene = try XCTUnwrap(UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }.first)
        let controller = UIHostingController(rootView: MarkdownSamplePage(scheme: .light, size: .large, sourceOverride: source))
        let window = UIWindow(windowScene: scene)
        window.frame = CGRect(x: 0, y: 0, width: 390, height: 480)
        window.rootViewController = controller; window.isHidden = false
        defer { window.isHidden = true; window.rootViewController = nil }
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
        attachment.name = "markdown-literal-markers"
        attachment.lifetime = .keepAlways
        add(attachment)
    }

    func testImageWithoutAltUsesReadableLinkPlaceholder() {
        let document = ChatMarkdownDocument("![](https://example.com/no-alt.png)")
        XCTAssertTrue(text(document).contains("查看图片链接"))
        XCTAssertFalse(text(document).contains("\u{FFFC}"))
        let runs = flatten(document.blocks).flatMap { Array($0.text.runs) }
        XCTAssertTrue(runs.allSatisfy { $0.imageURL == nil })
        XCTAssertTrue(runs.compactMap(\.link).contains { $0.absoluteString == "https://example.com/no-alt.png" })
    }
}
