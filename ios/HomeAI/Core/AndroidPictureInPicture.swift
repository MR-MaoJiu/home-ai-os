import AVFoundation
import AVKit
import CoreImage
import Observation
import UIKit
@preconcurrency import WebRTC

/// 系统画中画只播放真实远端视频帧，不创建视频通话、麦克风或静音音轨。
@MainActor @Observable
final class AndroidPictureInPicturePlayer: NSObject, AVPictureInPictureControllerDelegate, AVPictureInPictureSampleBufferPlaybackDelegate {
    @ObservationIgnored let displayLayer = AVSampleBufferDisplayLayer()
    private(set) var active = false
    private(set) var possible = false
    private(set) var starting = false
    private(set) var hasVideo = false
    private(set) var videoSize = CGSize.zero
    private(set) var error: String?
    private nonisolated let playbackState = AndroidSamplePlaybackState()
    private var controller: AVPictureInPictureController?
    private var observation: NSKeyValueObservation?
    private var track: RTCVideoTrack?
    private var receiver: AndroidSampleReceiver?
    private var generation = UUID()
    private var requestedStart = false
    private var preserveOnStop = false
    private var videoLease: UUID?
    private let imageContext = CIContext(options: [.cacheIntermediates: false])
    var onRestore: (() -> Void)?
    var onStopped: ((_ preservePresentation: Bool) -> Void)?
    var onActive: (() -> Void)?

    override init() {
        super.init()
        displayLayer.videoGravity = .resizeAspect
        displayLayer.backgroundColor = UIColor.black.cgColor
        var timebase: CMTimebase?
        if CMTimebaseCreateWithSourceClock(allocator: kCFAllocatorDefault, sourceClock: CMClockGetHostTimeClock(), timebaseOut: &timebase) == noErr,
           let timebase {
            CMTimebaseSetTime(timebase, time: CMClockGetTime(CMClockGetHostTimeClock()))
            CMTimebaseSetRate(timebase, rate: 1)
            displayLayer.controlTimebase = timebase
        }
        guard AVPictureInPictureController.isPictureInPictureSupported() else { return }
        let source = AVPictureInPictureController.ContentSource(sampleBufferDisplayLayer: displayLayer, playbackDelegate: self)
        let controller = AVPictureInPictureController(contentSource: source)
        controller.delegate = self
        controller.canStartPictureInPictureAutomaticallyFromInline = true
        self.controller = controller
        observation = controller.observe(\.isPictureInPicturePossible, options: [.initial, .new]) { [weak self] _, change in
            let possible = change.newValue == true
            Task { @MainActor in
                guard let self else { return }
                self.possible = possible
                if possible && self.requestedStart { self.startIfPossible() }
            }
        }
    }

    var supported: Bool { controller != nil }

    func attach(_ track: RTCVideoTrack?) {
        guard self.track !== track else { return }
        if let receiver { self.track?.remove(receiver) }
        generation = UUID(); self.track = track; receiver = nil
        hasVideo = false; videoSize = .zero; playbackState.reset()
        displayLayer.flushAndRemoveImage()
        guard let track else {
            requestedStart = false
            stop(preservingPresentation: true)
            releaseVideoLease(); controller?.invalidatePlaybackState(); return
        }
        let receiver = AndroidSampleReceiver(player: self, generation: generation)
        self.receiver = receiver; track.add(receiver)
    }

    func requestStart() {
        guard supported else { error = "当前设备不支持系统画中画，仍可使用应用内小窗"; return }
        if hasVideo {
            do { try prepareVideoLease() }
            catch { self.error = error.localizedDescription; return }
        }
        requestedStart = true; preserveOnStop = false; error = nil
        startIfPossible()
    }

    private func startIfPossible() {
        guard requestedStart, !active, !starting, hasVideo, let controller, controller.isPictureInPicturePossible else { return }
        do {
            try prepareVideoLease()
            starting = true
            controller.startPictureInPicture()
        } catch { requestedStart = false; starting = false; self.error = error.localizedDescription }
    }

    func stop(preservingPresentation: Bool) {
        requestedStart = false; preserveOnStop = preservingPresentation
        if active || starting { controller?.stopPictureInPicture() }
        else if !preservingPresentation { releaseVideoLease() }
    }

    func close() {
        stop(preservingPresentation: false)
        attach(nil); releaseVideoLease()
        error = nil
    }

