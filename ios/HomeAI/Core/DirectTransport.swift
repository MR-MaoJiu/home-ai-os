import Foundation
import CryptoKit
@preconcurrency import WebRTC

/// 只负责两端数据通道；信令交换由调用方提供，不连接任何中继。
@MainActor
final class DirectTransport: NSObject, RTCPeerConnectionDelegate, RTCDataChannelDelegate {
    private static let factory = RTCPeerConnectionFactory()
    private var peer: RTCPeerConnection!
    private var channel: RTCDataChannel!
    private var inbox: [Data] = []
    private var closed = false
    private var busy = false
    private var sessionID: String?
    private var offerDigest: String?
    private var answerAccepted = false
    private let identity = DeviceIdentity()
    private let requirePublicPath: Bool
    private let frameSize = 1024
    private let maxMessage = 16384
    private let window = 64
    private let maximumBody = 30 * 1024 * 1024
    private let ack = Data([0]) + Data("homeai-ack-v1".utf8)

    init(stunURLs: [String] = [], requirePublicPath: Bool = false) throws {
        self.requirePublicPath = requirePublicPath
        guard stunURLs.count <= 2, stunURLs.allSatisfy({ $0.hasPrefix("stun:") && $0.count <= 256 && !$0.contains("@") && !$0.contains("?") && !$0.contains("#") && !$0.contains("\n") && !$0.contains("\r") }) else { throw APIClient.APIError.message("只允许 STUN，禁止 TURN 中继") }
        super.init()
        let config = RTCConfiguration()
        config.iceServers = stunURLs.map { RTCIceServer(urlStrings: [$0]) }
        config.iceTransportPolicy = .all
        config.tcpCandidatePolicy = .disabled
        config.sdpSemantics = .unifiedPlan
        peer = Self.factory.peerConnection(with: config, constraints: RTCMediaConstraints(mandatoryConstraints: nil, optionalConstraints: nil), delegate: self)
        let dataConfig = RTCDataChannelConfiguration()
        dataConfig.isNegotiated = true
        dataConfig.channelId = 0
        dataConfig.isOrdered = true
        dataConfig.`protocol` = "homeai.direct.v1"
        channel = peer.dataChannel(forLabel: "homeai", configuration: dataConfig)
        channel.delegate = self
    }

    private static func canonical(_ value: [String: Any]) throws -> Data {
        try JSONSerialization.data(withJSONObject: value, options: [.sortedKeys, .withoutEscapingSlashes])
    }

    private func error(_ text: String) -> Error { APIClient.APIError.message(text) }

    private func waitUntil(_ predicate: () -> Bool, seconds: TimeInterval = 15, deadline: Date? = nil, reason: String = "当前网络无法建立或维持直连，不会回退到中继") async throws {
        let timeoutAt = min(Date().addingTimeInterval(seconds), deadline ?? .distantFuture)
        while !predicate() {
            if closed { throw error("直连已关闭") }
            if Date() >= timeoutAt { throw error(reason) }
            try await Task.sleep(for: .milliseconds(10))
        }
        try Task.checkCancellation()
        if closed { throw error("直连已关闭") }
    }

    private static func validateSDP(_ sdp: String) throws {
        guard sdp.utf8.count <= 32768, sdp.unicodeScalars.allSatisfy({ $0.isASCII }) else { throw APIClient.APIError.message("直连描述无效") }
        let lines = sdp.components(separatedBy: "\r\n")
        let media = lines.filter { $0.hasPrefix("m=") }
        guard media.count == 1, media[0].hasPrefix("m=application "), media[0].contains("UDP/DTLS/SCTP"),
              lines.contains(where: { $0.hasPrefix("a=fingerprint:") }), lines.contains("a=end-of-candidates") else {
            throw APIClient.APIError.message("需要完整的加密数据通道描述")
        }
        let candidates = lines.filter { $0.hasPrefix("a=candidate:") }
        guard !candidates.isEmpty, candidates.count <= 32 else { throw APIClient.APIError.message("没有可用直连地址") }
        for candidate in candidates {
            let fields = candidate.split(separator: " ")
            guard fields.count >= 8, fields[1] == "1", fields[2].lowercased() == "udp", fields[6] == "typ",
                  ["host", "srflx"].contains(String(fields[7])) else { throw APIClient.APIError.message("仅允许 UDP 直连候选，禁止中继") }
        }
    }

    private static func publicCandidates(_ sdp: String) -> String {
        sdp.components(separatedBy: "\r\n").filter { line in
            !line.hasPrefix("a=candidate:") || line.contains(" typ srflx")
        }.joined(separator: "\r\n")
    }

