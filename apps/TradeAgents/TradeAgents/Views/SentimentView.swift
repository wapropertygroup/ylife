import Charts
import SwiftUI

/// The sentiment screen: CNN's Fear & Greed composite, where it stood at each of the
/// horizons CNN publishes, and the stored history behind it.
struct SentimentView: View {
    @State private var state: LoadState = .loading
    @State private var putCall: PutCall?
    @State private var skew: Skew?
    @Environment(Localization.self) private var loc

    enum LoadState {
        case loading
        case loaded(FearGreed)
        case failed(Error)
    }

    var body: some View {
        ZStack {
            Palette.background.ignoresSafeArea()
            content
        }
        .navigationTitle(loc(S.sentiment))
        .task { await load() }
    }

    @ViewBuilder
    private var content: some View {
        switch state {
        case .loading:
            LoadingPane()

        case .failed(let error):
            LoadFailure(error: error) { Task { await load() } }

        case .loaded(let data) where data.score == nil && data.history.isEmpty:
            EmptyPane(
                icon: "gauge.with.dots.needle.33percent",
                title: loc(S.noSentiment),
                detail: loc(S.noSentimentHint)
            )

        case .loaded(let data):
            // The three cards are independent on purpose. `/api/fear-greed` merges a
            // live CNN read with the DynamoDB series, and a failed CNN fetch still
            // answers 200 with the whole history and a null score — so a dashed
            // headline above a full chart is a real response, not a broken screen, and
            // gating the chart on the score would throw away the part that survived.
            ScrollView {
                LazyVStack(spacing: 12) {
                    ScoreCard(score: data.score, rating: data.rating)
                    ComparisonCard(data: data)
                    HistoryCard(history: data.history)
                    // Two independent reads on the same question the gauge above
                    // answers. They load separately so neither can take the CNN
                    // composite down with it.
                    if let putCall { PutCallCard(data: putCall) }
                    if let skew { SkewCard(data: skew) }
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
            let data = try await APIClient.shared.fearGreed()
            state = .loaded(data)
        } catch {
            state = .failed(error)
        }
        async let pcTask = try? APIClient.shared.putCall()
        async let skewTask = try? APIClient.shared.skew()
        putCall = await pcTask
        skew = await skewTask
    }
}

// MARK: - Bands

/// The index's five zones.
///
/// Only the *colour* is decided here. The words a reader sees are CNN's own `rating`
/// whenever the payload carries one: the boundaries below are the published ones, but a
/// locally derived label would disagree with the number's own source if CNN ever moved
/// them, while looking exactly as authoritative. The label here is the fallback for a
/// response that has a score and no rating.
private enum FearGreedBand {
    case extremeFear
    case fear
    case neutral
    case greed
    case extremeGreed

    /// Boundaries are half-open upwards, so a reading of exactly 45 is Neutral and has
    /// one answer rather than two.
    static func of(_ score: Double) -> FearGreedBand {
        switch score {
        case ..<25: return .extremeFear
        case ..<45: return .fear
        case ..<55: return .neutral
        case ..<75: return .greed
        default:    return .extremeGreed
        }
    }

    var label: LocalizedString {
        switch self {
        case .extremeFear:  return S.fgExtremeFear
        case .fear:         return S.fgFear
        case .neutral:      return S.fgNeutral
        case .greed:        return S.fgGreed
        case .extremeGreed: return S.fgExtremeGreed
        }
    }

    /// CNN's own wording, mapped back onto a band.
    ///
    /// The rule this screen was built on is that CNN's `rating` wins over a locally
    /// derived one, so a boundary CNN moves cannot silently disagree with the number
    /// beside it. Rendering that string directly is fine in English and wrong in
    /// Chinese — it put the single word "Fear" under a headline reading 恐慌与贪婪.
    /// Matching it to a band keeps CNN as the authority on *which* band while letting
    /// the app own the words, and an unrecognised rating still falls through to the
    /// raw string rather than being dropped.
    static func matching(rating: String) -> FearGreedBand? {
        switch rating.lowercased().trimmingCharacters(in: .whitespacesAndNewlines) {
        case "extreme fear":  return .extremeFear
        case "fear":          return .fear
        case "neutral":       return .neutral
        case "greed":         return .greed
        case "extreme greed": return .extremeGreed
        default:              return nil
        }
    }

    /// Greed and Extreme Greed share `Palette.up`: the palette holds exactly one green,
    /// and mixing a second one in here would put a colour on screen that appears nowhere
    /// in the web app it was copied from. The two bands are told apart by their labels.
    var tint: Color {
        switch self {
        case .extremeFear:          return Palette.down
        case .fear:                 return Palette.warn
        case .neutral:              return Palette.secondaryText
        case .greed, .extremeGreed: return Palette.up
        }
    }
}

// MARK: - Cards

private struct ScoreCard: View {
    let score: Double?
    let rating: String?
    @Environment(Localization.self) private var loc

    private var band: FearGreedBand? { score.map(FearGreedBand.of) }

    var body: some View {
        Card {
            VStack(alignment: .leading, spacing: 8) {
                Text(loc(S.fearGreed))
                    .font(.system(size: 9).weight(.medium))
                    .foregroundStyle(Palette.mutedText)
                    .textCase(.uppercase)
                HStack(alignment: .firstTextBaseline, spacing: 12) {
                    Text(Format.number(score, places: 0))
                        .font(.system(size: 52, weight: .bold, design: .rounded).monospacedDigit())
                        .foregroundStyle(band?.tint ?? Palette.secondaryText)
                    if let ratingLabel {
                        Chip(text: ratingLabel, tint: band?.tint ?? Palette.secondaryText)
                    }
                    Spacer()
                }
                // The number is meaningless without its scale, and this screen has no
                // gauge to carry it: 28 is only alarming once you know the range.
                Text(loc(S.fearGreedHint))
                    .font(.caption2)
                    .foregroundStyle(Palette.mutedText)
            }
        }
    }

    /// CNN's classification, in the reader's language — see `FearGreedBand.matching`.
    /// `rating` arrives as an empty string, not `nil`, when the upstream fetch returned
    /// nothing, so this has to be a trim-and-test rather than an `if let`.
    private var ratingLabel: String? {
        let text = rating?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        if !text.isEmpty {
            if let matched = FearGreedBand.matching(rating: text) { return loc(matched.label) }
            return text        // a word CNN added; shown as-is beats not shown
        }
        return band.map { loc($0.label) }
    }
}

private struct ComparisonCard: View {
    let data: FearGreed
    @Environment(Localization.self) private var loc

    var body: some View {
        Card {
            VStack(alignment: .leading, spacing: 10) {
                Text(loc(S.comparedWith))
                    .font(.system(size: 9).weight(.medium))
                    .foregroundStyle(Palette.mutedText)
                    .textCase(.uppercase)
                HStack(alignment: .top, spacing: 10) {
                    tile(loc(S.prevClose), data.prevClose)
                    tile(loc(S.oneWeek), data.prevWeek)
                    tile(loc(S.oneMonth), data.prevMonth)
                    tile(loc(S.oneYear), data.prevYear)
                }
            }
        }
    }

    /// Tinted by each reading's own band rather than by `Format.tint`. The index cannot
    /// go negative, so the equity tint would hand back the up colour for every value and
    /// quietly render a row of Extreme Fear readings in green.
    private func tile(_ label: String, _ value: Double?) -> some View {
        StatTile(
            label: label,
            value: Format.number(value, places: 0),
            tint: value.map { FearGreedBand.of($0).tint } ?? Palette.secondaryText
        )
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

private struct HistoryCard: View {
    let history: [FearGreedPoint]
    @Environment(Localization.self) private var loc

    var body: some View {
        Card {
            VStack(alignment: .leading, spacing: 10) {
                Text(loc(S.history))
                    .font(.system(size: 9).weight(.medium))
                    .foregroundStyle(Palette.mutedText)
                    .textCase(.uppercase)

                if history.count > 1 {
                    TimeSeriesChart(
                        series: [ChartSeries(
                            id: loc(S.fearGreed), color: Palette.brand,
                            // FearGreedPoint carries epoch millis and its own
                            // `rating`; only the date and score are plotted.
                            points: history.map { PricePoint(date: $0.date, price: $0.y) })],
                        height: 170,
                        // Fixed 0-100: this is an index with published band
                        // boundaries, so the absolute level is the reading and a
                        // fitted domain would make a quiet fortnight look dramatic.
                        fixedYDomain: 0.0...100.0,
                        rules: [25, 50, 75],
                        format: { Format.number($0, places: 0) }
                    )
                } else {
                    // Stated, not skipped. A blank space here would read as a flat index.
                    Text(loc(S.noReadings))
                        .font(.caption2)
                        .foregroundStyle(Palette.secondaryText)
                        .frame(height: 170, alignment: .center)
                }

                if let countLabel {
                    Text(countLabel)
                        .font(.caption2)
                        .foregroundStyle(Palette.mutedText)
                }
            }
        }
    }

    /// How much data is behind the line. The series is the merged DynamoDB + CNN
    /// history, so its length is a property of this deployment rather than a fixed
    /// window, and four automatic tick labels do not tell a reader whether they are
    /// looking at one year or five.
    private var countLabel: String? {
        guard !history.isEmpty else { return nil }
        return Format.number(Double(history.count), places: 0) + " " + loc(S.fgReadings)
    }
}

#Preview {
    SentimentView()
        .environment(Localization())
}

// MARK: - Options-derived sentiment

/// The equity put/call ratio, against its own 20-day average.
///
/// A second opinion on the same question the Fear & Greed gauge answers, and an
/// independent one: this is what option buyers actually paid for, where the composite
/// above is CNN's blend of seven indicators.
struct PutCallCard: View {
    let data: PutCall
    @Environment(Localization.self) private var loc

    private var points: [PricePoint] { data.series.tail(180) }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .firstTextBaseline) {
                Text(loc(S.putCall)).font(.subheadline.weight(.semibold))
                Spacer()
                Text(Format.number(data.current, places: 2))
                    .font(.title3.weight(.semibold).monospacedDigit())
                if let chg = data.dayChg {
                    // A rising put/call is more hedging, which is risk-off — so the
                    // usual green-is-up tint is inverted here, as it is for VIX.
                    Text(Format.signedPercent(chg, places: 2))
                        .font(.caption.monospacedDigit())
                        .foregroundStyle(Format.tint(-chg))
                }
            }

            if points.count > 1 {
                TimeSeriesChart(
                    series: [ChartSeries(id: loc(S.putCall), color: Palette.brand,
                                         points: points)],
                    height: 120,
                    rules: data.ma20.map { [$0] } ?? [],
                    format: { Format.number($0, places: 2) }
                )

                if let ma = data.ma20 {
                    Text("\(loc(S.putCall20d)) \(Format.number(ma, places: 2))")
                        .font(.caption2).foregroundStyle(Palette.mutedText)
                }
            }
        }
        .padding(14)
        .background(Palette.card, in: RoundedRectangle(cornerRadius: 14))
        .overlay(RoundedRectangle(cornerRadius: 14).stroke(Palette.border))
    }
}

