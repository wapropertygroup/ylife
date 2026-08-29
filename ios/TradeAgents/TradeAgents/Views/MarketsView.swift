import Charts
import SwiftUI

/// The markets screen: index cards with native sparklines, the sector strip, and —
/// prominently — how old the data is.
struct MarketsView: View {
    @State private var state: LoadState = .loading

    enum LoadState {
        case loading
        case loaded(MarketsResponse)
        case failed(String)
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
        NavigationStack {
            ZStack {
                Palette.background.ignoresSafeArea()
                content
            }
            .navigationTitle("trade-agents")
            .toolbarTitleDisplayMode(.inlineLarge)
        }
        .task { await load() }
    }

    @ViewBuilder
    private var content: some View {
        switch state {
        case .loading:
            ProgressView().tint(Palette.brand)

        case .failed(let message):
            VStack(spacing: 14) {
                Image(systemName: "exclamationmark.triangle")
                    .font(.largeTitle)
                    .foregroundStyle(Palette.down)
                Text(message)
                    .font(.footnote)
                    .foregroundStyle(Palette.secondaryText)
                    .multilineTextAlignment(.center)
                Button("Retry") { Task { await load() } }
                    .buttonStyle(.borderedProminent)
                    .tint(Palette.brand)
            }
            .padding(32)

        case .loaded(let data):
            ScrollView {
                LazyVStack(spacing: 12) {
                    if let meta = data.meta {
                        FreshnessBanner(meta: meta)
                    }
                    if !data.sectors.isEmpty {
                        SectorStrip(sectors: data.sectors)
                    }
                    ForEach(ordered(data.indices)) { instrument in
                        InstrumentCard(instrument: instrument)
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
        } catch {
            state = .failed(error.localizedDescription)
        }
    }
}

/// The server's freshness verdict, shown rather than hidden.
///
/// `status` distinguishes a live quote from a session close from genuinely stale
/// data — a distinction the backend works to compute and that every web page
/// surfaces. Reproducing it here is the point: the risk in a phone app is a number
/// that looks current because it is on screen.
private struct FreshnessBanner: View {
    let meta: Meta

    private var isStale: Bool { meta.stale == true || meta.status == "stale" }

    private var label: String {
        switch meta.status {
        case "realtime":      return "Live"
        case "session_close": return "At session close"
        case "stale":         return "Stale"
        default:              return meta.status?.capitalized ?? "Unknown"
        }
    }

    var body: some View {
        HStack(spacing: 8) {
            Circle()
                .fill(isStale ? Palette.down : (meta.marketOpen == true ? Palette.up : Palette.secondaryText))
                .frame(width: 7, height: 7)
            Text(label)
                .font(.caption.weight(.medium))
                .foregroundStyle(isStale ? Palette.down : Palette.secondaryText)
            if let age = meta.ageLabel {
                Text("· \(age)")
                    .font(.caption)
                    .foregroundStyle(Palette.secondaryText)
            }
            Spacer()
        }
        .padding(.horizontal, 4)
    }
}

private struct SectorStrip: View {
    let sectors: [Sector]

    var body: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 8) {
                ForEach(sectors) { sector in
                    VStack(alignment: .leading, spacing: 3) {
                        Text(sector.label)
                            .font(.caption2)
                            .foregroundStyle(Palette.secondaryText)
                        Text(Format.signedPercent(sector.dayChange))
                            .font(.caption.weight(.semibold).monospacedDigit())
                            .foregroundStyle(Format.tint(sector.dayChange))
                    }
                    .padding(.horizontal, 10)
                    .padding(.vertical, 8)
                    .background(Palette.card, in: RoundedRectangle(cornerRadius: 8))
                    .overlay(RoundedRectangle(cornerRadius: 8).stroke(Palette.border))
                }
            }
            .padding(.horizontal, 4)
        }
    }
}

private struct InstrumentCard: View {
    let instrument: Instrument

    /// Parsed once per card rather than inside the chart body.
    private var points: [PricePoint] { instrument.daily?.points ?? [] }

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
                Chart(points) { point in
                    LineMark(
                        x: .value("Date", point.date),
                        y: .value("Price", point.price)
                    )
                    .interpolationMethod(.monotone)
                    .foregroundStyle(Format.tint(instrument.dayChange))
                }
                // A sparkline: no axes, and the y-domain clamped to the data so a
                // small move is still visible instead of flattened against a
                // zero-based axis.
                .chartXAxis(.hidden)
                .chartYAxis(.hidden)
                .chartYScale(domain: yDomain)
                .frame(height: 44)
            } else {
                // Stated, not skipped. An empty gap here would read as a flat market.
                Text("No price history")
                    .font(.caption2)
                    .foregroundStyle(Palette.secondaryText)
                    .frame(height: 44, alignment: .center)
            }

            HStack(spacing: 14) {
                Stat(label: "YTD", value: Format.signedPercent(instrument.ytd),
                     tint: Format.tint(instrument.ytd))
                if let rsi = instrument.rsi14 {
                    Stat(label: "RSI", value: Format.number(rsi), tint: .primary)
                }
                if let pe = instrument.pe {
                    Stat(label: "P/E", value: Format.number(pe), tint: .primary)
                }
                Spacer()
            }
        }
        .padding(14)
        .background(Palette.card, in: RoundedRectangle(cornerRadius: 14))
        .overlay(RoundedRectangle(cornerRadius: 14).stroke(Palette.border))
    }

    private var yDomain: ClosedRange<Double> {
        let values = points.map(\.price)
        guard let low = values.min(), let high = values.max(), high > low else {
            return 0...1
        }
        let pad = (high - low) * 0.08
        return (low - pad)...(high + pad)
    }
}

private struct Stat: View {
    let label: String
    let value: String
    let tint: Color

    var body: some View {
        VStack(alignment: .leading, spacing: 1) {
            Text(label)
                .font(.system(size: 9))
                .foregroundStyle(Palette.secondaryText)
            Text(value)
                .font(.caption2.monospacedDigit())
                .foregroundStyle(tint)
        }
    }
}

/// Formatting helpers. An absent value prints as an em dash rather than 0 — the
/// payload is full of legitimate nulls (a commodity has no P/E) and rendering those
/// as zero would be a false statement about the instrument.
enum Format {
    static func price(_ value: Double?) -> String {
        guard let value else { return "—" }
        return value.formatted(.number.precision(.fractionLength(2)))
    }

    static func number(_ value: Double?) -> String {
        guard let value else { return "—" }
        return value.formatted(.number.precision(.fractionLength(1)))
    }

    static func signedPercent(_ value: Double?) -> String {
        guard let value else { return "—" }
        let sign = value >= 0 ? "+" : ""
        return sign + value.formatted(.number.precision(.fractionLength(2))) + "%"
    }

    static func tint(_ value: Double?) -> Color {
        guard let value else { return Palette.secondaryText }
        return value >= 0 ? Palette.up : Palette.down
    }
}

#Preview {
    MarketsView()
}