    func offer() async throws -> Data {
        guard sessionID == nil else { throw error("不能重复使用直连协商") }
        sessionID = (UUID().uuidString + UUID().uuidString).replacingOccurrences(of: "-", with: "").lowercased()
        do {
            let sdp: String = try await withCheckedThrowingContinuation { continuation in
                peer.offer(for: RTCMediaConstraints(mandatoryConstraints: nil, optionalConstraints: nil)) { description, failure in
                    if let failure { continuation.resume(throwing: failure) }
                    else if let description { continuation.resume(returning: description.sdp) }
                    else { continuation.resume(throwing: APIClient.APIError.message("未生成直连描述")) }
                }
            }
            try await setDescription(sdp, local: true)
            try await waitUntil({ peer.iceGatheringState == .complete }, seconds: 40, reason: "直连地址收集超时，请检查 STUN 与网络")
            guard var complete = peer.localDescription?.sdp else { throw error("直连描述缺失") }
            if !complete.contains("a=end-of-candidates") { complete += "a=end-of-candidates\r\n" }
            if requirePublicPath { complete = Self.publicCandidates(complete) }
            try Self.validateSDP(complete)
            let payload: [String: Any] = ["version": 1, "session": sessionID!, "type": "offer", "sdp": complete,
                                          "expires": Int(Date().timeIntervalSince1970) + 60, "offer_digest": NSNull()]
            let encoded = try Self.canonical(payload)
            offerDigest = DeviceIdentity.hash(encoded)
            let signature = try identity.sign(Data("homeai-direct-sdp:v1\n".utf8) + encoded)
            return try Self.canonical(["payload": payload, "signature": signature])
        } catch { close(); throw error }
    }

    func accept(_ data: Data, serverPublicKey: String) async throws {
        guard !answerAccepted, let sessionID, let offerDigest, data.count <= 36864 else { throw error("直连响应状态无效") }
        do {
            guard let envelope = try JSONSerialization.jsonObject(with: data) as? [String: Any],
                  Set(envelope.keys) == ["payload", "signature"], let payload = envelope["payload"] as? [String: Any],
                  Set(payload.keys) == ["version", "session", "type", "sdp", "expires", "offer_digest"],
                  let signatureString = envelope["signature"] as? String, let signature = Data(base64Encoded: signatureString),
                  payload["version"] as? Int == 1, payload["session"] as? String == sessionID,
                  payload["type"] as? String == "answer", payload["offer_digest"] as? String == offerDigest,
                  let expires = payload["expires"] as? Int, let sdp = payload["sdp"] as? String else { throw error("直连响应不属于当前会话") }
            let now = Date().timeIntervalSince1970
            guard Double(expires) > now, Double(expires) <= now + 65 else { throw error("直连响应已过期") }
            let key = try P256.Signing.PublicKey(pemRepresentation: serverPublicKey)
            let signed = Data("homeai-direct-sdp:v1\n".utf8) + (try Self.canonical(payload))
            guard key.isValidSignature(try P256.Signing.ECDSASignature(derRepresentation: signature), for: signed) else { throw error("家庭服务器直连身份不匹配") }
            try Self.validateSDP(sdp)
            answerAccepted = true
            let acceptedSDP = requirePublicPath ? Self.publicCandidates(sdp) : sdp
            try Self.validateSDP(acceptedSDP)
            try await setDescription(acceptedSDP, local: false)
            try await waitUntil({ channel.readyState == .open && peer.connectionState == .connected })
        } catch { close(); throw error }
    }

