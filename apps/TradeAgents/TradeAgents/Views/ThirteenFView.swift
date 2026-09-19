import Charts
import SwiftUI

/// One institution's latest 13F: what they hold, how concentrated it is, and what
/// moved since the prior filing.
struct ThirteenFView: View {
    @State private var state: LoadState = .loading
    @State private var fund: Fund = .berkshire
    @Environment(Localization.self) private var loc

    enum LoadState { case loading, loaded(ThirteenF), failed(Error) }

    /// The funds offered, as `(slug, display name)`. A short curated list rather
    /// than all twenty-two the backend tracks: a phone picker is a scroll, and
    /// these are the filings anyone opens the screen for. The slug is derived from
    /// the fund name the backend keys on, so adding one is a single line.
    enum Fund: String, CaseIterable, Identifiable {
        case berkshire   = "berkshire-hathaway"
        case bridgewater = "bridgewater-associates"
        case citadel     = "citadel-advisors"
        case point72     = "point72-asset-management"
        case deshaw      = "de-shaw"
        case blackrock   = "blackrock"

        var id: String { rawValue }

        var display: String {
            switch self {
            case .berkshire:   return "Berkshire Hathaway"
            case .bridgewater: return "Bridgewater"
            case .citadel:     return "Citadel"
            case .point72:     return "Point72"
            case .deshaw:      return "DE Shaw"
            case .blackrock:   return "BlackRock"
            }
        }
    }

    var body: some View {
        ZStack {
            Palette.background.ignoresSafeArea()
            VStack(spacing: 0) {
                Picker("", selection: $fund) {
                    ForEach(Fund.allCases) { f in Text(f.display).tag(f) }
                }
                .pickerStyle(.menu)
                .labelsHidden()
                .padding(.horizontal, Metrics.screenH).padding(.top, 8)

                switch state {
                case .loading: LoadingPane()
                case .failed(let e): LoadFailure(error: e) { Task { await load() } }
                case .loaded(let data): loaded(data)
                }
            }
        }
        .navigationTitle(loc(S.holdings13f))
        .task(id: fund) { await load() }
    }

    @ViewBuilder
    private func loaded(_ data: ThirteenF) -> some View {
        ScrollView {
            LazyVStack(spacing: Metrics.stackSpacing) {
                FilingHeader(data: data)
                if !data.holdings.isEmpty {
                    ConcentrationCard(holdings: data.holdings)
                    ForEach(data.holdings.prefix(25)) { h in HoldingRow(holding: h) }
                }
                Footnote(text: loc(S.f13Lag))
            }
            .padding(.horizontal, Metrics.screenH).padding(.vertical, Metrics.screenV)
        }
        .refreshable { await load() }
    }

    private func load() async {
        state = .loading
        do { state = .loaded(try await APIClient.shared.thirteenF(slug: fund.rawValue)) }
        catch { state = .failed(error) }
    }
}

private struct FilingHeader: View {
    let data: ThirteenF
    @Environment(Localization.self) private var loc

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 14) {
                StatTile(label: loc(S.f13Period), value: data.periodOfReport ?? "—")
                StatTile(label: loc(S.f13Filed), value: data.filingDate ?? "—")
                StatTile(label: loc(S.f13Positions), value: "\(data.totalHoldings ?? 0)")
                Spacer()
            }
            if let value = data.totalValueMillions {
                // The endpoint calls this "millions" but reports Berkshire at
                // 299,253,556 — which is thousands. Rendered compactly rather than
                // relabelled: guessing the unit and printing "$299T" would be a
                // fabricated figure, while a plain grouped number is just the
                // filing's own value.
                Text("\(loc(S.f13Value)): \(Format.grouped(value))")
                    .font(.caption).foregroundStyle(Palette.secondaryText)
            }
        }
        .padding(Metrics.cardPadding)
        .background(Palette.card, in: RoundedRectangle(cornerRadius: Metrics.cardRadius))
        .overlay(RoundedRectangle(cornerRadius: Metrics.cardRadius).stroke(Palette.border))
    }
}

