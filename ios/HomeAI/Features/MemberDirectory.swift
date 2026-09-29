import SwiftUI
import Observation

struct ChatMention: Codable, Hashable, Sendable { let member_id: String }

@MainActor @Observable
final class MemberDirectoryStore {
    var members: [FamilyMember] = []
    var ownerID = ""
    var error: String?
    private var namespace: String?
    private var loading = false
    func name(_ id: String) -> String { members.first { $0.id == id }?.name ?? "家庭成员" }
    func load(api: APIClient) async {
        guard !loading else { return }; loading = true; defer { loading = false }
        do {
            let current = try await api.syncNamespace()
            if namespace != current { members = []; namespace = current }
            if let cached = await ClientViewCache.shared.read(key: "members", namespace: current), let saved = try? JSONDecoder().decode([FamilyMember].self, from: cached) { members = saved }
            ownerID = try await api.ownerIdentity(expectedNamespace: current).userID
            let raw = try await api.request("GET", "/api/v1/members", expectedNamespace: current)
            guard !Task.isCancelled, await api.cachedNamespace() == current else { return }
            members = try JSONDecoder().decode([FamilyMember].self, from: raw); error = nil
            try? await ClientViewCache.shared.write(raw, key: "members", namespace: current)
        } catch { self.error = error.localizedDescription; if case APIClient.APIError.http(401, _) = error { members = [] } }
    }
}

struct MemberDirectoryView: View {
    @Environment(AppState.self) private var state
    @Environment(\.dismiss) private var dismiss
    @Binding var selected: [ChatMention]
    let directory: MemberDirectoryStore
    var body: some View {
        NavigationStack {
            List {
                Text("选择本条消息涉及的家庭成员。提及不会开放你的私人数据或整段聊天。").font(.caption).foregroundStyle(.secondary)
                if let error = directory.error { Text(error).foregroundStyle(.red) }
                ForEach(directory.members) { member in
                    Button {
                        if selected.contains(where: { $0.member_id == member.id }) { selected.removeAll { $0.member_id == member.id } }
                        else if selected.count < 8 { selected.append(ChatMention(member_id: member.id)) }
                    } label: {
                        HStack {
                            Label(member.name + (member.id == directory.ownerID ? "（自己）" : ""), systemImage: "person.circle")
                            Spacer()
                            if selected.contains(where: { $0.member_id == member.id }) { Image(systemName: "checkmark").foregroundStyle(.teal) }
                        }
                    }
                }
            }.navigationTitle("指定成员")
                .task(id: state.connectionRevision) { await directory.load(api: state.api) }
                .toolbar { ToolbarItem(placement: .confirmationAction) { Button("完成") { dismiss() } } }
        }
    }
}
