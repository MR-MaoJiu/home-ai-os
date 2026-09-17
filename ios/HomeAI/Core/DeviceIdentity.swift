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

final class PinnedSession: NSObject, URLSessionDelegate, @unchecked Sendable {
    let fingerprint: String
    init(fingerprint: String) { self.fingerprint = fingerprint.lowercased() }

    func urlSession(_ session: URLSession, didReceive challenge: URLAuthenticationChallenge, completionHandler: @escaping @Sendable (URLSession.AuthChallengeDisposition, URLCredential?) -> Void) {
        guard challenge.protectionSpace.authenticationMethod == NSURLAuthenticationMethodServerTrust,
              let trust = challenge.protectionSpace.serverTrust,
              let chain = SecTrustCopyCertificateChain(trust) as? [SecCertificate], let certificate = chain.first,
              DeviceIdentity.hash(SecCertificateCopyData(certificate) as Data) == fingerprint else {
            completionHandler(.cancelAuthenticationChallenge, nil)
            return
        }
        // 用户通过本机二维码确认的证书固定值是私有自签名服务器的信任锚。
        completionHandler(.useCredential, URLCredential(trust: trust))
    }
}
