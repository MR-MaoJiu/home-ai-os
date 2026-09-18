import SwiftUI
import PhotosUI
import VisionKit

struct SettingsView: View {
    @Environment(AppState.self) private var state
    @AppStorage("backgroundSyncEnabled") private var backgroundSyncEnabled = false
    @State private var server = "https://"
    @State private var fingerprint = ""
    @State private var token = ""
    @State private var pairingJSON = ""
    @State private var selectedPhoto: PhotosPickerItem?
    @State private var syncMessage = ""
    @State private var scanning = false
    @State private var location = LocationCapture()
    var body: some View {
        Form {
            Section("家庭服务器") {
                if DataScannerViewController.isSupported && DataScannerViewController.isAvailable {
                    Button("扫描服务器配对码", systemImage: "qrcode.viewfinder") { scanning = true }
                }
                Label(state.connected ? "已保存安全连接" : "尚未配对", systemImage: state.connected ? "lock.shield" : "wifi.slash")
                TextField("服务器 HTTPS 地址", text: $server).textInputAutocapitalization(.never).autocorrectionDisabled().keyboardType(.URL)
                TextField("证书 SHA-256 指纹", text: $fingerprint).textInputAutocapitalization(.never).autocorrectionDisabled()
                SecureField("一次性配对码", text: $token)
                Button("安全配对") { Task { await state.perform {
                    try await state.api.pair(PairingCode(url: server, fingerprint: fingerprint, token: token))
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
            .sheet(isPresented: $scanning) {
                PairingScanner { text in
                    scanning = false
                    do {
                        let code = try JSONDecoder().decode(PairingCode.self, from: Data(text.utf8))
                        server = code.url; fingerprint = code.fingerprint; token = code.token
                    } catch { state.error = "配对二维码格式无效" }
                }.ignoresSafeArea()
            }
            .onChange(of: selectedPhoto) { _, photo in
                guard let photo else { return }
                Task { await state.perform {
                    guard let data = try await photo.loadTransferable(type: Data.self) else { return }
                    try await ConnectorSync(api: state.api).uploadRecord(source: "photos", sourceID: DeviceIdentity.hash(data), kind: "photo.selected", payload: ["name": .string("用户选择的照片"), "content_base64": .string(data.base64EncodedString())])
                    syncMessage = "照片已同步"
                } }
            }
    }
    func sync(_ work: @escaping @MainActor (ConnectorSync) async throws -> Void) {
        Task { await state.perform { try await work(ConnectorSync(api: state.api)); syncMessage = "同步完成" } }
    }
}
