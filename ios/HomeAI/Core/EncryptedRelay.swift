import Foundation
import CryptoKit

/// 平台只看到签名密文；HTTP会话凭据、路径和正文均在HPKE内部。
actor EncryptedRelay {
    private static let requests = RelayRequestGate(limit: 3)
    struct Descriptor: Sendable {
        let serverID: String
        let keyID: String
        let publicKey: Data
        let expires: Double
    }
    private let access: DirectAccess
    private let serverID: String
    private let serverPublicKey: String
    private let namespace: String
    private var descriptor: Descriptor
    private static let session: URLSession = {
        let config = URLSessionConfiguration.ephemeral
        config.httpShouldSetCookies = false; config.httpCookieStorage = nil; config.urlCredentialStorage = nil
        config.urlCache = nil; config.timeoutIntervalForRequest = 15; config.timeoutIntervalForResource = 20
        return URLSession(configuration: config, delegate: NoRelayRedirect(), delegateQueue: nil)
    }()
    private init(access: DirectAccess, serverID: String, publicKey: String, namespace: String, descriptor: Descriptor) {
        self.access = access; self.serverID = serverID; serverPublicKey = publicKey; self.namespace = namespace; self.descriptor = descriptor
    }
    var isReady: Bool { access.expires > Date().timeIntervalSince1970 && descriptor.expires > Date().timeIntervalSince1970 + 5 }

    static func connect(access: DirectAccess, serverID: String, publicKey: String, namespace: String) async throws -> EncryptedRelay {
        let data = try await platform(access, method: "GET", path: "/api/relay/key")
        let value = try validateDescriptor(data, serverID: serverID, publicKey: publicKey, namespace: namespace)
        return EncryptedRelay(access: access, serverID: serverID, publicKey: publicKey, namespace: namespace, descriptor: value)
    }

    static func canonical(_ value: [String: Any]) throws -> Data {
        try JSONSerialization.data(withJSONObject: value, options: [.sortedKeys, .withoutEscapingSlashes])
    }
    static func verified(_ data: Data, domain: String, publicKey: String) throws -> [String: Any] {
        guard data.count <= 4 * 1024 * 1024,
              let envelope = try JSONSerialization.jsonObject(with: data) as? [String: Any], Set(envelope.keys) == ["payload", "signature"],
              let payload = envelope["payload"] as? [String: Any], let signature = envelope["signature"] as? String,
              let raw = Data(base64Encoded: signature), raw.count <= 100 else { throw APIClient.APIError.message("加密通道信封无效") }
        let key = try P256.Signing.PublicKey(pemRepresentation: publicKey)
        guard key.isValidSignature(try P256.Signing.ECDSASignature(derRepresentation: raw), for: Data(domain.utf8) + (try canonical(payload))) else {
            throw APIClient.APIError.message("家庭端加密身份不匹配")
        }
        return payload
    }
    static func validateDescriptor(_ data: Data, serverID: String, publicKey: String, namespace: String, now: Date = Date()) throws -> Descriptor {
        let p = try verified(data, domain: "homeai-relay-key:v1\n", publicKey: publicKey)
        guard Set(p.keys) == ["version", "server_id", "namespace_anchor", "key_id", "public_key", "expires", "transport_policy"],
              p["version"] as? Int == 1, p["server_id"] as? String == serverID, p["namespace_anchor"] as? String == namespace,
              p["transport_policy"] as? String == "smart_e2e", let keyID = p["key_id"] as? String,
              keyID.count == 32, keyID.allSatisfy({ $0.isHexDigit && !$0.isUppercase }),
              let encoded = p["public_key"] as? String, let key = Data(base64Encoded: encoded), key.count == 32,
              let expires = p["expires"] as? Int, Double(expires) > now.timeIntervalSince1970 + 5, Double(expires) <= now.timeIntervalSince1970 + 910 else {
            throw APIClient.APIError.message("家庭端未授权此加密中继会话，或会话已经过期")
        }
        return Descriptor(serverID: serverID, keyID: keyID, publicKey: key, expires: Double(expires))
    }
    static func info(_ direction: String, _ serverID: String, _ id: String, _ keyID: String) -> Data {
        Data("homeai-relay-hpke:v1|\(direction)|\(serverID)|\(id)|\(keyID)".utf8)
    }

    private enum RelayError: Error { case keyChanged }

    func request(_ request: URLRequest, deviceID: String) async throws -> (Data, Int) {
        try await self.request(deviceID: deviceID) { request }
    }

    func request(deviceID: String, makeRequest: @escaping @Sendable () async throws -> URLRequest) async throws -> (Data, Int) {
        // 按设备共享并发额度，重建通道不会绕开限额；排队后才签家庭端请求。
        try await Self.requests.withPermit(key: serverID + ":" + deviceID) {
            let request = try await makeRequest()
            return try await self.requestWithPermit(request, deviceID: deviceID)
        }
    }

    private func requestWithPermit(_ request: URLRequest, deviceID: String) async throws -> (Data, Int) {
        do { return try await perform(request, deviceID: deviceID) }
        catch RelayError.keyChanged {
            // 平台明确尚未接收该请求时，才允许使用新密钥重新封装一次。
            let data = try await Self.platform(access, method: "GET", path: "/api/relay/key")
            descriptor = try Self.validateDescriptor(data, serverID: serverID, publicKey: serverPublicKey, namespace: namespace)
            return try await perform(request, deviceID: deviceID)
        }
    }

    nonisolated static func shouldDiscardConnection(after error: Error) -> Bool {
        if error is CancellationError || (error as? URLError)?.code == .cancelled { return false }
        if let failure = error as? RelayPlatformError { return !failure.isTransient }
        return !RelayRecovery.isTransientRead(error)
    }

    private func perform(_ request: URLRequest, deviceID: String) async throws -> (Data, Int) {
        try Task.checkCancellation()
        let deadline = Date().addingTimeInterval(115)
        let current = descriptor
        let id = UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased()
        let replyKey = Curve25519.KeyAgreement.PrivateKey()
        guard let url = request.url, let components = URLComponents(url: url, resolvingAgainstBaseURL: false) else { throw APIClient.APIError.message("请求地址无效") }
        let target = components.percentEncodedPath + (components.percentEncodedQuery.map { "?" + $0 } ?? "")
        let body = request.httpBody ?? Data()
        guard target.hasPrefix("/api/v1/"), body.count <= 1_400_000 else { throw APIClient.APIError.message("备用通道请使用分块上传或分页读取") }
        let allowed: Set<String> = ["authorization", "content-type", "x-homeai-time", "x-homeai-nonce", "x-homeai-signature"]
        let headers = Dictionary(uniqueKeysWithValues: (request.allHTTPHeaderFields ?? [:]).filter { allowed.contains($0.key.lowercased()) }.map { ($0.key.lowercased(), $0.value) })
        let metadata: [String: Any] = ["wire_version": 3, "kind": "request", "id": id, "method": request.httpMethod ?? "GET", "target": target, "headers": headers, "size": body.count]
        let clear = try Self.canonical(["metadata": metadata, "body": body.base64EncodedString(), "response_public_key": replyKey.publicKey.rawRepresentation.base64EncodedString()])
        var sender = try HPKE.Sender(recipientKey: Curve25519.KeyAgreement.PublicKey(rawRepresentation: current.publicKey), ciphersuite: .Curve25519_SHA256_ChachaPoly,
            info: Self.info("request", serverID, id, current.keyID))
        let sealed = try sender.seal(clear)
        let payload: [String: Any] = ["version": 1, "id": id, "server_id": serverID, "device_id": deviceID, "key_id": current.keyID,
            "expires": Int(Date().timeIntervalSince1970) + 120, "enc": sender.encapsulatedKey.base64EncodedString(), "ciphertext": sealed.base64EncodedString()]
        let envelope = try Self.canonical(["payload": payload, "signature": DeviceIdentity().sign(Data("homeai-relay-request:v1\n".utf8) + Self.canonical(payload))])
        _ = try await RelayRecovery.submit(envelope, deadline: deadline) { body in
            try await Self.platform(self.access, method: "POST", path: "/api/relay/requests", body: body, deadline: deadline)
        }
        while Date() < deadline {
            try Task.checkCancellation()
            // 已被接收的请求只补取原结果，网络抖动不能创建第二个业务请求。
            let response = try await RelayRecovery.read(deadline: deadline) {
                try await Self.platform(self.access, method: "GET", path: "/api/relay/responses/" + id, deadline: deadline)
            }
            guard let root = try JSONSerialization.jsonObject(with: response) as? [String: Any] else { throw APIClient.APIError.message("备用通道响应无效") }
            guard let value = root["envelope"] as? [String: Any] else { continue }
            let p = try Self.verified(Self.canonical(value), domain: "homeai-relay-response:v1\n", publicKey: serverPublicKey)
            guard Set(p.keys) == ["version", "id", "server_id", "key_id", "expires", "enc", "ciphertext"], p["version"] as? Int == 1,
                  p["id"] as? String == id, p["server_id"] as? String == serverID, p["key_id"] as? String == current.keyID,
                  let expiry = p["expires"] as? Int, Double(expiry) > Date().timeIntervalSince1970,
                  Double(expiry) <= Date().timeIntervalSince1970 + 125,
                  let encString = p["enc"] as? String, let enc = Data(base64Encoded: encString), enc.count == 32,
                  let cipherString = p["ciphertext"] as? String, let cipher = Data(base64Encoded: cipherString), cipher.count <= 2*1024*1024+16 else {
                throw APIClient.APIError.message("备用响应与本次请求不匹配")
            }
            var recipient = try HPKE.Recipient(privateKey: replyKey, ciphersuite: .Curve25519_SHA256_ChachaPoly,
                info: Self.info("response", serverID, id, current.keyID), encapsulatedKey: enc)
            let plaintext = try recipient.open(cipher)
            guard let result = try JSONSerialization.jsonObject(with: plaintext) as? [String: Any], Set(result.keys) == ["status", "body"],
                  let status = result["status"] as? Int, (100...599).contains(status), let encodedBody = result["body"] as? String,
                  let bytes = Data(base64Encoded: encodedBody) else { throw APIClient.APIError.message("家庭端响应无效") }
            return (bytes, status)
        }
        throw APIClient.APIError.message("家庭请求尚未返回，未自动重放操作")
    }

    private static func platform(_ access: DirectAccess, method: String, path: String, body: Data = Data(), deadline: Date? = nil) async throws -> Data {
        try Task.checkCancellation()
        guard access.expires > Date().timeIntervalSince1970, body.count <= 4*1024*1024,
              let base = URLComponents(string: access.portal_url), base.scheme == "https", base.host != nil,
              base.user == nil, base.password == nil, base.query == nil, base.fragment == nil, ["", "/"].contains(base.path),
              path.hasPrefix("/api/relay/"), !path.contains("?"), let url = URL(string: access.portal_url.trimmingCharacters(in: CharacterSet(charactersIn: "/")) + path) else {
            throw APIClient.APIError.message("备用连接授权无效")
        }
        let timestamp = String(Int(Date().timeIntervalSince1970)), nonce = UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased()
        let proof = ["homeai-connect-relay:v1", "device", access.grant_id, timestamp, nonce, method, path, DeviceIdentity.hash(body)].joined(separator: "\n")
        var request = URLRequest(url: url); request.httpMethod = method; request.httpBody = body.isEmpty ? nil : body
        if let deadline {
            guard deadline > Date() else { throw APIClient.APIError.message("家庭请求尚未返回，未自动重放操作") }
            request.timeoutInterval = min(15, max(0.1, deadline.timeIntervalSinceNow))
        }
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        for (header,value) in ["X-Connect-ID": access.grant_id, "X-Connect-Credential": access.credential, "X-Connect-Time": timestamp, "X-Connect-Nonce": nonce,
                              "X-Connect-Signature": try DeviceIdentity().sign(Data(proof.utf8))] { request.setValue(value, forHTTPHeaderField: header) }
        let (stream,response) = try await session.bytes(for: request)
        var data = Data()
        for try await byte in stream {
            guard data.count < 4*1024*1024 else { throw APIClient.APIError.message("备用响应超过限制") }
            data.append(byte)
        }
        if (response as? HTTPURLResponse)?.statusCode == 409, method == "POST", path == "/api/relay/requests",
           let root = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
           let detail = root["detail"] as? [String: Any], detail["code"] as? String == "relay_key_changed" { throw RelayError.keyChanged }
        guard let http = response as? HTTPURLResponse else { throw APIClient.APIError.message("加密备用连接响应无效") }
        guard (200..<300).contains(http.statusCode) else {
            throw RelayPlatformError(status: http.statusCode, retryAfter: http.value(forHTTPHeaderField: "Retry-After").flatMap(Double.init))
        }
        return data
    }
}

