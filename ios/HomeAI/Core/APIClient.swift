import Foundation
import CryptoKit

struct Connection: Codable, Sendable {
    var url: String
    var fingerprint: String
    var token: String
    var refreshToken: String
    var expiresAt: Date
    var tlsKeyFingerprint: String? = nil
    var deviceID: String? = nil
    var ownerID: String? = nil
    var serverID: String? = nil
    var serverPublicKey: String? = nil
    var namespaceAnchor: String? = nil
    var prefersDirect: Bool? = nil
    var directAccess: DirectAccess? = nil
    var addresses: [String]? = nil
    var namespaceIdentity: String { namespaceAnchor ?? tlsKeyFingerprint ?? fingerprint }
}

struct PairingCode: Codable, Sendable {
    var remote: EnrollmentAccess? = nil
    var expires_at: Double? = nil
    var url: String
    var fingerprint: String
    var token: String
    var schema_version: String? = nil
    var server_id: String? = nil
    var server_public_key: String? = nil
    var namespace_anchor: String? = nil
    var addresses: [String]? = nil
}

/// 网络与身份集中在单一 actor，避免页面自行处理签名或泄露会话。
struct TaskStateEvent: Decodable, Sendable {
    struct Item: Decodable, Sendable, Identifiable, Equatable {
        let id: String; let status: String
        var used_android: Bool? = nil
        var android_workspace_id: String? = nil
        var android_task_id: String? = nil
    }
    let type: String
    let tasks: [Item]
    let has_more: Bool
    let notification_revision: String?
    let data_revision: Int?
    let memory_revision: Int?
    let automation_revision: Int?
    var android_revision: Int? = nil
    var android_active_task_id: String? = nil
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
    private let identityGate = AsyncOperationGate()
    private var rejectedNamespace: String?
    private var authorizationSerial = 0
    private var authorizationFailureHandler: (@Sendable (String) async -> Void)?
    private var reachabilityHandler: (@Sendable (String, UUID) async -> Void)?
    private var pairingAttempt = UUID()
    private let directGate = AsyncOperationGate()
    private let directConnectionWork = SharedClientOperation<Void>()
    private var lastDirectFailure: (generation: UUID, publicOnly: Bool, until: Date, error: Error)?
    private var directPublicOnly = false
    private var fullGatheringNextAttempt = false
    private var directTransport: DirectTransport?
    private var directEvents: [UUID: Task<Void, Never>] = [:]
    private var eventSockets: [UUID: URLSessionWebSocketTask] = [:]
    private let preferredConnectionWork = SharedClientOperation<Void>()
    private struct HTTPSRoute {
        let address: URL
        let session: URLSession
        let generation: UUID
    }
    private var fastHTTPS: HTTPSRoute?
    private var localProbeAfter = Date.distantPast
    private var encryptedRelay: EncryptedRelay?
    private var relayGeneration: UUID?
    private var directUpgrade: Task<Void, Never>?
    private var directUpgradeAfter = Date.distantPast
    private enum PreparedRoute: Sendable {
        case local(FastConnection.Verified)
        case relay(EncryptedRelay)
    }

    func networkPathChanged() {
        // 换网后重新验证局域网地址；已发出的请求保留原连接，不重放。
        fastHTTPS?.session.finishTasksAndInvalidate()
        fastHTTPS = nil
        localProbeAfter = .distantPast
        directUpgradeAfter = .distantPast
    }

    private func preparePreferredConnection() async throws {
        let started = generation
        if relayGeneration != started { encryptedRelay = nil; relayGeneration = nil }
        if let route = fastHTTPS, route.generation == started { return }
        if let directTransport, await directTransport.isReady { return }
        if let encryptedRelay, await encryptedRelay.isReady { scheduleDirectUpgrade(); return }
        try await preferredConnectionWork.run(key: started.uuidString) {
            if await self.prepareFastRoute(expectedGeneration: started) { return }
            try await self.connectRemoteDirect(requirePublicPath: await self.directPublicOnly, resetEvents: false)
        }
        guard generation == started else { throw APIError.message("连接身份已切换") }
    }

