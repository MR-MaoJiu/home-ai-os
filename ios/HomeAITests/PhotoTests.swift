import XCTest
import ImageIO
import UniformTypeIdentifiers
import SwiftUI
import WebKit
@testable import HomeAI

final class PhotoTests: XCTestCase {
    @MainActor
    func testRealPhotoPreparationAndAnalysis() async throws {
        let environment = ProcessInfo.processInfo.environment
        guard let pairPath = environment["HOMEAI_VISION_PAIR_FILE"] ?? environment["TEST_RUNNER_HOMEAI_VISION_PAIR_FILE"],
              let imagePath = environment["HOMEAI_VISION_SAMPLE"] ?? environment["TEST_RUNNER_HOMEAI_VISION_SAMPLE"] else {
            throw XCTSkip("需要真实视觉服务、图片及隔离配对文件")
        }
        let original = try Data(contentsOf: URL(fileURLWithPath: imagePath))
        let originalSource = try XCTUnwrap(CGImageSourceCreateWithData(original as CFData, nil))
        let tagged = NSMutableData()
        let destination = try XCTUnwrap(CGImageDestinationCreateWithData(tagged, UTType.jpeg.identifier as CFString, 1, nil))
        CGImageDestinationAddImageFromSource(destination, originalSource, 0, [
            kCGImagePropertyGPSDictionary: [kCGImagePropertyGPSLatitude: 30.0, kCGImagePropertyGPSLatitudeRef: "N"],
            kCGImagePropertyExifDictionary: [kCGImagePropertyExifUserComment: "PRIVATE-METADATA-TEST"],
            kCGImagePropertyTIFFDictionary: [kCGImagePropertyTIFFMake: "PRIVATE-DEVICE-TEST"]
        ] as CFDictionary)
        XCTAssertTrue(CGImageDestinationFinalize(destination))
        let jpeg = try PhotoPreparation.jpeg(tagged as Data)
        let source = try XCTUnwrap(CGImageSourceCreateWithData(jpeg as CFData, nil))
        let properties = try XCTUnwrap(CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as? [CFString: Any])
        XCTAssertNil(properties[kCGImagePropertyGPSDictionary])
        let exif = properties[kCGImagePropertyExifDictionary] as? [String: Any] ?? [:]
        XCTAssertTrue(Set(exif.keys).isSubset(of: ["ColorSpace", "PixelXDimension", "PixelYDimension"]))
        let tiff = properties[kCGImagePropertyTIFFDictionary] as? [String: Any] ?? [:]
        XCTAssertNil(tiff["Make"])
        XCTAssertLessThanOrEqual(try XCTUnwrap(properties[kCGImagePropertyPixelWidth] as? Int), 1536)
        struct Fixture: Decodable { let pairing: PairingCode }
        let fixture = try JSONDecoder().decode(Fixture.self, from: Data(contentsOf: URL(fileURLWithPath: pairPath)))
        let api = APIClient(persistConnection: false)
        try await api.pair(fixture.pairing)
        let namespace = try await api.syncNamespace()
        let body = try JSONSerialization.data(withJSONObject: ["records": [["source": "photos", "source_id": UUID().uuidString,
            "kind": "photo.selected", "version": 1, "payload": ["name": "公开自然照片验收", "content_base64": jpeg.base64EncodedString()]]]])
        struct Records: Decodable { let records: [DataEntry] }
        let records = try JSONDecoder().decode(Records.self, from: await api.request("POST", "/api/v1/data/sync", body: body, expectedNamespace: namespace))
        let record = try XCTUnwrap(records.records.first)
        let request = try JSONSerialization.data(withJSONObject: ["capability": "photo.analyze@v1", "idempotency_key": UUID().uuidString,
            "step_timeout_seconds": 180, "arguments": ["record_id": record.id, "question": "用中文描述图片中可见的自然景物。"]])
        let task = try JSONDecoder().decode(TaskCreated.self, from: await api.request("POST", "/api/v1/tasks", body: request, expectedNamespace: namespace))
        _ = try await api.request("POST", "/_test/run/" + task.id, expectedNamespace: namespace)
        let result = try JSONDecoder().decode(TaskResult.self, from: await api.request("GET", "/api/v1/tasks/" + task.id, expectedNamespace: namespace))
        XCTAssertEqual(result.status, "SUCCEEDED", result.error ?? "")
        guard case .object(let fields) = result.result, case .string(let text) = fields["text"] else { return XCTFail("缺少模型分析") }
        XCTAssertTrue(text.contains("山"), text)
        _ = try await api.request("DELETE", "/api/v1/data/" + record.id, expectedNamespace: namespace)
        let deleted = try JSONDecoder().decode(TaskResult.self, from: await api.request("GET", "/api/v1/tasks/" + task.id, expectedNamespace: namespace))
        XCTAssertNil(deleted.result)
    }
}

