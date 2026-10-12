import SwiftUI
import UIKit

/// 触控视图尺寸就是实际视频区域，不包含信箱黑边或悬浮控件。
struct RemoteTouchSurface: UIViewRepresentable {
    let enabled: Bool
    let epoch: Int
    let cancellationID: UUID
    let onEvent: (AndroidTouchEvent, UUID) -> Bool

    func makeUIView(context: Context) -> AndroidRemoteTouchView {
        let view = AndroidRemoteTouchView()
        view.onEvent = onEvent
        view.configure(enabled: enabled, epoch: epoch, cancellationID: cancellationID)
        return view
    }
    func updateUIView(_ view: AndroidRemoteTouchView, context: Context) {
        view.onEvent = onEvent
        view.configure(enabled: enabled, epoch: epoch, cancellationID: cancellationID)
    }
    static func dismantleUIView(_ view: AndroidRemoteTouchView, coordinator: ()) {
        view.invalidate(deferNotification: true)
    }
}

@MainActor
final class AndroidRemoteTouchView: UIView {
    var onEvent: ((AndroidTouchEvent, UUID) -> Bool)?
    private var pointers: [ObjectIdentifier: Int] = [:]
    private var touches: [ObjectIdentifier: UITouch] = [:]
    private var points: [Int: AndroidTouchPoint] = [:]
    private var physicalTouches: Set<ObjectIdentifier> = []
    private var suppressed = false
    private var enabled = false
    private var epoch = 0
    private var cancellationID: UUID?
    private var previousSize = CGSize.zero
    private var sourceID = UUID()

    init() {
        super.init(frame: .zero)
        isMultipleTouchEnabled = true
        isExclusiveTouch = false
        backgroundColor = .clear
        accessibilityIdentifier = "android.touch.surface"
    }
    required init?(coder: NSCoder) { fatalError("不支持从归档恢复远程触控") }

    func configure(enabled: Bool, epoch: Int, cancellationID: UUID) {
        if self.epoch != epoch || self.cancellationID != cancellationID || (!enabled && self.enabled) {
            invalidate(deferNotification: true)
            physicalTouches.removeAll(); suppressed = false
        }
        self.epoch = epoch; self.cancellationID = cancellationID; self.enabled = enabled
        isUserInteractionEnabled = enabled
    }

    override func layoutSubviews() {
        super.layoutSubviews()
        if previousSize != .zero && previousSize != bounds.size { invalidate(deferNotification: true) }
        previousSize = bounds.size
    }

    override func touchesBegan(_ began: Set<UITouch>, with event: UIEvent?) {
        let ordered = began.sorted {
            if $0.timestamp != $1.timestamp { return $0.timestamp < $1.timestamp }
            return $0.location(in: self).x < $1.location(in: self).x
        }
        for touch in ordered { physicalTouches.insert(ObjectIdentifier(touch)) }
        guard enabled, !suppressed else { return }
        guard physicalTouches.count <= 5 else { invalidate(); return }
        if pointers.isEmpty { sourceID = UUID() }
        updateExistingPositions()
        for touch in ordered {
            guard bounds.contains(touch.location(in: self)), let id = (0..<5).first(where: { !pointers.values.contains($0) }),
                  let point = normalized(touch, id: id) else { invalidate(); return }
            let key = ObjectIdentifier(touch)
            pointers[key] = id; touches[key] = touch; points[id] = point
            guard emit(pointers.count == 1 ? .down : .pointer_down, changedID: id) else { invalidate(); return }
        }
    }

    override func touchesMoved(_ moved: Set<UITouch>, with event: UIEvent?) {
        guard enabled, !suppressed, !pointers.isEmpty else { return }
        updateExistingPositions()
        if !emit(.move, changedID: nil) { invalidate() }
    }

    override func touchesEnded(_ ended: Set<UITouch>, with event: UIEvent?) {
        let ordered = ended.sorted { (pointers[ObjectIdentifier($0)] ?? 99) < (pointers[ObjectIdentifier($1)] ?? 99) }
        updateExistingPositions()
        for touch in ordered {
            let key = ObjectIdentifier(touch)
            defer { physicalTouches.remove(key); touches.removeValue(forKey: key); pointers.removeValue(forKey: key) }
            guard enabled, !suppressed, let id = pointers[key] else { continue }
            if let point = normalized(touch, id: id) { points[id] = point }
            // 抬起事件仍包含该指头，发送后才从活动触点中移除。
            if !emit(pointers.count == 1 ? .up : .pointer_up, changedID: id) { invalidate() }
            points.removeValue(forKey: id)
        }
        if physicalTouches.isEmpty { suppressed = false; clearTrackedTouches() }
    }

    override func touchesCancelled(_ canceled: Set<UITouch>, with event: UIEvent?) {
        invalidate()
        for touch in canceled { physicalTouches.remove(ObjectIdentifier(touch)) }
        if physicalTouches.isEmpty { suppressed = false }
    }

    func invalidate(deferNotification: Bool = false) {
        let hadTouches = !pointers.isEmpty
        let last = orderedPoints
        clearTrackedTouches()
        suppressed = !physicalTouches.isEmpty
        if hadTouches {
            let callback = onEvent, source = sourceID
            let cancellation = AndroidTouchEvent(phase: .cancel, changedID: nil, points: last)
            if deferNotification { Task { @MainActor in _ = callback?(cancellation, source) } }
            else { _ = callback?(cancellation, source) }
        }
    }

    private var orderedPoints: [AndroidTouchPoint] { points.values.sorted { $0.id < $1.id } }
    private func clearTrackedTouches() { pointers.removeAll(); touches.removeAll(); points.removeAll() }
    private func emit(_ phase: AndroidTouchPhase, changedID: Int?) -> Bool {
        onEvent?(AndroidTouchEvent(phase: phase, changedID: changedID, points: orderedPoints), sourceID) == true
    }
    private func updateExistingPositions() {
        for (key, touch) in touches {
            if let id = pointers[key], let value = normalized(touch, id: id) { points[id] = value }
        }
    }
    private func normalized(_ touch: UITouch, id: Int) -> AndroidTouchPoint? {
        guard bounds.width > 0, bounds.height > 0 else { return nil }
        let location = touch.location(in: self)
        guard location.x.isFinite, location.y.isFinite else { return nil }
        // 已开始的拖动可以超出画面，边缘坐标裁剪后仍保留真实抬起/取消时机。
        return AndroidTouchPoint(id: id, x: Double(min(1, max(0, location.x / bounds.width))),
            y: Double(min(1, max(0, location.y / bounds.height))))
    }
}
