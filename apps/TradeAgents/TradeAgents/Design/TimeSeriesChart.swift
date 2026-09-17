import Charts
import SwiftUI

/// One named line on a `TimeSeriesChart`.
struct ChartSeries: Identifiable {
    let id: String
    let color: Color
    let points: [PricePoint]
    /// Fills under the line. Only meaningful for a single-series chart — two
    /// overlapping translucent fills read as a third colour that means nothing.
    var filled: Bool = false

    /// Points inside `range`, plus one either side so the line enters and leaves
    /// the plot edges instead of stopping short of them, thinned to `limit`.
    ///
    /// The thinning is not cosmetic. WALCL is ~6,000 daily observations and this
    /// view re-renders on every mouse move while a pointer is over it, so at full
    /// zoom the hover readout would be asking Charts to lay out six thousand marks
    /// sixty times a second. Beyond roughly one point per horizontal pixel nothing
    /// is gained: the extra marks land on top of each other.
    ///
    /// First and last are always kept, so the line still starts and ends where the
    /// data does rather than at whatever the stride happened to land on — a series
    /// that visibly stops short of its own latest value reads as missing data.
    func slice(_ range: ClosedRange<Date>?, limit: Int = 1400) -> [PricePoint] {
        var window = points
        if let range {
            guard let first = points.firstIndex(where: { $0.date >= range.lowerBound }) else {
                return Array(points.suffix(1))
            }
            let last = points.lastIndex(where: { $0.date <= range.upperBound }) ?? points.count - 1
            let lo = max(0, first - 1)
            let hi = min(points.count - 1, max(last + 1, lo))
            guard lo <= hi else { return [] }
            window = Array(points[lo...hi])
        }
        guard window.count > limit else { return window }
        let stride = window.count / limit + 1
        var thinned = window.enumerated().filter { $0.offset % stride == 0 }.map(\.element)
        if let last = window.last, thinned.last?.date != last.date { thinned.append(last) }
        return thinned
    }
}

/// A date-indexed line chart with a value readout and zoom.
///
/// Exists because the charts in this app are built from whatever history the
/// endpoint ships, and several of them ship a great deal: the Fed balance sheet
/// runs from 2003 and the 10Y−2Y spread from 2016. Rendered whole they answer
/// "what happened over two decades" and cannot answer "what is it now, and what
/// was it in March" — there is no way to read a value off them and no way to look
/// closer. Fifteen charts now need that, so it lives here once.
///
/// Three decisions worth keeping:
///
/// - **The y-domain follows the visible window.** Zooming the x-axis while the
///   y-axis still spans the full history is the failure mode that makes zoom
///   useless: a month of WALCL inside a 0–9,000 axis is a flat line. Refitting is
///   what makes the zoom worth having. A caller can still pin the domain
///   (`fixedYDomain`) where the absolute level *is* the reading — breadth is a
///   percentage of a fixed universe, and refitting it would redraw a quiet range
///   as a dramatic one.
/// - **The readout occupies its space at rest.** It shows the latest value when
///   nothing is hovered and the hovered value otherwise, so the layout never
///   shifts and the chart is still labelled when the pointer is elsewhere.
/// - **Hover and drag do different jobs per platform.** macOS has a pointer, so
///   hover reads values and drag pans. iOS has no hover, so drag reads values and
///   the range buttons do the navigating. Wiring drag to both would make every
///   attempt to read a value also scroll the chart out from under the reader.
struct TimeSeriesChart: View {
    let series: [ChartSeries]
    var height: CGFloat = 150
    var fixedYDomain: ClosedRange<Double>?
    /// Horizontal reference lines — 50% on a breadth chart, 0 on a spread.
    var rules: [Double] = []
    /// Shaded vertical spans behind the line — NBER recessions on the spread chart.
    var bands: [ClosedRange<Date>] = []
    var format: (Double) -> String = { Format.number($0, places: 2) }
    var showsLegend: Bool = false
    /// Drops both axes. For the instrument cards, which are sparklines: fourteen
    /// of them on one screen, each already carrying its price, change and 52-week
    /// range, so a full axis pair on every one is noise. The hover readout still
    /// works, which is the point — a sparkline you can interrogate.
    var compact: Bool = false

    @State private var visible: ClosedRange<Date>?
    @State private var cursor: Date?
    @State private var gestureStart: ClosedRange<Date>?
    @Environment(Localization.self) private var loc