final class RecordDetailTests: XCTestCase {
    func testNestedFieldsPreserveFullTextAndAllArrayItems() {
        let long = String(repeating: "完整内容\n", count: 3000)
        let value = JSONValue.object(["text": .string(long), "measurements": .array([.object(["value": .number(1)]), .object(["value": .number(2)])])])
        let root = RecordFieldNode(name: "记录", value: value, path: "/record")
        XCTAssertEqual(root.children?.count, 2)
        XCTAssertEqual(root.children?.first(where: { $0.name == "text" })?.text, long)
        let measurements = root.children?.first(where: { $0.name == "measurements" })
        XCTAssertEqual(measurements?.children?.count, 2)
        XCTAssertEqual(measurements?.children?.last?.children?.first?.text, "2")
    }

    func testNestedBinaryFieldUsesAttachmentPathRatherThanEncodedText() {
        let encoded = Data("actual attachment".utf8).base64EncodedString()
        let payload: [String: JSONValue] = ["a/b": .array([.object(["content_base64": .string(encoded)])])]
        XCTAssertEqual(RecordAttachment.encodedValue(path: "/a~1b/0/content_base64", payload: payload), encoded)
        let node = RecordFieldNode(name: "content_base64", value: .string(encoded), path: "/a~1b/0/content_base64")
        XCTAssertTrue(node.isAttachment)
        XCTAssertNil(node.text)
    }

    func testWebContentDecodesAsCompleteTextBeforeOptionalHTMLPreview() throws {
        let html = Data("<html><img src='https://example.invalid/private'></html>".utf8)
        XCTAssertEqual(RecordAttachment.format(name: "disguised.docx", data: html), .text)
        XCTAssertEqual(RecordAttachment.format(name: "image.svg", data: Data("<svg/>".utf8)), .text)
        let disguised = Data([0xef, 0xbb, 0xbf]) + Data("<!-- comment --><svg><image href='https://example.invalid/a'/></svg>".utf8)
        XCTAssertEqual(RecordAttachment.format(name: "disguised.doc", data: disguised), .text)
        XCTAssertEqual(RecordAttachment.format(name: "invalid.docx", data: Data([0x01, 0x00, 0x02])), .unsupported)
        XCTAssertEqual(RecordAttachment.text(html), String(data: html, encoding: .utf8))
        XCTAssertEqual(RecordAttachment.format(name: "unknown.bin", data: Data([0xff, 0x01])), .unsupported)
    }

    func testOnlyDeclaredHTMLGetsWebpageModeAndKeepsOriginalBytes() throws {
        XCTAssertTrue(RecordAttachment.isHTML(name: "页面.HTML"))
        XCTAssertTrue(RecordAttachment.isHTML(name: "页面.htm"))
        XCTAssertTrue(RecordAttachment.isHTML(name: "页面.xhtml"))
        XCTAssertTrue(RecordAttachment.isHTML(name: "附件", mime: " Text/HTML; charset=utf-8"))
        XCTAssertTrue(RecordAttachment.isHTML(name: "附件", mime: "application/xhtml+xml"))
        XCTAssertFalse(RecordAttachment.isHTML(name: "image.svg", mime: "image/svg+xml"))
        XCTAssertFalse(RecordAttachment.isHTML(name: "disguised.docx"))
        let source = "<!doctype html><html><body>完整源码与网页切换</body></html>"
        let bytes = Data(source.utf8)
        let preview = try ProtectedRecordPreview(data: bytes, name: "页面.html")
        defer { preview.remove() }
        XCTAssertTrue(preview.isHTML)
        XCTAssertEqual(preview.format, .text)
        XCTAssertEqual(preview.text, source)
        XCTAssertEqual(preview.contents, bytes)
        XCTAssertEqual(try Data(contentsOf: preview.url), bytes)
    }

