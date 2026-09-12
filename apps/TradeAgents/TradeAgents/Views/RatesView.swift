import SwiftUI

/// The rates screen: what fed funds futures say the FOMC will do, meeting by meeting.
///
/// Everything below the first card is an *expectation* derived from the ZQ curve, not
/// policy — so the screen leads with the target range the Fed has actually set and only
/// then shows what is priced. Reversed, the implied path is the first number a reader
/// sees and reads as fact.
struct RatesView: View {
    @State private var state: LoadState = .loading

    enum LoadState {
        case loading
        case loaded(FedWatch)
        case failed(Error)
    }

    var body: some View {
        ZStack {
            Palette.background.ignoresSafeArea()
            content
        }
        .navigationTitle("Rates")
        .task { await load() }
    }

    @ViewBuilder
    private var content: some View {
        switch state {
        case .loading:
            LoadingPane()

        case .failed(let error):
            // `/api/fedwatch` answers 202 while its cache warms, which the client turns
            // into `APIError.warming` — so a cold box lands here and LoadFailure
            // presents it as "check again" rather than as a failure.
            LoadFailure(error: error) { Task { await load() } }

        case .loaded(let data) where data.meetings.isEmpty:
            // A 200 with no meetings is a real answer: the server returns one when the
            // futures curve is too sparse to derive a path. Saying so beats an empty
            // scroll view, which reads as a rendering bug.
            EmptyPane(
                icon: "building.columns",
                title: "No meetings priced",
                detail: "The futures curve was too sparse to derive a path."
            )

        case .loaded(let data):
            ScrollView {
                LazyVStack(spacing: 12) {
                    CurrentRangeCard(current: data.current, asOf: data.asOf, meta: data.meta)
                    ForEach(data.meetings) { meeting in
                        // Outcome tables for the nearest meeting only. The probability
                        // tree widens with the horizon, so the last meeting carries a
                        // dozen ranges and eight such tables is a wall of numbers that
                        // buries the one distribution anybody is trading.
                        MeetingCard(
                            meeting: meeting,
                            showsOutcomes: meeting.id == data.meetings.first?.id
                        )
                    }
                }
                .padding(.horizontal, 16)
                .padding(.vertical, 12)
            }
            // Matches the gesture the web app grew for its installed PWA, where
            // there is no reload button in standalone mode.
            .refreshable { await load() }
        }
    }

    private func load() async {
        do {
            let data = try await APIClient.shared.fedwatch()
            state = .loaded(data)
        } catch {
            state = .failed(error)
        }
    }
}

// MARK: - Header

private struct CurrentRangeCard: View {
    let current: FedCurrent?
    let asOf: String?
    let meta: Meta?

    var body: some View {
        Card {
            VStack(alignment: .leading, spacing: 10) {
                if let meta {
                    FreshnessBanner(meta: meta)
                }
                Text("Target range")
                    .font(.system(size: 9).weight(.medium))
                    .foregroundStyle(Palette.mutedText)
                    .textCase(.uppercase)
                Text(rangeLabel)
                    .font(.system(size: 34, weight: .semibold, design: .rounded).monospacedDigit())
                HStack(alignment: .top, spacing: 18) {
                    StatTile(label: "Effective", value: Format.percent(current?.effr, places: 2))
                    StatTile(label: "Midpoint", value: Format.percent(current?.mid, places: 2))
                    StatTile(label: "Curve as of", value: Format.day(asOf))
                    Spacer()
                }
            }
        }
    }

    /// The server already formats this ("3.50–3.75"), so it is displayed rather than
    /// recomposed from `lower` and `upper`: a second place that decides the dash and the
    /// decimal count is a second place for them to drift from the outcome rows below.
    private var rangeLabel: String {
        let label = current?.label?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        return label.isEmpty ? "—" : label
    }
}

// MARK: - One meeting

private struct MeetingCard: View {
    let meeting: FedMeeting
    let showsOutcomes: Bool

