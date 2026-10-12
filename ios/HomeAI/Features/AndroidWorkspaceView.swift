import SwiftUI
@preconcurrency import WebRTC

struct AndroidWorkspaceView: View {
    @Environment(AppState.self) private var state
    @Environment(\.scenePhase) private var scenePhase
    let session: AndroidPresentationCoordinator
    private var store: AndroidWorkspaceStore { session.workspace }
    @State private var inputText = ""
    @State private var hideInput = true
    @State private var showInput = false
    @State private var showTransfer = false
    @State private var showApps = false
    @State private var expanded = false
    @State private var panelAnchor = CGPoint(x: 1, y: 0.65)
    @GestureState private var panelDrag = CGSize.zero
    private var renderedSize: CGSize { session.player.videoSize }

    var body: some View {
        GeometryReader { geometry in
            let videoSize = store.screen.map { CGSize(width: $0.width, height: $0.height) } ?? CGSize(width: 9, height: 16)
            let videoRect = AndroidRemoteLayout.videoRect(video: videoSize, container: geometry.size)
            let panelSize = AndroidRemoteLayout.panelSize(expanded: expanded, container: geometry.size)
            let center = AndroidRemoteLayout.panelCenter(anchor: panelAnchor, size: panelSize, container: geometry.size)
            ZStack(alignment: .topLeading) {
                Color.black.ignoresSafeArea()
                screen(in: videoRect)
                floatingPanel(size: panelSize, container: geometry.size, center: center)
                    .position(AndroidRemoteLayout.clamp(center: CGPoint(x: center.x + panelDrag.width, y: center.y + panelDrag.height),
                        size: panelSize, container: geometry.size))
                    .zIndex(1)
                if !isControlling {
                    header.padding(.horizontal, 12).padding(.top, 8).zIndex(2)
                    if let error = store.error {
                        Text(error).font(.caption).foregroundStyle(.white)
                            .padding(12).frame(maxWidth: .infinity)
                            .background(.black.opacity(0.82), in: RoundedRectangle(cornerRadius: 12))
                            .padding(.horizontal, 12).padding(.top, 62)
                            .contentShape(Rectangle()).onTapGesture { }.zIndex(3)
                    }
                }
            }
            .coordinateSpace(name: "android.fullscreen.canvas")
            .frame(width: geometry.size.width, height: geometry.size.height)
        }
        .background(.black)
        .preferredColorScheme(.dark)
        .statusBarHidden(true)
        .persistentSystemOverlays(.hidden)
        .ignoresSafeArea(.keyboard)
        .accessibilityIdentifier("android.fullscreen")
        .onDisappear { clearInput() }
        .onChange(of: scenePhase) { _, phase in
            if phase != .active { store.cancelTouches() }
            if phase == .background { clearInput(); showTransfer = false; showApps = false }
        }
        .onChange(of: store.workspace?.epoch) { _, _ in clearInput(); showApps = false }
        .onChange(of: isControlling) { _, controlling in
            if controlling { store.cancelTouches(); expanded = false }
        }
        .onChange(of: store.error) { _, error in
            if isControlling && error != nil { expanded = true }
        }
        .onChange(of: showInput) { _, shown in if shown { store.cancelTouches() } }
        .onChange(of: showTransfer) { _, shown in if shown { store.cancelTouches() } }
        .onChange(of: showApps) { _, shown in if shown { store.cancelTouches() } }
        .sheet(isPresented: $showInput, onDismiss: { inputText = ""; hideInput = true }) {
            inputSheet
        }
        .confirmationDialog("打开应用", isPresented: $showApps, titleVisibility: .visible) {
            if store.workspace?.packages.contains("*") == true {
                Button("前往安卓主页打开应用") { Task { await store.input("home") } }
            }
            ForEach((store.workspace?.packages ?? []).filter { $0 != "*" }, id: \.self) { package in
                Button(package == "com.android.settings" ? "系统设置" : package) {
                    Task { await store.input("launch", package: package) }
                }
            }
        }
        .confirmationDialog("交还 AI 操作？", isPresented: $showTransfer, titleVisibility: .visible) {
            Button("交还 AI") { clearInput(); Task { await store.setControl("ai") } }
        } message: {
            Text("结束本次人工接管，允许 AI 接收后续设备任务。")
        }
    }