    func testLegacyHTMLTextAndMissingMetadataUseDocumentDetection() {
        let html = "\u{FEFF} <!-- 文档说明 -->\n<!DOCTYPE HTML><html><body>完整原件</body></html>"
        XCTAssertTrue(RecordTextContent(title: "文件全文", text: html).isHTML)
        XCTAssertTrue(RecordAttachment.isHTML(name: "无扩展名资料", mime: "application/octet-stream", data: Data(html.utf8)))
        XCTAssertTrue(RecordAttachment.isHTML(name: "原文.txt", data: Data(html.utf8)))
        XCTAssertFalse(RecordAttachment.isHTML(name: "伪装.docx", data: Data(html.utf8)))
        XCTAssertFalse(RecordTextContent(title: "文件全文", text: "# 解析标题\n这是已转换的 Markdown").isHTML)
        XCTAssertFalse(RecordTextContent(title: "说明", text: "如何使用 <html> 标签").isHTML)
        XCTAssertFalse(RecordTextContent(title: "图形", text: "<svg></svg>").isHTML)
    }

    @MainActor
    func testLegacyFullTextEntryShowsHTMLModePicker() async throws {
        let source = "<!doctype html><html><body>旧全文入口</body></html>"
        let controller = UIHostingController(rootView: RecordLongTextView(content: RecordTextContent(title: "文件全文", text: source)))
        let scene = try XCTUnwrap(UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }.first)
        let window = UIWindow(windowScene: scene); window.rootViewController = controller; window.isHidden = false
        defer { window.isHidden = true; window.rootViewController = nil }
        func picker(_ view: UIView) -> UISegmentedControl? {
            if let control = view as? UISegmentedControl { return control }
            return view.subviews.compactMap(picker).first
        }
        for _ in 0..<40 {
            controller.view.layoutIfNeeded()
            if picker(controller.view) != nil { break }
            try await Task.sleep(for: .milliseconds(100))
        }
        let control = try XCTUnwrap(picker(controller.view))
        XCTAssertEqual(control.numberOfSegments, 2)
        XCTAssertEqual(control.titleForSegment(at: 0), "代码")
        XCTAssertEqual(control.titleForSegment(at: 1), "网页")
    }

    @MainActor
    func testActualImageBytesProducePhotoPreview() throws {
        let png = try XCTUnwrap(Data(base64Encoded: "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4z8DwHwAFAAH/iZk9HQAAAABJRU5ErkJggg=="))
        XCTAssertNotNil(RecordAttachment.image(png))
        XCTAssertEqual(RecordAttachment.format(name: "照片", data: png), .quickLook)
        XCTAssertTrue(RecordAttachment.safeName("照片", data: png).hasSuffix(".png"))
        let longName = RecordAttachment.safeName(String(repeating: "中文文件", count: 100) + ".pdf", data: Data("%PDF-1.7".utf8))
        XCTAssertLessThanOrEqual(longName.utf8.count, 201)
        XCTAssertTrue(longName.hasSuffix(".pdf"))
    }

    func testProtectedPreviewCleanupAndExportBytesAreIndependent() throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: root) }
        let bytes = Data("原始文件的完整内容".utf8)
        let preview = try ProtectedRecordPreview(data: bytes, name: "../../private/file.txt", temporaryRoot: root)
        XCTAssertTrue(preview.url.standardizedFileURL.path.hasPrefix(root.standardizedFileURL.path + "/"))
        XCTAssertEqual(try Data(contentsOf: preview.url), bytes)
        #if !targetEnvironment(simulator)
        // 模拟器宿主文件系统不返回 iOS 数据保护属性；真机仍必须严格验证完整保护。
        let attributes = try FileManager.default.attributesOfItem(atPath: preview.url.path)
        XCTAssertEqual(attributes[.protectionKey] as? String, FileProtectionType.complete.rawValue)
        #endif
        let export = RecordExportDocument(data: preview.contents)
        preview.remove()
        XCTAssertFalse(FileManager.default.fileExists(atPath: preview.url.path))
        XCTAssertEqual(export.data, bytes)
    }
}