    private func prepareFastRoute(expectedGeneration: UUID) async -> Bool {
        guard generation == expectedGeneration, !directPublicOnly,
              let saved = connection, let id = saved.serverID, let key = saved.serverPublicKey, let anchor = saved.namespaceAnchor else { return false }
        let probeLocal = Date() >= localProbeAfter
        localProbeAfter = Date().addingTimeInterval(10)
        let choice = await withTaskGroup(of: PreparedRoute?.self, returning: PreparedRoute?.self) { group in
            if probeLocal {
                group.addTask {
                    if let proof = await FastConnection.probe([saved.url] + (saved.addresses ?? []), serverID: id, publicKey: key, namespace: anchor) { return .local(proof) }
                    return nil
                }
            }
            if let access = saved.directAccess {
                group.addTask {
                    if let relay = try? await EncryptedRelay.connect(access: access, serverID: id, publicKey: key, namespace: anchor) { return .relay(relay) }
                    return nil
                }
            }
            for await result in group {
                if let result { group.cancelAll(); return result }
            }
            return nil
        }
        guard generation == expectedGeneration, !Task.isCancelled, let choice else { return false }
        switch choice {
        case .local(let found):
            let config = URLSessionConfiguration.ephemeral
            config.httpShouldSetCookies = false; config.urlCredentialStorage = nil; config.httpCookieStorage = nil
            config.timeoutIntervalForRequest = 15
            let session = URLSession(configuration: config, delegate: PinnedSession(fingerprint: found.identity.proof.fingerprint, keyFingerprint: found.identity.tlsKey), delegateQueue: nil)
            fastHTTPS = HTTPSRoute(address: URL(string: found.address)!, session: session, generation: expectedGeneration)
        case .relay(let relay):
            encryptedRelay = relay
            relayGeneration = expectedGeneration
            scheduleDirectUpgrade()
        }
        return true
    }

    private func scheduleDirectUpgrade() {
        guard directUpgrade == nil, Date() >= directUpgradeAfter else { return }
        directUpgradeAfter = Date().addingTimeInterval(60)
        let started = generation
        directUpgrade = Task {
            defer { directUpgrade = nil }
            guard generation == started else { return }
            try? await connectRemoteDirect(requirePublicPath: false, resetEvents: false)
        }
    }

    init(persistConnection: Bool = true) {
        self.persistConnection = persistConnection
        if persistConnection, let data = DeviceIdentity.read("connection"), let saved = try? JSONDecoder().decode(Connection.self, from: data) {
            connection = saved
            session = URLSession(configuration: .ephemeral, delegate: PinnedSession(fingerprint: saved.fingerprint, keyFingerprint: saved.tlsKeyFingerprint), delegateQueue: nil)
        }
    }

    #if DEBUG
    func verifyEncryptedRelayForAcceptance() async throws -> Double {
        _ = try await request("GET", "/api/v1/me")
        guard let saved = connection, let access = saved.directAccess, let id = saved.serverID,
              let key = saved.serverPublicKey, let anchor = saved.namespaceAnchor, let deviceID = saved.deviceID,
              let base = URL(string: saved.url) else { throw APIError.message("缺少已配对连接") }
        let start = Date()
        let relay = try await EncryptedRelay.connect(access: access, serverID: id, publicKey: key, namespace: anchor)
        let request = try signedRequest("GET", "/api/v1/me", body: nil, token: saved.token, base: base)
        let (data, status) = try await relay.request(request, deviceID: deviceID)
        guard status == 200, !data.isEmpty else { throw APIError.message("加密通道真实鉴权失败") }
        return Date().timeIntervalSince(start)
    }

    func expireAccessForAcceptance() throws {
        guard !persistConnection, connection != nil else { throw APIError.message("只允许隔离验收会话强制刷新") }
        connection?.expiresAt = .distantPast
    }

    /// 仅用于隔离真机验收，复用已配对设备；不覆盖用户的持久连接。
    func restoreAcceptanceConnection(_ saved: Connection, devicePublicKey: String) throws {
        guard !persistConnection, try identity.publicPEM() == devicePublicKey, saved.directAccess?.transport_policy == "direct_only" else {
            throw APIError.message("验收配置与当前设备密钥不匹配")
        }
        connection = saved
        generation = UUID()
        session = URLSession(configuration: .ephemeral, delegate: PinnedSession(fingerprint: saved.fingerprint, keyFingerprint: saved.tlsKeyFingerprint), delegateQueue: nil)
    }
    #endif

    func isConnected() -> Bool { connection != nil }
    func connectionGeneration() -> UUID { generation }

    func restoreConnectionIfNeeded() {
        guard connection == nil, persistConnection,
              let data = DeviceIdentity.read("connection"),
              let saved = try? JSONDecoder().decode(Connection.self, from: data) else { return }
        generation = UUID()
        connection = saved
        session = URLSession(configuration: .ephemeral, delegate: PinnedSession(fingerprint: saved.fingerprint, keyFingerprint: saved.tlsKeyFingerprint), delegateQueue: nil)
    }

