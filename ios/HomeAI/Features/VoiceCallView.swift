import SwiftUI

struct VoiceCallView: View {
    @State private var call = VoiceCalls.shared
    @Environment(\.dismiss) private var dismiss
    var body: some View {
        NavigationStack {
            VStack(spacing: 24) {
                Spacer()
                Image(systemName: call.activeID == nil ? "phone.down.circle" : "waveform.circle.fill")
                    .font(.system(size: 80)).foregroundStyle(.teal)
                Text("Home AI").font(.largeTitle.bold())
                Text(call.status).foregroundStyle(.secondary)
                if let error = call.error { Text(error).foregroundStyle(.red).font(.callout) }
                ScrollView {
                    VStack(alignment: .leading, spacing: 18) {
                        if !call.transcript.isEmpty { Text(call.transcript).font(.callout).foregroundStyle(.secondary) }
                        if !call.answer.isEmpty { Text(call.answer).textSelection(.enabled) }
                    }.frame(maxWidth: .infinity, alignment: .leading)
                }.frame(maxHeight: 260)
                Spacer()
                HStack(spacing: 44) {
                    if call.isRinging {
                        Button("接听", systemImage: "phone.fill") { call.answerIncoming() }.tint(.green)
                    } else {
                        Button { call.toggleMute() } label: { Label(call.muted ? "取消静音" : "静音", systemImage: call.muted ? "mic.slash.fill" : "mic.fill") }
                            .disabled(call.activeID == nil)
                    }
                    Button { call.end(); if call.activeID == nil { dismiss() } } label: {
                        Image(systemName: "phone.down.fill").font(.title).foregroundStyle(.white).frame(width: 72,height: 72).background(.red,in: Circle())
                    }.accessibilityLabel("挂断")
                }
                if call.status == "正在回答" { Button("停止朗读") { call.interruptReply() } }
                Text("停顿后自动发送。挂断停止通话，已提交的任务仍可在聊天中查看。").font(.caption).foregroundStyle(.secondary)
            }.padding(24)
                .toolbar { ToolbarItem(placement: .topBarTrailing) { Button("收起") { dismiss() } } }
        }
    }
}

struct VoiceCallSettingsView: View {
    @Environment(AppState.self) private var state
    @State private var preferences = VoiceCallPreferences()
    @State private var report: VoiceCallSettings?
    @State private var busy = false
    @State private var error: String?
    @State private var message = ""
    var body: some View {
        Form {
            Section("语音服务") {
                if let report {
                    LabeledContent("语音转文字", value: report.capabilities["transcription"]?.configured == true ? "已配置本地识别" : "未配置")
                    LabeledContent("文字转语音", value: report.capabilities["synthesis"]?.configured == true ? "已配置本地合成" : "未配置")
                    LabeledContent("来电推送", value: report.apns_ready && report.device_registered ? "已配置" : "尚未就绪")
                }
                Button("开始语音通话") { Task { await VoiceCalls.shared.start() } }.disabled(busy)
                NavigationLink("文字朗读") { SpeechView(text: "", expectedNamespace: nil) }
            }
            Section("主动来电") {
                Toggle("允许 Home AI 主动呼叫我", isOn: $preferences.incoming_enabled)
                Stepper("免打扰开始：\(preferences.quiet_start):00", value: $preferences.quiet_start, in: 0...23)
                Stepper("免打扰结束：\(preferences.quiet_end):00", value: $preferences.quiet_end, in: 0...23)
                Stepper("24小时最多 \(preferences.daily_limit) 次", value: $preferences.daily_limit, in: 1...8)
                TextField("内置音色", text: $preferences.speaker)
                Button("保存设置") { Task { await save() } }.disabled(busy)
                Text("只呼叫本人，不拨打电话网号码。主动来电至少间隔30分钟；开始和结束时间相同表示不设免打扰。当前时区：\(preferences.timezone)").font(.caption).foregroundStyle(.secondary)
            }
            Section {
                Button("注册来电设备") { Task { await VoiceCalls.shared.synchronize(); await load() } }.disabled(busy)
                Button("向我发起测试来电") { Task { await test() } }.disabled(busy || report?.device_registered != true || report?.apns_ready != true)
                Text("测试按钮会立即请求一次真实来电，不受免打扰限制。其他主动来电仅在开启后发起。").font(.caption).foregroundStyle(.secondary)
            }
            if busy { ProgressView() }
            if let error { Text(error).foregroundStyle(.red) }
            if !message.isEmpty { Text(message).foregroundStyle(.secondary) }
        }.navigationTitle("语音与来电").task { await load() }
    }
    private func load() async {
        do {
            report = try JSONDecoder().decode(VoiceCallSettings.self, from: await state.api.request("GET", "/api/v1/voice/settings"))
            if let report { preferences = report.preferences }
        } catch { self.error = error.localizedDescription }
    }
    private func save() async {
        busy = true; defer { busy = false }
        do { _ = try await state.api.request("PUT", "/api/v1/voice/settings", body: JSONEncoder().encode(preferences)); message = "设置已保存"; error = nil }
        catch { self.error = error.localizedDescription }
    }
    private func test() async {
        busy = true; defer { busy = false }
        do {
            let body = try JSONSerialization.data(withJSONObject: ["client_key": UUID().uuidString])
            _ = try await state.api.request("POST", "/api/v1/voice/test-call", body: body)
            message = "已申请测试来电，请等待系统来电界面"; error = nil
        } catch { self.error = error.localizedDescription }
    }
}
