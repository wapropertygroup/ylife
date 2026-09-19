import Charts
import SwiftUI

/// The rates screen: what fed funds futures say the FOMC will do, meeting by meeting.
///
/// Everything below the first card is an *expectation* derived from the ZQ curve, not
/// policy — so the screen leads with the target range the Fed has actually set and only
/// then shows what is priced. Reversed, the implied path is the first number a reader
/// sees and reads as fact.
struct RatesView: View {
    @State private var state: LoadState = .loading
    @State private var curves: YieldCurves?
    @State private var spread: YieldSpread?
    @Environment(Localization.self) private var loc

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
        .navigationTitle(loc(S.rates))
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
                title: loc(S.noMeetings),
                detail: loc(S.noMeetingsHint)
            )

        case .loaded(let data):
            ScrollView {
                LazyVStack(spacing: Metrics.stackSpacing) {
                    CurrentRangeCard(current: data.current, asOf: data.asOf, meta: data.meta)

                    // The curve and the spread sit above the meeting cards: they are
                    // what the market has already done to the whole term structure,
                    // where the meetings below are one instrument's forecast of the
                    // front end. They load separately and are simply absent until they
                    // arrive, rather than blocking the screen the view is named for.
                    if let curves { YieldCurveCard(curves: curves) }
                    if let spread { YieldSpreadCard(spread: spread) }

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
                .padding(.horizontal, Metrics.screenH)
                .padding(.vertical, Metrics.screenV)
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
        // Fetched after, and independently: these two are additions to the screen, so
        // a failure in either must not take down the FOMC path the screen exists for.
        // Concurrently with each other, because they share nothing — sequentially they
        // are two round trips to the same box for no reason.
        async let curveTask = try? APIClient.shared.yieldCurves()
        async let spreadTask = try? APIClient.shared.yieldSpread()
        curves = await curveTask
        spread = await spreadTask
    }
}

// MARK: - Header

private struct CurrentRangeCard: View {
    let current: FedCurrent?
    let asOf: String?
    let meta: Meta?
    @Environment(Localization.self) private var loc