    init(series: [ChartSeries],
         height: CGFloat = 150,
         fixedYDomain: ClosedRange<Double>? = nil,
         rules: [Double] = [],
         bands: [ClosedRange<Date>] = [],
         showsLegend: Bool = false,
         compact: Bool = false,
         format: @escaping (Double) -> String = { Format.number($0, places: 2) }) {
        self.series = series
        self.height = height
        self.fixedYDomain = fixedYDomain
        self.rules = rules
        self.bands = bands
        self.showsLegend = showsLegend
        self.compact = compact
        self.format = format
    }

    // MARK: - Domains

    private var fullRange: ClosedRange<Date>? {
        let dates = series.flatMap { $0.points.map(\.date) }
        guard let lo = dates.min(), let hi = dates.max(), lo < hi else { return nil }
        return lo...hi
    }

    private var window: ClosedRange<Date>? { visible ?? fullRange }

    private var sliced: [ChartSeries] {
        series.map {
            ChartSeries(id: $0.id, color: $0.color, points: $0.slice(window), filled: $0.filled)
        }
    }

    /// Fitted to what is on screen, padded, and widened to include any reference
    /// line — a 50% rule the domain excludes would be drawn outside the plot.
    private var yDomain: ClosedRange<Double> {
        if let fixedYDomain { return fixedYDomain }
        var values = sliced.flatMap { $0.points.map(\.price) }
        values.append(contentsOf: rules)
        guard let lo = values.min(), let hi = values.max() else { return 0...1 }
        guard hi > lo else { return (lo - max(0.5, abs(lo) * 0.05))...(hi + max(0.5, abs(hi) * 0.05)) }
        let pad = (hi - lo) * 0.12
        return (lo - pad)...(hi + pad)
    }

    private var spanDays: Double {
        guard let w = window else { return 0 }
        return w.upperBound.timeIntervalSince(w.lowerBound) / 86_400
    }

    // MARK: - Readout

    /// The value of each series at `cursor`, or the last value when nothing is
    /// hovered. Nearest point rather than interpolated: these are observations,
    /// and inventing a value between two of them would be a fabricated reading
    /// on a chart whose whole purpose is to report measured ones.
    private var readout: (date: Date, values: [(String, Color, Double)])? {
        var out: [(String, Color, Double)] = []
        var stamp: Date?
        for s in sliced where !s.points.isEmpty {
            let hit: PricePoint?
            if let cursor {
                hit = s.points.min { abs($0.date.timeIntervalSince(cursor))
                                   < abs($1.date.timeIntervalSince(cursor)) }
            } else {
                hit = s.points.last
            }
            guard let hit else { continue }
            out.append((s.id, s.color, hit.price))
            if stamp == nil || (cursor == nil && hit.date > stamp!) { stamp = hit.date }
        }
        guard let stamp, !out.isEmpty else { return nil }
        return (stamp, out)
    }