    var body: some View {
        Card {
            VStack(alignment: .leading, spacing: 10) {
                header

                if slices.isEmpty {
                    // Stated, not skipped — a missing bar with nothing in its place
                    // reads as a certainty rather than as an absence.
                    Text("No probability breakdown")
                        .font(.caption2)
                        .foregroundStyle(Palette.secondaryText)
                } else {
                    DirectionBar(slices: slices)
                    // The percentages sit under the bar rather than inside it. A 2%
                    // tail is a couple of points wide at any readable bar height, so an
                    // inline label would be clipped to nothing on exactly the segment
                    // worth reading: the small chance of a surprise.
                    HStack(alignment: .top, spacing: 16) {
                        ForEach(slices) { slice in
                            StatTile(
                                label: slice.label,
                                value: Format.percent(slice.value, places: 1),
                                tint: slice.tint
                            )
                        }
                        Spacer()
                    }
                }

                if let note {
                    Text(note)
                        .font(.caption2)
                        .foregroundStyle(Palette.warn)
                }

                if showsOutcomes, let outcomes = meeting.outcomes, !outcomes.isEmpty {
                    Divider().overlay(Palette.border)
                    OutcomeTable(outcomes: outcomes)
                }
            }
        }
    }

    private var header: some View {
        HStack(alignment: .firstTextBaseline) {
            VStack(alignment: .leading, spacing: 2) {
                Text(title)
                    .font(.subheadline.weight(.semibold))
                if let subtitle {
                    Text(subtitle)
                        .font(.caption2)
                        .foregroundStyle(Palette.secondaryText)
                }
            }
            Spacer()
            VStack(alignment: .trailing, spacing: 2) {
                Text(Format.percent(meeting.impliedRate, places: 2))
                    .font(.subheadline.weight(.semibold).monospacedDigit())
                Text(changeLabel)
                    .font(.caption.monospacedDigit())
                    .foregroundStyle(Self.directionTint(meeting.changeBp))
            }
        }
    }

    private var title: String {
        let label = meeting.label?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        return label.isEmpty ? Format.day(meeting.date) : label
    }

    /// The decision date under the month label. Suppressed when the server sent no
    /// label, because the title has then already fallen back to this same string and
    /// the card would print the date twice.
    private var subtitle: String? {
        let label = meeting.label?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        return label.isEmpty ? nil : Format.day(meeting.date)
    }

    /// `note` arrives as `""` far more often than as `nil` — the server writes an empty
    /// string on every meeting it has nothing to say about — so an `if let` alone would
    /// hang a blank amber line under most of these cards.
    private var note: String? {
        let text = meeting.note?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        return text.isEmpty ? nil : text
    }

    /// Basis points, signed the way `Format.signedPercent` signs a percentage: the "+"
    /// is the whole difference between a 12bp hike and a 12bp cut at a glance.
    private var changeLabel: String {
        guard let bp = meeting.changeBp else { return "—" }
        let sign = bp > 0 ? "+" : ""
        return sign + Format.number(bp, places: 1) + " bp"
    }

    /// Cut / hold / hike, dropping whichever the server did not send.
    ///
    /// A missing probability is not a zero probability, so it is left out of the bar and
    /// the legend entirely rather than drawn as a zero-width segment labelled 0.0% —
    /// which would be a claim about the meeting instead of an admission about the feed.
    /// A genuine `0.0` is kept: that one *is* a measurement.
    private var slices: [ProbabilitySlice] {
        let raw: [(String, Double?, Color)] = [
            ("Cut", meeting.cutProb, Palette.up),
            ("Hold", meeting.holdProb, Palette.secondaryText),
            ("Hike", meeting.hikeProb, Palette.down),
        ]
        return raw.compactMap { label, value, tint in
            guard let value else { return nil }
            return ProbabilitySlice(label: label, value: value, tint: tint)
        }
    }

