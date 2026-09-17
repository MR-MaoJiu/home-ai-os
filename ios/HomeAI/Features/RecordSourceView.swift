import SwiftUI

struct RecordReference: Identifiable {
    let id: String
    let title: String
    let version: Int?
}

struct RecordSourceView: View {
    @Environment(AppState.self) private var state
    let source: RecordReference
    @State private var record: DataEntry?
    @State private var error: String?

    var body: some View {
        Group {
            if let record {
                ScrollView {
                    VStack(alignment: .leading, spacing: 12) {
                        Text(record.title).font(.title2)
                        if let version = source.version { Text("引用版本：\(version)").font(.caption).foregroundStyle(.secondary) }
                        if let cited = source.version, let current = record.version, cited != current {
                            Text("资料已更新，以下显示当前版本。").font(.caption).foregroundStyle(.secondary)
                        }
                        Text(record.payload["markdown"]?.description ?? record.payload["content"]?.description ?? JSONValue.object(record.payload).description)
                            .textSelection(.enabled)
                    }.frame(maxWidth: .infinity, alignment: .leading).padding()
                }
            } else if let error { ContentUnavailableView("资料不可用", systemImage: "lock.doc", description: Text(error)) }
            else { ProgressView("正在验证资料权限…") }
        }
        .navigationTitle("回答来源")
        .task {
            do {
                let data = try await state.api.request("GET", "/api/v1/data/" + source.id)
                record = try JSONDecoder().decode(DataEntry.self, from: data)
            } catch { self.error = error.localizedDescription }
        }
    }
}