    // MARK: - Body

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            header
            chart
            if !compact, visible != nil || spanDays > 400 { rangeBar }
        }
    }

    private var header: some View {
        HStack(spacing: 10) {
            if let readout {
                Text(Format.axisDate(readout.date, spanDays: 0))
                    .font(.caption2.monospacedDigit())
                    .foregroundStyle(cursor == nil ? Palette.mutedText : Palette.secondaryText)
                ForEach(readout.values, id: \.0) { name, colour, value in
                    HStack(spacing: 4) {
                        if readout.values.count > 1 || showsLegend {
                            Circle().fill(colour).frame(width: 6, height: 6)
                            Text(name).font(.caption2).foregroundStyle(Palette.secondaryText)
                        }
                        Text(format(value)).font(.caption.monospacedDigit().weight(.medium))
                    }
                }
            }
            Spacer()
        }
        // Reserved whether or not anything is hovered, so the chart does not jump
        // down the screen the moment a pointer enters it.
        .frame(height: 16)
    }

    private var chart: some View {
        Chart {
            // Behind everything: a band is context, and a line drawn under one
            // would be dimmed by it exactly where it matters most.
            ForEach(Array(bands.enumerated()), id: \.offset) { _, span in
                RectangleMark(
                    xStart: .value("From", span.lowerBound),
                    xEnd: .value("To", span.upperBound)
                )
                .foregroundStyle(Palette.secondaryText.opacity(0.16))
            }
            ForEach(rules, id: \.self) { value in
                RuleMark(y: .value("Rule", value))
                    .foregroundStyle(Palette.border)
                    .lineStyle(StrokeStyle(lineWidth: 1, dash: [3, 3]))
            }
            ForEach(sliced) { s in
                ForEach(s.points) { p in
                    if s.filled {
                        AreaMark(x: .value("Date", p.date), y: .value("Value", p.price))
                            .foregroundStyle(s.color.opacity(0.13))
                    }
                    LineMark(x: .value("Date", p.date), y: .value("Value", p.price))
                        .foregroundStyle(s.color)
                        .interpolationMethod(.monotone)
                }
                .foregroundStyle(by: .value("Series", s.id))
            }
            if let cursor, let readout {
                RuleMark(x: .value("Cursor", cursor))
                    .foregroundStyle(Palette.secondaryText.opacity(0.5))
                    .lineStyle(StrokeStyle(lineWidth: 1))
                ForEach(readout.values, id: \.0) { name, colour, value in
                    PointMark(x: .value("Cursor", cursor), y: .value("Value", value))
                        .foregroundStyle(colour)
                        .symbolSize(36)
                        .annotation(position: .top, spacing: 2) { EmptyView() }
                        .accessibilityLabel(name)
                }
            }
        }
        .chartForegroundStyleScale(domain: sliced.map(\.id), range: sliced.map(\.color))
        .chartLegend(showsLegend ? .visible : .hidden)
        .chartYScale(domain: yDomain)
        .chartXScale(domain: window.map { $0.lowerBound...$0.upperBound } ?? Date()...Date())
        .modifier(AxisStyle(compact: compact, window: window))
        .chartOverlay { proxy in
            GeometryReader { geo in
                Rectangle().fill(.clear).contentShape(Rectangle())
                    .gesture(magnify(proxy: proxy, geo: geo))
                    #if os(macOS)
                    .onContinuousHover { phase in
                        switch phase {
                        case .active(let location): cursor = date(at: location, proxy, geo)
                        case .ended: cursor = nil
                        }
                    }
                    .gesture(pan(proxy: proxy, geo: geo))
                    #else
                    .gesture(
                        DragGesture(minimumDistance: 0)
                            .onChanged { cursor = date(at: $0.location, proxy, geo) }
                            .onEnded { _ in cursor = nil }
                    )
                    #endif
            }
        }
        .frame(height: height)
    }

    private var rangeBar: some View {
        HStack(spacing: 6) {
            ForEach(ChartRange.applicable(spanDays: fullSpanDays), id: \.self) { r in
                Button {
                    apply(r)
                } label: {
                    Text(loc(r.label))
                        .font(.system(size: 10).weight(.medium))
                        .padding(.horizontal, 7).padding(.vertical, 3)
                        .background(isActive(r) ? Palette.brand.opacity(0.22) : Palette.well,
                                    in: Capsule())
                        .foregroundStyle(isActive(r) ? Palette.brand : Palette.secondaryText)
                }
                .buttonStyle(.plain)
            }
            Spacer()
            if visible != nil {
                Button { visible = nil } label: {
                    Text(loc(S.chartReset))
                        .font(.system(size: 10))
                        .foregroundStyle(Palette.secondaryText)
                }
                .buttonStyle(.plain)
            }
        }
    }

    private var fullSpanDays: Double {
        guard let f = fullRange else { return 0 }
        return f.upperBound.timeIntervalSince(f.lowerBound) / 86_400
    }

    private func isActive(_ r: ChartRange) -> Bool {
        guard let w = window else { return false }
        let days = w.upperBound.timeIntervalSince(w.lowerBound) / 86_400
        guard let want = r.days else { return visible == nil }
        return abs(days - want) < max(2, want * 0.02)
    }

    private func apply(_ r: ChartRange) {
        guard let full = fullRange else { return }
        guard let days = r.days else { visible = nil; return }
        let lower = max(full.lowerBound, full.upperBound.addingTimeInterval(-days * 86_400))
        visible = lower < full.upperBound ? lower...full.upperBound : full
    }

    // MARK: - Gestures

    private func date(at point: CGPoint, _ proxy: ChartProxy, _ geo: GeometryProxy) -> Date? {
        let origin = geo[proxy.plotFrame!].origin
        return proxy.value(atX: point.x - origin.x, as: Date.self)
    }

    /// Pinch to zoom, anchored on the middle of the visible window.
    ///
    /// Clamped to the data at both ends so a hard zoom-out cannot scroll the
    /// series off the plot, and floored at a day so it cannot collapse to an
    /// empty domain — Swift Charts renders an inverted or zero-width domain as a
    /// blank plot with no error, which reads as a broken chart.
    private func magnify(proxy: ChartProxy, geo: GeometryProxy) -> some Gesture {
        MagnifyGesture()
            .onChanged { value in
                let base = gestureStart ?? window
                if gestureStart == nil { gestureStart = window }
                guard let base, let full = fullRange else { return }
                let span = base.upperBound.timeIntervalSince(base.lowerBound)
                let scaled = max(86_400, span / max(0.2, value.magnification))
                let mid = base.lowerBound.addingTimeInterval(span / 2)
                let lo = mid.addingTimeInterval(-scaled / 2)
                let hi = mid.addingTimeInterval(scaled / 2)
                visible = clamp(lo...hi, to: full)
            }
            .onEnded { _ in gestureStart = nil }
    }

    #if os(macOS)
    /// Drag to pan. Only on macOS, where hover already owns the readout.
    private func pan(proxy: ChartProxy, geo: GeometryProxy) -> some Gesture {
        DragGesture()
            .onChanged { value in
                let base = gestureStart ?? window
                if gestureStart == nil { gestureStart = window }
                guard let base, let full = fullRange else { return }
                let width = geo[proxy.plotFrame!].width
                guard width > 0 else { return }
                let span = base.upperBound.timeIntervalSince(base.lowerBound)
                let shift = -Double(value.translation.width) / Double(width) * span
                let lo = base.lowerBound.addingTimeInterval(shift)
                let hi = base.upperBound.addingTimeInterval(shift)
                visible = clamp(lo...hi, to: full)
            }
            .onEnded { _ in gestureStart = nil }
    }
    #endif

    /// Keep a window inside the data, preserving its width where possible.
    private func clamp(_ range: ClosedRange<Date>,
                       to full: ClosedRange<Date>) -> ClosedRange<Date> {
        let span = min(range.upperBound.timeIntervalSince(range.lowerBound),
                       full.upperBound.timeIntervalSince(full.lowerBound))
        var lower = range.lowerBound
        if lower < full.lowerBound { lower = full.lowerBound }
        if lower.addingTimeInterval(span) > full.upperBound {
            lower = full.upperBound.addingTimeInterval(-span)
        }
        return lower...lower.addingTimeInterval(span)
    }
}