    /// Deliberately not `Format.tint`, which was written for prices and returns green
    /// for any non-negative number. On this screen green already means *cut*, so the
    /// equity tint would paint a hike in the cut colour and contradict the bar sitting
    /// directly beneath it. These colours encode direction, not good news.
    static func directionTint(_ change: Double?) -> Color {
        guard let change, change != 0 else { return Palette.secondaryText }
        return change < 0 ? Palette.up : Palette.down
    }
}

/// One leg of the direction bar.
private struct ProbabilitySlice: Identifiable {
    let label: String
    let value: Double
    let tint: Color

    var id: String { label }
}

/// Cut / hold / hike as a single stacked bar.
private struct DirectionBar: View {
    let slices: [ProbabilitySlice]

    /// Widths are shares of the slices' *own* total, not of 100. The server rounds each
    /// leg to one decimal independently, so the three add to 99.9 or 100.1 as often as
    /// to 100, and dividing by a hard-coded 100 leaves a sliver of bare track at the end
    /// of the bar that reads as a fourth, unexplained outcome.
    private var total: Double { slices.reduce(0) { $0 + max($1.value, 0) } }

    var body: some View {
        GeometryReader { geo in
            HStack(spacing: 0) {
                ForEach(slices) { slice in
                    Rectangle()
                        .fill(slice.tint)
                        .frame(width: width(slice, in: geo.size.width))
                }
            }
        }
        .frame(height: 10)
        .background(Palette.well)
        .clipShape(Capsule())
    }

    private func width(_ slice: ProbabilitySlice, in full: CGFloat) -> CGFloat {
        // `total` is zero if every leg came back zero. Dividing anyway yields NaN, which
        // SwiftUI reports as an invalid frame dimension and then draws unpredictably.
        guard total > 0 else { return 0 }
        return full * CGFloat(max(slice.value, 0) / total)
    }
}

// MARK: - Outcomes for the nearest meeting

private struct OutcomeTable: View {
    let outcomes: [FedOutcome]

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Where the range lands")
                .font(.system(size: 9).weight(.medium))
                .foregroundStyle(Palette.mutedText)
                .textCase(.uppercase)
            // Left in the server's order, which is lowest range first. Sorting by
            // probability would put the modal outcome on top and destroy the shape of
            // the distribution — these rows are adjacent 25bp steps, and reading how
            // the mass is spread across them is the point of listing them at all.
            ForEach(outcomes) { outcome in
                OutcomeRow(outcome: outcome)
            }
        }
    }
}

private struct OutcomeRow: View {
    let outcome: FedOutcome

    var body: some View {
        HStack(spacing: 8) {
            Text(rangeLabel)
                .font(.caption.monospacedDigit())
            if let steps = outcome.steps {
                Chip(text: Self.stepsLabel(steps))
            }
            Spacer()
            Text(Format.percent(outcome.prob, places: 1))
                .font(.caption.weight(.semibold).monospacedDigit())
                .foregroundStyle(MeetingCard.directionTint(outcome.steps.map { Double($0) }))
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 7)
        .background(Palette.well, in: RoundedRectangle(cornerRadius: 8))
    }

    /// "3.50–3.75", the shape the server writes for the current range.
    ///
    /// Both ends are required. `Format.number` renders a missing one as an em dash, and
    /// "—–3.75" reads as a malformed range rather than as a half-known one.
    private var rangeLabel: String {
        guard outcome.lower != nil, outcome.upper != nil else { return "—" }
        return Format.number(outcome.lower, places: 2) + "–" + Format.number(outcome.upper, places: 2)
    }

    /// `steps` is a signed count of 25bp moves away from *today's* range, so the sign is
    /// the direction and has to survive into the label: "25 bp" on its own does not say
    /// whether the Fed cut or hiked to get there.
    private static func stepsLabel(_ steps: Int) -> String {
        guard steps != 0 else { return "No change" }
        let sign = steps > 0 ? "+" : ""
        return sign + Format.number(Double(steps * 25), places: 0) + " bp"
    }
}

#Preview {
    RatesView()
}