/// 平台错误独立于家庭 API 的错误，不能把平台 401 当成家庭配对撤销。
struct RelayPlatformError: LocalizedError, Sendable {
    let status: Int
    var retryAfter: Double? = nil
    var isTransient: Bool { [408, 429, 500, 502, 503, 504].contains(status) }
    var errorDescription: String? {
        switch status {
        case 401, 403: return "远程连接授权暂不可用，请检查家庭端的远程服务状态"
        case 404: return "本次连接请求已过期，未重新执行操作"
        case 429: return "连接队列繁忙，请稍后再试"
        case 503: return "家庭端暂时离线或加密备用连接未开启"
        case 408, 500, 502, 504: return "远程连接暂时超时，请稍后再试"
        default: return "加密备用连接请求失败（\(status)）"
        }
    }
}

enum RelayRecovery {
    static func retryDelay(_ retryAfter: Double?, attempt: Int) -> Double {
        if let retryAfter, retryAfter.isFinite, retryAfter >= 0 { return retryAfter }
        return min(2, 0.25 * pow(2, Double(min(max(attempt, 0), 3))))
    }

    static func isTransientRead(_ error: Error) -> Bool {
        if let failure = error as? RelayPlatformError { return failure.isTransient }
        guard let failure = error as? URLError else { return false }
        return [.timedOut, .cannotFindHost, .cannotConnectToHost, .networkConnectionLost,
                .dnsLookupFailed, .notConnectedToInternet, .resourceUnavailable].contains(failure.code)
    }

