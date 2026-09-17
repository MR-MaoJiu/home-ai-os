import XCTest
import Security
@testable import HomeAI

@MainActor
final class CertificateTests: XCTestCase {
    private func validate(_ name: String) async throws -> Bool {
        let url = try XCTUnwrap(Bundle(for: Self.self).url(forResource: name, withExtension: "der"))
        let data = try Data(contentsOf: url)
        let date = Date(timeIntervalSince1970: 1_789_632_000)
        return await Task.detached {
            guard let certificate = SecCertificateCreateWithData(nil, data as CFData) else { return false }
            return PinnedSession.pinnedCertificateIsValid(certificate, at: date)
        }.value
    }
    func testValidPinnedCertificate() async throws {
        let valid = try await validate("valid")
        XCTAssertTrue(valid)
    }
    func testExpiredPinnedCertificateIsRejected() async throws {
        let valid = try await validate("expired")
        XCTAssertFalse(valid)
    }
    func testFuturePinnedCertificateIsRejected() async throws {
        let valid = try await validate("future")
        XCTAssertFalse(valid)
    }
}
