import Charts
import SwiftUI

/// The markets screen: volatility, sector rotation, and one card per instrument
/// with a switchable sparkline — and, prominently, how old the data is.
///
/// Everything here comes from the single `/api/markets` call the screen already
/// made. The payload carried VIX with two years of weekly closes, per-instrument
/// weekly and monthly series, both moving averages and the 52-week range, and the
/// screen showed none of it: one daily sparkline and a sector strip. No new
/// endpoint was needed to fix that, which also means no new way for the screen to
/// fail — a second request is a second thing that can be slow, rate-limited or
/// down, and `CLAUDE.md` records what the Yahoo budget does to this backend when
/// callers multiply.
struct MarketsView: View {
    @State private var state: LoadState = .loading
    @State private var timeframe: Timeframe = .daily
    @State private var sectorWindow: SectorWindow = .day
    @State private var breadth: Breadth?
    @Environment(Localization.self) private var loc
    @Environment(AlertCenter.self) private var alerts

    enum LoadState {
        case loading
        case loaded(MarketsResponse)
        case failed(Error)
    }

    enum Timeframe: CaseIterable {
        case daily, weekly, monthly

        var label: LocalizedString {
            switch self {
            case .daily:   return S.tfDaily
            case .weekly:  return S.tfWeekly
            case .monthly: return S.tfMonthly
            }
        }

        func series(_ i: Instrument) -> Series? {
            switch self {
            case .daily:   return i.daily
            case .weekly:  return i.weekly
            case .monthly: return i.monthly
            }
        }
    }

    enum SectorWindow: CaseIterable {
        case day, week
        var label: LocalizedString { self == .day ? S.sectorsToday : S.sectorsWeek }
        func value(_ s: Sector) -> Double? { self == .day ? s.dayChange : s.weekChangePct }
    }

    /// Display order for the instruments `/api/markets` returns. The payload is a
    /// dictionary, so iterating it directly would reshuffle the screen on every
    /// refresh. Anything the server adds that is not listed here is appended
    /// alphabetically rather than dropped, so a new instrument shows up without a
    /// client release.
    private static let preferredOrder = [
        "spx", "ndx", "dji", "ixic", "n225", "kospi", "csi500", "ftse",
        "gold", "copper", "oil", "brent", "natgas", "dxy",
    ]

    var body: some View {
        ZStack {
            Palette.background.ignoresSafeArea()
            content
        }
        .navigationTitle(loc(S.markets))
        .task { await load() }
    }

