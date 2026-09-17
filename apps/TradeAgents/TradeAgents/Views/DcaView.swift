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
                    LazyVStack(spacing: 12) {
                        CoverageBar(data: data)
                        // Unscorable rows sort last, never as V=0 — "could not be
                        // measured" is not "at its most expensive ever", and putting
                        // it at the top of a list headed "cheapest" would be a plain
                        // lie. Mirrors what routes.py does for the web table.
                        let rows = data.rows.sorted {
                            ($0.v == nil ? -1 : $0.v!) > ($1.v == nil ? -1 : $1.v!)
                        }
                        VScoreChart(rows: rows.filter { $0.v != nil })
                        ForEach(rows) { row in DcaRowCard(row: row) }
                        Footnote(text: loc(S.dcaNotRank))
                    }
                    .padding(.horizontal, 16).padding(.vertical, 12)
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
        .padding(14)
        .background(Palette.card, in: RoundedRectangle(cornerRadius: 14))
        .overlay(RoundedRectangle(cornerRadius: 14).stroke(Palette.border))
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
    @Environment(Localization.self) private var loc

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
                        Text(loc(S.dcaUnscored))
                            .font(.caption).foregroundStyle(Palette.secondaryText)
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

            HStack(spacing: 12) {
                // The DCF branch is shown only when it actually scored. A branch
                // that was dropped renormalises onto the relative score and is
                // absent, not neutral — printing a placeholder here would suggest
                // it had been measured and found unremarkable.
                if row.blended == true, let vDcf = row.vDcf, let w = row.wDcf {
                    StatTile(label: "DCF", value: "\(Format.number(vDcf)) · \(Int(w * 100))%")
                }
                if let m = row.multiplier {
                    StatTile(label: "×", value: Format.number(m) + "×",
                             tint: m >= 1 ? Palette.up : Palette.down)
                }
                if let years = row.years, let vintages = row.vintages {
                    StatTile(label: "", value: "\(Format.number(years))y · \(vintages)")
                }
                Spacer()
            }
        }
        .padding(14)
        .background(Palette.card, in: RoundedRectangle(cornerRadius: 14))
        .overlay(RoundedRectangle(cornerRadius: 14).stroke(Palette.border))
    }
}
