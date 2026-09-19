import Charts
import SwiftUI

/// Commodities: sixteen contracts grouped by complex, and the macro ratios.
///
/// Every figure here arrives in the single `/api/commodities` call, so the screen
/// is as detailed as the payload allows rather than as detailed as a second
/// request would cost.
struct CommoditiesView: View {
    @State private var state: LoadState = .loading
    @State private var fiveYear = false
    @Environment(Localization.self) private var loc

    enum LoadState { case loading, loaded(CommoditiesResponse), failed(Error) }

    var body: some View {
        ZStack {
            Palette.background.ignoresSafeArea()
            switch state {
            case .loading: LoadingPane()
            case .failed(let e): LoadFailure(error: e) { Task { await load() } }
            case .loaded(let data):
                ScrollView {
                    LazyVStack(spacing: Metrics.stackSpacing) {
                        rangePicker
                        ForEach(data.grouped(), id: \.0) { group, items in
                            Text(loc(Self.groupLabel(group)))
                                .font(.caption.weight(.semibold))
                                .foregroundStyle(Palette.secondaryText)
                                .frame(maxWidth: .infinity, alignment: .leading)
                                .padding(.top, 2)
                            ForEach(items) { item in
                                CommodityCard(item: item, fiveYear: fiveYear)
                            }
                        }
                        if !data.ratios.isEmpty {
                            Text(loc(S.comRatios))
                                .font(.caption.weight(.semibold))
                                .foregroundStyle(Palette.secondaryText)
                                .frame(maxWidth: .infinity, alignment: .leading)
                                .padding(.top, 2)
                            ForEach(data.ratios.keys.sorted(), id: \.self) { key in
                                if let ratio = data.ratios[key] { RatioCard(ratio: ratio) }
                            }
                        }
                    }
                    .padding(.horizontal, Metrics.screenH)
                    .padding(.vertical, Metrics.screenV)
                }
                .refreshable { await load() }
            }
        }
        .navigationTitle(loc(S.commodities))
        .task { await load() }
    }

    /// One switch for every chart on the screen rather than per card: the reason
    /// to look at five years of copper is to compare it with five years of gold,
    /// and a per-card control makes that a dozen taps.
    private var rangePicker: some View {
        Picker("", selection: $fiveYear) {
            Text(loc(S.comOneYear)).tag(false)
            Text(loc(S.comFiveYear)).tag(true)
        }
        .pickerStyle(.segmented)
        .labelsHidden()
    }

    static func groupLabel(_ group: String) -> LocalizedString {
        switch group {
        case "energy":     return S.comEnergy
        case "metals":     return S.comMetals
        case "industrial": return S.comIndustrial
        case "agri":       return S.comAgri
        case "macro":      return S.comMacro
        default:           return LocalizedString(group, group)
        }
    }

    private func load() async {
        do { state = .loaded(try await APIClient.shared.commodities()) }
        catch { state = .failed(error) }
    }
}

private struct CommodityCard: View {
    let item: Commodity
    let fiveYear: Bool
    @Environment(Localization.self) private var loc

    private var points: [PricePoint] { item.points(fiveYear: fiveYear) }

    var body: some View {
        VStack(alignment: .leading, spacing: 7) {
            header
            if points.count > 1 {
                TimeSeriesChart(
                    series: [ChartSeries(id: item.name,
                                         color: Format.tint(item.dayChgPct),
                                         points: points)],
                    height: 64,
                    // Both moving averages, because above-or-below is the whole
                    // question for a trend follower and the payload already has
                    // them. Absent ones simply do not draw.
                    rules: [item.ma50, item.ma200].compactMap { $0 },
                    compact: true,
                    format: { Format.number($0, places: 2) }
                )
            }
            returns
            if item.low52 != nil || item.rsi14 != nil { footer }
        }
        .padding(Metrics.cardPadding)
        .background(Palette.card, in: RoundedRectangle(cornerRadius: Metrics.cardRadius))
        .overlay(RoundedRectangle(cornerRadius: Metrics.cardRadius).stroke(Palette.border))
    }