    private func setDescription(_ sdp: String, local: Bool) async throws {
        try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
            let completion: @Sendable (Error?) -> Void = { failure in
                if let failure { continuation.resume(throwing: failure) } else { continuation.resume() }
            }
            let description = RTCSessionDescription(type: local ? .offer : .answer, sdp: sdp)
            if local { peer.setLocalDescription(description, completionHandler: completion) }
            else { peer.setRemoteDescription(description, completionHandler: completion) }
        }
    }

    private func send(_ data: Data, deadline: Date? = nil) async throws {
        guard data.count <= maxMessage else { throw error("直连数据帧过大") }
        try await waitUntil({ channel.readyState == .open && channel.bufferedAmount <= UInt64(maxMessage * 4) }, seconds: 45, deadline: deadline)
        guard channel.sendData(RTCDataBuffer(data: data, isBinary: true)) else { throw error("直连发送失败；请核对操作结果，不要自动重试") }
    }

    private func receive(deadline: Date? = nil) async throws -> Data {
        try await waitUntil({ !inbox.isEmpty }, seconds: 60, deadline: deadline)
        return inbox.removeFirst()
    }

    struct Response: Decodable {
        let kind: String
        let wire_version: Int
        let id: String
        let status: Int
        let size: Int
        let headers: [String: String]
    }

    func request(_ request: URLRequest) async throws -> (Data, Int) {
        try await waitUntil({ !busy }, seconds: 180)
        busy = true
        let deadline = Date().addingTimeInterval(180)
        defer { busy = false }
        do {
            guard answerAccepted, let url = request.url, let components = URLComponents(url: url, resolvingAgainstBaseURL: false) else { throw error("直连尚未就绪") }
            let path = components.percentEncodedPath + (components.percentEncodedQuery.map { "?" + $0 } ?? "")
            let body = request.httpBody ?? Data()
            guard body.count <= maximumBody, path.hasPrefix("/api/v1/") else { throw error("不支持该直连请求") }
            let identifier = UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased()
            let headers = Dictionary(uniqueKeysWithValues: (request.allHTTPHeaderFields ?? [:]).map { ($0.key.lowercased(), $0.value) })
            try await send(Self.canonical(["wire_version": 3, "kind": "request", "id": identifier, "method": request.httpMethod ?? "GET", "target": path, "headers": headers, "size": body.count]), deadline: deadline)
            for (index, offset) in stride(from: 0, to: body.count, by: frameSize).enumerated() {
                try await send(body.subdata(in: offset..<min(body.count, offset + frameSize)), deadline: deadline)
                if (index + 1) % window == 0 || offset + frameSize >= body.count {
                    guard try await receive(deadline: deadline) == ack else { throw error("直连分帧确认不匹配") }
                }
            }
            let response = try JSONDecoder().decode(Response.self, from: await receive(deadline: deadline))
            guard response.wire_version == 3, response.kind == "response", response.id == identifier, (100...599).contains(response.status), (0...maximumBody).contains(response.size) else { throw error("直连响应不匹配") }
            var data = Data()
            var frames = 0
            while data.count < response.size {
                let frame = try await receive(deadline: deadline)
                guard !frame.isEmpty, frame.count <= frameSize, frame.count <= response.size - data.count else { throw error("直连响应长度错误") }
                data.append(frame)
                frames += 1
                if frames % window == 0 || data.count == response.size { try await send(ack, deadline: deadline) }
            }
            return (data, response.status)
        } catch { close(); throw error }
    }

    var isReady: Bool { !closed && answerAccepted && channel.readyState == .open && peer.connectionState == .connected }

    func close() {
        guard !closed else { return }
        closed = true
        inbox.removeAll()
        channel?.close()
        peer?.close()
    }

    nonisolated func dataChannelDidChangeState(_ dataChannel: RTCDataChannel) {
        if dataChannel.readyState == .closed { Task { @MainActor in close() } }
    }
    nonisolated func dataChannel(_ dataChannel: RTCDataChannel, didReceiveMessageWith buffer: RTCDataBuffer) {
        let data = buffer.data
        let binary = buffer.isBinary
        Task { @MainActor in
            guard !closed else { return }
            guard binary, data.count <= maxMessage, inbox.count < 128 else { close(); return }
            inbox.append(data)
        }
    }
    nonisolated func peerConnection(_ peerConnection: RTCPeerConnection, didChange stateChanged: RTCSignalingState) {}
    nonisolated func peerConnection(_ peerConnection: RTCPeerConnection, didAdd stream: RTCMediaStream) {}
    nonisolated func peerConnection(_ peerConnection: RTCPeerConnection, didRemove stream: RTCMediaStream) {}
    nonisolated func peerConnectionShouldNegotiate(_ peerConnection: RTCPeerConnection) {}
    nonisolated func peerConnection(_ peerConnection: RTCPeerConnection, didChange newState: RTCIceConnectionState) {}
    nonisolated func peerConnection(_ peerConnection: RTCPeerConnection, didChange newState: RTCIceGatheringState) {}
    nonisolated func peerConnection(_ peerConnection: RTCPeerConnection, didGenerate candidate: RTCIceCandidate) {}
    nonisolated func peerConnection(_ peerConnection: RTCPeerConnection, didRemove candidates: [RTCIceCandidate]) {}
    nonisolated func peerConnection(_ peerConnection: RTCPeerConnection, didOpen dataChannel: RTCDataChannel) { dataChannel.close() }
}
