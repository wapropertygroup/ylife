import Foundation

/// A block-level element of a report section.
///
/// Why parse blocks at all, rather than handing the whole string to
/// `AttributedString(markdown:)`: that API is *inline* markdown. It understands bold
/// and links, and it renders a pipe table as literal pipes and a `##` heading as a
/// line beginning with two hashes. Reports here are full of both — one sampled run
/// carried 49 table rows — and `report_email.py` exists on the server for exactly
/// this reason, having found that a renderer which splits on blank lines "would
/// deliver a report's pipe tables as literal pipes".
///
/// So blocks are parsed here and inline formatting is delegated to AttributedString
/// *within* a block, where it is the right tool.
enum MarkdownBlock {
    case heading(level: Int, text: String)
    case paragraph(String)
    case bullets([String])
    case numbered([String])
    case table(MarkdownTable)
    case code(String)
    case quote(String)
    case rule
}

/// A block plus its position.
///
/// Identity is positional because the payloads are not unique — two sections of a
/// report legitimately contain the same sentence, and a `ForEach` keyed on the text
/// would render one of them and silently drop the other.
struct IndexedBlock: Identifiable {
    let id: Int
    let block: MarkdownBlock
}

struct MarkdownTable {
    let header: [String]
    let rows: [[String]]
    let alignments: [MarkdownAlignment]
}

enum MarkdownAlignment {
    case leading, center, trailing
}

/// A small, line-oriented Markdown block parser.
///
/// Deliberately not a full CommonMark implementation. It covers what the models
/// actually emit — ATX headings, `*`/`-`/`•` bullets, ordered lists, pipe tables,
/// fenced code, block quotes and horizontal rules — and treats everything else as a
/// paragraph. The failure mode of an unsupported construct is therefore "shown as
/// written", never "swallowed", which is the right way round for a document whose
/// numbers a reader is about to act on.
enum MarkdownParser {
    static func parse(_ source: String) -> [IndexedBlock] {
        var blocks: [MarkdownBlock] = []
        // Normalise line endings first. A CRLF payload otherwise leaves a trailing \r
        // on every line, which defeats the `hasPrefix("|")` and fence checks below and
        // silently downgrades the whole document to paragraphs.
        let lines = source
            .replacingOccurrences(of: "\r\n", with: "\n")
            .replacingOccurrences(of: "\r", with: "\n")
            .components(separatedBy: "\n")

        var index = 0
        var paragraph: [String] = []

        func flushParagraph() {
            let text = paragraph.joined(separator: "\n").trimmingCharacters(in: .whitespacesAndNewlines)
            if !text.isEmpty { blocks.append(.paragraph(text)) }
            paragraph.removeAll()
        }

        while index < lines.count {
            let line = lines[index]
            let trimmed = line.trimmingCharacters(in: .whitespaces)

            // Fenced code. Scans to the closing fence, or to the end of the document if
            // the model never closed it — an unterminated fence must not discard the
            // remainder of the report.
            if trimmed.hasPrefix("```") {
                flushParagraph()
                var body: [String] = []
                index += 1
                while index < lines.count,
                      !lines[index].trimmingCharacters(in: .whitespaces).hasPrefix("```") {
                    body.append(lines[index])
                    index += 1
                }
                index += 1 // consume the closing fence, if there was one
                blocks.append(.code(body.joined(separator: "\n")))
                continue
            }

            if trimmed.isEmpty {
                flushParagraph()
                index += 1
                continue
            }

            // Horizontal rule. Checked before the table delimiter because `---` on its
            // own is a rule, and before headings because `***` is not emphasis here.
            if isRule(trimmed) {
                flushParagraph()
                blocks.append(.rule)
                index += 1
                continue
            }

            // ATX heading.
            if trimmed.hasPrefix("#") {
                let hashes = trimmed.prefix(while: { $0 == "#" }).count
                if hashes <= 6 {
                    let rest = trimmed.dropFirst(hashes).trimmingCharacters(in: .whitespaces)
                    // `#hashtag` is not a heading. Requiring the space is what stops a
                    // model writing "#1 risk" from producing an H1 reading "1 risk".
                    if !rest.isEmpty, trimmed.dropFirst(hashes).first == " " {
                        flushParagraph()
                        blocks.append(.heading(level: hashes, text: rest))
                        index += 1
                        continue
                    }
                }
            }

            // Pipe table: a header row followed by a delimiter row. Both are required —
            // a single line containing pipes is far more often prose than a one-row
            // table, and misreading it as a table hides the sentence inside a grid.
            if trimmed.hasPrefix("|"), index + 1 < lines.count,
               let alignments = parseDelimiter(lines[index + 1]) {
                flushParagraph()
                let header = splitRow(trimmed)
                var rows: [[String]] = []
                index += 2
                while index < lines.count {
                    let candidate = lines[index].trimmingCharacters(in: .whitespaces)
                    guard candidate.hasPrefix("|") else { break }
                    rows.append(splitRow(candidate))
                    index += 1
                }
                blocks.append(.table(MarkdownTable(
                    header: header,
                    rows: normalise(rows, width: header.count),
                    alignments: pad(alignments, to: header.count)
                )))
                continue
            }

            // Block quote.
            if trimmed.hasPrefix(">") {
                flushParagraph()
                var body: [String] = []
                while index < lines.count {
                    let candidate = lines[index].trimmingCharacters(in: .whitespaces)
                    guard candidate.hasPrefix(">") else { break }
                    body.append(candidate.dropFirst().trimmingCharacters(in: .whitespaces))
                    index += 1
                }
                blocks.append(.quote(body.joined(separator: "\n")))
                continue
            }

            // Bullets. `•` is included because models emit it directly rather than as
            // markdown surprisingly often, and a literal bullet character at the start
            // of every line otherwise renders as a paragraph of bullets.
            if let item = bulletBody(trimmed) {
                flushParagraph()
                var items = [item]
                index += 1
                while index < lines.count,
                      let next = bulletBody(lines[index].trimmingCharacters(in: .whitespaces)) {
                    items.append(next)
                    index += 1
                }
                blocks.append(.bullets(items))
                continue
            }

            // Ordered list.
            if let item = numberedBody(trimmed) {
                flushParagraph()
                var items = [item]
                index += 1
                while index < lines.count,
                      let next = numberedBody(lines[index].trimmingCharacters(in: .whitespaces)) {
                    items.append(next)
                    index += 1
                }
                blocks.append(.numbered(items))
                continue
            }

            paragraph.append(line)
            index += 1
        }
        flushParagraph()

        return blocks.enumerated().map { IndexedBlock(id: $0.offset, block: $0.element) }
    }