    private var header: some View {
        HStack(alignment: .firstTextBaseline, spacing: 6) {
            if let emoji = item.emoji { Text(emoji).font(.caption) }
            Text(item.name).font(.subheadline.weight(.semibold))
            if let unit = item.unit {
                Text(unit).font(.caption2).foregroundStyle(Palette.mutedText)
            }
            Spacer()
            Text(Format.number(item.last, places: 2))
                .font(.subheadline.weight(.semibold).monospacedDigit())
            Text(Format.signedPercent(item.dayChgPct))
                .font(.caption.monospacedDigit())
                .foregroundStyle(Format.tint(item.dayChgPct))
        }
    }

    /// Six horizons in one row. A commodity's story is almost never the day —
    /// coffee down 1% today and 24% on the year are different facts, and the
    /// screen used to be able to show neither.
    private var returns: some View {
        HStack(spacing: 0) {
            ForEach(Self.horizons, id: \.0) { label, value in
                let v = value(item)
                VStack(spacing: 1) {
                    Text(loc(label)).font(.system(size: 9))
                        .foregroundStyle(Palette.mutedText)
                    Text(Format.signedPercent(v, places: 1))
                        .font(.system(size: 11).monospacedDigit())
                        .foregroundStyle(Format.tint(v))
                }
                .frame(maxWidth: .infinity)
            }
        }
    }

    private static let horizons: [(LocalizedString, (Commodity) -> Double?)] = [
        (S.com1w, { $0.ret1w }), (S.com1m, { $0.ret1m }), (S.com3m, { $0.ret3m }),
        (S.com6m, { $0.ret6m }), (S.comYtd, { $0.retYtd }), (S.com1y, { $0.ret1y }),
    ]

    private var footer: some View {
        HStack(spacing: 8) {
            if let low = item.low52, let high = item.high52 {
                RangeBar(low: low, high: high, value: item.last, position: item.pos52)
            }
            if let rsi = item.rsi14 {
                Text("RSI \(Format.number(rsi, places: 0))")
                    .font(.system(size: 10).monospacedDigit())
                    // 70/30 are the conventional lines, and the only reading here
                    // that is worth a colour.
                    .foregroundStyle(rsi >= 70 ? Palette.down
                                     : rsi <= 30 ? Palette.up : Palette.mutedText)
            }
        }
    }
}

/// Where the last price sits between the 52-week low and high.
private struct RangeBar: View {
    let low: Double
    let high: Double
    let value: Double?
    let position: Double?

    /// The server's own `pos52` when it sent one, rather than recomputing — the
    /// two would disagree at the edges, where it matters most.
    private var fraction: Double {
        if let position { return min(max(position / 100, 0), 1) }
        guard let value, high > low else { return 0 }
        return min(max((value - low) / (high - low), 0), 1)
    }

    var body: some View {
        HStack(spacing: 5) {
            Text(Format.number(low, places: 0))
                .font(.system(size: 9).monospacedDigit()).foregroundStyle(Palette.mutedText)
            GeometryReader { geo in
                ZStack(alignment: .leading) {
                    Capsule().fill(Palette.well)
                    Circle().fill(Palette.brand)
                        .frame(width: 5, height: 5)
                        .offset(x: max(0, geo.size.width * fraction - 2.5))
                }
            }
            .frame(height: 5)
            Text(Format.number(high, places: 0))
                .font(.system(size: 9).monospacedDigit()).foregroundStyle(Palette.mutedText)
        }
    }
}

private struct RatioCard: View {
    let ratio: CommodityRatio

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .firstTextBaseline) {
                Text(ratio.label).font(.subheadline.weight(.semibold))
                Spacer()
                Text(Format.number(ratio.current, places: 2))
                    .font(.subheadline.weight(.semibold).monospacedDigit())
            }
            // The server's own one-line explanation. Carried rather than
            // rewritten here: a ratio nobody can interpret is decoration, and the
            // backend already says what a high gold/copper means.
            if let desc = ratio.desc, !desc.isEmpty {
                Text(desc).font(.caption2).foregroundStyle(Palette.secondaryText)
            }
            if ratio.points.count > 1 {
                TimeSeriesChart(
                    series: [ChartSeries(id: ratio.label, color: Palette.brand,
                                         points: ratio.points)],
                    height: 72,
                    format: { Format.number($0, places: 2) }
                )
            }
        }
        .padding(Metrics.cardPadding)
        .background(Palette.card, in: RoundedRectangle(cornerRadius: Metrics.cardRadius))
        .overlay(RoundedRectangle(cornerRadius: Metrics.cardRadius).stroke(Palette.border))
    }
}