    func pair(_ code: PairingCode) async throws {
        rejectedNamespace = nil
        if let expires = code.expires_at, expires <= Date().timeIntervalSince1970 { throw APIError.message("配对二维码已过期，请在管理端重新生成") }
        if code.schema_version == "3.0" { try await pairRemote(code); return }
        let attempt = UUID()
        pairingAttempt = attempt
        guard let url = URL(string: code.url), url.scheme == "https", code.fingerprint.count == 64 else { throw APIError.message("需要 HTTPS 地址和完整服务器证书指纹") }
        var verified: ServerTrust.Verified?
        if code.schema_version == "2.0" {
            guard let identifier = code.server_id, let publicKey = code.server_public_key, let anchor = code.namespace_anchor else {
                throw APIError.message("第二版配对信息缺少稳定身份")
            }
            verified = try await ServerTrust.probe(url: code.url, serverID: identifier, publicKey: publicKey)
            guard verified?.proof.namespace_anchor == anchor else { throw APIError.message("服务器数据命名空间不匹配") }
        } else if code.schema_version != nil { throw APIError.message("不支持此配对协议版本") }
        guard pairingAttempt == attempt else { throw APIError.message("配对请求已变化") }
        for address in [code.url] + (code.addresses ?? []) { _ = try ServerTrust.origin(address) }
        guard (code.addresses?.count ?? 0) <= 8 else { throw APIError.message("配对地址过多") }
        let pinning = PinnedSession(fingerprint: verified?.proof.fingerprint ?? code.fingerprint)
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
        connection = Connection(url: code.url, fingerprint: verified?.proof.fingerprint ?? code.fingerprint, token: result.access_token, refreshToken: result.refresh_token, expiresAt: Date().addingTimeInterval(Double(result.expires_in)))
        connection?.tlsKeyFingerprint = pinning.acceptedKeyFingerprint
        connection?.deviceID = result.device_id
        if let verified {
            connection?.serverID = verified.proof.server_id
            connection?.serverPublicKey = verified.proof.server_public_key
            connection?.namespaceAnchor = verified.proof.namespace_anchor
            let candidates = [code.url] + (code.addresses ?? []) + verified.proof.addresses
            var urls: [String] = []
            for address in candidates {
                if !urls.contains(address) && urls.count < 8 { urls.append(address) }
            }
            connection?.addresses = urls
        }
        await disableDirect()
        session = transport
        try persist()
    }

    private func pairRemote(_ code: PairingCode) async throws {
        #if DEBUG
        print("PAIR_STAGE remote_start")
        #endif
        guard let access = code.remote, let serverID = code.server_id,
              let publicKey = code.server_public_key, let anchor = code.namespace_anchor,
              code.token.count >= 32 else { throw APIError.message("远程配对码缺少家庭身份") }
        _ = try P256.Signing.PublicKey(pemRepresentation: publicKey)
        let attempt = UUID(); pairingAttempt = attempt
        let key = SymmetricKey(data: SHA256.hash(data: Data(("homeai-pair-bootstrap:v1\n" + code.token).utf8)))
        let body = try JSONSerialization.data(withJSONObject: ["token": code.token, "public_key": identity.publicPEM(), "signature": identity.sign(Data(("homeai-pair:" + code.token).utf8)), "name": "iPhone"])
        let aad = Data(("homeai-pair-bootstrap:v1\n" + access.id + "\nrequest").utf8)
        let sealed = try AES.GCM.seal(body, using: key, authenticating: aad)
        guard let combined = sealed.combined else { throw APIError.message("无法加密配对信息") }
        // 同一密文提交具有幂等保证，网络短暂中断可安全重试一次。
        do { _ = try await ConnectSignalling.enrollment(access, suffix: "request", ciphertext: combined.base64EncodedString()) }
        catch let error as URLError where [.timedOut, .networkConnectionLost, .cannotConnectToHost, .secureConnectionFailed].contains(error.code) {
            try await Task.sleep(for: .seconds(1))
            _ = try await ConnectSignalling.enrollment(access, suffix: "request", ciphertext: combined.base64EncodedString())
        }
        #if DEBUG
        print("PAIR_STAGE request_accepted")
        #endif
        let deadline = Date().addingTimeInterval(90)
        while Date() < deadline {
            try Task.checkCancellation()
            guard pairingAttempt == attempt else { throw APIError.message("配对请求已更新") }
            let received: Data
            do { received = try await ConnectSignalling.enrollment(access, suffix: "response") }
            catch let error as URLError where [.timedOut, .networkConnectionLost, .cannotConnectToHost, .secureConnectionFailed].contains(error.code) {
                try await Task.sleep(for: .seconds(1))
                continue
            }
            struct Reply: Decodable { let ciphertext: String? }
            if let encrypted = try JSONDecoder().decode(Reply.self, from: received).ciphertext, let raw = Data(base64Encoded: encrypted) {
                let decoded = try AES.GCM.open(AES.GCM.SealedBox(combined: raw), using: key, authenticating: Data(("homeai-pair-bootstrap:v1\n" + access.id + "\nresponse").utf8))
                struct Result: Decodable { let direct_access: DirectAccess }
                #if DEBUG
                print("PAIR_STAGE response_decrypted")
                #endif
                let tokens = try JSONDecoder().decode(Tokens.self, from: decoded)
                let direct = try JSONDecoder().decode(Result.self, from: decoded).direct_access
                guard direct.portal_url == access.portal_url, direct.instance_id == access.instance_id else { throw APIError.message("配对平台或家庭实例不匹配") }
                stopTaskEvents(); await directTransport?.close()
                generation = UUID()
                connection = Connection(url: code.url.isEmpty ? "https://homeai.invalid" : code.url, fingerprint: code.fingerprint, token: tokens.access_token, refreshToken: tokens.refresh_token, expiresAt: Date().addingTimeInterval(Double(tokens.expires_in)))
                connection?.deviceID = tokens.device_id; connection?.serverID = serverID
                connection?.serverPublicKey = publicKey; connection?.namespaceAnchor = anchor
                connection?.directAccess = direct; connection?.prefersDirect = true; connection?.addresses = code.addresses
                session = URLSession(configuration: .ephemeral, delegate: PinnedSession(fingerprint: code.fingerprint), delegateQueue: nil)
                // 保存配对后即使当前网络无法打洞，后续也只尝试已验证身份的直连。
                try persist()
                try await connectRemoteDirect()
                #if DEBUG
                print("PAIR_STAGE direct_connected")
                #endif
                return
            }
            try await Task.sleep(for: .seconds(1))
        }
        throw APIError.message("家庭服务器没有响应，请确认在线并重新生成二维码")
    }

