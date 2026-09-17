import Charts
import SwiftUI

/// The Fed balance sheet: total assets over two decades, and the lines that make it up.
///
/// `/api/fed` returns 27 FRED series in one response. Charting all 27 on a phone
/// would be a scrolling wall nobody reads, so this shows the six that answer the
/// question the page exists for — how much liquidity the Fed is supplying, and
/// through which channel. The rest are in the payload and can be added without a
/// second request, which is the reason to take them all at once.
struct FedView: View {
    @State private var state: LoadState = .loading
    @Environment(Localization.self) private var loc

    enum LoadState { case loading, loaded(FedResponse), failed(Error) }

    /// `(FRED id, label, unit)`. Ordered as the balance sheet reads: what the Fed
    /// owns first, then what it owes.
    private static let shown: [(String, LocalizedString)] = [
        ("WALCL",     S.fedSeriesWALCL),
        ("TREAST",    S.fedSeriesTREAST),
        ("WSHOMCB",   S.fedSeriesWSHOMCB),
        ("WRESBAL",   S.fedSeriesWRESBAL),
        ("RRPONTSYD", S.fedSeriesRRPONTSYD),
        ("WTREGEN",   S.fedSeriesWTREGEN),
    ]

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
                        ForEach(Self.shown, id: \.0) { id, label in
                            if let series = data.series[id], series.points.count > 1 {
                                SeriesCard(title: loc(label), subtitle: id,
                                           points: series.points, tint: .teal)
                            }
                        }
                        Footnote(text: loc(S.fedNote))
                    }
                    .padding(.horizontal, 16).padding(.vertical, 12)
                }
                .refreshable { await load() }
            }
        }
        .navigationTitle(loc(S.fed))
        .task { await load() }
    }

    private func load() async {
        do { state = .loaded(try await APIClient.shared.fed()) }
        catch { state = .failed(error) }
    }
}

/// One dated line with its latest value. Shared by the Fed and valuation screens,
/// because a chart of a FRED series and a chart of a banked multiple are the same
/// object with different units.
struct SeriesCard: View {
    let title: String
    let subtitle: String?
    let points: [PricePoint]
    let tint: Color

    private var latest: Double? { points.last?.price }

    /// Change over the last year of data, which is the horizon the reader is
    /// actually asking about. Nil when the series is shorter than that rather
    /// than comparing against whatever the first point happens to be — a "change"
    /// measured over an arbitrary window is a different quantity, not a rough one.
    private var yearChange: Double? {
        guard let last = points.last else { return nil }
        let cutoff = last.date.addingTimeInterval(-365 * 24 * 3600)
        guard let old = points.last(where: { $0.date <= cutoff }), old.price != 0 else { return nil }
        return (last.price / old.price - 1) * 100
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .firstTextBaseline) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(title).font(.subheadline.weight(.semibold))
                    if let subtitle {
                        Text(subtitle).font(.caption2).foregroundStyle(Palette.secondaryText)
                    }
                }
                Spacer()
                VStack(alignment: .trailing, spacing: 2) {
                    Text(Format.number(latest))
                        .font(.subheadline.weight(.semibold).monospacedDigit())
                    if let yearChange {
                        Text(Format.signedPercent(yearChange))
                            .font(.caption2.monospacedDigit())
                            .foregroundStyle(Format.tint(yearChange))
                    }
                }
            }
            Chart(points) { p in
                AreaMark(x: .value("Date", p.date), y: .value("Value", p.price))
                    .foregroundStyle(tint.opacity(0.13))
                LineMark(x: .value("Date", p.date), y: .value("Value", p.price))
                    .interpolationMethod(.monotone)
                    .foregroundStyle(tint)
            }
            .chartYAxis { AxisMarks(position: .leading) }
            .localizedDateAxis(dates: points.map(\.date))
            .frame(height: 110)
        }
        .padding(14)
        .background(Palette.card, in: RoundedRectangle(cornerRadius: 14))
        .overlay(RoundedRectangle(cornerRadius: 14).stroke(Palette.border))
    }
}

/// A caveat the reader needs in order to read the screen correctly. Small, but not
/// hidden behind a tap: the 13F lag and the forward-series ratchet are both things
/// that make the numbers above mean something different than they appear to.
struct Footnote: View {
    let text: String
    var body: some View {
        Text(text)
            .font(.caption2)
            .foregroundStyle(Palette.secondaryText)
            .fixedSize(horizontal: false, vertical: true)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.horizontal, 4)
            .padding(.top, 2)
    }
}