/// CBOE SKEW against VIX.
///
/// Two series on one chart because the comparison is the content: SKEW is the price of
/// the tail *relative* to at-the-money vol, so a high SKEW with a low VIX — which is
/// what the current reading is — says something a single line cannot.
struct SkewCard: View {
    let data: Skew
    @Environment(Localization.self) private var loc

    private struct Plot: Identifiable {
        let id = UUID()
        let date: Date
        let value: Double
        let series: String
    }

    /// SKEW runs around 110-170 and VIX around 12-40, so they cannot share an axis
    /// without flattening VIX into a line along the floor. VIX is plotted on its own
    /// scale underneath instead of being rescaled onto SKEW's — a rescaled series
    /// renders as though it were the real number, which is the mistake this file's
    /// neighbours all avoid.
    private var skewPoints: [PricePoint] { data.skewSeries.tail(250) }
    private var vixPoints: [PricePoint] { data.vixSeries.tail(250) }

    private var bandLabel: LocalizedString? {
        switch data.latest?.band?.lowercased() {
        case "low":      return S.skewLow
        case "normal":   return S.skewNormal
        case "elevated": return S.skewElevated
        case "extreme":  return S.skewExtreme
        default:         return nil
        }
    }

    private var bandTint: Color {
        switch data.latest?.band?.lowercased() {
        case "extreme":  return Palette.down
        case "elevated": return Palette.warn
        default:         return Palette.secondaryText
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .firstTextBaseline) {
                Text(loc(S.skewIndex)).font(.subheadline.weight(.semibold))
                Spacer()
                Text(Format.number(data.latest?.skew, places: 1))
                    .font(.title3.weight(.semibold).monospacedDigit())
                if let band = bandLabel {
                    Chip(text: loc(band), tint: bandTint)
                }
            }

            if let pct = data.latest?.percentile {
                HStack(spacing: 6) {
                    Text(loc(S.percentileLbl))
                        .font(.caption2).foregroundStyle(Palette.secondaryText)
                    Text(Format.number(pct, places: 1))
                        .font(.caption.monospacedDigit())
                }
            }

            if skewPoints.count > 1 {
                TimeSeriesChart(
                    series: [ChartSeries(id: loc(S.skewIndex), color: Palette.brand,
                                         points: skewPoints)],
                    height: 110,
                    format: { Format.number($0, places: 1) }
                )
            }

            if vixPoints.count > 1 {
                Text("VIX").font(.caption2).foregroundStyle(Palette.secondaryText)
                TimeSeriesChart(
                    series: [ChartSeries(id: "VIX", color: .orange, points: vixPoints)],
                    height: 80,
                    format: { Format.number($0, places: 1) }
                )
            }

            // What a high reading does and does not mean. Without this the number is
            // routinely read as a crash forecast, which it is not.
            Text(loc(S.skewNote))
                .font(.caption2).foregroundStyle(Palette.mutedText)
        }
        .padding(14)
        .background(Palette.card, in: RoundedRectangle(cornerRadius: 14))
        .overlay(RoundedRectangle(cornerRadius: 14).stroke(Palette.border))
    }
}
