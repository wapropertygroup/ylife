import Charts
import SwiftUI

/// The DCA engine's ranked table: how cheap each name is against its own history,
/// and what that does to a recurring contribution.
struct DcaView: View {
    @State private var state: LoadState = .loading
    @Environment(Localization.self) private var loc

    enum LoadState { case loading, loaded(DcaList), failed(Error) }

    var body: some View {
        ZStack {
            Palette.background.ignoresSafeArea()
            switch state {
            case .loading: LoadingPane()
            case .failed(let e): LoadFailure(error: e) { Task { await load() } }
            case .loaded(let data):
                ScrollView {
                    LazyVStack(spacing: Metrics.stackSpacing) {
                        CoverageBar(data: data)
                        // Unscorable rows sort last, never as V=0 — "could not be
                        // measured" is not "at its most expensive ever", and putting
                        // it at the top of a list headed "cheapest" would be a plain
                        // lie. Mirrors what routes.py does for the web table.
                        let rows = data.rows.sorted {
                            ($0.v == nil ? -1 : $0.v!) > ($1.v == nil ? -1 : $1.v!)
                        }
                        VScoreChart(rows: rows.filter { $0.v != nil })
                        ForEach(rows) { row in
                            DcaRowCard(row: row,
                                       minObservations: data.minObservations)
                        }
                        Footnote(text: loc(S.dcaNotRank))
                    }
                    .padding(.horizontal, Metrics.screenH).padding(.vertical, Metrics.screenV)
                }
                .refreshable { await load() }
            }
        }
        .navigationTitle(loc(S.dca))
        .task { await load() }
    }

    private func load() async {
        do { state = .loaded(try await APIClient.shared.dca()) }
        catch { state = .failed(error) }
    }
}

private struct CoverageBar: View {
    let data: DcaList
    @Environment(Localization.self) private var loc

    var body: some View {
        HStack(spacing: 10) {
            Text("\(data.scored ?? 0) / \(data.universe ?? 0) \(loc(S.dcaScored))")
                .font(.caption).foregroundStyle(Palette.secondaryText)
            Spacer()
            if let base = data.baseDca {
                Text("\(Format.price(base)) \(loc(S.dcaPerName))")
                    .font(.caption.monospacedDigit()).foregroundStyle(Palette.secondaryText)
            }
        }
        .padding(.horizontal, 4)
    }
}

/// Every scored name as one bar, cheapest first.
///
/// The per-row cards below carry the detail; this is the shape of the whole
/// universe in one glance, which is the thing a list of sixty cards cannot show.
private struct VScoreChart: View {
    let rows: [DcaRow]
    @Environment(Localization.self) private var loc

    /// Capped, because sixty full-height bars is a screen of its own and the ends
    /// are where the information is. Both ends are kept — the dearest names are as
    /// actionable as the cheapest, in the opposite direction.
    private var shown: [DcaRow] {
        guard rows.count > 20 else { return rows }
        return Array(rows.prefix(10)) + Array(rows.suffix(10))
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(loc(S.dcaTitle)).font(.subheadline.weight(.semibold))
            Chart(shown) { row in
                // Explicit thickness: the default is derived from the band and
                // collapses to a hairline once there are twenty of them.
                BarMark(
                    x: .value("V", row.v ?? 0),
                    y: .value("Ticker", row.ticker),
                    height: .fixed(10)
                )
                .foregroundStyle(Self.tint(row.v))
                .cornerRadius(2)
            }
            .chartXScale(domain: 0...100)
            .categoryNameAxis()
            .frame(height: CGFloat(shown.count) * 20 + 24)
        }
        .padding(Metrics.cardPadding)
        .background(Palette.card, in: RoundedRectangle(cornerRadius: Metrics.cardRadius))
        .overlay(RoundedRectangle(cornerRadius: Metrics.cardRadius).stroke(Palette.border))
    }

    /// V is a *cheapness* score, so the colour runs the opposite way to a price
    /// change: high is green because it is cheap, not because it went up.
    static func tint(_ v: Double?) -> Color {
        guard let v else { return Palette.secondaryText }
        if v >= 60 { return Palette.up }
        if v <= 20 { return Palette.down }
        return Palette.secondaryText
    }
}

private struct DcaRowCard: View {
    let row: DcaRow
    /// From the list, not the row: it is a constant of the engine, sent once.
    let minObservations: Int?
    @Environment(Localization.self) private var loc