    fileprivate func receive(_ frame: RTCVideoFrame, generation: UUID) {
        guard self.generation == generation, track != nil, !playbackState.snapshot.paused else { return }
        guard let pixels = Self.pixelBuffer(frame.buffer), let oriented = rotated(pixels, rotation: frame.rotation) else { return }
        var format: CMVideoFormatDescription?
        guard CMVideoFormatDescriptionCreateForImageBuffer(allocator: kCFAllocatorDefault, imageBuffer: oriented, formatDescriptionOut: &format) == noErr,
              let format else { return }
        var timing = CMSampleTimingInfo(duration: .invalid,
            presentationTimeStamp: CMClockGetTime(CMClockGetHostTimeClock()), decodeTimeStamp: .invalid)
        var sample: CMSampleBuffer?
        guard CMSampleBufferCreateReadyWithImageBuffer(allocator: kCFAllocatorDefault, imageBuffer: oriented,
            formatDescription: format, sampleTiming: &timing, sampleBufferOut: &sample) == noErr, let sample else { return }
        if displayLayer.status == .failed { displayLayer.flush() }
        if displayLayer.isReadyForMoreMediaData { displayLayer.enqueue(sample) }
        let size = CGSize(width: CVPixelBufferGetWidth(oriented), height: CVPixelBufferGetHeight(oriented))
        if videoSize != size { videoSize = size }
        if !hasVideo {
            hasVideo = true; playbackState.setHasVideo(true)
            do { try prepareVideoLease() } catch { self.error = error.localizedDescription }
            controller?.invalidatePlaybackState()
        }
        if requestedStart { startIfPossible() }
    }

    private static func pixelBuffer(_ buffer: any RTCVideoFrameBuffer) -> CVPixelBuffer? {
        if let native = buffer as? RTCCVPixelBuffer, !native.requiresCropping(),
           CVPixelBufferGetWidth(native.pixelBuffer) == Int(buffer.width), CVPixelBufferGetHeight(native.pixelBuffer) == Int(buffer.height) {
            return native.pixelBuffer
        }
        let source = buffer.toI420()
        var pixels: CVPixelBuffer?
        guard CVPixelBufferCreate(kCFAllocatorDefault, Int(source.width), Int(source.height),
            kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange,
            [kCVPixelBufferIOSurfacePropertiesKey: [:]] as CFDictionary, &pixels) == kCVReturnSuccess, let pixels else { return nil }
        CVPixelBufferLockBaseAddress(pixels, [])
        defer { CVPixelBufferUnlockBaseAddress(pixels, []) }
        guard let y = CVPixelBufferGetBaseAddressOfPlane(pixels, 0), let uv = CVPixelBufferGetBaseAddressOfPlane(pixels, 1) else { return nil }
        let yStride = CVPixelBufferGetBytesPerRowOfPlane(pixels, 0), uvStride = CVPixelBufferGetBytesPerRowOfPlane(pixels, 1)
        for row in 0..<Int(source.height) { memcpy(y.advanced(by: row * yStride), source.dataY.advanced(by: row * Int(source.strideY)), Int(source.width)) }
        for row in 0..<Int(source.chromaHeight) {
            let output = uv.advanced(by: row * uvStride).assumingMemoryBound(to: UInt8.self)
            for column in 0..<Int(source.chromaWidth) {
                output[column * 2] = source.dataU[row * Int(source.strideU) + column]
                output[column * 2 + 1] = source.dataV[row * Int(source.strideV) + column]
            }
        }
        return pixels
    }

    private func rotated(_ pixels: CVPixelBuffer, rotation: RTCVideoRotation) -> CVPixelBuffer? {
        if rotation == ._0 { return pixels }
        let orientation: CGImagePropertyOrientation = rotation == ._90 ? .right : rotation == ._180 ? .down : .left
        let image = CIImage(cvPixelBuffer: pixels).oriented(orientation)
        var target: CVPixelBuffer?
        guard CVPixelBufferCreate(kCFAllocatorDefault, Int(image.extent.width), Int(image.extent.height), kCVPixelFormatType_32BGRA,
            [kCVPixelBufferIOSurfacePropertiesKey: [:]] as CFDictionary, &target) == kCVReturnSuccess, let target else { return nil }
        imageContext.render(image, to: target)
        return target
    }

    private func prepareVideoLease() throws {
        if videoLease == nil {
            let lease = UUID()
            try AppAudioLease.shared.beginVideo(lease)
            videoLease = lease
        }
    }
    private func releaseVideoLease() {
        if let videoLease { self.videoLease = nil; AppAudioLease.shared.endVideo(videoLease) }
    }