    private var header: some View {
        HStack(alignment: .center, spacing: 12) {
            Button {
                clearInput(); session.close()
            } label: {
                Image(systemName: "xmark").font(.system(size: 17, weight: .semibold))
                    .frame(width: 44, height: 44).background(.ultraThinMaterial, in: Circle())
            }.buttonStyle(.plain).accessibilityLabel("退出安卓全屏").accessibilityIdentifier("android.close")
            Spacer()
            VStack(alignment: .trailing, spacing: 3) {
                Text(store.workspace?.label ?? "我的安卓设备").font(.subheadline.weight(.semibold)).lineLimit(1)
                Text(connectionLabel).font(.caption).foregroundStyle(.secondary)
            }.padding(.horizontal, 12).padding(.vertical, 8)
                .background(.ultraThinMaterial, in: Capsule())
                .contentShape(Rectangle()).onTapGesture { }
        }.foregroundStyle(.white)
    }

    private var connectionLabel: String {
        if store.connecting { return "正在连接…" }
        if store.recovering { return "正在恢复连接…" }
        return store.online ? "直连 · " + (store.workspace?.modeLabel ?? "仅观看") : "未连接"
    }

    private var isControlling: Bool {
        store.online && store.workspace?.controlled(by: store.deviceID) == true
    }

    private func screen(in rect: CGRect) -> some View {
        Group {
            if let frame = store.screen, store.track != nil, store.online {
                ZStack {
                    AndroidSampleVideoSurface(player: session.player)
                    if let reason = frame.blocked_reason {
                        Color.black.overlay {
                            Label(reason, systemImage: "lock.shield").foregroundStyle(.white)
                                .multilineTextAlignment(.center).padding()
                        }
                    }
                    RemoteTouchSurface(enabled: screenCanInteract && renderedSize == CGSize(width: frame.width, height: frame.height),
                        epoch: store.workspace?.epoch ?? 0, cancellationID: store.touchCancellationID,
                        onEvent: { event, source in store.handleTouch(event, sourceID: source) })
                        // 正常新帧不重建触控；只有尺寸或控制权改变才销毁旧触点。
                        .id("\(rect):\(frame.width):\(frame.height):\(store.workspace?.epoch ?? 0)")

                }
                .accessibilityLabel("安卓屏幕，\(screenCanInteract ? "可触控" : "仅观看")")
                .accessibilityIdentifier("android.screen")
            } else {
                VStack(spacing: 16) {
                    if store.connecting { ProgressView().tint(.white) }
                    else { Image(systemName: "iphone.gen3.radiowaves.left.and.right").font(.system(size: 40)) }
                    Text(store.connecting ? "正在等待安卓画面…" : store.workspace == nil ? "尚未连接个人安卓设备" : "画面已暂停")
                        .font(.headline).multilineTextAlignment(.center)
                    if !store.connecting {
                        Text(store.workspace == nil ? "在家庭后台绑定设备后即可查看。" : "点击悬浮按钮重新连接。")
                            .font(.subheadline).foregroundStyle(.secondary).multilineTextAlignment(.center)
                        Button("重新连接") { session.reconnect() }
                            .buttonStyle(.borderedProminent).tint(.teal)
                    }
                }.foregroundStyle(.white).padding()
            }
        }
        .frame(width: rect.width, height: rect.height)
        .position(x: rect.midX, y: rect.midY)
    }

    private var screenCanInteract: Bool {
        store.canTouch && !showInput && !showTransfer && !showApps && scenePhase == .active
    }