/// The preset windows offered under a chart.
///
/// Only those the data can actually fill are shown: offering 5Y on a series with
/// eight months of history gives a button that looks like it did nothing.
enum ChartRange: Hashable {
    case month, quarter, halfYear, year, fiveYear, all

    var days: Double? {
        switch self {
        case .month:    return 30
        case .quarter:  return 91
        case .halfYear: return 182
        case .year:     return 365
        case .fiveYear: return 365 * 5
        case .all:      return nil
        }
    }

    var label: LocalizedString {
        switch self {
        case .month:    return S.range1M
        case .quarter:  return S.range3M
        case .halfYear: return S.range6M
        case .year:     return S.range1Y
        case .fiveYear: return S.range5Y
        case .all:      return S.rangeAll
        }
    }

    static func applicable(spanDays: Double) -> [ChartRange] {
        var out: [ChartRange] = []
        for r in [ChartRange.month, .quarter, .halfYear, .year, .fiveYear] {
            if let d = r.days, spanDays > d * 1.2 { out.append(r) }
        }
        out.append(.all)
        return out
    }
}


/// Axes for `TimeSeriesChart`, present or hidden.
///
/// A `ViewModifier` rather than an `if` in the modifier chain: `.chartXAxis(.hidden)`
/// and `.localizedDateAxis(...)` produce different types, so branching inline does not
/// type-check — and applying both in sequence silently lets the *last* one win, which
/// is how a "hidden" axis ends up drawn.
private struct AxisStyle: ViewModifier {
    let compact: Bool
    let window: ClosedRange<Date>?

    func body(content: Content) -> some View {
        if compact {
            content
                .chartXAxis(.hidden)
                .chartYAxis(.hidden)
        } else {
            content
                .chartYAxis { AxisMarks(position: .leading) }
                .localizedDateAxis(dates: window.map { [$0.lowerBound, $0.upperBound] } ?? [])
        }
    }
}
