import Foundation

struct Connection: Codable, Sendable {
    var url: String
    var fingerprint: String
    var token: String
    var refreshToken: String
    var expiresAt: Date
    var tlsKeyFingerprint: String? = nil
}

struct PairingCode: Codable, Sendable {
    var url: String
    var fingerprint: String
    var token: String
}

/// 网络与身份集中在单一 actor，避免页面自行处理签名或泄露会话。
actor APIClient {
    private var connection: Connection?
    private let identity = DeviceIdentity()
    private var session: URLSession?

    init() {
        if let data = DeviceIdentity.read("connection"), let saved = try? JSONDecoder().decode(Connection.self, from: data) {
            connection = saved
            session = URLSession(configuration: .ephemeral, delegate: PinnedSession(fingerprint: saved.fingerprint, keyFingerprint: saved.tlsKeyFingerprint), delegateQueue: nil)
        }
    }

    func isConnected() -> Bool { connection != nil }

    func pair(_ code: PairingCode) async throws {
        guard let url = URL(string: code.url), url.scheme == "https", code.fingerprint.count == 64 else { throw APIError.message("需要 HTTPS 地址和完整服务器证书指纹") }
        let pinning = PinnedSession(fingerprint: code.fingerprint)
        let transport = URLSession(configuration: .ephemeral, delegate: pinning, delegateQueue: nil)
        var request = URLRequest(url: url.appendingPathComponent("api/v1/pair"))
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(withJSONObject: ["token": code.token, "public_key": identity.publicPEM(), "signature": identity.sign(Data(("homeai-pair:" + code.token).utf8)), "name": "iPhone"])
        let (data, response) = try await transport.data(for: request)
        try Self.validate(data, response)
        let result = try JSONDecoder().decode(Tokens.self, from: data)
        connection = Connection(url: code.url, fingerprint: code.fingerprint, token: result.access_token, refreshToken: result.refresh_token, expiresAt: Date().addingTimeInterval(Double(result.expires_in)))
        connection?.tlsKeyFingerprint = pinning.acceptedKeyFingerprint
        session = transport
        try persist()
    }

    struct Tokens: Decodable { let access_token: String; let refresh_token: String; let expires_in: Int }
    enum APIError: LocalizedError {
        case message(String)
        var errorDescription: String? { if case .message(let text) = self { text } else { nil } }
    }

    private func persist() throws { try DeviceIdentity.save(JSONEncoder().encode(connection), name: "connection") }

    func request(_ method: String, _ path: String, body: Data? = nil) async throws -> Data {
        guard var saved = connection, let session, let base = URL(string: saved.url) else { throw APIError.message("请先配对家庭服务器") }
        if saved.expiresAt.timeIntervalSinceNow < 60 && path != "/api/v1/session/renew" {
            let data = try await send("POST", "/api/v1/session/renew", body: nil, token: saved.refreshToken, base: base, session: session)
            let tokens = try JSONDecoder().decode(Tokens.self, from: data)
            saved.token = tokens.access_token
            saved.refreshToken = tokens.refresh_token
            saved.expiresAt = Date().addingTimeInterval(Double(tokens.expires_in))
            connection = saved
            try persist()
        }
        return try await send(method, path, body: body, token: saved.token, base: base, session: session)
    }

    private func send(_ method: String, _ path: String, body: Data?, token: String, base: URL, session: URLSession) async throws -> Data {
        guard let url = URL(string: path, relativeTo: base) else { throw APIError.message("无效请求地址") }
        let timestamp = String(Date().timeIntervalSince1970)
        let nonce = UUID().uuidString
        let signedPath = url.path(percentEncoded: true) + (url.query(percentEncoded: true).map { "?" + $0 } ?? "")
        let proof = [timestamp, nonce, method, signedPath, DeviceIdentity.hash(body ?? Data()), DeviceIdentity.hash(Data(token.utf8))].joined(separator: "\n")
        var request = URLRequest(url: url)
        request.httpMethod = method
        request.httpBody = body
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue("Bearer " + token, forHTTPHeaderField: "Authorization")
        request.setValue(timestamp, forHTTPHeaderField: "X-HomeAI-Time")
        request.setValue(nonce, forHTTPHeaderField: "X-HomeAI-Nonce")
        request.setValue(try identity.sign(Data(proof.utf8)), forHTTPHeaderField: "X-HomeAI-Signature")
        let (data, response) = try await session.data(for: request)
        try Self.validate(data, response)
        return data
    }

    private static func validate(_ data: Data, _ response: URLResponse) throws {
        guard let response = response as? HTTPURLResponse, (200..<300).contains(response.statusCode) else {
            let body = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
            throw APIError.message(body?["detail"] as? String ?? "服务器请求失败")
        }
    }
}
