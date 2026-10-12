import SwiftUI

/// 消息原文仍由服务端与现有加密缓存管理；解析结果只随当前视图存活。
struct ChatMarkdownText: View {
    let source: String
    @State private var renderedSource: String?
    @State private var document: ChatMarkdownDocument?

    init(_ source: String) { self.source = source }

    var body: some View {
        Group {
            if renderedSource == source, let document {
                ChatMarkdownBlocks(blocks: document.blocks)
            } else {
                Text(verbatim: source)
            }
        }
        .textSelection(.enabled)
        .tint(.teal)
        .environment(\.openURL, OpenURLAction { url in
            guard let safe = ChatMarkdownDocument.safeURL(url) else { return .discarded }
            return .systemAction(safe)
        })
        .task(id: source) {
            let parsed = ChatMarkdownDocument(source)
            guard !Task.isCancelled else { return }
            document = parsed
            renderedSource = source
        }
    }
}

private struct ChatMarkdownBlocks: View {
    let blocks: [ChatMarkdownDocument.Block]
    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            ForEach(blocks) { ChatMarkdownBlock(block: $0) }
        }
    }
}

private struct ChatMarkdownInline: View {
    let text: AttributedString
    var body: some View {
        Text(styled).fixedSize(horizontal: false, vertical: true)
    }
    private var styled: AttributedString {
        var result = text
        for run in text.runs where run.inlinePresentationIntent?.contains(.code) == true {
            result[run.range].font = .system(.body, design: .monospaced)
            result[run.range].backgroundColor = Color.secondary.opacity(0.12)
        }
        return result
    }
}

private struct ChatMarkdownBlock: View {
    let block: ChatMarkdownDocument.Block
    @State private var htmlPreview: RecordTextContent?
    @ViewBuilder var body: some View {
        if let kind = block.kind {
            switch kind {
            case .paragraph:
                ChatMarkdownInline(text: block.text)
            case .header(let level):
                ChatMarkdownInline(text: block.text).font(headingFont(level)).padding(.top, 4)
                    .accessibilityAddTraits(.isHeader)
            case .codeBlock(let language):
                codeBlock(language: language)
            case .blockQuote:
                ChatMarkdownBlocks(blocks: block.children)
                    .foregroundStyle(.secondary).padding(.leading, 12)
                    .overlay(alignment: .leading) { Rectangle().fill(.teal.opacity(0.45)).frame(width: 3) }
            case .orderedList:
                list(ordered: true)
            case .unorderedList:
                list(ordered: false)
            case .table(let columns):
                ChatMarkdownTable(block: block, columns: columns)
            case .thematicBreak:
                Divider().padding(.vertical, 4)
            default:
                ChatMarkdownInline(text: block.text)
                ChatMarkdownBlocks(blocks: block.children)
            }
        } else {
            ChatMarkdownInline(text: block.text)
        }
    }

    private func headingFont(_ level: Int) -> Font {
        switch level {
        case 1: .title2.bold()
        case 2: .title3.bold()
        case 3: .headline
        default: .body.bold()
        }
    }

    private func codeBlock(language: String?) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text(language.flatMap { $0.isEmpty ? nil : $0 } ?? "代码").foregroundStyle(.secondary)
                Spacer()
                if let content = ChatHTMLCodePreview.content(language: language, text: String(block.text.characters)) {
                    Button("查看网页", systemImage: "globe") { htmlPreview = content }
                        .accessibilityIdentifier("chatHTMLPreview")
                }
                Button("复制", systemImage: "doc.on.doc") { UIPasteboard.general.string = String(block.text.characters) }
                    .accessibilityLabel("复制代码")
            }.font(.caption)
            ScrollView(.horizontal) {
                Text(verbatim: String(block.text.characters))
                    .font(.system(.callout, design: .monospaced))
                    .fixedSize(horizontal: true, vertical: false)
                    .padding(.bottom, 3)
            }
        }.padding(12).background(.quaternary, in: RoundedRectangle(cornerRadius: 10))
            .sheet(item: $htmlPreview) { RecordLongTextView(content: $0) }
            .onChange(of: block.text) { _, _ in htmlPreview = nil }
    }

    private func list(ordered: Bool) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            ForEach(block.children) { item in
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Text(marker(item, ordered: ordered)).foregroundStyle(.secondary).frame(minWidth: 18, alignment: .trailing)
                    ChatMarkdownBlocks(blocks: item.children)
                }
            }
        }
    }

    private func marker(_ item: ChatMarkdownDocument.Block, ordered: Bool) -> String {
        // 不根据解析后的文字猜测复选框，避免吞掉代码或转义后的字面 “[x]”。
        if ordered, case .listItem(let ordinal)? = item.kind { return "\(ordinal)." }
        return "•"
    }
}

enum ChatHTMLCodePreview {
    static func content(language: String?, text: String) -> RecordTextContent? {
        guard let language, ["html", "htm", "xhtml"].contains(language.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()) else { return nil }
        // 仅显式 HTML 代码块提供手动预览，普通消息不会自动作为网页运行。
        return RecordTextContent(title: "HTML 预览", text: text, isHTML: true)
    }
}

private struct ChatMarkdownTable: View {
    let block: ChatMarkdownDocument.Block
    let columns: [PresentationIntent.TableColumn]
    @ScaledMetric(relativeTo: .body) private var cellWidth = 150.0

    var body: some View {
        ScrollView(.horizontal) {
            Grid(alignment: .topLeading, horizontalSpacing: 0, verticalSpacing: 0) {
                ForEach(block.children) { row in
                    let header = isHeader(row)
                    GridRow {
                        ForEach(columns.indices, id: \.self) { column in
                            ChatMarkdownInline(text: cell(row, column: column))
                                .fontWeight(header ? .semibold : .regular)
                                .frame(width: cellWidth, alignment: alignment(column))
                                .padding(10)
                                .background(header ? Color.secondary.opacity(0.12) : .clear)
                                .overlay(alignment: .bottom) { Divider() }
                        }
                    }
                }
            }.padding(.bottom, 3)
        }
    }
    private func isHeader(_ row: ChatMarkdownDocument.Block) -> Bool {
        if case .tableHeaderRow? = row.kind { return true }
        return false
    }
    private func cell(_ row: ChatMarkdownDocument.Block, column: Int) -> AttributedString {
        row.children.first { if case .tableCell(let index)? = $0.kind { return index == column }; return false }?.text ?? AttributedString()
    }
    private func alignment(_ column: Int) -> Alignment {
        switch columns[column].alignment {
        case .right: .trailing
        case .center: .center
        default: .leading
        }
    }
}