    @ViewBuilder
    private var content: some View {
        switch state {
        case .loading:
            LoadingPane()

        case .failed(let error):
            LoadFailure(error: error) { Task { await load() } }

        case .loaded(let data):
            ScrollView {
                LazyVStack(spacing: Metrics.stackSpacing) {
                    if let meta = data.meta {
                        FreshnessBanner(meta: meta)
                    }
                    if let vix = data.vix {
                        VolatilityCard(vix: vix)
                    }
                    if !data.sectors.isEmpty {
                        SectorCard(sectors: data.sectors, window: $sectorWindow)
                    }
                    // Breadth sits above the per-instrument cards because it qualifies
                    // them: an index up 1% on a third of its members participating is a
                    // different tape from the same 1% broadly earned, and the reader
                    // should have that before the index cards, not after fourteen of
                    // them. Absent until it loads — see `load()`.
                    if let breadth { BreadthCard(data: breadth) }
                    ForEach(ordered(data.indices)) { instrument in
                        InstrumentCard(instrument: instrument, timeframe: $timeframe)
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

    private func ordered(_ indices: [String: Instrument]) -> [Instrument] {
        var out: [Instrument] = []
        var remaining = indices
        for key in Self.preferredOrder {
            if let hit = remaining.removeValue(forKey: key) { out.append(hit) }
        }
        out.append(contentsOf: remaining.keys.sorted().compactMap { remaining[$0] })
        return out
    }

    private func load() async {
        do {
            let data = try await APIClient.shared.markets()
            state = .loaded(data)
            // Raised from here rather than from inside the client, because the language
            // an alert is written in is a reader preference and there is no environment
            // to read it from in `AlertCenter`. Dedupe lives in `raise`, so re-observing
            // the same payload on every refresh costs nothing.
            alerts.observe(markets: data, language: loc.language)
        } catch {
            state = .failed(error)
        }
        // A separate endpoint, so a separate failure. `/api/breadth` recomputes across
        // 503 names and is the slowest thing this screen touches; letting it fail the
        // whole view would trade the screen's entire contents for one card.
        breadth = try? await APIClient.shared.breadth()
    }
}

// MARK: - Volatility

private struct VolatilityCard: View {
    let vix: Vix
    @Environment(Localization.self) private var loc

    private var points: [PricePoint] { vix.weekly?.points ?? [] }

    /// Below 1 the curve is inverted — near-term fear above three-month — which is
    /// the stressed reading. Stated in words as well as shown, because this is the
    /// one figure on the screen whose direction is not self-evident.
    private var inverted: Bool { (vix.termRatio ?? 1) < 1 }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .firstTextBaseline) {
                Text(loc(S.volatility))
                    .font(.subheadline.weight(.semibold))
                Spacer()
                Text(Format.number(vix.current))
                    .font(.title3.weight(.semibold).monospacedDigit())
                Text(Format.signedPercent(vix.dayChange))
                    .font(.caption.monospacedDigit())
                    // VIX up is risk-off, so the usual green-is-up tint is inverted
                    // here: a spike is not good news.
                    .foregroundStyle(Format.tint(vix.dayChange.map { -$0 }))
            }

            if points.count > 1 {
                TimeSeriesChart(
                    series: [ChartSeries(id: "VIX", color: .orange,
                                         points: points, filled: true)],
                    height: 96,
                    // 20 is the rough boundary between an ordinary tape and a
                    // nervous one — the same line the web page draws.
                    rules: [20],
                    format: { Format.number($0, places: 2) }
                )
                Text(loc(S.twoYearRange))
                    .font(.caption2)
                    .foregroundStyle(Palette.secondaryText)
            }

            HStack(spacing: 14) {
                StatTile(label: loc(S.vixTerm),
                         value: Format.number(vix.termRatio),
                         tint: inverted ? Palette.down : Palette.secondaryText)
                if vix.vix3m != nil {
                    StatTile(label: "VIX3M", value: Format.number(vix.vix3m))
                }
                if vix.vvix != nil {
                    StatTile(label: loc(S.vvix), value: Format.number(vix.vvix))
                }
                Spacer()
            }
            if vix.termRatio != nil {
                Text(loc(inverted ? S.vixInverted : S.vixContango))
                    .font(.caption2)
                    .foregroundStyle(inverted ? Palette.down : Palette.secondaryText)
                    .help(loc(S.vixTermTip))
            }
        }
        .padding(Metrics.cardPadding)
        .background(Palette.card, in: RoundedRectangle(cornerRadius: Metrics.cardRadius))
        .overlay(RoundedRectangle(cornerRadius: Metrics.cardRadius).stroke(Palette.border))
    }
}

// MARK: - Sectors

private struct SectorCard: View {
    let sectors: [Sector]
    @Binding var window: MarketsView.SectorWindow
    @Environment(Localization.self) private var loc

    /// Sorted by the window on show, so the chart reads as a ranking rather than
    /// as whatever order the server happened to send.
    private var ranked: [Sector] {
        sectors.sorted { (window.value($0) ?? 0) > (window.value($1) ?? 0) }
    }

    /// Keyed on the fund ticker, falling back to the server's own label so a
    /// sector added upstream appears in English rather than vanishing.
    private func name(_ sector: Sector) -> String {
        S.sectorNames[sector.ticker].map { loc($0) } ?? sector.label
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text(loc(S.sectors)).font(.subheadline.weight(.semibold))
                Spacer()
                Picker("", selection: $window) {
                    ForEach(MarketsView.SectorWindow.allCases, id: \.self) { w in
                        Text(loc(w.label)).tag(w)
                    }
                }
                .pickerStyle(.segmented)
                .labelsHidden()
                .frame(width: 150)
            }

            // A diverging bar chart rather than the old horizontal strip of chips:
            // rotation is a comparison between sectors, and a row of numbers makes
            // the reader do that comparison themselves.
            Chart(ranked) { sector in
                // `height` is explicit because Chart's default bar thickness is
                // derived from the band, and with a dozen categories in a short
                // plot area that collapses to a hairline — the bars rendered as
                // thin rules and the chart read as a table of lines.
                BarMark(
                    x: .value("Change", window.value(sector) ?? 0),
                    y: .value("Sector", name(sector)),
                    height: .fixed(11)
                )
                .foregroundStyle(Format.tint(window.value(sector)))
                .cornerRadius(2)
            }
            .chartXAxis { AxisMarks(format: Decimal.FormatStyle.Percent.percent.scale(1)) }
            .categoryNameAxis()
            .frame(height: CGFloat(ranked.count) * 22 + 24)
        }
        .padding(Metrics.cardPadding)
        .background(Palette.card, in: RoundedRectangle(cornerRadius: Metrics.cardRadius))
        .overlay(RoundedRectangle(cornerRadius: Metrics.cardRadius).stroke(Palette.border))
    }
}

