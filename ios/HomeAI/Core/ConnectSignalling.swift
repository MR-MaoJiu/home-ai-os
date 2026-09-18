import Foundation

struct DirectAccess: Codable, Sendable {
    let grant_id: String
    let credential: String
    let expires: Double
    let portal_url: String
    let instance_id: String
    let stun_urls: [String]
    let transport_policy: String
}

private final class NoSignalRedirect: NSObject, URLSessionTaskDelegate, @unchecked Sendable {
    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest, completionHandler: @escaping @Sendable (URLRequest?) -> Void) {
        completionHandler(nil)
    }
}

enum ConnectSignalling {
    /// 平台请求不包含家庭会话令牌；仅设备连接授权和有界信令。
    static func request(_ access: DirectAccess, method: String, path: String, body: Data = Data()) async throws -> Data {
        guard access.transport_policy == "direct_only", access.expires > Date().timeIntervalSince1970,
              let base = URLComponents(string: access.portal_url), base.scheme == "https", base.host != nil,
              base.user == nil, base.password == nil, base.query == nil, base.fragment == nil, ["", "/"].contains(base.path),
              path.hasPrefix("/api/direct/"), !path.contains("?"), body.count <= 40000,
              let url = URL(string: access.portal_url.trimmingCharacters(in: CharacterSet(charactersIn: "/")) + path) else {
            throw APIClient.APIError.message("直连平台授权无效或已过期，请在家庭网络重新授权")
        }
        let timestamp = String(Int(Date().timeIntervalSince1970))
        let nonce = UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased()
        let proof = ["homeai-connect-direct:v1", "device", access.grant_id, timestamp, nonce, method, path, DeviceIdentity.hash(body)].joined(separator: "\n")
        var request = URLRequest(url: url)
        request.httpMethod = method
        request.httpBody = body.isEmpty ? nil : body
        request.httpShouldHandleCookies = false
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue(access.grant_id, forHTTPHeaderField: "X-Connect-ID")
        request.setValue(access.credential, forHTTPHeaderField: "X-Connect-Credential")
        request.setValue(timestamp, forHTTPHeaderField: "X-Connect-Time")
        request.setValue(nonce, forHTTPHeaderField: "X-Connect-Nonce")
        request.setValue(try DeviceIdentity().sign(Data(proof.utf8)), forHTTPHeaderField: "X-Connect-Signature")
        let config = URLSessionConfiguration.ephemeral
        config.httpCookieStorage = nil
        config.urlCredentialStorage = nil
        config.timeoutIntervalForRequest = 20
        config.timeoutIntervalForResource = 25
        let delegate = NoSignalRedirect()
        let session = URLSession(configuration: config, delegate: delegate, delegateQueue: nil)
        defer { session.invalidateAndCancel() }
        let (stream, response) = try await session.bytes(for: request, delegate: delegate)
        guard let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode) else {
            throw APIClient.APIError.message("连接平台拒绝协商或暂时不可用")
        }
        var result = Data()
        for try await byte in stream {
            guard result.count < 40000 else { throw APIClient.APIError.message("平台信令响应超过限制") }
            result.append(byte)
        }
        return result
    }
}

struct EnrollmentAccess: Codable, Sendable {
    let id: String
    let credential: String
    let expires: Double
    let portal_url: String
    let instance_id: String
}

extension ConnectSignalling {
    /// 仅传输短期配对密文，二维码中的家庭配对秘密不提交给平台。
    static func enrollment(_ access: EnrollmentAccess, suffix: String, ciphertext: String? = nil) async throws -> Data {
        guard access.expires > Date().timeIntervalSince1970, access.id.count == 32,
              access.id.allSatisfy({ $0.isHexDigit }), ["request", "response"].contains(suffix),
              let base = URLComponents(string: access.portal_url), base.scheme == "https", base.host != nil,
              base.user == nil, base.password == nil, base.query == nil, base.fragment == nil, ["", "/"].contains(base.path),
              let url = URL(string: access.portal_url.trimmingCharacters(in: CharacterSet(charactersIn: "/")) + "/api/direct/enrollments/" + access.id + "/" + suffix) else {
            throw APIClient.APIError.message("配对二维码已过期或平台地址无效")
        }
        var request = URLRequest(url: url)
        request.httpMethod = ciphertext == nil ? "GET" : "POST"
        request.setValue(access.credential, forHTTPHeaderField: "X-Enrollment-Credential")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        if let ciphertext { request.httpBody = try JSONSerialization.data(withJSONObject: ["ciphertext": ciphertext]) }
        let config = URLSessionConfiguration.ephemeral
        config.httpCookieStorage = nil; config.urlCredentialStorage = nil
        config.timeoutIntervalForRequest = 20; config.timeoutIntervalForResource = 25
        let delegate = NoSignalRedirect()
        let session = URLSession(configuration: config, delegate: delegate, delegateQueue: nil)
        defer { session.invalidateAndCancel() }
        let (stream, response) = try await session.bytes(for: request, delegate: delegate)
        guard let http = response as? HTTPURLResponse, http.statusCode == 200 else { throw APIClient.APIError.message("首次配对授权失效或平台不可用，请重新生成二维码") }
        var data = Data()
        for try await byte in stream {
            guard data.count < 16384 else { throw APIClient.APIError.message("配对响应过大") }
            data.append(byte)
        }
        return data
    }
}
