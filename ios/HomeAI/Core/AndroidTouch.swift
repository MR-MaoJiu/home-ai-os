import Foundation

struct AndroidTouchPoint: Codable, Equatable, Sendable {
    let id: Int
    let x: Double
    let y: Double
    var isValid: Bool { (0..<5).contains(id) && x.isFinite && y.isFinite && (0...1).contains(x) && (0...1).contains(y) }
}

enum AndroidTouchPhase: String, Codable, Sendable {
    case down, pointer_down, move, pointer_up, up, cancel, keepalive
}
struct AndroidTouchEvent: Equatable, Sendable {
    let phase: AndroidTouchPhase
    let changedID: Int?
    let points: [AndroidTouchPoint]
}
struct AndroidTouchPacket: Encodable, Sendable {
    let gesture_id: String
    let epoch: Int
    let sequence: Int
    let frame_id: String
    let phase: AndroidTouchPhase
    let changed_id: Int?
    let points: [AndroidTouchPoint]
}
struct AndroidTouchHandshake: Decodable, Sendable {
    let status: String
    let protocolVersion: Int?
    let reason: String?
    let channelID: String?
    let heartbeatRequired: Bool?
    enum CodingKeys: String, CodingKey {
        case status, reason
        case protocolVersion = "protocol", channelID = "channel_id", heartbeatRequired = "heartbeat_required"
    }
    var isReady: Bool { status == "ready" && protocolVersion == 1 && heartbeatRequired == true && AndroidTouchWire.validChannelID(channelID) }
}

/// 通道代次只在传输外层使用；业务手势及其缓存不保存可跨连接复用的凭据。
enum AndroidTouchWire {
    static func validChannelID(_ value: String?) -> Bool {
        guard let value, value.utf8.count == 32 else { return false }
        return value.utf8.allSatisfy { (48...57).contains($0) || (65...90).contains($0) || (97...122).contains($0) || $0 == 45 || $0 == 95 }
    }
    static func encode(_ packet: AndroidTouchPacket, channelID: String?) throws -> Data {
        guard validChannelID(channelID), let channelID,
              var object = try JSONSerialization.jsonObject(with: JSONEncoder().encode(packet)) as? [String: Any] else {
            throw APIClient.APIError.message("触控通道尚未完成安全握手")
        }
        object["channel_id"] = channelID
        return try JSONSerialization.data(withJSONObject: object)
    }
}

/// 心跳挑战与手势确认是不同消息，固定字段防止将控制数据误当成执行命令。
struct AndroidTouchHeartbeatMessage: Equatable, Sendable {
    let status: String
    let channelID: String
    let nonce: String

    static func decode(_ data: Data) -> AndroidTouchHeartbeatMessage? {
        guard data.count <= 1024, let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              Set(object.keys) == Set(["status", "channel_id", "nonce"]),
              let status = object["status"] as? String, ["ping", "alive"].contains(status),
              let channelID = object["channel_id"] as? String, AndroidTouchWire.validChannelID(channelID),
              let nonce = object["nonce"] as? String, nonce.utf8.count == 32,
              nonce.utf8.allSatisfy({ (48...57).contains($0) || (97...102).contains($0) }) else { return nil }
        return .init(status: status, channelID: channelID, nonce: nonce)
    }
    func reply() throws -> Data {
        try JSONSerialization.data(withJSONObject: ["type": "heartbeat", "channel_id": channelID, "nonce": nonce])
    }
}

/// 只有当前代次已成功回过的挑战获服务端确认，才能开始新的触控。
struct AndroidTouchLiveness {
    private(set) var channelID: String?
    private var beganAt: TimeInterval?
    private var lastAliveAt: TimeInterval?
    private var pending: [String: TimeInterval] = [:]
    private var seen: [String] = []