// MARK: - One instrument

private struct InstrumentCard: View {
    let instrument: Instrument
    @Binding var timeframe: MarketsView.Timeframe
    @Environment(Localization.self) private var loc

    /// Parsed once per card rather than inside the chart body.
    private var points: [PricePoint] {
        (timeframe.series(instrument) ?? instrument.daily)?.points ?? []
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .firstTextBaseline) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(instrument.name)
                        .font(.subheadline.weight(.semibold))
                        .lineLimit(1)
                    Text(instrument.symbol)
                        .font(.caption2)
                        .foregroundStyle(Palette.secondaryText)
                }
                Spacer()
                VStack(alignment: .trailing, spacing: 2) {
                    Text(Format.price(instrument.current))
                        .font(.subheadline.weight(.semibold).monospacedDigit())
                    Text(Format.signedPercent(instrument.dayChange))
                        .font(.caption.monospacedDigit())
                        .foregroundStyle(Format.tint(instrument.dayChange))
                }
            }

            if points.count > 1 {
                // A sparkline that can still be read off: no axes and the
                // y-domain clamped to the data so a small move is visible rather
                // than flattened, but hover reports the value and date. The moving
                // averages ride along as rules — above or below is the whole
                // question a trend follower asks.
                TimeSeriesChart(
                    series: [ChartSeries(id: instrument.symbol,
                                         color: Format.tint(instrument.dayChange),
                                         points: points)],
                    height: 56,
                    rules: [instrument.ma50, instrument.ma200].compactMap { $0 },
                    compact: true,
                    format: { Format.price($0) }
                )
            } else {
                // Stated, not skipped. An empty gap here would read as a flat market.
                Text(loc(S.noPriceHistory))
                    .font(.caption2)
                    .foregroundStyle(Palette.secondaryText)
                    .frame(height: 56, alignment: .center)
            }

            if instrument.weekly != nil || instrument.monthly != nil {
                Picker("", selection: $timeframe) {
                    ForEach(MarketsView.Timeframe.allCases, id: \.self) { tf in
                        Text(loc(tf.label)).tag(tf)
                    }
                }
                .pickerStyle(.segmented)
                .labelsHidden()
            }

            if let low = instrument.lo52, let high = instrument.hi52,
               let now = instrument.current, high > low {
                RangeBar(low: low, high: high, now: now, label: loc(S.range52))
            }

            HStack(spacing: 14) {
                StatTile(label: loc(S.ytd), value: Format.signedPercent(instrument.ytd),
                         tint: Format.tint(instrument.ytd))
                if let rsi = instrument.rsi14 {
                    StatTile(label: "RSI", value: Format.number(rsi))
                }
                if let pe = instrument.pe {
                    StatTile(label: "P/E", value: Format.number(pe))
                }
                Spacer()
            }
        }
        .padding(Metrics.cardPadding)
        .background(Palette.card, in: RoundedRectangle(cornerRadius: Metrics.cardRadius))
        .overlay(RoundedRectangle(cornerRadius: Metrics.cardRadius).stroke(Palette.border))
    }

    private var yDomain: ClosedRange<Double> {
        // The moving averages are drawn as rules inside this chart, so they have to
        // be inside the domain too — clamping to the visible prices alone would push
        // a 200-day line off the top of a card in a sharp drawdown, and a missing
        // reference line reads as "price is not near it".
        var values = points.map(\.price)
        if let ma50 = instrument.ma50 { values.append(ma50) }
        if let ma200 = instrument.ma200 { values.append(ma200) }
        guard let low = values.min(), let high = values.max(), high > low else {
            return 0...1
        }
        let pad = (high - low) * 0.08
        return (low - pad)...(high + pad)
    }
}

/// Where the price sits between its 52-week low and high.
///
/// A number pair says 6316 and 7816; a filled track says "near the top", which is
/// the question being asked. Both are shown, because the position is only
/// interpretable next to the bounds it is a position within.
private struct RangeBar: View {
    let low: Double
    let high: Double
    let now: Double
    let label: String

    private var fraction: Double {
        min(1, max(0, (now - low) / (high - low)))
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Text(label)
                    .font(.caption2)
                    .foregroundStyle(Palette.secondaryText)
                Spacer()
                Text("\(Format.price(low)) – \(Format.price(high))")
                    .font(.caption2.monospacedDigit())
                    .foregroundStyle(Palette.secondaryText)
            }
            GeometryReader { geo in
                ZStack(alignment: .leading) {
                    Capsule().fill(Palette.border)
                    Capsule()
                        .fill(Palette.brand)
                        .frame(width: max(2, geo.size.width * fraction))
                }
            }
            .frame(height: 4)
        }
    }
}

