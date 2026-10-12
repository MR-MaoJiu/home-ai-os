import AVFoundation
import AudioToolbox
import Foundation
@preconcurrency import WebRTC

/// WebRTC 的纯输出设备：RemoteIO 输入总线明确关闭，所有录音接口均拒绝。
/// 控制状态只在 AppAudioLease 串行队列读写；渲染回调只使用已启动前固定的拉流块。
final class AndroidPlaybackAudioDevice: NSObject, RTCAudioDevice, @unchecked Sendable {
    private let lease = UUID()
    private var delegate: (any RTCAudioDeviceDelegate)?
    private var pull: RTCAudioDeviceGetPlayoutDataBlock?
    private var unit: AudioUnit?
    private var initialized = false
    private var playoutInitialized = false
    private var requested = false
    private var preparingOutput = false
    private var enabled = false
    private var interrupted = false
    private var observer: NSObjectProtocol?
    private var callback: (@Sendable (Bool, String?) -> Void)?
    private let audio = AppAudioLease.shared

    var deviceInputSampleRate: Double { 48_000 }
    var inputIOBufferDuration: TimeInterval { 0.01 }
    var inputNumberOfChannels: Int { 1 } // 格式占位；从不创建输入 AudioUnit 或调用 deliverRecordedData。
    var inputLatency: TimeInterval { 0 }
    var deviceOutputSampleRate: Double { 48_000 }
    var outputIOBufferDuration: TimeInterval { 0.01 }
    var outputNumberOfChannels: Int { 2 }
    var outputLatency: TimeInterval { audio.perform { AVAudioSession.sharedInstance().outputLatency } }
    var isInitialized: Bool { audio.perform { initialized } }
    var isPlayoutInitialized: Bool { audio.perform { playoutInitialized } }
    var isPlaying: Bool { audio.perform { requested } }
    var isRecordingInitialized: Bool { false }
    var isRecording: Bool { false }

    func initialize(with delegate: any RTCAudioDeviceDelegate) -> Bool {
        audio.perform {
            self.delegate = delegate
            pull = delegate.getPlayoutData
            initialized = true
            if observer == nil {
                observer = NotificationCenter.default.addObserver(forName: AVAudioSession.interruptionNotification, object: nil, queue: nil) { [weak self] note in
                    guard let raw = note.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt else { return }
                    self?.handleInterruption(raw == AVAudioSession.InterruptionType.began.rawValue)
                }
            }
            return true
        }
    }

    func setStatusHandler(_ handler: @escaping @Sendable (Bool, String?) -> Void) { audio.perform { callback = handler } }
    func setPlaybackEnabled(_ enabled: Bool) {
        audio.perform {
            self.enabled = enabled
            if enabled { resumeIfPossible() }
            else { stopOutput(); audio.release(lease); callback?(false, nil) }
        }
    }

    func initializePlayout() -> Bool { audio.perform { playoutInitialized = initialized; return initialized } }
    func startPlayout() -> Bool {
        audio.perform {
            guard initialized else { return false }
            requested = true
            audio.observeAvailability(lease) { [weak self] in self?.audio.perform { self?.resumeIfPossible() } }
            resumeIfPossible()
            return true
        }
    }
    func stopPlayout() -> Bool {
        audio.perform {
            requested = false
            audio.removeAvailabilityObserver(lease)
            stopOutput(); audio.release(lease); callback?(false, nil)
            return true
        }
    }
    func terminateDevice() -> Bool {
        audio.perform {
            _ = stopPlayout()
            if let observer { NotificationCenter.default.removeObserver(observer) }
            observer = nil; initialized = false; playoutInitialized = false; pull = nil; delegate = nil
            return true
        }
    }
    func initializeRecording() -> Bool { false }
    func startRecording() -> Bool { false }
    func stopRecording() -> Bool { true }

    func shutdown() { _ = terminateDevice() }