    private func floatingPanel(size: CGSize, container: CGSize, center: CGPoint) -> some View {
        Group {
            if expanded {
                VStack(spacing: 10) {
                    HStack {
                        HStack(spacing: 7) {
                            Image(systemName: "line.3.horizontal").foregroundStyle(.secondary)
                            VStack(alignment: .leading, spacing: 3) {
                                Text(store.workspace?.modeLabel ?? "操作").font(.subheadline.weight(.semibold))
                                if isControlling {
                                    Text(connectionLabel).font(.caption2).foregroundStyle(.secondary)
                                }
                            }
                            Spacer(minLength: 0)
                        }.contentShape(Rectangle())
                            .gesture(panelGesture(center: center, size: size, container: container))
                            .accessibilityLabel("拖动控制面板")
                        Button { store.cancelTouches(); expanded = false } label: {
                            Image(systemName: "minus").frame(width: 36, height: 36)
                        }.buttonStyle(.plain).accessibilityLabel("收起控制面板")
                            .accessibilityIdentifier("android.panel.collapse")
                        if isControlling {
                            Button {
                                clearInput(); session.close()
                            } label: {
                                Image(systemName: "xmark").frame(width: 44, height: 44)
                            }.buttonStyle(.plain).accessibilityLabel("退出安卓全屏")
                                .accessibilityIdentifier("android.close")
                        }
                    }
                    ScrollView {
                        VStack(spacing: 12) {
                            if isControlling, let error = store.error {
                                Text(error).font(.caption).foregroundStyle(.orange)
                                    .frame(maxWidth: .infinity, alignment: .leading)
                            }
                            controlButtons
                            Button {
                                clearInput(); session.showInline(startSystemPiP: true)
                            } label: {
                                Label("开启画中画", systemImage: "pip.enter").frame(maxWidth: .infinity, minHeight: 44)
                            }.buttonStyle(.bordered)
                            if let reason = store.touchStatus, store.online {
                                Text(reason).font(.caption).foregroundStyle(.secondary)
                                Button(store.restoringTouch ? "正在恢复触控…" : "恢复触控") {
                                    Task { await store.restoreTouch() }
                                }.buttonStyle(.bordered).disabled(store.restoringTouch)
                            }
                            Toggle(isOn: Binding(get: { store.audioRequested && store.audioAvailable }, set: { store.setAudioEnabled($0) })) {
                                Label("安卓声音", systemImage: store.audioRequested && store.audioAvailable ? "speaker.wave.2" : "speaker.slash")
                            }.disabled(!store.audioAvailable)
                                .accessibilityIdentifier("android.audio.toggle")
                            Text(store.audioStatus).font(.caption).foregroundStyle(.secondary)
                            Toggle(isOn: Binding(get: { store.displayState?.device_screen_off == true }, set: { off in
                                Task { await store.setDeviceScreenOff(off) }
                            })) {
                                Label("关闭安卓实体屏幕", systemImage: "iphone.slash")
                            }.disabled(!store.canChangeDisplay)
                                .accessibilityIdentifier("android.display.off")
                            if store.changingDisplay { ProgressView("正在更新实体屏幕…").frame(maxWidth: .infinity, alignment: .leading) }
                            Text(store.displayStatus).font(.caption).foregroundStyle(.secondary)
                            Text("远程画面保持播放；结束或断线后恢复。不会解锁手机。")
                                .font(.caption).foregroundStyle(.secondary)
                            if let error = store.displayError {
                                Text(error).font(.caption).foregroundStyle(.orange).frame(maxWidth: .infinity, alignment: .leading)
                            }
                            if let workspace = store.workspace, workspace.controlled(by: store.deviceID) {
                                humanButtons(workspace)
                            } else if store.workspace?.mode == "human" {
                                Text("其他设备正在操作，接管后原设备会停止输入。")
                                    .font(.caption).foregroundStyle(.secondary)
                            } else {
                                Text("接管后可直接触控画面。").font(.caption).foregroundStyle(.secondary)
                            }
                            if !store.online && !store.connecting {
                                Button("重新连接", systemImage: "arrow.clockwise") {
                                    session.reconnect()
                                }.buttonStyle(.bordered).frame(maxWidth: .infinity)
                            }
                        }
                    }.scrollIndicators(.hidden)
                }.padding(12)
            } else {
                Button { store.cancelTouches(); expanded = true } label: {
                    ZStack(alignment: .topTrailing) {
                        Image(systemName: "slider.horizontal.3").font(.system(size: 23, weight: .medium))
                            .frame(maxWidth: .infinity, maxHeight: .infinity)
                        Circle().fill(store.error != nil ? Color.orange : store.online ? Color.teal : Color.gray)
                            .frame(width: 8, height: 8).padding(7)
                    }
                }.buttonStyle(.plain).accessibilityLabel("展开安卓控制面板")
                    .accessibilityIdentifier("android.panel.expand")
                    .highPriorityGesture(panelGesture(center: center, size: size, container: container))
            }
        }
        .frame(width: size.width, height: size.height)
        .foregroundStyle(.white)
        .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: expanded ? 22 : 28))
        .overlay { RoundedRectangle(cornerRadius: expanded ? 22 : 28).stroke(.white.opacity(0.18), lineWidth: 1).allowsHitTesting(false) }
        .shadow(color: .black.opacity(0.3), radius: 16, y: 6)
        // 禁用按钮和面板空隙也占据触控区域，不把点击透传给下层安卓屏幕。
        .contentShape(Rectangle()).onTapGesture { }
        .accessibilityIdentifier("android.floating.controls")
    }

    private func panelGesture(center: CGPoint, size: CGSize, container: CGSize) -> some Gesture {
        DragGesture(minimumDistance: 8)
            .updating($panelDrag) { value, drag, _ in drag = value.translation }
            .onChanged { _ in store.cancelTouches() }
            .onEnded { value in
                panelAnchor = AndroidRemoteLayout.anchor(center: CGPoint(x: center.x + value.translation.width, y: center.y + value.translation.height),
                    size: size, container: container)
            }
    }

    private var controlButtons: some View {
        VStack(spacing: 8) {
            Button {
                Task { await store.setControl("human") }
            } label: {
                Label(store.workspace?.controlled(by: store.deviceID) == true ? "已接管" : "接管操作", systemImage: "hand.tap")
                    .frame(maxWidth: .infinity, minHeight: 34)
            }.buttonStyle(.borderedProminent).tint(.teal)
                .disabled(!store.online || store.changingControl || store.workspace?.controlled(by: store.deviceID) == true)
            HStack {
                Button("交还 AI", systemImage: "sparkles") { showTransfer = true }
                    .frame(maxWidth: .infinity, minHeight: 34).disabled(store.workspace?.mode == "ai")
                Button("暂停", systemImage: "pause.fill") { Task { await store.setControl("paused") } }
                    .frame(maxWidth: .infinity, minHeight: 34).disabled(store.workspace?.mode == "paused")
            }.buttonStyle(.bordered).font(.subheadline)
                .disabled(!store.online || store.changingControl)
        }
    }

    private func humanButtons(_ workspace: AndroidWorkspace) -> some View {
        VStack(spacing: 8) {
            HStack {
                Button("返回", systemImage: "chevron.backward") { Task { await store.input("back") } }
                    .frame(maxWidth: .infinity, minHeight: 34)
                Button("主页", systemImage: "house") { Task { await store.input("home") } }
                    .frame(maxWidth: .infinity, minHeight: 34)
            }.buttonStyle(.bordered)
            HStack {
                Button("文字", systemImage: "keyboard") { inputText = ""; hideInput = true; showInput = true }
                    .frame(maxWidth: .infinity, minHeight: 34).accessibilityIdentifier("android.input.open")
                Button { store.cancelTouches(); showApps = true } label: {
                    Label("应用", systemImage: "square.grid.2x2").frame(maxWidth: .infinity, minHeight: 34)
                }.disabled(workspace.packages.isEmpty)
            }.buttonStyle(.bordered)
        }.font(.subheadline).disabled(!store.canControl)
    }

    private var inputSheet: some View {
        NavigationStack {
            VStack(alignment: .leading, spacing: 16) {
                Text("发送到安卓当前选中的输入框").font(.subheadline).foregroundStyle(.secondary)
                HStack {
                    Group {
                        if hideInput { SecureField("输入文字", text: $inputText) }
                        else { TextField("输入文字", text: $inputText) }
                    }.textInputAutocapitalization(.never).autocorrectionDisabled().textFieldStyle(.roundedBorder)
                    Button(hideInput ? "显示文字" : "隐藏文字", systemImage: hideInput ? "eye" : "eye.slash") { hideInput.toggle() }
                        .labelStyle(.iconOnly)
                }
                Button {
                    let text = inputText
                    clearInput()
                    Task { await store.input("text", text: text) }
                } label: { Label("发送文字", systemImage: "arrow.up").frame(maxWidth: .infinity, minHeight: 40) }
                    .buttonStyle(.borderedProminent).tint(.teal)
                    .disabled(!store.canControl || inputText.isEmpty || inputText.count > 500)
                Spacer(minLength: 0)
            }.padding()
                .navigationTitle("输入文字").navigationBarTitleDisplayMode(.inline)
                .toolbar { ToolbarItem(placement: .cancellationAction) { Button("取消") { clearInput() } } }
        }.presentationDetents([.height(260), .medium]).presentationDragIndicator(.visible)
            .preferredColorScheme(.dark)
    }

    private func clearInput() {
        store.cancelTouches()
        inputText = ""; hideInput = true; showInput = false
    }
}