    mutating func begin(channelID: String, at time: TimeInterval) {
        invalidate(); self.channelID = channelID; beganAt = time
    }
    mutating func invalidate() {
        channelID = nil; beganAt = nil; lastAliveAt = nil; pending.removeAll(); seen.removeAll()
    }
    func canReply(to message: AndroidTouchHeartbeatMessage) -> Bool {
        message.status == "ping" && message.channelID == channelID && !seen.contains(message.nonce)
    }
    mutating func didReply(to message: AndroidTouchHeartbeatMessage, at time: TimeInterval) {
        guard canReply(to: message) else { return }
        pending = pending.filter { time - $0.value <= 1.5 }
        pending[message.nonce] = time; seen.append(message.nonce)
        if seen.count > 64 { seen.removeFirst() }
    }
    @discardableResult
    mutating func accept(_ message: AndroidTouchHeartbeatMessage, at time: TimeInterval) -> Bool {
        guard message.status == "alive", message.channelID == channelID,
              let repliedAt = pending.removeValue(forKey: message.nonce),
              time >= repliedAt, time - repliedAt <= 1.5 else { return false }
        lastAliveAt = time
        return true
    }
    func isReady(at time: TimeInterval) -> Bool {
        guard channelID != nil, let lastAliveAt else { return false }
        return time >= lastAliveAt && time - lastAliveAt < 1.5
    }
    func isExpired(at time: TimeInterval) -> Bool {
        guard let lastProof = lastAliveAt ?? beganAt else { return false }
        return time - lastProof >= 1.5
    }
}

struct AndroidTouchAck: Decodable, Sendable {
    let gesture_id: String
    let sequence: Int
    let status: String
    let reason: String?
    var isValid: Bool {
        UUID(uuidString: gesture_id) != nil && sequence > 0 && ["accepted", "canceled", "rejected"].contains(status)
    }
}

/// 可靠有序通道只发送当前手势；合并移动，不重放失败、取消或断线后的输入。
@MainActor
final class AndroidTouchPipeline {
    private struct Gesture {
        let id: String
        let epoch: Int
        let frameID: String
        var nextSequence = 1
        var acknowledged = 0
        var sentAt: [Int: TimeInterval] = [:]
        var points: [AndroidTouchPoint]
        var pointerIDs: Set<Int>
        var ended = false
        var terminalSequence: Int?
        var lastSentAt: TimeInterval = 0
        var lastMoveAt: TimeInterval = -.infinity
    }
    private struct Pending { let gestureID: String; var event: AndroidTouchEvent }
    private let send: (AndroidTouchPacket) -> Bool
    private let now: () -> TimeInterval
    private let failure: (String) -> Void
    private let activity: (Bool) -> Void
    private var gestures: [String: Gesture] = [:]
    private var activeID: String?
    private var pending: [Pending] = []
    private var failed = false
    var isActive: Bool { activeID != nil }
    var pendingCount: Int { pending.count }
    var hasPendingWork: Bool { !gestures.isEmpty || !pending.isEmpty }

    init(now: @escaping () -> TimeInterval = { ProcessInfo.processInfo.systemUptime },
         send: @escaping (AndroidTouchPacket) -> Bool,
         activity: @escaping (Bool) -> Void = { _ in }, failure: @escaping (String) -> Void) {
        self.now = now; self.send = send; self.activity = activity; self.failure = failure
    }

    @discardableResult
    func enqueue(_ event: AndroidTouchEvent, epoch: Int, frameID: String) -> Bool {
        guard !failed else { return false }
        if event.phase == .cancel { cancel(); return true }
        guard (1...5).contains(event.points.count), event.points.allSatisfy(\.isValid),
              Set(event.points.map(\.id)).count == event.points.count else { fail("触控坐标无效，手势已取消"); return false }
        let identifier: String
        if event.phase == .down {
            guard activeID == nil, event.points.count == 1, event.changedID == event.points.first?.id,
                  epoch > 0, (16...128).contains(frameID.utf8.count), gestures.count < 4 else { fail("触控开始顺序无效，手势已取消"); return false }
            identifier = UUID().uuidString.lowercased()
            gestures[identifier] = Gesture(id: identifier, epoch: epoch, frameID: frameID,
                points: event.points, pointerIDs: Set(event.points.map(\.id)))
            activeID = identifier; activity(true)
        } else {
            guard let current = activeID, var gesture = gestures[current], gesture.epoch == epoch else {
                fail("控制权或手势已变化，请重新触控"); return false
            }
            identifier = current
            let incoming = Set(event.points.map(\.id))
            switch event.phase {
            case .pointer_down:
                guard let changed = event.changedID, !gesture.pointerIDs.contains(changed),
                      incoming == gesture.pointerIDs.union([changed]) else { fail("新增触点顺序无效"); return false }
                gesture.pointerIDs = incoming
            case .move:
                guard event.changedID == nil, incoming == gesture.pointerIDs else { fail("触点移动顺序无效"); return false }
            case .pointer_up, .up:
                guard let changed = event.changedID, incoming == gesture.pointerIDs, incoming.contains(changed),
                      (event.phase == .up ? incoming.count == 1 : incoming.count > 1) else { fail("触点抬起顺序无效"); return false }
                gesture.pointerIDs.remove(changed)
                if event.phase == .up { gesture.ended = true; activeID = nil; activity(false) }
            default: fail("不支持的触控输入"); return false
            }
            gesture.points = event.points
            gestures[identifier] = gesture
        }
        // 只覆盖连续移动，不越过任何按下、增加触点或抬起事件。
        if event.phase == .move, pending.last?.gestureID == identifier, pending.last?.event.phase == .move {
            pending[pending.count - 1].event = event
        } else {
            guard pending.count < 48 else { fail("触控发送队列已满，手势已取消"); return false }
            pending.append(Pending(gestureID: identifier, event: event))
        }
        flush()
        return !failed
    }