    // MARK: - Line classification

    private static func isRule(_ line: String) -> Bool {
        guard line.count >= 3 else { return false }
        let stripped = line.replacingOccurrences(of: " ", with: "")
        return stripped.allSatisfy { $0 == "-" } || stripped.allSatisfy { $0 == "*" }
            || stripped.allSatisfy { $0 == "_" }
    }

    private static func bulletBody(_ line: String) -> String? {
        for marker in ["- ", "* ", "+ ", "• "] where line.hasPrefix(marker) {
            return String(line.dropFirst(marker.count)).trimmingCharacters(in: .whitespaces)
        }
        // A bare "•" with no space still reads as a bullet to a human.
        if line.hasPrefix("•") {
            return String(line.dropFirst()).trimmingCharacters(in: .whitespaces)
        }
        return nil
    }

    private static func numberedBody(_ line: String) -> String? {
        let digits = line.prefix(while: \.isNumber)
        guard !digits.isEmpty, digits.count <= 3 else { return nil }
        let rest = line.dropFirst(digits.count)
        guard rest.hasPrefix(". ") || rest.hasPrefix(") ") else { return nil }
        return String(rest.dropFirst(2)).trimmingCharacters(in: .whitespaces)
    }

    /// `|---|:---:|---:|` → alignments, or nil if this is not a delimiter row.
    private static func parseDelimiter(_ line: String) -> [MarkdownAlignment]? {
        let trimmed = line.trimmingCharacters(in: .whitespaces)
        guard trimmed.hasPrefix("|") else { return nil }
        let cells = splitRow(trimmed)
        guard !cells.isEmpty else { return nil }
        var out: [MarkdownAlignment] = []
        for cell in cells {
            let value = cell.trimmingCharacters(in: .whitespaces)
            // At least one dash, and nothing but dashes and colons.
            guard value.contains("-"), value.allSatisfy({ $0 == "-" || $0 == ":" }) else {
                return nil
            }
            if value.hasPrefix(":"), value.hasSuffix(":") { out.append(.center) }
            else if value.hasSuffix(":") { out.append(.trailing) }
            else { out.append(.leading) }
        }
        return out
    }

    /// Splits `| a | b |` into `["a", "b"]`.
    ///
    /// Honours `\|` as an escaped pipe. Without that a cell containing a pipe — which
    /// a model writing about option spreads or absolute values does produce — shifts
    /// every subsequent column one place left, and the row still renders, just wrong.
    private static func splitRow(_ line: String) -> [String] {
        var cells: [String] = []
        var current = ""
        var escaped = false
        // Drop the leading and trailing pipe so they do not yield empty edge cells.
        var body = Substring(line)
        if body.hasPrefix("|") { body = body.dropFirst() }
        if body.hasSuffix("|") { body = body.dropLast() }

        for character in body {
            if escaped {
                current.append(character)
                escaped = false
            } else if character == "\\" {
                escaped = true
            } else if character == "|" {
                cells.append(current.trimmingCharacters(in: .whitespaces))
                current = ""
            } else {
                current.append(character)
            }
        }
        cells.append(current.trimmingCharacters(in: .whitespaces))
        return cells
    }

    /// Pads or truncates every row to the header width.
    ///
    /// A ragged row is common in model output. Left ragged it would either crash an
    /// index-based Grid or, worse, shift cells into the wrong columns — a number
    /// appearing under the wrong heading is the one failure here that is both silent
    /// and material.
    private static func normalise(_ rows: [[String]], width: Int) -> [[String]] {
        rows.map { row in
            if row.count == width { return row }
            if row.count > width { return Array(row.prefix(width)) }
            return row + Array(repeating: "", count: width - row.count)
        }
    }

    private static func pad(_ alignments: [MarkdownAlignment], to width: Int) -> [MarkdownAlignment] {
        if alignments.count >= width { return Array(alignments.prefix(width)) }
        return alignments + Array(repeating: .leading, count: width - alignments.count)
    }
}