    /// 当前通过可信 HTTPS 交换信令；业务请求切换到 DTLS 数据通道。
    func enableDirect() async throws {
        let started = generation
        guard let saved = connection, let publicKey = saved.serverPublicKey,
              let base = URL(string: saved.url), let session else { throw APIError.message("请先绑定稳定服务器身份") }
        let transport = try await DirectTransport()
        await directTransport?.close()
        directTransport = transport
        stopTaskEvents()
        do {
            let offer = try await transport.offer()
            let object = try JSONSerialization.jsonObject(with: offer)
            let body = try JSONSerialization.data(withJSONObject: ["envelope": object])
            let request = try signedRequest("POST", "/api/v1/direct/offer", body: body, token: saved.token, base: base)
            let (answer, response) = try await session.data(for: request)
            try Self.validate(answer, response)
            try await transport.accept(answer, serverPublicKey: publicKey)
            guard started == generation else { throw APIError.message("直连协商期间连接已切换") }
        } catch { await transport.close(); throw error }
    }

    func configureRemoteDirectAccess() async throws {
        let started = generation
        let data = try await request("POST", "/api/v1/remote/direct-access")
        let access = try JSONDecoder().decode(DirectAccess.self, from: data)
        guard started == generation, access.transport_policy == "direct_only" else { throw APIError.message("连接授权期间身份已变化") }
        connection?.directAccess = access
        lastDirectFailure = nil
        try persist()
    }

    func connectRemoteDirect(requirePublicPath: Bool = false, resetEvents: Bool = true) async throws {
        if requirePublicPath { fastHTTPS?.session.finishTasksAndInvalidate(); fastHTTPS = nil; encryptedRelay = nil }
        let started = generation
        let key = started.uuidString + (requirePublicPath ? ":public" : ":all")
        // 同一身份的页面共用一个建连结果，失败也一起返回，避免排队重复协商。
        try await directConnectionWork.run(key: key) {
            try await self.directGate.withPermit {
                try await self.establishRemoteDirect(requirePublicPath: requirePublicPath, resetEvents: resetEvents, expectedGeneration: started)
            }
        }
        guard started == generation else { throw APIError.message("连接身份已切换") }
    }