extension RecordDetailTests {
    @MainActor
    func testExistingHTMLFileShowsCodeAndWebpageModes() async throws {
        let env = ProcessInfo.processInfo.environment
        guard env["HOMEAI_READONLY_HTML_CHECK"] == "1" || env["TEST_RUNNER_HOMEAI_READONLY_HTML_CHECK"] == "1" else {
            throw XCTSkip("仅在用户报告 HTML 预览问题的已配对设备上只读验收")
        }
        let api = AppServices.api
        await api.restoreConnectionIfNeeded()
        let namespace = try await api.syncNamespace()
        let page = try JSONDecoder().decode(DataPage.self, from: await api.request("GET", "/api/v1/data?limit=100", expectedNamespace: namespace))
        let record = try XCTUnwrap(page.records.first { row in
            ["document.file", "document.import"].contains(row.kind) && RecordAttachment.isHTML(name: row.payload["name"]?.description ?? row.title, mime: row.payload["mime_type"]?.description)
        }, "没有找到当前成员可见的 HTML 附件")
        let data = try await api.request("GET", "/api/v1/files/" + record.id + "/content", expectedNamespace: namespace)
        let preview = try ProtectedRecordPreview(data: data, name: record.payload["name"]?.description ?? record.title, mime: record.payload["mime_type"]?.description)
        defer { preview.remove() }
        XCTAssertTrue(preview.isHTML && preview.text != nil, "原件必须被识别为可解码 HTML")
        let controller = UIHostingController(rootView: RecordFilePreview(item: preview))
        let scene = try XCTUnwrap(UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }.first)
        let window = UIWindow(windowScene: scene); window.rootViewController = controller; window.isHidden = false
        defer { window.isHidden = true; window.rootViewController = nil }
        func find<T: UIView>(_ type: T.Type, in view: UIView) -> T? {
            if let matched = view as? T { return matched }
            return view.subviews.compactMap { find(type, in: $0) }.first
        }
        for _ in 0..<50 {
            controller.view.layoutIfNeeded()
            if find(UISegmentedControl.self, in: controller.view) != nil { break }
            try await Task.sleep(for: .milliseconds(100))
        }
        let picker = try XCTUnwrap(find(UISegmentedControl.self, in: controller.view))
        XCTAssertTrue(find(UITextView.self, in: controller.view)?.text == preview.text, "源码预览必须保留原文；不在测试日志中输出正文")
        XCTAssertEqual(picker.selectedSegmentIndex, 0)
        picker.selectedSegmentIndex = 1; picker.sendActions(for: .valueChanged)
        var rendered = false
        for _ in 0..<80 {
            controller.view.layoutIfNeeded()
            if let web = find(WKWebView.self, in: controller.view),
               let ready = try? await web.evaluateJavaScript("document.readyState === 'complete' && document.body.children.length > 0"), ready as? Bool == true {
                XCTAssertFalse(web.configuration.defaultWebpagePreferences.allowsContentJavaScript)
                rendered = true; break
            }
            try await Task.sleep(for: .milliseconds(100))
        }
        XCTAssertTrue(rendered, "实际 HTML 原件应进入网页模式")
        print("HTML_EXISTING_ATTACHMENT_PREVIEW_OK code_switch=true webpage_loaded=true private_content_logged=false")
    }

    @MainActor
    func testExistingRealAttachmentReadOnly() async throws {
        let env = ProcessInfo.processInfo.environment
        guard env["HOMEAI_READONLY_RECORD_CHECK"] == "1" || env["TEST_RUNNER_HOMEAI_READONLY_RECORD_CHECK"] == "1" else {
            throw XCTSkip("只在用户授权的原配对设备上读取已有真实资料")
        }
        let api = AppServices.api
        await api.restoreConnectionIfNeeded()
        let namespace = try await api.syncNamespace()
        let page = try JSONDecoder().decode(DataPage.self, from: await api.request("GET", "/api/v1/data?limit=20", expectedNamespace: namespace))
        guard let summary = page.records.first(where: { ["document.file", "document.import", "photo.selected"].contains($0.kind) }) else {
            throw XCTSkip("当前没有已有真实附件，不向正式库写入演示数据")
        }
        let record = try JSONDecoder().decode(DataEntry.self, from: await api.request("GET", "/api/v1/data/" + summary.id, expectedNamespace: namespace))
        let content = try await api.request("GET", "/api/v1/files/" + record.id + "/content", expectedNamespace: namespace)
        if let digest = record.payload["sha256"]?.description, digest.count == 64 { XCTAssertEqual(DeviceIdentity.hash(content), digest.lowercased()) }
        if record.kind == "photo.selected" { XCTAssertNotNil(RecordAttachment.image(content)) }
        else {
            struct Details: Decodable { let record_id: String; let parsed: DataEntry?; let parse_status: String }
            let details = try JSONDecoder().decode(Details.self, from: await api.request("GET", "/api/v1/files/" + record.id + "/details", expectedNamespace: namespace))
            XCTAssertEqual(details.record_id, record.id)
            XCTAssertTrue(["ready", "stale", "not_available"].contains(details.parse_status))
            if details.parse_status == "ready" { XCTAssertEqual(details.parsed?.kind, "document.parsed") }
        }
        print("EXISTING_ATTACHMENT_READ_OK content_bytes=\(content.count)")
    }
}
