import AVFoundation
import Foundation

/// 只协调应用内现有播放/录音的音频会话；旧持有者不能关闭新持有者的会话。
/// 所有状态和 AudioUnit 生命周期均通过 queue 串行访问。
final class AppAudioLease: @unchecked Sendable {
    enum Kind: Sendable, Equatable { case speech, recording, remotePlayback, call }
    static let shared = AppAudioLease()
    private let queue = DispatchQueue(label: "dev.homeai.audio-lease")
    private let key = DispatchSpecificKey<UInt8>()
    private var owner: UUID?
    private var kind: Kind?
    private var interrupted: (@Sendable () -> Void)?
    private var videoLeases: Set<UUID> = []
    private var availability: [UUID: @Sendable () -> Void] = [:]

    private init() { queue.setSpecific(key: key, value: 1) }

    func perform<T>(_ action: () throws -> T) rethrows -> T {
        if DispatchQueue.getSpecific(key: key) != nil { return try action() }
        return try queue.sync(execute: action)
    }

    /// 远控声音不抢占朗读或录音；后两者结束后可恢复仍在前台的远控播放。
    @discardableResult
    func claim(_ lease: UUID, kind requested: Kind, onInterrupted: @escaping @Sendable () -> Void) throws -> Bool {
        try perform {
            if owner == lease { return true }
            if kind == .call && requested != .call { throw LeaseError.call }
            if requested == .remotePlayback && owner != nil { return false }
            if requested == .speech && kind == .recording { throw LeaseError.recording }
            let previous = interrupted
            owner = nil; kind = nil; interrupted = nil
            previous?()
            let session = AVAudioSession.sharedInstance()
            do {
                switch requested {
                case .call: try session.setCategory(.playAndRecord, mode: .voiceChat, options: [.allowBluetoothHFP, .defaultToSpeaker])
                case .speech: try session.setCategory(.playback, mode: .spokenAudio)
                case .recording: try session.setCategory(.record, mode: .measurement)
                case .remotePlayback:
                    try session.setCategory(.playback, mode: .default)
                    try session.setPreferredSampleRate(48_000)
                    try session.setPreferredIOBufferDuration(0.01)
                }
                if requested != .call { try session.setActive(true) }
                owner = lease; kind = requested; interrupted = onInterrupted
                return true
            } catch {
                try? session.setActive(false, options: .notifyOthersOnDeactivation)
                throw error
            }
        }
    }

    func release(_ lease: UUID) {
        perform {
            guard owner == lease else { return }
            owner = nil; kind = nil; interrupted = nil
            if videoLeases.isEmpty {
                try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
            } else {
                try? AVAudioSession.sharedInstance().setCategory(.playback, mode: .default)
                try? AVAudioSession.sharedInstance().setActive(true)
            }
            let callbacks = Array(availability.values)
            queue.async { callbacks.forEach { $0() } }
        }
    }

    /// 视频画中画使用真实视频播放会话，不生成任何占位音频。
    func beginVideo(_ lease: UUID) throws {
        try perform {
            guard kind != .recording && kind != .call else { throw LeaseError.recording }
            if owner == nil {
                try AVAudioSession.sharedInstance().setCategory(.playback, mode: .default)
                try AVAudioSession.sharedInstance().setActive(true)
            }
            videoLeases.insert(lease)
        }
    }
    func endVideo(_ lease: UUID) {
        perform {
            videoLeases.remove(lease)
            if videoLeases.isEmpty && owner == nil {
                try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
            }
        }
    }

    func isCurrent(_ lease: UUID) -> Bool { perform { owner == lease } }
    func observeAvailability(_ lease: UUID, _ callback: @escaping @Sendable () -> Void) { perform { availability[lease] = callback } }
    func removeAvailabilityObserver(_ lease: UUID) { _ = perform { availability.removeValue(forKey: lease) } }
    private enum LeaseError: LocalizedError {
        case recording, call
        var errorDescription: String? { self == .call ? "语音通话期间不能启动其他录音或播放" : "当前正在录音，结束后可继续播放" }
    }
}