    var body: some View {
        Card {
            VStack(alignment: .leading, spacing: 10) {
                if let meta {
                    FreshnessBanner(meta: meta)
                }
                Text(loc(S.targetRange))
                    .font(.system(size: 9).weight(.medium))
                    .foregroundStyle(Palette.mutedText)
                    .textCase(.uppercase)
                Text(rangeLabel)
                    .font(.system(size: 34, weight: .semibold, design: .rounded).monospacedDigit())
                HStack(alignment: .top, spacing: 18) {
                    StatTile(label: loc(S.effective), value: Format.percent(current?.effr, places: 2))
                    StatTile(label: loc(S.midpoint), value: Format.percent(current?.mid, places: 2))
                    StatTile(label: loc(S.curveAsOf), value: Format.day(asOf))
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
    @Environment(Localization.self) private var loc

    var body: some View {
        // The three labels are resolved *here* and handed down. `ProbabilitySlice` is a
        // plain value type with no environment of its own, and `slices(…)` runs outside
        // the view hierarchy — so the choice is between threading the resolved strings
        // through or giving the slice its own opinion about what "Cut" says, which is a
        // second source of truth for a word already in the table.
        let slices = slices(cut: loc(S.cut), hold: loc(S.hold), hike: loc(S.hike))
        return Card {
            VStack(alignment: .leading, spacing: 10) {
                header

                if slices.isEmpty {
                    // Stated, not skipped — a missing bar with nothing in its place
                    // reads as a certainty rather than as an absence.
                    Text(loc(S.noBreakdown))
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

    /// The meeting's month, in the reader's language.
    ///
    /// Derived from `date` rather than shown from `label`: the server builds that
    /// with `strftime("%b %Y")`, so it is English whatever the reader chose, and it
    /// sat directly above a correctly localized `2026年10月27日`. The server string
    /// is still the fallback for a date that will not parse.
    private var title: String {
        if let derived = Format.monthYear(meeting.date) { return derived }
        let label = meeting.label?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        return label.isEmpty ? Format.day(meeting.date) : label
    }

    /// The decision date under the month label. Suppressed when the title has fallen
    /// all the way back to that same date string, so the card does not print it twice.
    private var subtitle: String? {
        let day = Format.day(meeting.date)
        return title == day ? nil : day
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
        return sign + Format.number(bp, places: 1) + " " + loc(S.basisPoints)
    }

    /// Cut / hold / hike, dropping whichever the server did not send.
    ///
    /// A missing probability is not a zero probability, so it is left out of the bar and
    /// the legend entirely rather than drawn as a zero-width segment labelled 0.0% —
    /// which would be a claim about the meeting instead of an admission about the feed.
    /// A genuine `0.0` is kept: that one *is* a measurement.
    ///
    /// Takes the labels rather than resolving them, so this stays callable from anywhere
    /// — see the note in `body`.
    private func slices(cut: String, hold: String, hike: String) -> [ProbabilitySlice] {
        let raw: [(String, Double?, Color)] = [
            (cut, meeting.cutProb, Palette.up),
            (hold, meeting.holdProb, Palette.secondaryText),
            (hike, meeting.hikeProb, Palette.down),
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
    @Environment(Localization.self) private var loc

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(loc(S.whereRange))
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
    @Environment(Localization.self) private var loc

    var body: some View {
        HStack(spacing: 8) {
            Text(rangeLabel)
                .font(.caption.monospacedDigit())
            if let steps = outcome.steps {
                // Resolved at the call site for the same reason the direction labels
                // are: `stepsLabel` is a pure string builder with no environment.
                Chip(text: Self.stepsLabel(steps,
                                           noChange: loc(S.noChange),
                                           bp: loc(S.basisPoints)))
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
    private static func stepsLabel(_ steps: Int, noChange: String, bp: String) -> String {
        guard steps != 0 else { return noChange }
        let sign = steps > 0 ? "+" : ""
        return sign + Format.number(Double(steps * 25), places: 0) + " " + bp
    }
}

#Preview {
    RatesView()
        .environment(Localization())
}

// MARK: - Yield curve

/// The three sovereign curves the endpoint carries, one at a time.
///
/// A picker rather than three stacked charts: the reader is comparing shape against
/// shape — inverted, flat, steep — and three small charts in a column is the one
/// layout that makes that comparison hard. The y-domain is fitted per country because
/// JGBs at 1.5-4% and Treasuries at 4-5.4% on a shared axis flattens both.
private struct YieldCurveCard: View {
    let curves: YieldCurves
    @State private var country: Country = .us
    @Environment(Localization.self) private var loc

    enum Country: String, CaseIterable, Identifiable {
        case us, cn, jp
        var id: String { rawValue }
        var label: LocalizedString {
            switch self {
            case .us: return S.curveUS
            case .cn: return S.curveCN
            case .jp: return S.curveJP
            }
        }
    }

    private var curve: YieldCurve? {
        switch country {
        case .us: return curves.us
        case .cn: return curves.cn
        case .jp: return curves.jp
        }
    }

    private struct Point: Identifiable {
        let id: String
        let months: Int
        let yield: Double
    }

    private var points: [Point] {
        (curve?.ordered ?? []).map { Point(id: $0.label, months: $0.months, yield: $0.yield) }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text(loc(S.yieldCurve)).font(.subheadline.weight(.semibold))
                Spacer()
                Picker("", selection: $country) {
                    ForEach(Country.allCases) { c in Text(loc(c.label)).tag(c) }
                }
                .pickerStyle(.segmented)
                .labelsHidden()
                .frame(width: 190)
            }

            if points.count > 1 {
                Chart(points) { p in
                    LineMark(x: .value("Tenor", p.id), y: .value("Yield", p.yield))
                        .interpolationMethod(.monotone)
                        .foregroundStyle(Palette.brand)
                    PointMark(x: .value("Tenor", p.id), y: .value("Yield", p.yield))
                        .foregroundStyle(Palette.brand)
                        .symbolSize(18)
                }
                // Ordered explicitly. A category axis otherwise arranges the tenors by
                // first appearance in the data, which is dictionary order and therefore
                // arbitrary between launches — the curve would redraw scrambled, and
                // differently each time.
                .chartXScale(domain: points.map(\.id))
                .chartYScale(domain: yDomain)
                .chartYAxis { AxisMarks(position: .leading) }
                .frame(height: 150)

                if let spread = curve?.spread10y3m {
                    HStack(spacing: 8) {
                        Text(loc(S.spread10y3m))
                            .font(.caption2).foregroundStyle(Palette.secondaryText)
                        Text(Format.signedPercent(spread, places: 2))
                            .font(.caption.monospacedDigit())
                            .foregroundStyle(Format.tint(spread))
                        // An inverted curve is the single thing this chart is read for,
                        // and a small negative number does not announce itself.
                        if spread < 0 {
                            Chip(text: loc(S.inverted), tint: Palette.down)
                        }
                    }
                }
            } else {
                Text(loc(S.noPriceHistory))
                    .font(.caption2).foregroundStyle(Palette.secondaryText)
            }
        }
        .padding(Metrics.cardPadding)
        .background(Palette.card, in: RoundedRectangle(cornerRadius: Metrics.cardRadius))
        .overlay(RoundedRectangle(cornerRadius: Metrics.cardRadius).stroke(Palette.border))
    }

    /// Padded around the data, never zero-based: the shape of a curve spanning
    /// 4.14-5.39% is invisible against an axis starting at zero, and that shape is
    /// the entire content of the chart.
    private var yDomain: ClosedRange<Double> {
        let ys = points.map(\.yield)
        guard let lo = ys.min(), let hi = ys.max() else { return 0...1 }
        guard hi > lo else { return (lo - 0.5)...(hi + 0.5) }
        let pad = (hi - lo) * 0.2
        return (lo - pad)...(hi + pad)
    }
}

// MARK: - 10Y − 3M spread

/// The classic inversion chart, with the recession flag the endpoint ships beside it.
private struct YieldSpreadCard: View {
    let spread: YieldSpread
    @Environment(Localization.self) private var loc

    private struct Point: Identifiable {
        let id = UUID()
        let date: Date
        let value: Double
        let recession: Bool
    }

    /// Thinned to roughly a weekly cadence. The payload is ~2,500 daily observations
    /// and a ten-year line drawn at that density is slower to render than to read;
    /// the last point is kept whatever the stride, so the chart still ends today.
    private var points: [Point] {
        let parsed: [Point] = zip(zip(spread.dates, spread.spread), spread.recession)
            .compactMap { pair, rec in
                let (date, value) = pair
                guard let value, let parsed = DateParse.iso(date) else { return nil }
                return Point(date: parsed, value: value, recession: (rec ?? 0) != 0)
            }
        guard parsed.count > 600 else { return parsed }
        let stride = parsed.count / 600 + 1
        var thinned = parsed.enumerated().filter { $0.offset % stride == 0 }.map(\.element)
        if let last = parsed.last, thinned.last?.date != last.date { thinned.append(last) }
        return thinned
    }

    /// The recession flag collapsed into contiguous spans.
    ///
    /// The endpoint ships one 0/1 per observation, which the previous chart drew
    /// as one translucent rule per flagged day — hundreds of overlapping strokes
    /// for a single recession. As spans it is one rectangle each, and the footnote
    /// promising shading is honoured rather than merely claimed.
    private var recessions: [ClosedRange<Date>] {
        var out: [ClosedRange<Date>] = []
        var start: Date?
        var previous: Date?
        for p in points {
            if p.recession {
                if start == nil { start = p.date }
                previous = p.date
            } else if let s = start, let e = previous {
                out.append(s...max(e, s))
                start = nil
                previous = nil
            }
        }
        if let s = start, let e = previous { out.append(s...max(e, s)) }
        return out
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text(loc(S.yieldSpread)).font(.subheadline.weight(.semibold))
                Spacer()
                if let latest = points.last {
                    Text(Format.signedPercent(latest.value, places: 2))
                        .font(.callout.weight(.semibold).monospacedDigit())
                        .foregroundStyle(Format.tint(latest.value))
                }
            }

            if points.count > 1 {
                // Zero is drawn as a rule rather than left to an axis tick, because
                // below it is an inversion and that is the whole reading.
                TimeSeriesChart(
                    series: [ChartSeries(id: loc(S.yieldSpread), color: Palette.brand,
                                         points: points.map {
                                             PricePoint(date: $0.date, price: $0.value)
                                         })],
                    height: 150,
                    rules: [0],
                    bands: recessions,
                    format: { Format.signedPercent($0, places: 2) }
                )

                Text(loc(S.recessionShade))
                    .font(.caption2).foregroundStyle(Palette.mutedText)
            }
        }
        .padding(Metrics.cardPadding)
        .background(Palette.card, in: RoundedRectangle(cornerRadius: Metrics.cardRadius))
        .overlay(RoundedRectangle(cornerRadius: Metrics.cardRadius).stroke(Palette.border))
    }
}