    func receive(_ ack: AndroidTouchAck) {
        guard !failed, ack.isValid, var gesture = gestures[ack.gesture_id] else { return }
        guard ack.sequence < gesture.nextSequence else {
            fail("触控确认顺序不匹配，手势已取消"); return
        }
        // 看门狗或接管取消引用最后已接受序号，不能按重复成功确认忽略。
        guard ack.status == "accepted" else {
            fail(ack.reason.map { String($0.prefix(200)) } ?? "安卓端已取消当前手势"); return
        }
        guard ack.sequence > gesture.acknowledged else { return }
        guard ack.sequence == gesture.acknowledged + 1 else {
            fail("触控确认顺序不匹配，手势已取消"); return
        }
        gesture.acknowledged = ack.sequence
        gesture.sentAt.removeValue(forKey: ack.sequence)
        if gesture.terminalSequence == ack.sequence { gestures.removeValue(forKey: ack.gesture_id) }
        else { gestures[ack.gesture_id] = gesture }
        flush()
    }

    func tick() {
        guard !failed else { return }
        let time = now()
        for gesture in gestures.values {
            for (sequence, sent) in gesture.sentAt {
                if time - sent > (sequence == 1 ? 3 : 2) { fail("触控确认超时，手势已取消且不会重发"); return }
            }
        }
        if let activeID, let gesture = gestures[activeID], gesture.nextSequence > 1,
           time - gesture.lastSentAt >= 0.5, !pending.contains(where: { $0.gestureID == activeID }) {
            guard pending.count < 48 else { fail("触控发送队列已满，手势已取消"); return }
            pending.append(Pending(gestureID: activeID, event: AndroidTouchEvent(phase: .keepalive, changedID: nil, points: [])))
        }
        flush()
    }

    func cancel() {
        let active = gestures.values.filter { $0.nextSequence > 1 && $0.terminalSequence == nil }
        pending.removeAll(); gestures.removeAll(); activeID = nil; activity(false)
        for gesture in active {
            _ = send(AndroidTouchPacket(gesture_id: gesture.id, epoch: gesture.epoch,
                sequence: gesture.nextSequence, frame_id: gesture.frameID, phase: .cancel,
                changed_id: nil, points: []))
        }
    }

    private func fail(_ reason: String) {
        guard !failed else { return }; failed = true
        cancel(); failure(reason)
    }

    private func flush() {
        while !pending.isEmpty && !failed {
            let item = pending[0]
            guard var gesture = gestures[item.gestureID] else { pending.removeFirst(); continue }
            // 通道与服务端都严格保序，触点边界不等待 ACK，避免网络往返压缩双击间隔。
            let time = now()
            if item.event.phase == .move && time - gesture.lastMoveAt < 0.05 {
                if pending.count > 1 && pending[1].gestureID == item.gestureID && pending[1].event.phase != .move {
                    pending.removeFirst(); continue // 后续边界事件已经携带最新的全部坐标。
                }
                return
            }
            guard gesture.sentAt.count < 32 else { fail("触控确认积压，手势已取消"); return }
            let packet = AndroidTouchPacket(gesture_id: gesture.id, epoch: gesture.epoch,
                sequence: gesture.nextSequence, frame_id: gesture.frameID, phase: item.event.phase,
                changed_id: item.event.changedID, points: item.event.points)
            guard send(packet) else { fail("触控通道暂不可用，手势已取消且不会重发"); return }
            pending.removeFirst()
            gesture.sentAt[gesture.nextSequence] = time
            if item.event.phase == .up || item.event.phase == .cancel { gesture.terminalSequence = gesture.nextSequence }
            gesture.nextSequence += 1; gesture.lastSentAt = time
            if item.event.phase == .move { gesture.lastMoveAt = time }
            gestures[item.gestureID] = gesture
        }
    }
}
