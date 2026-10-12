import AVFoundation
import SwiftUI

/// 应用内小窗只观看；所有输入仍须回到全屏明确接管。
struct AndroidMiniPlayer: View {
    let session: AndroidPresentationCoordinator
    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 0) {
                Button { session.close() } label: { Image(systemName: "xmark").frame(width: 44, height: 44) }
                    .accessibilityLabel("关闭安卓小窗")
                Spacer(minLength: 0)
                Text(session.workspace.workspace?.mode == "ai" ? "AI 正在操作" : "安卓设备").font(.caption).lineLimit(1)
                Spacer(minLength: 0)
                Button { session.showFullScreen() } label: { Image(systemName: "arrow.up.left.and.arrow.down.right").frame(width: 44, height: 44) }
                    .accessibilityLabel("安卓全屏")
            }.buttonStyle(.plain)
            ZStack {
                Color.black
                if session.player.hasVideo && !session.player.active {
                    AndroidSampleVideoSurface(player: session.player)
                } else {
                    VStack(spacing: 8) {
                        if session.workspace.connecting { ProgressView().tint(.white) }
                        Image(systemName: session.player.active ? "pip" : "iphone")
                        Text(session.player.active ? "正在系统画中画播放" : "正在连接安卓画面").font(.caption).multilineTextAlignment(.center)
                    }.padding(8)
                }
            }.aspectRatio(9.0 / 12.0, contentMode: .fit)
                .allowsHitTesting(false)
            if let error = session.player.error ?? session.workspace.error {
                Text(error).font(.caption2).lineLimit(3).padding(8)
            }
        }
        .foregroundStyle(.white)
        .frame(width: 172)
        .background(.black.opacity(0.9), in: RoundedRectangle(cornerRadius: 14))
        .clipShape(RoundedRectangle(cornerRadius: 14))
        .overlay { RoundedRectangle(cornerRadius: 14).stroke(.white.opacity(0.15)) }
        .shadow(radius: 10, y: 4)
        .contentShape(Rectangle()).onTapGesture { }
        .accessibilityIdentifier("android.mini.player")
    }
}

/// 呈现容器只挂载共享视频层，移除视图绝不释放网络会话。
struct AndroidSampleVideoSurface: UIViewRepresentable {
    let player: AndroidPictureInPicturePlayer
    func makeUIView(context: Context) -> AndroidSampleVideoHost {
        let view = AndroidSampleVideoHost()
        view.mount(player.displayLayer)
        return view
    }
    func updateUIView(_ view: AndroidSampleVideoHost, context: Context) { view.mount(player.displayLayer) }
    static func dismantleUIView(_ view: AndroidSampleVideoHost, coordinator: ()) { view.unmount() }
}

final class AndroidSampleVideoHost: UIView {
    private weak var videoLayer: AVSampleBufferDisplayLayer?
    init() { super.init(frame: .zero); backgroundColor = .black; isUserInteractionEnabled = false }
    required init?(coder: NSCoder) { fatalError("不支持归档恢复视频容器") }
    func mount(_ video: AVSampleBufferDisplayLayer) {
        videoLayer = video
        if video.superlayer !== layer { video.removeFromSuperlayer(); layer.addSublayer(video) }
        layoutVideo()
    }
    func unmount() {
        if videoLayer?.superlayer === layer { videoLayer?.removeFromSuperlayer() }
        videoLayer = nil
    }
    override func layoutSubviews() { super.layoutSubviews(); layoutVideo() }
    private func layoutVideo() {
        CATransaction.begin(); CATransaction.setDisableActions(true)
        if videoLayer?.superlayer === layer { videoLayer?.frame = bounds }
        CATransaction.commit()
    }
}
