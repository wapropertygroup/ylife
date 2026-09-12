import SwiftUI

/// Renders parsed Markdown blocks.
///
/// Parsing is done by the caller and handed in already parsed, because a SwiftUI body
/// can be evaluated many times per second and a report section runs to several
/// thousand characters — re-parsing on every evaluation is the classic way a scroll
/// view turns to treacle. `ReportSectionCard` parses once into `@State`.
struct MarkdownView: View {
    let blocks: [IndexedBlock]

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            ForEach(blocks) { item in
                switch item.block {
                case .heading(let level, let text):
                    Text(Inline.render(text))
                        .font(Self.headingFont(level))
                        .foregroundStyle(.primary)
                        .padding(.top, level <= 2 ? 6 : 2)

                case .paragraph(let text):
                    Text(Inline.render(text))
                        .font(.callout)
                        .foregroundStyle(Palette.secondaryText)
                        .textSelection(.enabled)

                case .bullets(let items):
                    VStack(alignment: .leading, spacing: 5) {
                        ForEach(Array(items.enumerated()), id: \.offset) { _, item in
                            HStack(alignment: .firstTextBaseline, spacing: 7) {
                                Text("•").foregroundStyle(Palette.brand)
                                Text(Inline.render(item))
                                    .font(.callout)
                                    .foregroundStyle(Palette.secondaryText)
                            }
                        }
                    }

                case .numbered(let items):
                    VStack(alignment: .leading, spacing: 5) {
                        ForEach(Array(items.enumerated()), id: \.offset) { index, item in
                            HStack(alignment: .firstTextBaseline, spacing: 7) {
                                Text("\(index + 1).")
                                    .font(.callout.monospacedDigit())
                                    .foregroundStyle(Palette.brand)
                                Text(Inline.render(item))
                                    .font(.callout)
                                    .foregroundStyle(Palette.secondaryText)
                            }
                        }
                    }

                case .table(let table):
                    MarkdownTableView(table: table)

                case .code(let body):
                    Text(body)
                        .font(.caption.monospaced())
                        .foregroundStyle(Palette.secondaryText)
                        .padding(10)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .background(Palette.well, in: RoundedRectangle(cornerRadius: 8))
                        .textSelection(.enabled)

                case .quote(let text):
                    HStack(alignment: .top, spacing: 8) {
                        Rectangle()
                            .fill(Palette.brand)
                            .frame(width: 2)
                        Text(Inline.render(text))
                            .font(.callout.italic())
                            .foregroundStyle(Palette.secondaryText)
                    }

                case .rule:
                    Divider().overlay(Palette.border)
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private static func headingFont(_ level: Int) -> Font {
        switch level {
        case 1:  return .title3.weight(.bold)
        case 2:  return .headline
        case 3:  return .subheadline.weight(.semibold)
        default: return .callout.weight(.semibold)
        }
    }
}

/// Inline formatting — bold, italic, inline code, links.
enum Inline {
    /// Parses inline Markdown, falling back to the literal text.
    ///
    /// `AttributedString(markdown:)` **throws on malformed input**, and model output is
    /// exactly where an unbalanced `**` shows up. A `try?` returning nil that was then
    /// rendered as an empty Text would delete a sentence from a financial report and
    /// leave no trace, so the fallback is the raw string rather than nothing.
    static func render(_ source: String) -> AttributedString {
        let prepared = restoreInlineHTML(source)
        let options = AttributedString.MarkdownParsingOptions(
            // Inline only: block syntax was already handled by MarkdownParser, and
            // letting this pass re-interpret it would turn a table row's pipes into
            // nothing and swallow leading hashes.
            interpretedSyntax: .inlineOnlyPreservingWhitespace
        )
        guard var attributed = try? AttributedString(markdown: prepared, options: options) else {
            return AttributedString(prepared)
        }
        stripUnsafeLinks(&attributed)
        return attributed
    }

    /// Converts the handful of inline HTML tags the models actually emit.
    ///
    /// Not speculative tidying: `<br>` turns up in real reports, inside table cells,
    /// because a pipe table has no other way to hold two lines — one sampled run has
    /// `macd <br> macdh` in a cell. Left alone it renders as the literal characters
    /// "<br>", which is what this app did before.
    ///
    /// The tag set is taken from the server's own allowlist rather than invented:
    /// `static/markdown.js` (`INLINE_HTML`) and `report_email.py` restore exactly
    /// these after escaping, so handling them here is what keeps the app, the web page
    /// and the completion mail rendering one report the same way.
    ///
    /// Tags with a clean Markdown equivalent become that — the delimiter is symmetric,
    /// so one substitution covers both the opening and closing tag. The rest are
    /// stripped to their text: losing the underline on `<u>` is cosmetic, whereas
    /// printing "<u>" at the reader is a defect. Nothing is ever dropped along with
    /// its content.
    private static func restoreInlineHTML(_ source: String) -> String {
        // Cheap bail-out. The overwhelming majority of sections contain no HTML at
        // all, and this runs per block on documents of several thousand characters.
        guard source.contains("<") else { return source }

        var out = source
        for (pattern, replacement) in Self.inlineHTMLRules {
            out = out.replacingOccurrences(
                of: pattern,
                with: replacement,
                options: [.regularExpression, .caseInsensitive]
            )
        }
        return out
    }

    /// Ordered: `<br>` is matched before the bare-strip rule so it becomes a newline
    /// rather than being silently removed. `strong` before `s`, and `sub`/`sup`/`small`
    /// before `s`, for the same reason — a shorter alternative listed first would match
    /// the prefix of a longer tag name and leave the remainder as text.
    private static let inlineHTMLRules: [(String, String)] = [
        // Horizontal whitespace either side of the tag is consumed with it. Models
        // write `macd <br> macdh`, and preserving those spaces (which
        // `.inlineOnlyPreservingWhitespace` faithfully would) leaves the second line
        // indented by one space inside a table cell.
        (#"[ \t]*<\s*br\s*/?\s*>[ \t]*"#, "\n"),
        (#"<\s*/?\s*(?:strong|b)\s*/?\s*>"#, "**"),
        (#"<\s*/?\s*(?:em|i)\s*/?\s*>"#, "*"),
        (#"<\s*/?\s*code\s*/?\s*>"#, "`"),
        (#"<\s*/?\s*(?:small|sub|sup|mark|ins|del|u|s)\s*/?\s*>"#, ""),
    ]

    /// Drops link destinations that are not https.
    ///
    /// Reports are model output derived from news sources, and the showcase feed is
    /// public — so a link here is attacker-influenced text being turned into something
    /// the reader can tap. This mirrors the posture the server already takes: both
    /// `report_email.py` and `static/markdown.js` deliberately leave `<a>` out of the
    /// inline-tag allowlist rather than emit an unvetted attribute.
    ///
    /// The link text is always kept. Removing the words as well as the destination
    /// would silently edit the report's prose, which is a worse failure than a
    /// non-tappable reference.
    private static func stripUnsafeLinks(_ text: inout AttributedString) {
        for run in text.runs where run.link != nil {
            let scheme = run.link?.scheme?.lowercased()
            if scheme != "https" {
                text[run.range].link = nil
            }
        }
    }
}

/// A pipe table.
///
/// Horizontally scrollable rather than wrapped: these tables are mostly numeric and
/// four to six columns wide, and squeezing them to a phone's width turns every cell
/// into a two-line wrap that is far harder to read across than a scroll. The header
/// stays inside the scroll so it travels with its columns.
private struct MarkdownTableView: View {
    let table: MarkdownTable

    var body: some View {
        ScrollView(.horizontal, showsIndicators: true) {
            Grid(alignment: .topLeading, horizontalSpacing: 0, verticalSpacing: 0) {
                GridRow {
                    ForEach(Array(table.header.enumerated()), id: \.offset) { index, cell in
                        cellView(cell, index: index, isHeader: true)
                    }
                }
                ForEach(Array(table.rows.enumerated()), id: \.offset) { rowIndex, row in
                    GridRow {
                        ForEach(Array(row.enumerated()), id: \.offset) { index, cell in
                            cellView(cell, index: index, isHeader: false, striped: rowIndex.isMultiple(of: 2))
                        }
                    }
                }
            }
            .background(Palette.well, in: RoundedRectangle(cornerRadius: 8))
            .overlay(RoundedRectangle(cornerRadius: 8).stroke(Palette.border))
            .padding(.vertical, 2)
        }
    }

    @ViewBuilder
    private func cellView(_ text: String, index: Int, isHeader: Bool, striped: Bool = false) -> some View {
        let alignment = table.alignments.indices.contains(index) ? table.alignments[index] : .leading
        Text(Inline.render(text))
            .font(isHeader ? .caption.weight(.semibold) : .caption)
            .foregroundStyle(isHeader ? Color.primary : Palette.secondaryText)
            .multilineTextAlignment(textAlignment(alignment))
            .frame(minWidth: 64, idealWidth: 130, maxWidth: 260,
                   alignment: frameAlignment(alignment))
            .fixedSize(horizontal: false, vertical: true)
            .padding(.horizontal, 10)
            .padding(.vertical, 7)
            .background(isHeader ? Palette.border : (striped ? Color.clear : Palette.card.opacity(0.5)))
    }

    private func textAlignment(_ alignment: MarkdownAlignment) -> TextAlignment {
        switch alignment {
        case .leading:  return .leading
        case .center:   return .center
        case .trailing: return .trailing
        }
    }

    private func frameAlignment(_ alignment: MarkdownAlignment) -> Alignment {
        switch alignment {
        case .leading:  return .leading
        case .center:   return .center
        case .trailing: return .trailing
        }
    }
}