/// How much of the book sits in the largest positions.
///
/// The concentration is the story in a 13F — Berkshire's top holding is a fifth of
/// the portfolio — and a list of rows buries it. Top ten only: the tail is dozens
/// of sub-1% lines that no reading turns on.
private struct ConcentrationCard: View {
    let holdings: [Holding]
    @Environment(Localization.self) private var loc

    private var top: [Holding] {
        Array(holdings.sorted { ($0.pctPortfolio ?? 0) > ($1.pctPortfolio ?? 0) }.prefix(10))
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(loc(S.f13Top)).font(.subheadline.weight(.semibold))
            Chart(top) { h in
                BarMark(
                    x: .value("Weight", h.pctPortfolio ?? 0),
                    y: .value("Name", h.ticker ?? h.name),
                    height: .fixed(12)
                )
                .foregroundStyle(Palette.brand)
                .cornerRadius(2)
            }
            .chartXAxis { AxisMarks(format: Decimal.FormatStyle.Percent.percent.scale(1)) }
            .categoryNameAxis()
            .frame(height: CGFloat(top.count) * 22 + 24)
        }
        .padding(Metrics.cardPadding)
        .background(Palette.card, in: RoundedRectangle(cornerRadius: Metrics.cardRadius))
        .overlay(RoundedRectangle(cornerRadius: Metrics.cardRadius).stroke(Palette.border))
    }
}

private struct HoldingRow: View {
    let holding: Holding
    @Environment(Localization.self) private var loc

    /// The filing's own verdict, translated.
    ///
    /// The five cases are exactly what `sec13f.py` emits — `new`, `increased`,
    /// `reduced`, `unchanged`, `unknown` — read off the backend rather than guessed.
    /// The first version here invented `added` and `trimmed`, which the server never
    /// sends, and so had no case for `increased`: it fell through to the passthrough
    /// arm and printed the raw English word on a Chinese page, directly under a
    /// column that was otherwise fully translated.
    ///
    /// `unknown` is deliberately silent rather than passed through. It means the
    /// prior quarter could not be compared, which is the absence of a verdict, and
    /// labelling a row with it would read as a finding about the position.
    private var changeLabel: String? {
        switch holding.change {
        case "new":       return loc(S.f13New)
        case "increased": return loc(S.f13Added)
        case "reduced":   return loc(S.f13Trimmed)
        case "unchanged", "unknown", nil: return nil
        // A value added upstream after this app was built. Showing it beats hiding
        // the change entirely, and it is visibly untranslated, which is the signal.
        case let other?:  return other
        }
    }

    private var changeTint: Color {
        switch holding.change {
        case "new", "increased": return Palette.up
        case "reduced": return Palette.down
        default: return Palette.secondaryText
        }
    }

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 10) {
            VStack(alignment: .leading, spacing: 2) {
                Text(holding.ticker ?? "—")
                    .font(.subheadline.weight(.semibold).monospaced())
                Text(holding.name)
                    .font(.caption2).foregroundStyle(Palette.secondaryText).lineLimit(1)
            }
            Spacer()
            if let label = changeLabel {
                Text(label).font(.caption2).foregroundStyle(changeTint)
            }
            VStack(alignment: .trailing, spacing: 2) {
                Text(Format.number(holding.pctPortfolio) + "%")
                    .font(.subheadline.weight(.semibold).monospacedDigit())
                if let pct = holding.changePct, pct != 0 {
                    Text(Format.signedPercent(pct))
                        .font(.caption2.monospacedDigit())
                        .foregroundStyle(Format.tint(pct))
                }
            }
        }
        .padding(.horizontal, Metrics.cardPadding).padding(.vertical, 8)
        .background(Palette.card, in: RoundedRectangle(cornerRadius: Metrics.cardRadius))
        .overlay(RoundedRectangle(cornerRadius: Metrics.cardRadius).stroke(Palette.border))
    }
}
