import SwiftUI
import PhotosUI
import VisionKit
import CryptoKit
import LocalAuthentication

struct SettingsView: View {
    @Environment(AppState.self) private var state
    @AppStorage("backgroundSyncEnabled") private var backgroundSyncEnabled = false
    @State private var selectedPhoto: PhotosPickerItem?
    @State private var syncMessage = ""
    @State private var scanning = false
    @State private var pendingPairing: String?
    @State private var scannerError: String?
    @State private var location = LocationCapture()
    var body: some View {
        Form {
            Section("家庭服务器") {
                if DataScannerViewController.isSupported && DataScannerViewController.isAvailable {
                    Button("扫码配对", systemImage: "qrcode.viewfinder") { scanning = true }.disabled(state.busy || state.pairingFeedback == .connecting)
                } else { Text("当前设备无法使用扫码，请检查相机权限。") }
                switch state.pairingFeedback {
                case .idle: EmptyView()
                case .connecting:
                    ProgressView(state.pairingFeedback.message).accessibilityIdentifier("pairing.connecting")
                    Text("远程首次配对可能需要一两分钟，请保持 App 在前台。").font(.caption).foregroundStyle(.secondary)
                case .success:
                    Label(state.pairingFeedback.message, systemImage: "checkmark.circle.fill").foregroundStyle(.green).accessibilityIdentifier("pairing.success")
                case .failure(let message):
                    Label(message, systemImage: "exclamationmark.triangle.fill").foregroundStyle(.red).accessibilityIdentifier("pairing.failure")
                }
                Label(state.connected ? "配对信息已保存，不代表当前在线" : "尚未配对", systemImage: state.connected ? "lock.shield" : "wifi.slash")
                Text("在家庭管理端的“成员与设备”生成二维码。扫码后自动填写连接信息；已开通远程服务时可在外网首次配对。").font(.caption)
            }
            Section("后台同步") {
                Toggle("允许系统后台刷新", isOn: $backgroundSyncEnabled)
                    .onChange(of: backgroundSyncEnabled) { _, enabled in
                        state.configureBackgroundSync(enabled: enabled)
                    }
                Text("只同步已授权的服务器数据，不在后台录音，也不会自动扩大系统数据授权。执行时间由 iOS 决定，锁屏时可能无法访问受保护缓存。").font(.caption).foregroundStyle(.secondary)
                if !state.backgroundSyncStatus.isEmpty { Text(state.backgroundSyncStatus).font(.caption) }
            }
            Section("语音") {
                NavigationLink("音色与语音朗读") { SpeechView() }.disabled(!state.connected)
            }
            AppIconSettingsSection()
            Section("系统提醒写入") {
                NavigationLink("选择系统列表与同步规则") { ReminderSyncSettings() }.disabled(!state.connected)
            }
            Section("数据共享") {
                NavigationLink("健康与位置的持续共享") { ContinuousSharingView() }.disabled(!state.connected)
                Text("上传资料与长期记忆分别存储在家庭服务器。图片和文件在数据页逐条共享。").font(.caption)
            }
            Section("按需授权同步") {
                Button("同步未来 30 天日历") { sync { try await $0.calendar() } }
                Button("导入手机已有提醒") { sync { try await $0.reminders() } }
                Button("同步联系人") { sync { try await $0.contacts() } }
                Button("同步最近 7 天睡眠数据") { sync { try await $0.sleep() } }
                Button("同步本次位置") {
                    Task { await state.perform {
                        let coordinate = try await location.once()
                        try await ConnectorSync(api: state.api).uploadRecord(source: "location", sourceID: UUID().uuidString, kind: "location.point", payload: ["latitude": .number(coordinate.latitude), "longitude": .number(coordinate.longitude), "observed_at": .string(Date().ISO8601Format())])
                        syncMessage = "本次位置已同步"
                    } }
                }
                PhotosPicker("选择一张照片同步", selection: $selectedPhoto, matching: .images)
                if !syncMessage.isEmpty { Text(syncMessage).font(.caption).foregroundStyle(.secondary) }
            }.disabled(!state.connected || state.busy)
            Section("管理端登录") {
                NavigationLink("管理端动态码") { AdminCodeView() }
            }
            Section("关于") {
                Link("基于 Home AI OS · 查看源码", destination: URL(string: "https://github.com/MR-MaoJiu/home-ai-os")!)
            }
            Section("隐私") {
                Label("个人数据默认仅在本地处理", systemImage: "hand.raised")
                Text("授权由 iOS 系统管理，可随时在系统设置中撤回。已上传的数据需要在数据页面单独删除。").font(.caption).foregroundStyle(.secondary)
                Text("当前为开发版本：推送、后台调度真机表现、完整脱敏链与生产插件隔离仍需验收。").font(.caption).foregroundStyle(.secondary)
            }
        }.navigationTitle("设置")
            .sheet(isPresented: $scanning, onDismiss: {
                // 关闭扫码页后再配对，避免结果弹窗与 sheet 关闭动画冲突。
                if let message = scannerError {
                    scannerError = nil; pendingPairing = nil
                    state.reportPairingFailure(message)
                } else if let text = pendingPairing {
                    pendingPairing = nil
                    Task { await state.pair(scannedText: text) }
                }
            }) {
                NavigationStack {
                    PairingScanner(onCode: { text in
                        pendingPairing = text; scanning = false
                    }, onFailure: { message in
                        scannerError = message; scanning = false
                    })
                    .ignoresSafeArea(edges: .bottom)
                    .navigationTitle("扫描配对二维码")
                    .navigationBarTitleDisplayMode(.inline)
                    .toolbar { ToolbarItem(placement: .cancellationAction) { Button("取消") { scanning = false } } }
                }
            }
            .onChange(of: selectedPhoto) { _, photo in
                guard let photo else { return }
                Task { await state.perform {
                    guard let data = try await photo.loadTransferable(type: Data.self) else { return }
                    let prepared = try PhotoPreparation.jpeg(data)
                    try await ConnectorSync(api: state.api).uploadRecord(source: "photos", sourceID: DeviceIdentity.hash(data), kind: "photo.selected", payload: ["name": .string("用户选择的照片"), "content_base64": .string(prepared.base64EncodedString())])
                    syncMessage = "照片已同步"
                } }
            }
    }
    func sync(_ work: @escaping @MainActor (ConnectorSync) async throws -> Void) {
        Task { await state.perform { try await work(ConnectorSync(api: state.api)); syncMessage = "同步完成" } }
    }
}