    private func resumeIfPossible() {
        guard requested, initialized, enabled, !interrupted, unit == nil, !preparingOutput, let delegate else { return }
        do {
            guard try audio.claim(lease, kind: .remotePlayback, onInterrupted: { [weak self] in
                self?.stopOutput()
                self?.callback?(false, "已暂停安卓声音，正在使用其他音频")
            }) else {
                callback?(false, "等待朗读或录音结束后恢复声音")
                return
            }
            preparingOutput = true
            // 先在 ADM 线程解除旧实时线程约束，再启动新的系统输出回调。
            delegate.dispatchAsync { [weak self] in
                delegate.notifyAudioOutputInterrupted()
                self?.audio.perform { self?.completeOutputStart() }
            }
        } catch {
            enabled = false
            callback?(false, "安卓声音播放暂不可用，请关闭声音后重试")
        }
    }

    private func completeOutputStart() {
        guard preparingOutput else { return }
        preparingOutput = false
        guard requested, initialized, enabled, !interrupted, unit == nil, audio.isCurrent(lease) else { return }
        do {
            try createOutput()
            callback?(true, nil)
        } catch {
            enabled = false
            stopOutput(); audio.release(lease)
            callback?(false, "安卓声音播放暂不可用，请关闭声音后重试")
        }
    }

    private func createOutput() throws {
        var description = AudioComponentDescription(componentType: kAudioUnitType_Output,
            componentSubType: kAudioUnitSubType_RemoteIO, componentManufacturer: kAudioUnitManufacturer_Apple,
            componentFlags: 0, componentFlagsMask: 0)
        guard let component = AudioComponentFindNext(nil, &description) else { throw AudioFailure.unavailable }
        var output: AudioUnit?
        try require(AudioComponentInstanceNew(component, &output))
        guard let output else { throw AudioFailure.unavailable }
        unit = output
        var disabled: UInt32 = 0, active: UInt32 = 1
        try require(AudioUnitSetProperty(output, kAudioOutputUnitProperty_EnableIO, kAudioUnitScope_Input, 1, &disabled, UInt32(MemoryLayout<UInt32>.size)))
        try require(AudioUnitSetProperty(output, kAudioOutputUnitProperty_EnableIO, kAudioUnitScope_Output, 0, &active, UInt32(MemoryLayout<UInt32>.size)))
        var format = AudioStreamBasicDescription(mSampleRate: 48_000, mFormatID: kAudioFormatLinearPCM,
            mFormatFlags: kAudioFormatFlagIsSignedInteger | kAudioFormatFlagIsPacked,
            mBytesPerPacket: 4, mFramesPerPacket: 1, mBytesPerFrame: 4, mChannelsPerFrame: 2, mBitsPerChannel: 16, mReserved: 0)
        try require(AudioUnitSetProperty(output, kAudioUnitProperty_StreamFormat, kAudioUnitScope_Input, 0, &format, UInt32(MemoryLayout<AudioStreamBasicDescription>.size)))
        var render = AURenderCallbackStruct(inputProc: { context, flags, timestamp, bus, count, data in
            guard let data else { return noErr }
            let device = Unmanaged<AndroidPlaybackAudioDevice>.fromOpaque(context).takeUnretainedValue()
            if let pull = device.pull { return pull(flags, timestamp, Int(bus), count, data) }
            for buffer in UnsafeMutableAudioBufferListPointer(data) {
                if let pointer = buffer.mData { memset(pointer, 0, Int(buffer.mDataByteSize)) }
            }
            return noErr
        }, inputProcRefCon: Unmanaged.passUnretained(self).toOpaque())
        try require(AudioUnitSetProperty(output, kAudioUnitProperty_SetRenderCallback, kAudioUnitScope_Input, 0, &render, UInt32(MemoryLayout<AURenderCallbackStruct>.size)))
        try require(AudioUnitInitialize(output))
        try require(AudioOutputUnitStart(output))
    }

    private func stopOutput() {
        // 调用者已在音频串行队列；先停实时回调，再释放其引用的拉流块。
        preparingOutput = false
        if let unit {
            AudioOutputUnitStop(unit); AudioUnitUninitialize(unit); AudioComponentInstanceDispose(unit)
            self.unit = nil
        }
    }
    private func handleInterruption(_ began: Bool) {
        audio.perform {
            interrupted = began
            if began { stopOutput(); audio.release(lease); callback?(false, "系统已暂时中断声音") }
            else { resumeIfPossible() }
        }
    }
    private enum AudioFailure: Error { case unavailable }
    private func require(_ status: OSStatus) throws { if status != noErr { throw AudioFailure.unavailable } }
}
