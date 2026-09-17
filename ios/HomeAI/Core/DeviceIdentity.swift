import Foundation
import CryptoKit
import Security

/// 私钥在真机 Secure Enclave 内生成；模拟器使用 Keychain 保存的测试私钥。
final class DeviceIdentity: @unchecked Sendable {
    private let tag = "dev.homeai.device-key"

    static func read(_ name: String) -> Data? {
        let query: [String: Any] = [kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: "dev.homeai", kSecAttrAccount as String: name, kSecReturnData as String: true]
        var result: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &result) == errSecSuccess else { return nil }
        return result as? Data
    }

    static func save(_ data: Data, name: String) throws {
        let query: [String: Any] = [kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: "dev.homeai", kSecAttrAccount as String: name]
        let attributes: [String: Any] = [kSecValueData as String: data, kSecAttrAccessible as String: kSecAttrAccessibleWhenUnlockedThisDeviceOnly]
        let status = SecItemUpdate(query as CFDictionary, attributes as CFDictionary)
        if status == errSecItemNotFound {
            let created = SecItemAdd(query.merging(attributes) { _, new in new } as CFDictionary, nil)
            guard created == errSecSuccess else { throw IdentityError.keychain(created) }
        } else if status != errSecSuccess { throw IdentityError.keychain(status) }
    }

    enum IdentityError: Error { case keychain(OSStatus) }

    #if targetEnvironment(simulator)
    private func key() throws -> P256.Signing.PrivateKey {
        if let data = Self.read(tag) { return try P256.Signing.PrivateKey(rawRepresentation: data) }
        let key = P256.Signing.PrivateKey()
        try Self.save(key.rawRepresentation, name: tag)
        return key
    }
    #else
    private func key() throws -> SecureEnclave.P256.Signing.PrivateKey {
        if let data = Self.read(tag) { return try SecureEnclave.P256.Signing.PrivateKey(dataRepresentation: data) }
        let key = try SecureEnclave.P256.Signing.PrivateKey()
        try Self.save(key.dataRepresentation, name: tag)
        return key
    }
    #endif

    func publicPEM() throws -> String { try key().publicKey.pemRepresentation }
    func sign(_ data: Data) throws -> String { try key().signature(for: data).derRepresentation.base64EncodedString() }
    static func hash(_ data: Data) -> String { SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined() }
}

final class PinnedSession: NSObject, URLSessionTaskDelegate, @unchecked Sendable {
    let fingerprint: String
    let keyFingerprint: String?
    private let lock = NSLock()
    private var observedKey: String?
    var acceptedKeyFingerprint: String? { lock.withLock { observedKey } }
    init(fingerprint: String, keyFingerprint: String? = nil) {
        self.fingerprint = fingerprint.lowercased()
        self.keyFingerprint = keyFingerprint
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse, newRequest request: URLRequest, completionHandler: @escaping @Sendable (URLRequest?) -> Void) {
        // 不向重定向目标转发会话与设备签名，避免跨来源或降级到 HTTP。
        completionHandler(nil)
    }

    func urlSession(_ session: URLSession, didReceive challenge: URLAuthenticationChallenge, completionHandler: @escaping @Sendable (URLSession.AuthChallengeDisposition, URLCredential?) -> Void) {
        guard challenge.protectionSpace.authenticationMethod == NSURLAuthenticationMethodServerTrust,
              let trust = challenge.protectionSpace.serverTrust,
              let chain = SecTrustCopyCertificateChain(trust) as? [SecCertificate], let certificate = chain.first,
              let publicKey = SecCertificateCopyKey(certificate),
              let rawKey = SecKeyCopyExternalRepresentation(publicKey, nil) as Data? else {
            completionHandler(.cancelAuthenticationChallenge, nil)
            return
        }
        let currentKey = DeviceIdentity.hash(rawKey)
        let exactCertificate = DeviceIdentity.hash(SecCertificateCopyData(certificate) as Data) == fingerprint
        let stableKey = keyFingerprint == currentKey
        guard exactCertificate || stableKey else {
            completionHandler(.cancelAuthenticationChallenge, nil)
            return
        }
        if !exactCertificate {
            // 续期证书必须复用已信任公钥，并通过系统证书链、有效期与主机名校验。
            guard SecTrustEvaluateWithError(trust, nil) else {
                completionHandler(.cancelAuthenticationChallenge, nil)
                return
            }
        }
        lock.withLock { observedKey = currentKey }
        completionHandler(.useCredential, URLCredential(trust: trust))
    }
}