private struct AppIconSettingsSection: View {
    @Environment(\.scenePhase) private var scenePhase
    @State private var selectedIcon = UIApplication.shared.alternateIconName
    @State private var changing = false
    @State private var failure: String?

    var body: some View {
        Section {
            iconRow("女生款", preview: "FemaleIconPreview", iconName: nil)
            iconRow("男生款", preview: "MaleIconPreview", iconName: "AppIconMale")
            if changing { ProgressView("正在切换图标…") }
        } header: {
            Text("App 图标")
        } footer: {
            Text(UIApplication.shared.supportsAlternateIcons ? "默认使用女生款，可随时切换桌面图标。" : "当前环境不支持切换 App 图标。")
        }
        .onAppear { selectedIcon = UIApplication.shared.alternateIconName }
        .onChange(of: scenePhase) { _, phase in
            if phase == .active { selectedIcon = UIApplication.shared.alternateIconName }
        }
        .alert("图标切换失败", isPresented: Binding(get: { failure != nil }, set: { if !$0 { failure = nil } })) {
            Button("知道了", role: .cancel) { failure = nil }
        } message: {
            Text(failure ?? "")
        }
    }

    private func iconRow(_ title: String, preview: String, iconName: String?) -> some View {
        Button {
            guard !changing, selectedIcon != iconName else { return }
            changing = true
            Task { @MainActor in
                defer {
                    changing = false
                    // 以系统实际图标为准，切换失败时保留原选中项。
                    selectedIcon = UIApplication.shared.alternateIconName
                }
                do { try await UIApplication.shared.setAlternateIconName(iconName) }
                catch { failure = error.localizedDescription }
            }
        } label: {
            HStack(spacing: 12) {
                Image(preview).resizable().scaledToFit().frame(width: 52, height: 52)
                    .clipShape(RoundedRectangle(cornerRadius: 12))
                    .accessibilityHidden(true)
                Text(title).foregroundStyle(.primary)
                Spacer()
                if selectedIcon == iconName {
                    Image(systemName: "checkmark.circle.fill").foregroundStyle(.teal)
                }
            }
        }
        .disabled(changing || !UIApplication.shared.supportsAlternateIcons)
        .accessibilityLabel(title)
        .accessibilityValue(selectedIcon == iconName ? "已选中" : "未选中")
    }
}