    // 仅在持有 directGate 时调用，配对和后台恢复共享同一条建连流程。
    private func establishRemoteDirect(requirePublicPath: Bool, resetEvents: Bool, expectedGeneration: UUID) async throws {
        guard expectedGeneration == generation else { throw APIError.message("连接身份已切换") }
        if let directTransport, directPublicOnly == requirePublicPath {
            if await directTransport.recoverIfPossible() {
                guard expectedGeneration == generation else { throw APIError.message("连接身份已切换") }
                lastDirectFailure = nil; return
            }
        }
        guard expectedGeneration == generation else { throw APIError.message("连接身份已切换") }
        if let failure = lastDirectFailure, failure.generation == generation, failure.publicOnly == requirePublicPath, failure.until > Date() { throw failure.error }
        let started = generation
        guard let saved = connection, let access = saved.directAccess, let publicKey = saved.serverPublicKey else { throw APIError.message("请先在家庭网络为此设备授权远程直连") }
        directPublicOnly = requirePublicPath
        connection?.prefersDirect = true
        try persist()
        let transport = try await DirectTransport(stunURLs: access.stun_urls, requirePublicPath: requirePublicPath, gatherAllCandidates: fullGatheringNextAttempt)
        guard started == generation else { await transport.close(); throw APIError.message("连接身份已切换") }
        await directTransport?.close()
        guard started == generation else { await transport.close(); throw APIError.message("连接身份已切换") }
        directTransport = transport
        if resetEvents { stopTaskEvents() }
        do {
            let offer = try await transport.offer()
            guard let object = try JSONSerialization.jsonObject(with: offer) as? [String: Any],
                  let payload = object["payload"] as? [String: Any], let session = payload["session"] as? String else { throw APIError.message("直连信令无效") }
            let body = try JSONSerialization.data(withJSONObject: ["envelope": object])
            _ = try await ConnectSignalling.request(access, method: "POST", path: "/api/direct/offers", body: body)
            let deadline = Date().addingTimeInterval(45)
            let pollingStarted = Date()
            while Date() < deadline {
                try Task.checkCancellation()
                guard started == generation else { throw APIError.message("直连协商期间身份已变化") }
                let data = try await ConnectSignalling.request(access, method: "GET", path: "/api/direct/answers/" + session)
                if let response = try JSONSerialization.jsonObject(with: data) as? [String: Any], let answer = response["envelope"] as? [String: Any] {
                    try await transport.accept(JSONSerialization.data(withJSONObject: answer), serverPublicKey: publicKey)
                    guard started == generation else { throw APIError.message("连接已切换") }
                    fullGatheringNextAttempt = false
                    lastDirectFailure = nil
                    return
                }
                try await Task.sleep(for: Date().timeIntervalSince(pollingStarted) < 5 ? .milliseconds(200) : .milliseconds(500))
            }
            throw APIError.message("家庭服务器离线或当前网络无法直连；不会使用中继")
        } catch {
            // 快照候选无法建立连接时，下次收集完整候选；不重放业务请求。
            if !Task.isCancelled, !(error is CancellationError), await transport.candidateSnapshotWasEarly { fullGatheringNextAttempt = true }
            if started == generation, !Task.isCancelled, !(error is CancellationError) {
                lastDirectFailure = (started, requirePublicPath, Date().addingTimeInterval(2), error)
            }
            await transport.close()
            throw error
        }
    }

    func disableDirect() async {
        lastDirectFailure = nil
        await directTransport?.close()
        directTransport = nil
        directPublicOnly = false
        connection?.prefersDirect = false
        try? persist()
        stopTaskEvents()
    }

    struct TrustInfo: Sendable { let bound: Bool; let serverID: String?; let url: String; let addresses: [String] }
    func trustInfo() -> TrustInfo? {
        guard let saved = connection else { return nil }
        return TrustInfo(bound: saved.serverID != nil && saved.serverPublicKey != nil, serverID: saved.serverID,
                         url: saved.url, addresses: saved.addresses ?? [saved.url])
    }

    // 仅通过当前已固定证书的认证通道升级旧连接，界面必须由用户显式触发。
    func enrollServerIdentity() async throws {
        let started = generation
        guard let saved = connection, saved.serverID == nil, let session else { throw APIError.message("连接已绑定身份或尚未配对") }
        let nonce = (UUID().uuidString + UUID().uuidString).replacingOccurrences(of: "-", with: "").lowercased()
        let data = try await request("GET", "/api/v1/server/identity?nonce=" + nonce)
        let envelope = try JSONDecoder().decode(ServerTrust.Envelope.self, from: data)
        guard let payload = Data(base64Encoded: envelope.payload), let observer = session.delegate as? PinnedSession,
              let peer = observer.acceptedFingerprint else { throw APIError.message("无法确认当前证书") }
        let advertised = try JSONDecoder().decode(ServerIdentityProof.self, from: payload)
        let proof = try ServerTrust.validate(data, nonce: nonce, peerFingerprint: peer, serverID: nil, publicKey: advertised.server_public_key)
        guard started == generation, var current = connection, current.serverID == nil,
              current.tlsKeyFingerprint == proof.namespace_anchor else {
            throw APIError.message("旧连接无法安全迁移，请使用本机第二版配对信息重新配对")
        }
        current.serverID = proof.server_id; current.serverPublicKey = proof.server_public_key
        current.namespaceAnchor = proof.namespace_anchor
        current.addresses = Array(([current.url] + proof.addresses.filter { $0 != current.url }).prefix(8))
        connection = current; try persist()
    }

