import Charts
import SwiftUI

/// The markets screen: index cards with native sparklines, the sector strip, and —
/// prominently — how old the data is.
struct MarketsView: View {
    @State private var state: LoadState = .loading

    enum LoadState {
        case loading
        case loaded(MarketsResponse)
        case failed(Error)
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
        .navigationTitle("Markets")
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
            state = .failed(error)
        }
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
                StatTile(label: "YTD", value: Format.signedPercent(instrument.ytd),
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

#Preview {
    MarketsView()
}
