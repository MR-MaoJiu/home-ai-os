import XCTest
import CryptoKit
@testable import HomeAI

final class ServerIdentityTests: XCTestCase {
    func testRealCertificateRotationAndLegacyUpgrade() async throws {
        let environment = ProcessInfo.processInfo.environment
        guard let path = environment["HOMEAI_IDENTITY_FIXTURE"] ?? environment["TEST_RUNNER_HOMEAI_IDENTITY_FIXTURE"] else {
            throw XCTSkip("需要三个真实 TLS 验收端点，不以网络替身验证身份")
        }
        struct Fixture: Decodable { let pairing: PairingCode; let legacy: PairingCode; let renewed: String; let mismatch: String }
        let fixture = try JSONDecoder().decode(Fixture.self, from: Data(contentsOf: URL(fileURLWithPath: path)))
        let nonce = String(repeating: "ab", count: 32)
        let observer = PinnedSession(fingerprint: fixture.pairing.fingerprint)
        let transport = URLSession(configuration: .ephemeral, delegate: observer, delegateQueue: nil)
        defer { transport.invalidateAndCancel() }
        let proofURL = try XCTUnwrap(URL(string: fixture.pairing.url + "/api/v1/server/identity?nonce=" + nonce))
        let (proofData, _) = try await transport.data(from: proofURL)
        let publicKey = try XCTUnwrap(fixture.pairing.server_public_key)
        let identifier = try XCTUnwrap(fixture.pairing.server_id)
        XCTAssertThrowsError(try ServerTrust.validate(proofData, nonce: String(repeating: "cd", count: 32), peerFingerprint: fixture.pairing.fingerprint, serverID: identifier, publicKey: publicKey))
        XCTAssertThrowsError(try ServerTrust.validate(proofData, nonce: nonce, peerFingerprint: fixture.pairing.fingerprint, serverID: identifier, publicKey: publicKey, now: Date().addingTimeInterval(120)))
        let api = APIClient(persistConnection: false)
        try await api.pair(fixture.pairing)
        let before = try await api.syncNamespace()
        let bound = await api.trustInfo()
        XCTAssertEqual(bound?.bound, true)
        do {
            try await api.verifyServerAddress(fixture.mismatch)
            XCTFail("签名证书与实际 TLS 端点不符时必须拒绝")
        } catch { XCTAssertTrue(error.localizedDescription.contains("证书"), error.localizedDescription) }
        let unchanged = await api.trustInfo()
        XCTAssertEqual(unchanged?.url, fixture.pairing.url)
        try await api.verifyServerAddress(fixture.renewed)
        let after = try await api.syncNamespace()
        XCTAssertEqual(before, after)
        _ = try await api.request("GET", "/api/v1/me", expectedNamespace: before)
        let changed = await api.trustInfo()
        XCTAssertEqual(changed?.url, fixture.renewed)
        let legacy = APIClient(persistConnection: false)
        try await legacy.pair(fixture.legacy)
        let legacyBefore = try await legacy.syncNamespace()
        try await legacy.enrollServerIdentity()
        try await legacy.verifyServerAddress(fixture.renewed)
        let legacyAfter = try await legacy.syncNamespace()
        XCTAssertEqual(legacyBefore, legacyAfter)
        _ = try await legacy.request("GET", "/api/v1/me", expectedNamespace: legacyBefore)
        do {
            _ = try await ServerTrust.probe(url: fixture.renewed, serverID: try XCTUnwrap(fixture.pairing.server_id), publicKey: P256.Signing.PrivateKey().publicKey.pemRepresentation)
            XCTFail("不同身份密钥必须被拒绝")
        } catch { XCTAssertTrue(error.localizedDescription.contains("签名"), error.localizedDescription) }
    }
}
