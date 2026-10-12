import Foundation

/// 仅探测已配对身份保存的局域网地址；探测不携带家庭凭据。
enum FastConnection {
    static func localCandidates(_ values: [String]) -> [String] {
        var result: [String] = []
        for value in values {
            guard let url = try? ServerTrust.origin(value), let host = url.host?.lowercased() else { continue }
            let pieces = host.split(separator: ".").compactMap { Int($0) }
            let ipv4 = pieces.count == 4 && pieces.allSatisfy { (0...255).contains($0) } &&
                (pieces[0] == 10 || pieces[0] == 192 && pieces[1] == 168 || pieces[0] == 172 && (16...31).contains(pieces[1]))
            guard ipv4 || host.hasSuffix(".local") && host != "localhost.local" else { continue }
            let normalized = url.absoluteString.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
            if !result.contains(normalized) { result.append(normalized) }
        }
        return Array(result.prefix(8))
    }

    struct Verified: Sendable { let address: String; let identity: ServerTrust.Verified }
    static func probe(_ values: [String], serverID: String, publicKey: String, namespace: String) async -> Verified? {
        await withTaskGroup(of: Verified?.self) { group in
            for value in localCandidates(values) {
                group.addTask {
                    guard let proof = try? await ServerTrust.probe(url: value, serverID: serverID, publicKey: publicKey, timeout: 1.5),
                          proof.proof.namespace_anchor == namespace, !Task.isCancelled else { return nil }
                    return Verified(address: value, identity: proof)
                }
            }
            for await result in group {
                if let result { group.cancelAll(); return result }
            }
            return nil
        }
    }
}