    static func wait(_ seconds: Double) async throws { try await Task.sleep(for: .seconds(seconds)) }

    static func submit(_ envelope: Data, deadline: Date,
                       pause: @Sendable (Double) async throws -> Void = wait,
                       operation: @Sendable (Data) async throws -> Data) async throws -> Data {
        for attempt in 0...4 {
            try Task.checkCancellation()
            do { return try await operation(envelope) }
            catch let failure as RelayPlatformError where failure.status == 429 && attempt < 4 {
                let delay = retryDelay(failure.retryAfter, attempt: attempt)
                guard Date().addingTimeInterval(delay) < deadline else { throw failure }
                // 429 表示尚未接收；密文与幂等标识保持不变，仅外层请求证明重新生成。
                try await pause(delay)
            }
        }
        throw RelayPlatformError(status: 429)
    }

    static func read(deadline: Date,
                     pause: @Sendable (Double) async throws -> Void = wait,
                     operation: @Sendable () async throws -> Data) async throws -> Data {
        var attempt = 0
        while Date() < deadline {
            try Task.checkCancellation()
            do { return try await operation() }
            catch {
                guard isTransientRead(error) else { throw error }
                let delay = retryDelay((error as? RelayPlatformError)?.retryAfter, attempt: attempt)
                guard Date().addingTimeInterval(delay) < deadline else { throw error }
                try await pause(delay)
                attempt += 1
            }
        }
        throw APIClient.APIError.message("家庭请求尚未返回，未自动重放操作")
    }
}

