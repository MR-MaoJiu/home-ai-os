import XCTest
import ImageIO
import UniformTypeIdentifiers
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