    /// A multiplier term. 1.00x is deliberately muted rather than coloured — it
    /// did not move, which is neither good nor bad, and tinting it green or red
    /// would invent a signal out of the neutral case.
    static func termTint(_ m: Double) -> Color {
        if abs(m - 1.0) < 0.005 { return Palette.mutedText }
        return m > 1 ? Palette.up : Palette.down
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .firstTextBaseline) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(row.ticker).font(.subheadline.weight(.semibold).monospaced())
                    if let name = row.name {
                        Text(name).font(.caption2).foregroundStyle(Palette.secondaryText)
                            .lineLimit(1)
                    }
                }
                Spacer()
                VStack(alignment: .trailing, spacing: 2) {
                    if let v = row.v {
                        Text(Format.number(v))
                            .font(.title3.weight(.semibold).monospacedDigit())
                            .foregroundStyle(VScoreChart.tint(v))
                    } else {
                        // Named, not just "not scorable". That one sentence covered
                        // a bank with no tangible book and an ETF that files no
                        // statements at all — the first is a gap in one factor and
                        // the second can never be scored, and only the reason says
                        // which.
                        Text(loc(S.dcaUnscored))
                            .font(.caption).foregroundStyle(Palette.secondaryText)
                        if let why = row.noScoreReason {
                            Text(why.replacingOccurrences(of: "_", with: " "))
                                .font(.system(size: 9)).foregroundStyle(Palette.mutedText)
                                .multilineTextAlignment(.trailing)
                        }
                    }
                    if let amount = row.amount {
                        Text(Format.price(amount))
                            .font(.caption.monospacedDigit())
                    }
                }
            }

            if let v = row.v {
                // The same filled track the web page uses: a bare score is an
                // abstraction, a track is read at a glance.
                GeometryReader { geo in
                    ZStack(alignment: .leading) {
                        Capsule().fill(Palette.well)
                        Capsule().fill(VScoreChart.tint(v))
                            .frame(width: max(2, geo.size.width * v / 100))
                    }
                }
                .frame(height: 4)
            }

            // The three terms the product is made of, not just the product.
            //
            // `multiplier` is M_valuation x M_earnings x M_portfolio, so a card
            // showing only the product tells a reader their contribution moved
            // without saying which term moved it — and the three call for
            // completely different reactions: cheap-on-its-own-history, estimates
            // being revised, and already owning too much of it.
            //
            // 1.00x is dimmed rather than hidden. "Did not move" and "could not be
            // measured" are different statements, and an absent tile says the
            // second when the truth is usually the first.
            HStack(spacing: 12) {
                if let m = row.mValuation {
                    StatTile(label: loc(S.dcaValuation), value: Format.number(m) + "×",
                             tint: Self.termTint(m))
                }
                if let m = row.mEarnings {
                    StatTile(label: loc(S.dcaEarnings), value: Format.number(m) + "×",
                             tint: Self.termTint(m))
                }
                if let m = row.mPortfolio {
                    StatTile(label: loc(S.dcaPortfolio), value: Format.number(m) + "×",
                             tint: Self.termTint(m))
                }
                Spacer()
                if let m = row.multiplier {
                    StatTile(label: "=", value: Format.number(m) + "×",
                             tint: m >= 1 ? Palette.up : Palette.down,
                             alignment: .trailing)
                }
            }

            // The ceiling is on the *product*, which is why it can bite when no
            // single term looks extreme — a cheap name whose estimates are also
            // rising. Stated rather than left as a silently rounded number.
            if row.capped == true {
                Text("⚠︎ " + loc(S.dcaCapped))
                    .font(.system(size: 9).weight(.medium))
                    .foregroundStyle(Palette.warn)
                    .help(loc(S.dcaCappedTip))
            }

            HStack(spacing: 12) {
                // The DCF branch is shown only when it actually scored. A branch
                // that was dropped renormalises onto the relative score and is
                // absent, not neutral — printing a placeholder here would suggest
                // it had been measured and found unremarkable.
                if row.blended == true, let vDcf = row.vDcf, let w = row.wDcf {
                    StatTile(label: "DCF", value: "\(Format.number(vDcf)) · \(Int(w * 100))%")
                }
                if let p = row.peerPct {
                    StatTile(label: loc(S.dcaPeer), value: Format.number(p))
                }
                if let d = row.epsDrift {
                    StatTile(label: loc(S.dcaDrift),
                             value: Format.percent(d * 100),
                             tint: d >= 0 ? Palette.up : Palette.down)
                }
                if let p = row.positionPct, p > 0 {
                    StatTile(label: loc(S.dcaPosition), value: Format.percent(p))
                }
                if let years = row.years, let vintages = row.vintages {
                    StatTile(label: "", value: "\(Format.number(years))y · \(vintages)")
                }
                Spacer()
            }

            // Which factors the adaptive rule dropped, and how short each one's
            // history actually is. The names alone make a factor three weeks from
            // qualifying read identically to one forty weeks short.
            if let dropped = row.dropped, !dropped.isEmpty {
                Text(loc(S.dcaDropped) + ": " + dropped.map { name in
                    if let n = row.droppedObs?[name], let floor = minObservations {
                        return "\(name) \(n)/\(floor)"
                    }
                    return name
                }.joined(separator: " · "))
                    .font(.system(size: 9))
                    .foregroundStyle(Palette.mutedText)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(Metrics.cardPadding)
        .background(Palette.card, in: RoundedRectangle(cornerRadius: Metrics.cardRadius))
        .overlay(RoundedRectangle(cornerRadius: Metrics.cardRadius).stroke(Palette.border))
    }
}
