import Foundation
import CryptoKit
import Security

struct ServerIdentityProof: Decodable, Sendable {
    let schema_version: String
    let server_id: String
    let server_public_key: String
    let namespace_anchor: String
    let fingerprint: String
    let addresses: [String]
    let nonce: String
    let issued_at: Int
    let expires_at: Int
}

// 仅用于不带凭据的身份挑战。业务请求必须使用验证完成后的 PinnedSession。
final class IdentityProbeSession: NSObject, URLSessionTaskDelegate, @unchecked Sendable {
    private let lock = NSLock()
    private var observed: String?
    private var observedKey: String?
    var fingerprint: String? { lock.withLock { observed } }
    var keyFingerprint: String? { lock.withLock { observedKey } }

    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse, newRequest request: URLRequest, completionHandler: @escaping @Sendable (URLRequest?) -> Void) { completionHandler(nil) }

    func urlSession(_ session: URLSession, task: URLSessionTask, didReceive challenge: URLAuthenticationChallenge, completionHandler: @escaping @Sendable (URLSession.AuthChallengeDisposition, URLCredential?) -> Void) {
        urlSession(session, didReceive: challenge, completionHandler: completionHandler)
    }

    func urlSession(_ session: URLSession, didReceive challenge: URLAuthenticationChallenge, completionHandler: @escaping @Sendable (URLSession.AuthChallengeDisposition, URLCredential?) -> Void) {
        guard challenge.protectionSpace.authenticationMethod == NSURLAuthenticationMethodServerTrust,
              let trust = challenge.protectionSpace.serverTrust,
              let chain = SecTrustCopyCertificateChain(trust) as? [SecCertificate], let certificate = chain.first,
              PinnedSession.pinnedCertificateIsValid(certificate), let publicKey = SecCertificateCopyKey(certificate),
              let raw = SecKeyCopyExternalRepresentation(publicKey, nil) as Data? else {
            completionHandler(.cancelAuthenticationChallenge, nil); return
        }
        lock.withLock {
            observed = DeviceIdentity.hash(SecCertificateCopyData(certificate) as Data)
            observedKey = DeviceIdentity.hash(raw)
        }
        completionHandler(.useCredential, URLCredential(trust: trust))
    }
}

enum ServerTrust {
    struct Envelope: Decodable { let payload: String; let signature: String }
    struct Verified: Sendable { let proof: ServerIdentityProof; let tlsKey: String }

    static func validate(_ data: Data, nonce: String, peerFingerprint: String, serverID: String?, publicKey: String, now: Date = Date()) throws -> ServerIdentityProof {
        guard data.count <= 16384 else { throw APIClient.APIError.message("服务器身份响应过大") }
        let envelope = try JSONDecoder().decode(Envelope.self, from: data)
        guard let payload = Data(base64Encoded: envelope.payload), let signature = Data(base64Encoded: envelope.signature) else {
            throw APIClient.APIError.message("服务器身份签名编码无效")
        }
        let key = try P256.Signing.PublicKey(pemRepresentation: publicKey)
        var signed = Data("homeai-server-identity:v1\n".utf8); signed.append(payload)
        guard key.isValidSignature(try P256.Signing.ECDSASignature(derRepresentation: signature), for: signed) else {
            throw APIClient.APIError.message("服务器身份签名不匹配")
        }
        let proof = try JSONDecoder().decode(ServerIdentityProof.self, from: payload)
        let advertised = try P256.Signing.PublicKey(pemRepresentation: proof.server_public_key)
        let timestamp = Int(now.timeIntervalSince1970)
        guard proof.schema_version == "1.0", proof.nonce == nonce, UUID(uuidString: proof.server_id) != nil,
              serverID == nil || proof.server_id == serverID,
              key.rawRepresentation == advertised.rawRepresentation,
              proof.fingerprint == peerFingerprint, proof.issued_at <= timestamp + 10,
              proof.expires_at > timestamp, proof.expires_at - proof.issued_at == 60,
              proof.namespace_anchor.count == 64,
              proof.namespace_anchor.allSatisfy({ $0.isHexDigit && !$0.isUppercase }), proof.addresses.count <= 8 else {
            throw APIClient.APIError.message("服务器身份挑战、证书或有效期不匹配")
        }
        for address in proof.addresses { _ = try origin(address) }
        return proof
    }

    static func origin(_ text: String) throws -> URL {
        guard let url = URL(string: text), url.scheme == "https", url.host != nil,
              url.user == nil, url.password == nil, url.query == nil, url.fragment == nil,
              url.path.isEmpty || url.path == "/" else { throw APIClient.APIError.message("需要无凭据的 HTTPS 根地址") }
        return url
    }

    static func probe(url text: String, serverID: String, publicKey: String) async throws -> Verified {
        let base = try origin(text)
        let nonce = (UUID().uuidString + UUID().uuidString).replacingOccurrences(of: "-", with: "").lowercased()
        let observer = IdentityProbeSession()
        let config = URLSessionConfiguration.ephemeral
        config.httpShouldSetCookies = false; config.httpCookieStorage = nil; config.urlCredentialStorage = nil
        config.timeoutIntervalForRequest = 20
        let session = URLSession(configuration: config, delegate: observer, delegateQueue: nil)
        defer { session.invalidateAndCancel() }
        var request = URLRequest(url: base.appendingPathComponent("api/v1/server/identity").appending(queryItems: [URLQueryItem(name: "nonce", value: nonce)]))
        request.cachePolicy = .reloadIgnoringLocalCacheData
        let (bytes, response) = try await session.bytes(for: request, delegate: observer)
        var data = Data()
        for try await byte in bytes {
            guard data.count < 16384 else { throw APIClient.APIError.message("服务器身份响应过大") }
            data.append(byte)
        }
        guard (response as? HTTPURLResponse)?.statusCode == 200, let fingerprint = observer.fingerprint, let tlsKey = observer.keyFingerprint else {
            throw APIClient.APIError.message("服务器身份验证失败")
        }
        return Verified(proof: try validate(data, nonce: nonce, peerFingerprint: fingerprint, serverID: serverID, publicKey: publicKey), tlsKey: tlsKey)
    }
}
