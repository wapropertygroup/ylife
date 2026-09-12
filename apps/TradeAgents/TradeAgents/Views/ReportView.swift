import SwiftUI

/// One finished report, as a timeline of agent turns.
///
/// The sections arrive already split by `agent_roles.split_sections` on the server,
/// including each role's colour, icon and display name — so the app cannot drift from
/// the web page or the completion email on what a "Bull Researcher" is called or what
/// colour it is. That is worth preserving: three renderers of one report that disagree
/// about the cast is exactly the kind of thing nobody notices until a reader does.
struct ReportView: View {
    let job: AgentJob

    @State private var state: LoadState = .loading
    @State private var expanded: Set<Int> = []
    @Environment(Localization.self) private var loc

    /// The section to scroll to once loaded. Separate from `expanded` so that a reader
    /// opening other turns afterwards does not get yanked back to this one.
    @State private var focus: Int?

    enum LoadState {
        case loading
        case loaded(ShowcaseReport)
        case failed(Error)
    }

    var body: some View {
        ZStack {
            Palette.background.ignoresSafeArea()
            content
        }
        .navigationTitle(job.ticker)
        #if os(iOS)
        .navigationBarTitleDisplayMode(.inline)
        #endif
        .task { await load() }
    }

    @ViewBuilder
    private var content: some View {
        switch state {
        case .loading:
            LoadingPane(label: loc(S.loadingReport))

        case .failed(let error):
            LoadFailure(error: error) { Task { await load() } }

        case .loaded(let report) where report.sections.isEmpty:
            EmptyPane(icon: "doc.text",
                      title: loc(S.noSections),
                      detail: loc(S.noSectionsHint))

        case .loaded(let report):
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 12) {
                        ReportHeader(report: report, job: job)
                        ForEach(report.sections) { section in
                            ReportSectionCard(
                                section: section,
                                isExpanded: expanded.contains(section.id),
                                toggle: { toggle(section.id) }
                            )
                            .id(section.id)
                        }
                    }
                    .padding(.horizontal, 16)
                    .padding(.vertical, 12)
                }
                .task(id: focus) {
                    // Bring the auto-expanded turn into view.
                    //
                    // Without this the reader lands at the top of sixteen collapsed
                    // cards with the one open section far below the fold — which looks
                    // exactly like nothing was expanded at all, and quietly wastes the
                    // decision to open the Portfolio Manager's turn in the first place.
                    //
                    // The trade-off is landing part-way down a document. Accepted
                    // because the header carries the verdict chip anyway, so the thing
                    // scrolled past is supporting analysis rather than the conclusion.
                    guard let focus else { return }
                    proxy.scrollTo(focus, anchor: .top)
                }
            }
        }
    }

    private func toggle(_ id: Int) {
        if expanded.contains(id) { expanded.remove(id) } else { expanded.insert(id) }
    }

    private func load() async {
        do {
            let report = try await APIClient.shared.showcaseReport(id: job.id)
            state = .loaded(report)
            // Open the Portfolio Manager's turn, falling back to the first section.
            //
            // That turn is the decision the whole run exists to produce, and it is the
            // *last* section emitted — so a reader landing on a collapsed list would
            // have to scroll past fifteen analyst turns to reach the answer. This is
            // the same ordering trap report_email.py records, where an in-order walk
            // dropped the decision and kept seven analysts.
            let decisionSection = report.sections.last { $0.role?.group == "decision" }
            if let target = decisionSection ?? report.sections.first {
                expanded = [target.id]
                focus = target.id
            }
        } catch {
            state = .failed(error)
        }
    }
}

private struct ReportHeader: View {
    let report: ShowcaseReport
    let job: AgentJob
    @Environment(Localization.self) private var loc

