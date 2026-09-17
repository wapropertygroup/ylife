import Charts
import SwiftUI

/// Index multiples: where the S&P sits against its own long history, and the
/// forward multiples the server banks one row a day.
struct ValuationView: View {
    @State private var state: LoadState = .loading
    @Environment(Localization.self) private var loc

    enum LoadState { case loading, loaded(Multiples), failed(Error) }

    var body: some View {
        ZStack {
            Palette.background.ignoresSafeArea()
            switch state {
            case .loading: LoadingPane()
            case .failed(let e): LoadFailure(error: e) { Task { await load() } }
            case .loaded(let data):
                ScrollView {
                    LazyVStack(spacing: 12) {
                        if let meta = data.meta { FreshnessBanner(meta: meta) }
                        if let h = data.headline { HeadlineGrid(headline: h) }
                        BankedForwardCard(points: data.forwardHistory)
                        Footnote(text: loc(S.valBankedNote))
                    }
                    .padding(.horizontal, 16).padding(.vertical, 12)
                }
                .refreshable { await load() }
            }
        }
        .navigationTitle(loc(S.valuation))
        .task { await load() }
    }

    private func load() async {
        do { state = .loaded(try await APIClient.shared.multiples()) }
        catch { state = .failed(error) }
    }
}

private struct HeadlineGrid: View {
    let headline: MultiplesHeadline
    @Environment(Localization.self) private var loc

    private var items: [(String, HeadlineMetric)] {
        [(loc(S.valCape), headline.spxCape),
         (loc(S.valCapePct), headline.spxCapePercentile),
         (loc(S.valFwdSpx), headline.spxFwdRealized),
         (loc(S.valFwdQqq), headline.qqqForwardPe),
         (loc(S.valFwdSox), headline.soxForwardPe),
         (loc(S.valFwdN225), headline.n225ForwardPe)]
            .compactMap { label, metric in
                guard let metric, metric.value != nil else { return nil }
                return (label, metric)
            }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(loc(S.valTitle)).font(.subheadline.weight(.semibold))
            LazyVGrid(columns: [GridItem(.adaptive(minimum: 140), spacing: 8)], spacing: 8) {
                ForEach(items, id: \.0) { label, metric in
                    VStack(alignment: .leading, spacing: 2) {
                        Text(label).font(.caption2).foregroundStyle(Palette.secondaryText)
                            .lineLimit(1)
                        HStack(alignment: .firstTextBaseline, spacing: 4) {
                            Text(Format.number(metric.value, places: 2))
                                .font(.callout.weight(.semibold).monospacedDigit())
                            if let yoy = metric.yoy {
                                Text(Format.signedPercent(yoy, places: 1))
                                    .font(.caption2.monospacedDigit())
                                    .foregroundStyle(Format.tint(yoy))
                            }
                        }
                        // Coverage, where the endpoint reports it. A forward P/E
                        // built from 55 of 97 members is a weaker claim than one
                        // from 40 of 40, and printing the two identically would
                        // present them as equals.
                        if let c = metric.constituents {
                            Text(c).font(.caption2)
                                .foregroundStyle(Palette.mutedText)
                        }
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.horizontal, 10).padding(.vertical, 8)
                    .background(Palette.well, in: RoundedRectangle(cornerRadius: 8))
                }
            }
        }
        .padding(14)
        .background(Palette.card, in: RoundedRectangle(cornerRadius: 14))
        .overlay(RoundedRectangle(cornerRadius: 14).stroke(Palette.border))
    }
}

/// The banked forward-P/E series, SPY against QQQ.
///
/// Two lines rather than two cards: the whole point of banking both is that the
/// gap between them is the growth premium, and a reader cannot see a gap by
/// looking at two charts in turn.
private struct BankedForwardCard: View {
    let points: [ForwardPoint]
    @Environment(Localization.self) private var loc

    private struct Plot: Identifiable {
        let id = UUID()
        let date: Date
        let value: Double
        let series: String
    }

    private var plots: [Plot] {
        points.flatMap { p -> [Plot] in
            guard let d = p.parsedDate else { return [] }
            var out: [Plot] = []
            if let spy = p.spy { out.append(Plot(date: d, value: spy, series: "SPY")) }
            if let qqq = p.qqq { out.append(Plot(date: d, value: qqq, series: "QQQ")) }
            return out
        }
    }

    /// Clamped to the data with a small pad, never zero-based.
    ///
    /// Not cosmetic. The whole reason SPY and QQQ share one chart is the *gap*
    /// between them — the growth premium — and against a zero-based axis two
    /// multiples in the low 20s render as one thick line. A P/E of zero is not a
    /// reference point anyone reads from, so there is nothing lost in dropping it.
    private var yDomain: ClosedRange<Double> {
        let values = plots.map(\.value)
        guard let lo = values.min(), let hi = values.max() else { return 0...1 }
        guard hi > lo else { return (lo - 1)...(hi + 1) }   // a single vintage
        let pad = (hi - lo) * 0.15
        return (lo - pad)...(hi + pad)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(loc(S.valBanked)).font(.subheadline.weight(.semibold))
            if plots.count > 1 {
                Chart(plots) { p in
                    LineMark(x: .value("Date", p.date), y: .value("P/E", p.value))
                        .foregroundStyle(by: .value("Series", p.series))
                        .interpolationMethod(.monotone)
                }
                .chartYScale(domain: yDomain)
                .chartYAxis { AxisMarks(position: .leading) }
                .localizedDateAxis(desiredCount: 3, dates: plots.map(\.date))
                .chartLegend(position: .top, alignment: .leading)
                .frame(height: 150)
            } else {
                // The series starts empty and is worth nothing for months. Saying so
                // is the point: a silently missing chart looks like a broken screen,
                // and this one is correctly reporting that it has not accumulated yet.
                Text(loc(S.valBankedNote))
                    .font(.caption2).foregroundStyle(Palette.secondaryText)
            }
        }
        .padding(14)
        .background(Palette.card, in: RoundedRectangle(cornerRadius: 14))
        .overlay(RoundedRectangle(cornerRadius: 14).stroke(Palette.border))
    }
}