    nonisolated func pictureInPictureControllerWillStartPictureInPicture(_ pictureInPictureController: AVPictureInPictureController) {
        Task { @MainActor [weak self] in self?.starting = true }
    }
    nonisolated func pictureInPictureControllerDidStartPictureInPicture(_ pictureInPictureController: AVPictureInPictureController) {
        Task { @MainActor [weak self] in
            guard let self else { return }
            guard self.track != nil else { self.controller?.stopPictureInPicture(); return }
            self.active = true; self.starting = false; self.requestedStart = false; self.onActive?()
        }
    }
    nonisolated func pictureInPictureController(_ pictureInPictureController: AVPictureInPictureController, failedToStartPictureInPictureWithError error: Error) {
        Task { @MainActor [weak self] in
            guard let self else { return }
            self.active = false; self.starting = false; self.requestedStart = false
            self.error = "系统画中画未能启动，可继续使用应用内小窗"
        }
    }
    nonisolated func pictureInPictureControllerDidStopPictureInPicture(_ pictureInPictureController: AVPictureInPictureController) {
        Task { @MainActor [weak self] in
            guard let self else { return }
            let preserve = self.preserveOnStop
            self.active = false; self.starting = false; self.preserveOnStop = false; self.requestedStart = false
            if !preserve { self.releaseVideoLease() }
            self.onStopped?(preserve)
        }
    }
    nonisolated func pictureInPictureController(_ pictureInPictureController: AVPictureInPictureController,
        restoreUserInterfaceForPictureInPictureStopWithCompletionHandler completionHandler: @escaping (Bool) -> Void) {
        let completion = AndroidPiPRestoreCompletion(completionHandler)
        Task { @MainActor [weak self] in
            guard let self else { completion.finish(false); return }
            self.preserveOnStop = true; self.onRestore?(); completion.finish(true)
        }
    }
    nonisolated func pictureInPictureController(_ pictureInPictureController: AVPictureInPictureController, setPlaying playing: Bool) {
        playbackState.setPaused(!playing)
        Task { @MainActor [weak self] in self?.controller?.invalidatePlaybackState() }
    }
    nonisolated func pictureInPictureControllerTimeRangeForPlayback(_ pictureInPictureController: AVPictureInPictureController) -> CMTimeRange {
        playbackState.snapshot.hasVideo ? CMTimeRange(start: .zero, duration: .positiveInfinity) : .invalid
    }
    nonisolated func pictureInPictureControllerIsPlaybackPaused(_ pictureInPictureController: AVPictureInPictureController) -> Bool {
        let current = playbackState.snapshot
        return current.paused || !current.hasVideo
    }
    nonisolated func pictureInPictureController(_ pictureInPictureController: AVPictureInPictureController, didTransitionToRenderSize newRenderSize: CMVideoDimensions) {}
    nonisolated func pictureInPictureController(_ pictureInPictureController: AVPictureInPictureController, skipByInterval skipInterval: CMTime, completion: @escaping () -> Void) { completion() }

}

/// 主线程忙时丢弃中间视频帧，避免积压旧画面或把旧家庭帧交给新会话。
private final class AndroidSampleReceiver: NSObject, RTCVideoRenderer, @unchecked Sendable {
    private weak var player: AndroidPictureInPicturePlayer?
    private let generation: UUID
    private let lock = NSLock()
    private var pending = false
    private var latest: RTCVideoFrame?
    init(player: AndroidPictureInPicturePlayer, generation: UUID) { self.player = player; self.generation = generation }
    func setSize(_ size: CGSize) {}
    func renderFrame(_ frame: RTCVideoFrame?) {
        guard let frame else { return }
        let admitted = lock.withLock {
            latest = frame
            if pending { return false }
            pending = true; return true
        }
        guard admitted else { return }
        Task { @MainActor [self] in
            let current = lock.withLock { let value = latest; latest = nil; pending = false; return value }
            if let current { player?.receive(current, generation: generation) }
        }
    }
}

/// AVKit 的同步播放查询不保证在主线程，使用独立小锁避免跨 actor 同步访问。
private final class AndroidSamplePlaybackState: @unchecked Sendable {
    private let lock = NSLock()
    private var value = (hasVideo: false, paused: false)
    var snapshot: (hasVideo: Bool, paused: Bool) { lock.withLock { value } }
    func reset() { lock.withLock { value = (false, false) } }
    func setHasVideo(_ hasVideo: Bool) { lock.withLock { value.hasVideo = hasVideo } }
    func setPaused(_ paused: Bool) { lock.withLock { value.paused = paused } }
}

private final class AndroidPiPRestoreCompletion: @unchecked Sendable {
    private let callback: (Bool) -> Void
    init(_ callback: @escaping (Bool) -> Void) { self.callback = callback }
    @MainActor func finish(_ restored: Bool) { callback(restored) }
}