/// 全屏画面与悬浮面板共用的几何边界，黑边不会被当成安卓坐标。
enum AndroidRemoteLayout {
    static func videoRect(video: CGSize, container: CGSize) -> CGRect {
        guard video.width > 0, video.height > 0, container.width > 0, container.height > 0,
              video.width.isFinite, video.height.isFinite, container.width.isFinite, container.height.isFinite else { return .zero }
        let scale = min(container.width / video.width, container.height / video.height)
        let size = CGSize(width: video.width * scale, height: video.height * scale)
        return CGRect(x: (container.width - size.width) / 2, y: (container.height - size.height) / 2, width: size.width, height: size.height)
    }
    static func panelSize(expanded: Bool, container: CGSize) -> CGSize {
        expanded ? CGSize(width: min(304, max(56, container.width - 24)), height: min(320, max(56, container.height - 88))) : CGSize(width: 56, height: 56)
    }
    static func clamp(center: CGPoint, size: CGSize, container: CGSize) -> CGPoint {
        let fitsWidth = size.width + 24 <= container.width
        let fitsHeight = size.height + 76 <= container.height
        let minX = fitsWidth ? 12 + size.width / 2 : container.width / 2
        let maxX = fitsWidth ? container.width - 12 - size.width / 2 : minX
        let minY = fitsHeight ? 64 + size.height / 2 : container.height / 2
        let maxY = fitsHeight ? container.height - 12 - size.height / 2 : minY
        return CGPoint(x: min(maxX, max(minX, center.x.isFinite ? center.x : minX)),
                       y: min(maxY, max(minY, center.y.isFinite ? center.y : minY)))
    }
    static func panelCenter(anchor: CGPoint, size: CGSize, container: CGSize) -> CGPoint {
        let low = clamp(center: .zero, size: size, container: container)
        let high = clamp(center: CGPoint(x: container.width, y: container.height), size: size, container: container)
        return clamp(center: CGPoint(x: low.x + (high.x - low.x) * anchor.x, y: low.y + (high.y - low.y) * anchor.y), size: size, container: container)
    }
    static func anchor(center: CGPoint, size: CGSize, container: CGSize) -> CGPoint {
        let low = clamp(center: .zero, size: size, container: container)
        let high = clamp(center: CGPoint(x: container.width, y: container.height), size: size, container: container)
        let point = clamp(center: center, size: size, container: container)
        return CGPoint(x: high.x > low.x ? (point.x - low.x) / (high.x - low.x) : 0.5,
                       y: high.y > low.y ? (point.y - low.y) / (high.y - low.y) : 0.5)
    }
}

struct AndroidScreenPoint: Equatable {
    let x: Double
    let y: Double
    static func normalize(_ point: CGPoint, in rect: CGRect) -> AndroidScreenPoint? {
        // 居中缩放的加减法可能产生亚像素舍入误差；只容忍计算误差，不接受黑边触摸。
        let tolerance: CGFloat = 0.000001
        guard point.x.isFinite, point.y.isFinite, rect.width > 0, rect.height > 0,
              point.x >= rect.minX - tolerance, point.x <= rect.maxX + tolerance,
              point.y >= rect.minY - tolerance, point.y <= rect.maxY + tolerance else { return nil }
        return normalize(CGPoint(x: min(rect.width, max(0, point.x - rect.minX)),
                                 y: min(rect.height, max(0, point.y - rect.minY))), in: rect.size)
    }
    static func normalize(_ point: CGPoint, in size: CGSize) -> AndroidScreenPoint? {
        guard size.width > 0, size.height > 0, point.x.isFinite, point.y.isFinite,
              point.x >= 0, point.y >= 0, point.x <= size.width, point.y <= size.height else { return nil }
        return .init(x: Double(point.x / size.width), y: Double(point.y / size.height))
    }
}