    // 此操作只恢复信任和连接，不自动重放任何失败的业务请求。
    func verifyServerAddress(_ address: String) async throws {
        let started = generation
        guard let saved = connection, let identifier = saved.serverID, let key = saved.serverPublicKey else {
            throw APIError.message("请先通过原可信连接升级服务器身份，或使用第二版配对信息")
        }
        let verified = try await ServerTrust.probe(url: address, serverID: identifier, publicKey: key)
        guard started == generation, var current = connection, current.serverID == identifier,
              current.namespaceIdentity == verified.proof.namespace_anchor else { throw APIError.message("服务器身份或数据命名空间发生变化") }
        let knownAddresses = current.addresses ?? [current.url]
        current.url = address; current.fingerprint = verified.proof.fingerprint; current.tlsKeyFingerprint = verified.tlsKey
        var urls: [String] = []
        for item in [address] + verified.proof.addresses + knownAddresses where !urls.contains(item) && urls.count < 8 { urls.append(item) }
        current.addresses = urls
        for socket in eventSockets.values { socket.cancel(with: .goingAway, reason: nil) }
        eventSockets.removeAll(); await disableDirect(); generation = UUID()
        session?.invalidateAndCancel()
        current.prefersDirect = false
        connection = current
        session = URLSession(configuration: .ephemeral, delegate: PinnedSession(fingerprint: current.fingerprint, keyFingerprint: current.tlsKeyFingerprint), delegateQueue: nil)
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

    func pairedNamespace() -> String? {
        guard let saved = connection, let device = saved.deviceID else { return nil }
        return DeviceIdentity.hash(Data((saved.namespaceIdentity + ":" + device).utf8))
    }

    func cachedNamespace() -> String? {
        guard rejectedNamespace == nil else { return nil }
        return pairedNamespace()
    }

    func setAuthorizationFailureHandler(_ handler: @escaping @Sendable (String) async -> Void) {
        authorizationFailureHandler = handler
    }

    func setReachabilityHandler(_ handler: @escaping @Sendable (String, UUID) async -> Void) {
        reachabilityHandler = handler
    }

    func syncNamespace() async throws -> String {
        if connection?.deviceID == nil || rejectedNamespace != nil { try await loadIdentityIfNeeded() }
        guard let value = cachedNamespace() else { throw APIError.message("请先配对") }
        return value
    }

    struct OwnerIdentity: Sendable { let userID: String; let namespace: String }
    func cachedOwnerIdentity(expectedNamespace: String) -> OwnerIdentity? {
        guard cachedNamespace() == expectedNamespace, let saved = connection, let owner = saved.ownerID else { return nil }
        return OwnerIdentity(userID: owner, namespace: DeviceIdentity.hash(Data((saved.namespaceIdentity + ":owner:" + owner).utf8)))
    }

    func ownerIdentity(expectedNamespace: String) async throws -> OwnerIdentity {
        if let cached = cachedOwnerIdentity(expectedNamespace: expectedNamespace) { return cached }
        try await loadIdentityIfNeeded()
        guard let cached = cachedOwnerIdentity(expectedNamespace: expectedNamespace) else { throw APIError.message("连接已切换") }
        return cached
    }

    private func loadIdentityIfNeeded() async throws {
        let started = generation
        try await identityGate.withPermit { try await self.fetchIdentityIfNeeded(generation: started) }
    }

    private func fetchIdentityIfNeeded(generation started: UUID) async throws {
        guard started == generation else { throw APIError.message("连接已切换") }
        if connection?.ownerID != nil, connection?.deviceID != nil, rejectedNamespace == nil { return }
        _ = try await request("GET", "/api/v1/me")
    }

    private func persist() throws {
        if persistConnection { try DeviceIdentity.save(JSONEncoder().encode(connection), name: "connection") }
    }

    func request(_ method: String, _ path: String, body: Data? = nil, expectedNamespace: String? = nil, contentType: String = "application/json") async throws -> Data {
        if let expectedNamespace, pairedNamespace() != expectedNamespace { throw APIError.message("同步连接已切换，请重新开始") }
        let started = generation
        let namespace = pairedNamespace()
        let authorizationAtStart = authorizationSerial
        do {
            if connection?.prefersDirect == true {
                try await preparePreferredConnection()
                guard started == generation else { throw APIError.message("直连恢复期间身份已变化") }
            }
            guard var saved = connection, let session, let base = URL(string: saved.url) else { throw APIError.message("请先配对家庭服务器") }
            if saved.expiresAt.timeIntervalSinceNow < 60 && path != "/api/v1/session/renew" {
                saved = try await renewalGate.withPermit { try await self.renewIfNeeded(generation: started) }
            }
            let data: Data
            do {
                data = try await send(method, path, body: body, token: saved.token, base: base, session: session, expectedGeneration: started, contentType: contentType)
            } catch APIError.http(401, _) where path != "/api/v1/session/renew" {
                // 401 表示认证阶段已拒绝，刷新一次后重试；业务 403 不撤销设备身份。
                let rejectedToken = saved.token
                saved = try await renewalGate.withPermit { try await self.renewRejectedAccess(token: rejectedToken, generation: started) }
                data = try await send(method, path, body: body, token: saved.token, base: base, session: session, expectedGeneration: started, contentType: contentType)
            }
            guard started == generation else { throw APIError.message("连接已切换，请重试") }
            guard authorizationAtStart == authorizationSerial else { throw APIError.message("授权状态已变化，请重新验证") }
            if method == "GET", path == "/api/v1/me" {
                struct Me: Decodable { let user_id: String; let device_id: String; let server_addresses: [String]? }
                let verified = try JSONDecoder().decode(Me.self, from: data)
                connection?.ownerID = verified.user_id
                connection?.deviceID = verified.device_id
                if let addresses = verified.server_addresses {
                    var known = connection?.addresses ?? []
                    for value in addresses where (try? ServerTrust.origin(value)) != nil && !known.contains(value) { known.append(value) }
                    connection?.addresses = Array(known.suffix(8))
                }
                rejectedNamespace = nil
                if let verifiedNamespace = pairedNamespace() {
                    await ClientViewCache.shared.authorize(namespace: verifiedNamespace)
                    await MediaUploadStore.shared.authorize(namespace: verifiedNamespace)
                }
                try persist()
            }
            if let currentNamespace = cachedNamespace() {
                await reachabilityHandler?(currentNamespace, started)
            }
            guard started == generation, authorizationAtStart == authorizationSerial else { throw APIError.message("连接或授权状态已变化，请重试") }
            return data
        } catch {
            if started == generation, case APIError.http(let code, _) = error, Self.invalidatesCredentials(status: code), let namespace {
                let firstRejection = rejectedNamespace != namespace
                rejectedNamespace = namespace
                if firstRejection { authorizationSerial += 1 }
                connection?.ownerID = nil
                try? persist()
                await ClientViewCache.shared.invalidate(namespace: namespace, revoked: true)
                await DeviceDataSync().invalidate(namespace: namespace)
                await MediaUploadStore.shared.invalidate(namespace: namespace)
                if firstRejection { await authorizationFailureHandler?(namespace) }
            }
            throw error
        }
    }

    /// 前台状态流只推送标识与进度，业务结果必须继续调用鉴权接口。
    func taskEvents(taskID: String? = nil, expectedNamespace: String) async throws -> TaskEventSubscription {
        guard try await syncNamespace() == expectedNamespace else { throw APIError.message("连接已切换") }
        let started = generation
        if directTransport != nil || connection?.prefersDirect == true { return try directTaskEvents(taskID: taskID, namespace: expectedNamespace) }
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

    private func directTaskEvents(taskID: String?, namespace: String) throws -> TaskEventSubscription {
        if let taskID, UUID(uuidString: taskID) == nil { throw APIError.message("无效任务标识") }
        let identifier = UUID()
        let path = "/api/v1/direct/task-snapshot" + (taskID.map { "?task_id=" + $0 } ?? "")
        let events = AsyncThrowingStream<TaskStateEvent, Error>(bufferingPolicy: .bufferingNewest(1)) { continuation in
            let reader = Task {
                do {
                    while !Task.isCancelled {
                        let data = try await self.request("GET", path, expectedNamespace: namespace)
                        continuation.yield(try JSONDecoder().decode(TaskStateEvent.self, from: data))
                        try await Task.sleep(for: .seconds(1))
                    }
                    continuation.finish()
                } catch is CancellationError { continuation.finish() }
                catch { continuation.finish(throwing: error) }
                self.directEvents.removeValue(forKey: identifier)
            }
            directEvents[identifier] = reader
            continuation.onTermination = { _ in reader.cancel() }
        }
        return TaskEventSubscription(id: identifier, events: events)
    }

    func closeTaskEvents(_ identifier: UUID) {
        directEvents.removeValue(forKey: identifier)?.cancel()
        eventSockets.removeValue(forKey: identifier)?.cancel(with: .goingAway, reason: nil)
    }

    func stopTaskEvents() {
        for reader in directEvents.values { reader.cancel() }
        directEvents.removeAll()
        for socket in eventSockets.values { socket.cancel(with: .goingAway, reason: nil) }
        eventSockets.removeAll()
    }

    /// 返回规范资料标识；完成后的解析由服务端媒体 Worker 自动编排。
    func uploadDocument(name: String, contents: Data) async throws -> String {
        let namespace = try await syncNamespace()
        let limits = await MediaUploadLimits.fetch(api: self, namespace: namespace)
        let item = try await MediaUploadStore.shared.enqueue(data: contents, name: name, kind: "file", mime: "application/octet-stream", namespace: namespace, scope: "data", maximumBytes: limits.file_max_bytes)
        let completed = try await MediaUploadStore.shared.upload(id: item.id, namespace: namespace, api: self) { _ in }
        guard let asset = completed.asset else { throw APIError.message("文件上传尚未完成") }
        try await MediaUploadStore.shared.remove(id: item.id, namespace: namespace)
        return asset.record_id
    }

    nonisolated static func invalidatesCredentials(status: Int) -> Bool { status == 401 }

    private func renewRejectedAccess(token: String, generation started: UUID) async throws -> Connection {
        guard started == generation else { throw APIError.message("连接已切换") }
        if connection?.token == token { connection?.expiresAt = .distantPast }
        return try await renewIfNeeded(generation: started)
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
        let localRoute = fastHTTPS?.generation == expectedGeneration ? fastHTTPS : nil
        let request = try signedRequest(method, path, body: body, token: token, base: localRoute?.address ?? base, contentType: contentType)
        do {
            if let localRoute {
                let (data, response) = try await localRoute.session.data(for: request)
                guard expectedGeneration == generation else { throw APIError.message("连接已切换") }
                try Self.validate(data, response)
                return data
            }
            let directReady = if let directTransport { await directTransport.isReady } else { false }
            if !directReady, relayGeneration == expectedGeneration, let relay = encryptedRelay, let deviceID = connection?.deviceID {
                let data: Data, status: Int
                do {
                    (data, status) = try await relay.request(deviceID: deviceID) {
                        try await self.makeQueuedRelayRequest(method, path, body: body, token: token, base: base,
                            expectedGeneration: expectedGeneration, contentType: contentType)
                    }
                } catch {
                    if generation == expectedGeneration, encryptedRelay === relay, EncryptedRelay.shouldDiscardConnection(after: error) {
                        encryptedRelay = nil
                    }
                    throw error
                }
                guard expectedGeneration == generation else { throw APIError.message("连接已切换") }
                guard let response = HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: nil, headerFields: nil) else { throw APIError.message("加密响应无效") }
                try Self.validate(data, response)
                return data
            }
            if let directTransport {
                let (data, status) = try await directTransport.request(request)
                guard expectedGeneration == generation else { throw APIError.message("连接已切换，请重试") }
                guard let response = HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: nil, headerFields: nil) else { throw APIError.message("直连响应无效") }
                try Self.validate(data, response)
                return data
            }
            guard connection?.prefersDirect != true else { throw APIError.message("直连尚未建立，不允许回退") }
            let (data, response) = try await session.data(for: request)
            guard expectedGeneration == generation else { throw APIError.message("连接已切换，请重试") }
            try Self.validate(data, response)
            return data
        } catch {
            if let localRoute, error is URLError, fastHTTPS?.session === localRoute.session {
                fastHTTPS?.session.finishTasksAndInvalidate(); fastHTTPS = nil
            }
            guard expectedGeneration == generation else { throw APIError.message("连接已切换，请重试") }
            throw error
        }
    }

    private func makeQueuedRelayRequest(_ method: String, _ path: String, body: Data?, token: String, base: URL,
                                        expectedGeneration: UUID, contentType: String) throws -> URLRequest {
        try Task.checkCancellation()
        guard generation == expectedGeneration else { throw APIError.message("连接身份已切换") }
        return try signedRequest(method, path, body: body, token: token, base: base, contentType: contentType)
    }

    private static func validate(_ data: Data, _ response: URLResponse) throws {
        guard let response = response as? HTTPURLResponse, (200..<300).contains(response.statusCode) else {
            let body = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
            throw APIError.http((response as? HTTPURLResponse)?.statusCode ?? 0, body?["detail"] as? String ?? "服务器请求失败")
        }
    }
}