/// 标准 TOTP 验证器；密钥只保存在此设备 Keychain，不上传家庭服务端。
struct AdminCodeView: View {
    @Environment(\.scenePhase) private var phase
    @State private var credential = ""
    @State private var saved = ""
    @State private var error: String?
    @State private var unlocked = false
    private let key = "admin-totp-secret"
    var body: some View {
        List {
            Section {
                Text("导入家庭管理端初始化时显示的动态码密钥或 otpauth 地址。五分钟初始化凭据、iPhone 配对码、数据加密主密钥都不能用于生成动态码。").font(.caption)
                SecureField("动态码密钥或 otpauth:// 地址", text: $credential).textInputAutocapitalization(.never).autocorrectionDisabled()
                Button("保存并显示动态码") { Task { await save() } }.disabled(credential.isEmpty)
                if let error { Text(error).foregroundStyle(.red) }
            }
            if !saved.isEmpty {
                Section("管理端登录验证码") {
                    if unlocked && phase == .active {
                        TimelineView(.periodic(from: .now, by: 1)) { context in
                            Text((try? AdminTOTP.code(saved, at: context.date)) ?? "无效凭据").font(.largeTitle.monospacedDigit()).textSelection(.enabled)
                            Text("剩余 \(30 - Int(context.date.timeIntervalSince1970) % 30) 秒，已使用的动态码不能重复登录。").font(.caption)
                        }
                    } else { Button("验证身份后查看") { Task { unlocked = await authorize() } } }
                    Button("移除此设备保存的密钥", role: .destructive) { Task {
                        guard await authorize() else { return }
                        do { try DeviceIdentity.save(Data(), name: key); saved = ""; unlocked = false }
                        catch { self.error = error.localizedDescription }
                    } }
                }
            }
        }.navigationTitle("管理端动态码")
            .task { saved = DeviceIdentity.read(key).flatMap { String(data: $0, encoding: .utf8) } ?? "" }
            .onChange(of: phase) { _, value in if value != .active { unlocked = false } }
    }
    private func authorize() async -> Bool {
        do { return try await LAContext().evaluatePolicy(.deviceOwnerAuthentication, localizedReason: "查看家庭管理端动态码") }
        catch { self.error = error.localizedDescription; return false }
    }
    private func save() async {
        do {
            let normalized = try AdminTOTP.secret(credential)
            guard await authorize() else { return }
            try DeviceIdentity.save(Data(normalized.utf8), name: key)
            saved = normalized; credential = ""; unlocked = true; error = nil
        } catch { self.error = error.localizedDescription }
    }
}

enum AdminTOTP {
    static func secret(_ input: String) throws -> String {
        var raw = input.trimmingCharacters(in: .whitespacesAndNewlines)
        if raw.lowercased().hasPrefix("otpauth:") {
            guard let url = URLComponents(string: raw), url.scheme == "otpauth", url.host == "totp" else { throw failure }
            let items = url.queryItems ?? []
            func value(_ name: String) -> String? { items.first { $0.name == name }?.value }
            guard (value("algorithm") ?? "SHA1").uppercased() == "SHA1", (value("digits") ?? "6") == "6", (value("period") ?? "30") == "30", let secret = value("secret") else { throw failure }
            raw = secret
        }
        raw = raw.uppercased().filter { !$0.isWhitespace }.replacingOccurrences(of: "=", with: "")
        guard raw.count >= 16, raw.count <= 128 else { throw failure }
        _ = try decode(raw)
        return raw
    }
    static var failure: NSError { NSError(domain: "HomeAI.TOTP", code: 1, userInfo: [NSLocalizedDescriptionKey: "请输入有效的 Base32 动态码密钥；支持 SHA1、6 位、30 秒的标准 TOTP。"]) }
    static func decode(_ value: String) throws -> Data {
        let alphabet = Array("ABCDEFGHIJKLMNOPQRSTUVWXYZ234567")
        var buffer = 0, bits = 0
        var bytes: [UInt8] = []
        for char in value {
            guard let index = alphabet.firstIndex(of: char) else { throw failure }
            buffer = (buffer << 5) | index; bits += 5
            if bits >= 8 { bits -= 8; bytes.append(UInt8((buffer >> bits) & 255)); buffer &= (1 << bits) - 1 }
        }
        return Data(bytes)
    }
    static func code(_ secret: String, at date: Date) throws -> String {
        var counter = UInt64(max(0, date.timeIntervalSince1970) / 30).bigEndian
        let data = withUnsafeBytes(of: &counter) { Data($0) }
        let mac = Array(HMAC<Insecure.SHA1>.authenticationCode(for: data, using: SymmetricKey(data: try decode(secret))))
        let offset = Int(mac.last! & 15)
        let value = ((UInt32(mac[offset]) & 127) << 24) | (UInt32(mac[offset+1]) << 16) | (UInt32(mac[offset+2]) << 8) | UInt32(mac[offset+3])
        return String(format: "%06u", value % 1_000_000)
    }
}
