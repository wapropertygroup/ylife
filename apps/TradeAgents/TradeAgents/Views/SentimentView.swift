import Charts
import SwiftUI

/// The sentiment screen: CNN's Fear & Greed composite, where it stood at each of the
/// horizons CNN publishes, and the stored history behind it.
struct SentimentView: View {
    @State private var state: LoadState = .loading

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
        .navigationTitle("Sentiment")
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
                title: "No sentiment data",
                detail: "CNN returned nothing and there is no stored history yet."
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

    var label: String {
        switch self {
        case .extremeFear:  return "Extreme Fear"
        case .fear:         return "Fear"
        case .neutral:      return "Neutral"
        case .greed:        return "Greed"
        case .extremeGreed: return "Extreme Greed"
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

    private var band: FearGreedBand? { score.map(FearGreedBand.of) }

    var body: some View {
        Card {
            VStack(alignment: .leading, spacing: 8) {
                Text("Fear & Greed")
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
                Text("0 is Extreme Fear, 100 is Extreme Greed.")
                    .font(.caption2)
                    .foregroundStyle(Palette.mutedText)
            }
        }
    }

    /// CNN's wording first, the local band only as a fallback — and `rating` arrives as
    /// an empty string, not `nil`, when the upstream fetch returned nothing, so this has
    /// to be a trim-and-test rather than an `if let`.
    private var ratingLabel: String? {
        let text = rating?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        if !text.isEmpty { return text }
        return band?.label
    }
}

private struct ComparisonCard: View {
    let data: FearGreed

    var body: some View {
        Card {
            VStack(alignment: .leading, spacing: 10) {
                Text("Compared with")
                    .font(.system(size: 9).weight(.medium))
                    .foregroundStyle(Palette.mutedText)
                    .textCase(.uppercase)
                HStack(alignment: .top, spacing: 10) {
                    tile("Prev close", data.prevClose)
                    tile("1 week", data.prevWeek)
                    tile("1 month", data.prevMonth)
                    tile("1 year", data.prevYear)
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

    var body: some View {
        Card {
            VStack(alignment: .leading, spacing: 10) {
                Text("History")
                    .font(.system(size: 9).weight(.medium))
                    .foregroundStyle(Palette.mutedText)
                    .textCase(.uppercase)

                if history.count > 1 {
                    Chart(history) { point in
                        // The x value is the parsed `Date` itself: Swift Charts scales a
                        // date axis natively, with none of the web side's missing
                        // Chart.js adapter problem. Plotting the index of the point
                        // instead would space an irregular series evenly and hide every
                        // gap in the history — the misstatement a category axis makes.
                        LineMark(
                            x: .value("Date", point.date),
                            y: .value("Score", point.y)
                        )
                        .interpolationMethod(.monotone)
                        .foregroundStyle(Palette.brand)
                    }
                    // Fixed 0…100 because the index is bounded, unlike the price
                    // sparklines on /markets which clamp to their own data. A domain
                    // fitted to the window would redraw a quiet fortnight's 6-point
                    // wobble as a dramatic swing, and no two loads of this screen would
                    // be comparable to each other. Spelled as Doubles, not `0...100`:
                    // an integer domain does not match a Double axis, and the failure is
                    // a silently unscaled chart rather than a compiler complaint.
                    .chartYScale(domain: 0.0...100.0)
                    .chartYAxis {
                        // The band boundaries, so the gridlines say something rather
                        // than falling wherever an automatic stride puts them.
                        AxisMarks(values: [0.0, 25, 50, 75, 100]) { _ in
                            AxisGridLine().foregroundStyle(Palette.border)
                            AxisValueLabel().foregroundStyle(Palette.mutedText)
                        }
                    }
                    .chartXAxis {
                        AxisMarks(values: .automatic(desiredCount: 4)) { _ in
                            AxisGridLine().foregroundStyle(Palette.border)
                            AxisValueLabel().foregroundStyle(Palette.mutedText)
                        }
                    }
                    .frame(height: 170)
                } else {
                    // Stated, not skipped. A blank space here would read as a flat index.
                    Text("No history")
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
        return Format.number(Double(history.count), places: 0) + " daily readings"
    }
}

#Preview {
    SentimentView()
}