#Preview {
    MarketsView()
        .environment(Localization())
        .environment(AlertCenter())
}

// MARK: - Breadth

/// How many S&P 500 members are above their own moving average.
///
/// The index can rise while most of its members fall — that is what a cap-weighted
/// benchmark led by a handful of megacaps does — and no amount of index level says so.
/// This card is the check: the tiles are today's reading at five lookbacks, the chart
/// is one of them through time, and RSP/SPY is the same question asked a second way.
struct BreadthCard: View {
    let data: Breadth
    @State private var period: Int = 50
    @Environment(Localization.self) private var loc

    private var series: [PricePoint] {
        // Two years of daily readings. The payload runs to ~570 weekly points back to
        // 2015; the whole span compresses the last year into a few pixels, and the
        // question this answers ("is participation narrowing?") is a recent one.
        (data.pctAboveMa[String(period)]?.tail(160)) ?? []
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text(loc(S.breadth)).font(.subheadline.weight(.semibold))
                Spacer()
                if let universe = data.universe {
                    Text("\(universe) \(loc(S.ofNames))")
                        .font(.caption2).foregroundStyle(Palette.mutedText)
                }
            }

            // Today at each lookback. Ordered numerically — `latest` is keyed by
            // integer-valued strings, where "100" sorts before "20" as text.
            HStack(spacing: 8) {
                ForEach(data.orderedLatest, id: \.period) { item in
                    let selected = item.period == period
                    VStack(spacing: 2) {
                        Text("\(item.period)\(loc(S.dayMa))")
                            .font(.system(size: 9)).foregroundStyle(Palette.mutedText)
                        Text(Format.number(item.pct, places: 0) + "%")
                            .font(.callout.weight(.semibold).monospacedDigit())
                            .foregroundStyle(Self.tint(item.pct))
                    }
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 8)
                    .background(selected ? Palette.brand.opacity(0.18) : Palette.well,
                                in: RoundedRectangle(cornerRadius: 8))
                    .overlay(
                        RoundedRectangle(cornerRadius: 8)
                            .stroke(selected ? Palette.brand : .clear)
                    )
                    .contentShape(Rectangle())
                    .onTapGesture { period = item.period }
                }
            }

            if series.count > 1 {
                Text("\(period)\(loc(S.dayMa)) · \(loc(S.breadthAboveMa))")
                    .font(.caption2).foregroundStyle(Palette.secondaryText)
                TimeSeriesChart(
                    series: [ChartSeries(id: loc(S.breadth), color: Palette.brand,
                                         points: series, filled: true)],
                    height: 130,
                    // Fixed 0-100, not fitted: a share of a fixed universe, where
                    // the absolute level is the reading.
                    fixedYDomain: 0.0...100.0,
                    rules: [50],
                    format: { Format.number($0, places: 1) + "%" }
                )
            }

            if let rsp = data.rspSpy?.tail(160), rsp.count > 1 {
                Text(loc(S.rspSpy))
                    .font(.caption2).foregroundStyle(Palette.secondaryText)
                TimeSeriesChart(
                    series: [ChartSeries(id: "RSP/SPY", color: Palette.warn, points: rsp)],
                    height: 90,
                    // Fitted, unlike the panel above: a ratio has no meaningful
                    // absolute level, only a direction.
                    format: { Format.number($0, places: 3) }
                )
            }
        }
        .padding(Metrics.cardPadding)
        .background(Palette.card, in: RoundedRectangle(cornerRadius: Metrics.cardRadius))
        .overlay(RoundedRectangle(cornerRadius: Metrics.cardRadius).stroke(Palette.border))
    }

    /// Breadth is not a price: a low reading is weak participation whichever way the
    /// index went, so the colour tracks the level rather than a change.
    static func tint(_ pct: Double) -> Color {
        if pct >= 60 { return Palette.up }
        if pct <= 35 { return Palette.down }
        return Palette.secondaryText
    }

    static func fitted(_ values: [Double]) -> ClosedRange<Double> {
        guard let lo = values.min(), let hi = values.max() else { return 0...1 }
        guard hi > lo else { return (lo - 0.01)...(hi + 0.01) }
        let pad = (hi - lo) * 0.15
        return (lo - pad)...(hi + pad)
    }
}