    var body: some View {
        Card {
            VStack(alignment: .leading, spacing: 10) {
                HStack(alignment: .firstTextBaseline) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(report.ticker)
                            .font(.title2.weight(.bold).monospaced())
                        Text(Format.day(report.date))
                            .font(.caption)
                            .foregroundStyle(Palette.secondaryText)
                    }
                    Spacer()
                    Chip(text: Decision.label(report.decision, fallback: loc(S.noDecision)),
                         tint: Decision.tint(report.decision))
                }

                Divider().overlay(Palette.border)

                HStack(spacing: 16) {
                    StatTile(label: loc(S.finished), value: Format.timestamp(report.finishedAt))
                    if let elapsed = job.elapsedSec {
                        StatTile(label: loc(S.took), value: Format.elapsed(elapsed))
                    }
                    StatTile(label: loc(S.turns), value: "\(report.sections.count)")
                    Spacer()
                }

                if let models = modelLine {
                    Text(models)
                        .font(.caption2)
                        .foregroundStyle(Palette.mutedText)
                }
            }
        }
    }

    /// Which models produced this. Published by the server for a reason worth keeping
    /// visible: a report written on the cheapest tier should not read as one written
    /// on the most capable.
    private var modelLine: String? {
        let parts = [job.deepModel, job.quickModel].compactMap { $0 }
        guard !parts.isEmpty else { return nil }
        let unique = Array(NSOrderedSet(array: parts)).compactMap { $0 as? String }
        var line = unique.joined(separator: " · ")
        if let thinking = job.thinking, !thinking.isEmpty {
            line += " · \(loc(S.thinkingLabel)): \(thinking)"
        }
        return line
    }
}

/// One agent's turn, collapsed until opened.
///
/// Collapsed by default because a report is sixteen turns and several thousand words;
/// rendering all of it at once is both a wall of text and a real cost — one sampled
/// report is 215 KB of Markdown, and parsing every section up front is work done for
/// turns the reader will never open.
private struct ReportSectionCard: View {
    let section: ReportSection
    let isExpanded: Bool
    let toggle: () -> Void
    @Environment(Localization.self) private var loc

    /// Parsed once, on first expansion, and kept. Parsing inside `body` would redo it
    /// on every SwiftUI evaluation.
    @State private var blocks: [IndexedBlock] = []

    private var roleColor: Color {
        Color(webHex: section.role?.color) ?? Palette.brand
    }

    private var title: String {
        // A section with no role is the report's preamble, not an agent's turn — the
        // first one usually is. Labelling it with the team name or a placeholder role
        // would invent a speaker.
        //
        // `agent_roles.py` already ships both spellings of every role (`name` / `zh`),
        // so a Chinese reader gets 市场分析师 by *picking* one the server sent rather
        // than by translating here — which would be a second opinion about what an
        // agent is called, drifting from the web page and the completion mail.
        loc.pick(section.role?.name, section.role?.zh) ?? loc(S.summary)
    }

    /// The team divider under the role, likewise picked rather than translated. `pick`
    /// also treats a blank string as absent, which the API sends more often than null.
    private var team: String? {
        loc.pick(section.team, section.teamZh)
    }

    var body: some View {
        Card(padding: 0) {
            VStack(alignment: .leading, spacing: 0) {
                Button(action: toggle) {
                    HStack(spacing: 10) {
                        // The role's own colour, served by agent_roles.py.
                        RoundedRectangle(cornerRadius: 2)
                            .fill(roleColor)
                            .frame(width: 3, height: 30)

                        if let icon = section.role?.icon {
                            Text(icon).font(.body)
                        }

                        VStack(alignment: .leading, spacing: 1) {
                            Text(title)
                                .font(.subheadline.weight(.semibold))
                                .foregroundStyle(roleColor)
                            if let team {
                                Text(team)
                                    .font(.caption2)
                                    .foregroundStyle(Palette.mutedText)
                            }
                        }

                        Spacer()

                        Image(systemName: isExpanded ? "chevron.up" : "chevron.down")
                            .font(.caption.weight(.semibold))
                            .foregroundStyle(Palette.mutedText)
                    }
                    .padding(14)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)

                if isExpanded {
                    Divider().overlay(Palette.border)
                    MarkdownView(blocks: blocks)
                        .padding(14)
                }
            }
        }
        // Keyed on expansion rather than on appearance: a LazyVStack builds the card
        // before it is opened, so parsing on `.onAppear` would do the expensive work
        // for all sixteen turns anyway and defeat the point of collapsing them.
        .task(id: isExpanded) {
            guard isExpanded, blocks.isEmpty else { return }
            blocks = MarkdownParser.parse(section.body)
        }
    }
}
