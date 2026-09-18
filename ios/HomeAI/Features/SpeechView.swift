import SwiftUI

struct SpeechView: View {
    @Environment(AppState.self) private var state
    @Environment(\.scenePhase) private var scenePhase
    @State private var playback = SpeechPlayback(api: AppServices.api)
    @State private var text: String
    let expectedNamespace: String?
    let sources: [RecordReference]

    init(text: String = "", expectedNamespace: String? = nil, sources: [RecordReference] = []) {
        _text = State(initialValue: text)
        self.expectedNamespace = expectedNamespace
        self.sources = sources
    }

    var body: some View {
        Form {
            Section("朗读内容") {
                TextEditor(text: $text).frame(minHeight: 140).accessibilityLabel("朗读文本")
                    .disabled(playback.busy)
                Text("\(text.unicodeScalars.count) / 300 字符").font(.caption).foregroundStyle(.secondary)
            }
            Section("家庭服务器音色") {
                if !playback.voices.isEmpty {
                    Picker("音色", selection: $playback.selectedVoice) {
                        ForEach(playback.voices, id: \.self) { Text($0).tag($0) }
                    }.disabled(playback.busy)
                }
                Button("获取可用音色") { playback.loadVoices(expectedNamespace: expectedNamespace) }
                    .disabled(playback.busy || !state.connected)
                Text("使用服务器内置音色，不上传参考声音。文本在家庭服务器合成，音频仅在当前页面内存中播放。").font(.caption).foregroundStyle(.secondary)
            }
            Section {
                Button("合成并播放", systemImage: "play.fill") {
                    playback.speak(text, expectedNamespace: expectedNamespace, sources: sources)
                }.disabled(playback.busy || playback.selectedVoice.isEmpty || !state.connected || text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || text.unicodeScalars.count > 300)
                if playback.busy {
                    HStack { ProgressView(); Text(playback.status).font(.caption) }
                    Button(playback.playing ? "停止播放" : "取消等待与任务", role: .cancel) { playback.stop(cancelServer: true) }
                } else if !playback.status.isEmpty { Text(playback.status).font(.caption) }
                if let error = playback.error { Text(error).foregroundStyle(.red) }
            }
        }
        .navigationTitle("语音朗读")
        .task { if state.connected { playback.loadVoices(expectedNamespace: expectedNamespace) } }
        .onDisappear { playback.stop() }
        .onChange(of: scenePhase) { _, phase in if phase != .active { playback.stop() } }
        .onChange(of: state.connectionRevision) { _, _ in playback.stop(); playback.voices = []; playback.selectedVoice = "" }
        .onChange(of: state.connected) { _, connected in if !connected { playback.stop(); playback.voices = [] } }
    }
}