/// 可取消的设备级额度；等待者取消或获得额度后立即取消，都不会泄漏名额。
actor RelayRequestGate {
    private struct State {
        var running = 0
        var waiters: [(UUID, CheckedContinuation<Void, Error>)] = []
    }
    private let limit: Int
    private var states: [String: State] = [:]
    init(limit: Int) { self.limit = max(1, limit) }
    func waitingCount(key: String) -> Int { states[key]?.waiters.count ?? 0 }

    private func acquire(key: String) async throws {
        let identifier = UUID()
        try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
                if Task.isCancelled { continuation.resume(throwing: CancellationError()); return }
                var state = states[key] ?? State()
                if state.running < limit { state.running += 1; states[key] = state; continuation.resume() }
                else { state.waiters.append((identifier, continuation)); states[key] = state }
            }
        } onCancel: { Task { await self.cancel(key: key, identifier: identifier) } }
    }
    private func cancel(key: String, identifier: UUID) {
        guard var state = states[key], let index = state.waiters.firstIndex(where: { $0.0 == identifier }) else { return }
        let continuation = state.waiters.remove(at: index).1
        states[key] = state
        continuation.resume(throwing: CancellationError())
    }
    private func release(key: String) {
        guard var state = states[key] else { return }
        if state.waiters.isEmpty {
            state.running -= 1
            states[key] = state.running == 0 ? nil : state
        } else {
            let continuation = state.waiters.removeFirst().1
            states[key] = state
            continuation.resume()
        }
    }
    func withPermit<T: Sendable>(key: String, operation: @Sendable () async throws -> T) async throws -> T {
        try await acquire(key: key)
        defer { release(key: key) }
        try Task.checkCancellation()
        return try await operation()
    }
}

private final class NoRelayRedirect: NSObject, URLSessionTaskDelegate, @unchecked Sendable {
    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse, newRequest request: URLRequest, completionHandler: @escaping @Sendable (URLRequest?) -> Void) { completionHandler(nil) }
}
