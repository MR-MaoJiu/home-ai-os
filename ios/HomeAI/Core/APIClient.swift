import Foundation

struct Connection: Codable, Sendable {
    var url: String
    var fingerprint: String
    var token: String
    var refreshToken: String
    var expiresAt: Date
    var tlsKeyFingerprint: String? = nil
    var deviceID: String? = nil
}

struct PairingCode: Codable, Sendable {
    var url: String
    var fingerprint: String
    var token: String
}

/// 网络与身份集中在单一 actor，避免页面自行处理签名或泄露会话。
struct TaskStateEvent: Decodable, Sendable {
    struct Item: Decodable, Sendable, Identifiable { let id: String; let status: String }
    let type: String
    let tasks: [Item]
    let has_more: Bool
}

struct TaskEventSubscription: Sendable {
    let id: UUID
    let events: AsyncThrowingStream<TaskStateEvent, Error>
}

actor APIClient {
    private var connection: Connection?
    private let persistConnection: Bool
    private let identity = DeviceIdentity()
    private var session: URLSession?
    private var generation = UUID()
    private let renewalGate = AsyncOperationGate()
    private var pairingAttempt = UUID()
    private var eventSockets: [UUID: URLSessionWebSocketTask] = [:]

    init(persistConnection: Bool = true) {
        self.persistConnection = persistConnection
        if persistConnection, let data = DeviceIdentity.read("connection"), let saved = try? JSONDecoder().decode(Connection.self, from: data) {
            connection = saved
            session = URLSession(configuration: .ephemeral, delegate: PinnedSession(fingerprint: saved.fingerprint, keyFingerprint: saved.tlsKeyFingerprint), delegateQueue: nil)
        }
    }

    func isConnected() -> Bool { connection != nil }

    func restoreConnectionIfNeeded() {
        guard connection == nil, persistConnection,
              let data = DeviceIdentity.read("connection"),
              let saved = try? JSONDecoder().decode(Connection.self, from: data) else { return }
        generation = UUID()
        connection = saved
        session = URLSession(configuration: .ephemeral, delegate: PinnedSession(fingerprint: saved.fingerprint, keyFingerprint: saved.tlsKeyFingerprint), delegateQueue: nil)
    }

    func pair(_ code: PairingCode) async throws {
        let attempt = UUID()
        pairingAttempt = attempt
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
        guard pairingAttempt == attempt else { throw APIError.message("配对已被更新的请求替代") }
        for socket in eventSockets.values { socket.cancel(with: .goingAway, reason: nil) }
        eventSockets.removeAll()
        generation = UUID()
        connection = Connection(url: code.url, fingerprint: code.fingerprint, token: result.access_token, refreshToken: result.refresh_token, expiresAt: Date().addingTimeInterval(Double(result.expires_in)))
        connection?.tlsKeyFingerprint = pinning.acceptedKeyFingerprint
        connection?.deviceID = result.device_id
        session = transport
        try persist()
    }

    struct Tokens: Decodable { let access_token: String; let refresh_token: String; let expires_in: Int; let device_id: String? }
    enum APIError: LocalizedError {
        case message(String)
        case http(Int, String)
        var errorDescription: String? {
            switch self { case .message(let text), .http(_, let text): return text }
        }
    }

    func syncNamespace() async throws -> String {
        if connection?.deviceID == nil {
            let started = generation
            struct Me: Decodable { let device_id: String }
            let data = try await request("GET", "/api/v1/me")
            guard started == generation else { throw APIError.message("连接已切换，请重试") }
            connection?.deviceID = try JSONDecoder().decode(Me.self, from: data).device_id
            try persist()
        }
        guard let saved = connection, let device = saved.deviceID else { throw APIError.message("请先配对") }
        return DeviceIdentity.hash(Data(((saved.tlsKeyFingerprint ?? saved.fingerprint) + ":" + device).utf8))
    }

    private func persist() throws {
        if persistConnection { try DeviceIdentity.save(JSONEncoder().encode(connection), name: "connection") }
    }

    func request(_ method: String, _ path: String, body: Data? = nil, expectedNamespace: String? = nil, contentType: String = "application/json") async throws -> Data {
        if let expectedNamespace {
            guard let saved = connection, let device = saved.deviceID,
                  DeviceIdentity.hash(Data(((saved.tlsKeyFingerprint ?? saved.fingerprint) + ":" + device).utf8)) == expectedNamespace else {
                throw APIError.message("同步连接已切换，请重新开始")
            }
        }
        let started = generation
        guard var saved = connection, let session, let base = URL(string: saved.url) else { throw APIError.message("请先配对家庭服务器") }
        if saved.expiresAt.timeIntervalSinceNow < 60 && path != "/api/v1/session/renew" {
            saved = try await renewalGate.withPermit { try await self.renewIfNeeded(generation: started) }
        }
        let data = try await send(method, path, body: body, token: saved.token, base: base, session: session, expectedGeneration: started, contentType: contentType)
        guard started == generation else { throw APIError.message("连接已切换，请重试") }
        return data
    }

    /// 前台状态流只推送标识与进度，业务结果必须继续调用鉴权接口。
    func taskEvents(taskID: String? = nil, expectedNamespace: String) async throws -> TaskEventSubscription {
        guard try await syncNamespace() == expectedNamespace else { throw APIError.message("连接已切换") }
        let started = generation
        guard var saved = connection, let session, let base = URL(string: saved.url) else { throw APIError.message("请先配对") }
        if saved.expiresAt.timeIntervalSinceNow < 60 {
            saved = try await renewalGate.withPermit { try await self.renewIfNeeded(generation: started) }
        }
        if let taskID, UUID(uuidString: taskID) == nil { throw APIError.message("无效任务标识") }
        let path = taskID.map { "/api/v1/tasks/\($0)/events" } ?? "/api/v1/events/tasks"
        var request = try signedRequest("GET", path, body: nil, token: saved.token, base: base)
        var components = URLComponents(url: request.url!, resolvingAgainstBaseURL: true)!
        components.scheme = "wss"
        request.url = components.url
        let socket = session.webSocketTask(with: request)
        socket.maximumMessageSize = 256 * 1024
        let identifier = UUID()
        eventSockets[identifier] = socket
        socket.resume()
        let events = AsyncThrowingStream<TaskStateEvent, Error>(bufferingPolicy: .bufferingNewest(1)) { continuation in
            let reader = Task {
                do {
                    while !Task.isCancelled {
                        let message = try await socket.receive()
                        guard started == self.generation else { throw APIError.message("连接已切换") }
                        let data: Data
                        switch message {
                        case .data(let value): data = value
                        case .string(let value): data = Data(value.utf8)
                        @unknown default: throw APIError.message("未知状态流消息")
                        }
                        continuation.yield(try JSONDecoder().decode(TaskStateEvent.self, from: data))
                    }
                    continuation.finish()
                } catch {
                    if Task.isCancelled { continuation.finish() }
                    else if socket.closeCode.rawValue == 4401 { continuation.finish(throwing: APIError.http(401, "状态流会话已过期或授权已撤销")) }
                    else { continuation.finish(throwing: error) }
                }
                self.eventSockets.removeValue(forKey: identifier)
                socket.cancel(with: .goingAway, reason: nil)
            }
            continuation.onTermination = { _ in
                reader.cancel()
                socket.cancel(with: .goingAway, reason: nil)
            }
        }
        return TaskEventSubscription(id: identifier, events: events)
    }

    func closeTaskEvents(_ identifier: UUID) {
        eventSockets.removeValue(forKey: identifier)?.cancel(with: .goingAway, reason: nil)
    }

    func stopTaskEvents() {
        for socket in eventSockets.values { socket.cancel(with: .goingAway, reason: nil) }
        eventSockets.removeAll()
    }

    func uploadDocument(name: String, contents: Data) async throws -> String {
        guard contents.count <= 20 * 1024 * 1024 else { throw APIError.message("文件超过 20 MB") }
        let namespace = try await syncNamespace()
        let boundary = "HomeAI-" + UUID().uuidString
        let filename = String((name as NSString).lastPathComponent.prefix(200)).replacingOccurrences(of: "\"", with: "_").replacingOccurrences(of: "\r", with: "_").replacingOccurrences(of: "\n", with: "_")
        var body = Data("--\(boundary)\r\nContent-Disposition: form-data; name=\"file\"; filename=\"\(filename)\"\r\nContent-Type: application/octet-stream\r\n\r\n".utf8)
        body.append(contents)
        body.append(Data("\r\n--\(boundary)--\r\n".utf8))
        struct Identifier: Decodable { let id: String }
        let record = try JSONDecoder().decode(Identifier.self, from: await request("POST", "/api/v1/files", body: body, expectedNamespace: namespace, contentType: "multipart/form-data; boundary=\(boundary)"))
        let task = try JSONDecoder().decode(Identifier.self, from: await request("POST", "/api/v1/files/\(record.id)/parse", expectedNamespace: namespace))
        return task.id
    }

    private func renewIfNeeded(generation started: UUID) async throws -> Connection {
        guard started == generation, var saved = connection, let session, let base = URL(string: saved.url) else { throw APIError.message("连接已切换，请重试") }
        if saved.expiresAt.timeIntervalSinceNow >= 60 { return saved }
        let data = try await send("POST", "/api/v1/session/renew", body: nil, token: saved.refreshToken, base: base, session: session, expectedGeneration: started)
        guard started == generation else { throw APIError.message("连接已切换，请重试") }
        let tokens = try JSONDecoder().decode(Tokens.self, from: data)
        saved.token = tokens.access_token
        saved.refreshToken = tokens.refresh_token
        saved.expiresAt = Date().addingTimeInterval(Double(tokens.expires_in))
        connection = saved
        try persist()
        return saved
    }

    private func signedRequest(_ method: String, _ path: String, body: Data?, token: String, base: URL, contentType: String = "application/json") throws -> URLRequest {
        guard let url = URL(string: path, relativeTo: base) else { throw APIError.message("无效请求地址") }
        let timestamp = String(Date().timeIntervalSince1970)
        let nonce = UUID().uuidString
        let signedPath = url.path(percentEncoded: true) + (url.query(percentEncoded: true).map { "?" + $0 } ?? "")
        let proof = [timestamp, nonce, method, signedPath, DeviceIdentity.hash(body ?? Data()), DeviceIdentity.hash(Data(token.utf8))].joined(separator: "\n")
        var request = URLRequest(url: url)
        request.httpMethod = method
        request.httpBody = body
        request.setValue(contentType, forHTTPHeaderField: "Content-Type")
        request.setValue("Bearer " + token, forHTTPHeaderField: "Authorization")
        request.setValue(timestamp, forHTTPHeaderField: "X-HomeAI-Time")
        request.setValue(nonce, forHTTPHeaderField: "X-HomeAI-Nonce")
        request.setValue(try identity.sign(Data(proof.utf8)), forHTTPHeaderField: "X-HomeAI-Signature")
        return request
    }

    private func send(_ method: String, _ path: String, body: Data?, token: String, base: URL, session: URLSession, expectedGeneration: UUID, contentType: String = "application/json") async throws -> Data {
        let request = try signedRequest(method, path, body: body, token: token, base: base, contentType: contentType)
        do {
            let (data, response) = try await session.data(for: request)
            guard expectedGeneration == generation else { throw APIError.message("连接已切换，请重试") }
            try Self.validate(data, response)
            return data
        } catch {
            guard expectedGeneration == generation else { throw APIError.message("连接已切换，请重试") }
            throw error
        }
    }

    private static func validate(_ data: Data, _ response: URLResponse) throws {
        guard let response = response as? HTTPURLResponse, (200..<300).contains(response.statusCode) else {
            let body = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
            throw APIError.http((response as? HTTPURLResponse)?.statusCode ?? 0, body?["detail"] as? String ?? "服务器请求失败")
        }
    }
}
