import Foundation

/// 使用系统 Markdown 解析器保留块结构；只生成展示数据，不访问网络或执行内容。
struct ChatMarkdownDocument {
    struct Block: Identifiable {
        let id: Int
        let kind: PresentationIntent.Kind?
        let text: AttributedString
        let children: [Block]
    }

    let blocks: [Block]

    init(_ source: String) {
        guard let parsed = try? AttributedString(markdown: source, options: .init(
            interpretedSyntax: .full, failurePolicy: .returnPartiallyParsedIfPossible
        )) else {
            blocks = [Block(id: -1, kind: nil, text: AttributedString(source), children: [])]
            return
        }
        let root = Builder(id: 0, kind: nil)
        var nodes: [Int: Builder] = [:]
        var literalID = -1
        var previousLiteral: Builder?
        for run in parsed.runs {
            let path = Array((run.presentationIntent?.components ?? []).reversed())
            var parent = root
            if path.isEmpty {
                // 原始 HTML 等无块意图内容仍是普通文字，不进入 H5 或图片加载器。
                if let previousLiteral { parent = previousLiteral }
                else {
                    let literal = Builder(id: literalID, kind: nil)
                    literalID -= 1
                    root.children.append(literal)
                    previousLiteral = literal
                    parent = literal
                }
            } else {
                previousLiteral = nil
                for component in path {
                    if let existing = nodes[component.identity] { parent = existing }
                    else {
                        let node = Builder(id: component.identity, kind: component.kind)
                        parent.children.append(node)
                        nodes[component.identity] = node
                        parent = node
                    }
                }
            }
            var text = AttributedString(parsed[run.range])
            text.presentationIntent = nil
            if let imageURL = run.imageURL {
                let description = String(text.characters).replacingOccurrences(of: "\u{fffc}", with: "")
                    .trimmingCharacters(in: .whitespacesAndNewlines)
                text = AttributedString("图片：" + (description.isEmpty ? "查看图片链接" : description))
                text.link = Self.safeURL(imageURL)
            } else if let link = run.link {
                text.link = Self.safeURL(link)
            }
            // 图片仅保留显式可点击的链接，打开对话本身不会向第三方发送请求。
            text.imageURL = nil
            parent.text.append(text)
        }
        blocks = root.children.map { $0.block }
    }

    static func safeURL(_ raw: String) -> URL? {
        guard let url = URL(string: raw) else { return nil }
        return safeURL(url)
    }

    static func safeURL(_ url: URL) -> URL? {
        guard let scheme = url.scheme?.lowercased(), ["https", "http"].contains(scheme),
              let host = url.host, !host.isEmpty, url.user == nil, url.password == nil else { return nil }
        return url
    }

    private final class Builder {
        let id: Int
        let kind: PresentationIntent.Kind?
        var text = AttributedString()
        var children: [Builder] = []
        init(id: Int, kind: PresentationIntent.Kind?) { self.id = id; self.kind = kind }
        var block: Block { Block(id: id, kind: kind, text: text, children: children.map { $0.block }) }
    }
}
