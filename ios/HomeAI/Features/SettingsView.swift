import SwiftUI
import PhotosUI
import VisionKit
import UniformTypeIdentifiers

struct SettingsView: View {
    @Environment(AppState.self) private var state
    @AppStorage("backgroundSyncEnabled") private var backgroundSyncEnabled = false
    @State private var server = "https://"
    @State private var fingerprint = ""
    @State private var token = ""
    @State private var scannedPairing: PairingCode?
    @State private var trustInfo: APIClient.TrustInfo?
    @State private var candidateAddress = ""
    @State private var trustMessage = ""
    @State private var selectedPhoto: PhotosPickerItem?
    @State private var syncMessage = ""
    @State private var scanning = false
    @State private var importingPairing = false
    @State private var location = LocationCapture()
    var body: some View {
        Form {
            Section("家庭服务器") {
                if DataScannerViewController.isSupported && DataScannerViewController.isAvailable {
                    Button("扫描服务器配对码", systemImage: "qrcode.viewfinder") { scanning = true }
                }
                Button("导入本机生成的配对 JSON") { importingPairing = true }
                Label(state.connected ? "已保存安全连接" : "尚未配对", systemImage: state.connected ? "lock.shield" : "wifi.slash")
                TextField("服务器 HTTPS 地址", text: $server).textInputAutocapitalization(.never).autocorrectionDisabled().keyboardType(.URL)
                TextField("证书 SHA-256 指纹", text: $fingerprint).textInputAutocapitalization(.never).autocorrectionDisabled()
                SecureField("一次性配对码", text: $token)
                Button("安全配对") { Task { await state.perform {
                    let code: PairingCode
                    if let scannedPairing, scannedPairing.url == server, scannedPairing.fingerprint == fingerprint, scannedPairing.token == token {
                        code = scannedPairing
                    } else { code = PairingCode(url: server, fingerprint: fingerprint, token: token) }
                    try await state.api.pair(code)
                    state.records = []
                    state.activity = []
                    state.approvals = []
                    state.automations = []
                    state.taskStates = []
                    state.syncStatus = ""
                    state.systemReminderStatus = ""
                    state.connected = true
                    state.connectionRevision = UUID()
                    token = ""
                } } }.disabled(state.busy)
            }
            if let trustInfo {
                Section("服务器身份与远程地址") {
                    Label(trustInfo.bound ? "稳定服务器身份已绑定" : "当前连接仍使用旧版证书绑定", systemImage: "checkmark.shield")
                    if !trustInfo.bound {
                        Text("升级会通过当前已信任连接确认服务器公钥，保留同步与提醒身份。请在原地址仍能正常连接时完成。").font(.caption)
                        Button("通过当前可信连接升级身份") { Task { await state.perform {
                            try await state.api.enrollServerIdentity()
                            self.trustInfo = await state.api.trustInfo()
                            trustMessage = "稳定服务器身份已保存"
                        } } }.disabled(state.busy)
                    } else {
                        TextField("要验证的 HTTPS 地址", text: $candidateAddress).textInputAutocapitalization(.never).autocorrectionDisabled().keyboardType(.URL)
                        ForEach(trustInfo.addresses, id: \.self) { address in Button(address) { candidateAddress = address }.font(.caption) }
                        Button("验证身份并更新连接") { Task { await state.perform {
                            try await state.api.verifyServerAddress(candidateAddress)
                            self.trustInfo = await state.api.trustInfo()
                            state.connectionRevision = UUID()
                            trustMessage = "身份验证通过，连接已更新；没有重发此前失败的操作"
                        } } }.disabled(state.busy || candidateAddress.isEmpty)
                        Text("可用于证书换钥或切换到远程地址。验证期间不发送配对码、会话或设备签名；服务器身份不一致会拒绝更新。").font(.caption)
                    }
                    if !trustMessage.isEmpty { Text(trustMessage).font(.caption) }
                }
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
            Section("系统提醒写入") {
                NavigationLink("选择系统列表与同步规则") { ReminderSyncSettings() }.disabled(!state.connected)
            }
            Section("按需授权同步") {
                Button("同步未来 30 天日历") { sync { try await $0.calendar() } }
                Button("导入手机已有提醒") { sync { try await $0.reminders() } }
                Button("同步联系人") { sync { try await $0.contacts() } }
                Button("同步最近 7 天睡眠数据") { sync { try await $0.sleep() } }
                Button("分享本次位置") {
                    Task { await state.perform {
                        let coordinate = try await location.once()
                        try await ConnectorSync(api: state.api).uploadRecord(source: "location", sourceID: UUID().uuidString, kind: "location.point", payload: ["latitude": .number(coordinate.latitude), "longitude": .number(coordinate.longitude), "observed_at": .string(Date().ISO8601Format())])
                        syncMessage = "本次位置已同步"
                    } }
                }
                PhotosPicker("选择一张照片同步", selection: $selectedPhoto, matching: .images)
                if !syncMessage.isEmpty { Text(syncMessage).font(.caption).foregroundStyle(.secondary) }
            }.disabled(!state.connected || state.busy)
            Section("关于") {
                Link("基于 Home AI OS · 查看源码", destination: URL(string: "https://github.com/MR-MaoJiu/home-ai-os")!)
            }
            Section("隐私") {
                Label("个人数据默认仅在本地处理", systemImage: "hand.raised")
                Text("授权由 iOS 系统管理，可随时在系统设置中撤回。已上传的数据需要在数据页面单独删除。").font(.caption).foregroundStyle(.secondary)
                Text("当前为开发版本：推送、后台调度真机表现、完整脱敏链与生产插件隔离仍需验收。").font(.caption).foregroundStyle(.secondary)
            }
        }.navigationTitle("设置")
            .task(id: state.connectionRevision) {
                trustInfo = await state.api.trustInfo()
                candidateAddress = trustInfo?.url ?? ""
            }
            .sheet(isPresented: $scanning) {
                PairingScanner { text in
                    scanning = false
                    do {
                        let code = try JSONDecoder().decode(PairingCode.self, from: Data(text.utf8))
                        scannedPairing = code
                        server = code.url; fingerprint = code.fingerprint; token = code.token
                    } catch { state.error = "配对二维码格式无效" }
                }.ignoresSafeArea()
            }
            .fileImporter(isPresented: $importingPairing, allowedContentTypes: [.json]) { result in
                do {
                    let url = try result.get()
                    let access = url.startAccessingSecurityScopedResource()
                    defer { if access { url.stopAccessingSecurityScopedResource() } }
                    let handle = try FileHandle(forReadingFrom: url)
                    defer { try? handle.close() }
                    let data = try handle.read(upToCount: 16385) ?? Data()
                    guard data.count <= 16384 else { throw APIClient.APIError.message("配对文件过大") }
                    let code = try JSONDecoder().decode(PairingCode.self, from: data)
                    scannedPairing = code
                    server = code.url; fingerprint = code.fingerprint; token = code.token
                } catch { state.error = "无法导入配对文件：" + error.localizedDescription }
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
