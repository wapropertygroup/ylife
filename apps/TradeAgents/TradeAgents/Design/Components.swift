import Charts
import SwiftUI

// Shared chrome. These live here rather than as private types inside one screen
// because the second screen that needs a card or a freshness badge is where the two
// silently drift apart — and a freshness badge that means something different on two
// screens is worse than not having one.

extension View {
    /// The y-axis for a horizontal bar chart whose categories are names.
    ///
    /// Exists because the obvious spelling is wrong in a way that still renders.
    /// `.chartYAxis { AxisMarks(position: .leading) }` draws the category labels
    /// *inside* the plot area, directly on top of the bars: on the 13F screen every
    /// ticker sat across its own bar and the plot began at the card's edge. The
    /// chart looked populated and was unreadable, which is why it survived a build
    /// and a compile-check and was only caught by looking at a screenshot.
    ///
    /// Two things fix it and both are needed. `preset: .aligned` reserves a gutter
    /// outside the plot rather than letting the label overlap it, and the explicit
    /// `AxisValueLabel` gives Swift Charts a view to *measure* when sizing that
    /// gutter — with the default label it can resolve to nothing and the bars run
    /// back under the text.
    ///
    /// Only the y-axis, deliberately: the three charts using this plot percentages,
    /// percentages and a 0-100 score, so folding an x-format in here would put a `%`
    /// on the DCA chart's V scores.
    func categoryNameAxis() -> some View {
        chartYAxis {
            AxisMarks(preset: .aligned, position: .leading) { value in
                AxisValueLabel(horizontalSpacing: 8) {
                    if let name = value.as(String.self) {
                        Text(name)
                            .font(.caption2)
                            .foregroundStyle(Palette.secondaryText)
                            .lineLimit(1)
                    }
                }
            }
        }
    }

    /// An x-axis of dates, labelled in the reader's language.
    ///
    /// Swift Charts renders a date axis through the system locale, so the labels are
    /// the one part of a chart that ignores the app's language toggle entirely — a
    /// Chinese page showing `Aug 23`.
    ///
    /// `dates` is the series being plotted, and is used only to measure its span so
    /// the label precision can follow it: see `Format.axisDate`. Pass it. Omitting it
    /// assumes a short series, which on a decade of data prints the same `1月1日` at
    /// every tick.
    func localizedDateAxis(desiredCount: Int = 4, dates: [Date] = []) -> some View {
        let span: Double = {
            guard let lo = dates.min(), let hi = dates.max() else { return 0 }
            return hi.timeIntervalSince(lo) / 86_400
        }()
        return chartXAxis {
            // `.aligned` keeps the outermost labels inside the plot. Without it the
            // last one is centred on a gridline at the right edge and clipped by the
            // card — a zoomed Fed chart ended in "2…", which reads as a rendering
            // fault rather than as a date.
            AxisMarks(preset: .aligned, values: .automatic(desiredCount: desiredCount)) { value in
                AxisGridLine().foregroundStyle(Palette.border)
                AxisValueLabel {
                    if let date = value.as(Date.self) {
                        Text(Format.axisDate(date, spanDays: span))
                            .foregroundStyle(Palette.mutedText)
                    }
                }
            }
        }
    }
}

/// A bordered surface, matching the web app's card.
struct Card<Content: View>: View {
    var padding: CGFloat = 14
    @ViewBuilder var content: Content

    var body: some View {
        content
            .padding(padding)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Palette.card, in: RoundedRectangle(cornerRadius: 14))
            .overlay(RoundedRectangle(cornerRadius: 14).stroke(Palette.border))
    }
}

/// A small pill. `tint` colours the text and a faded fill.
struct Chip: View {
    let text: String
    var tint: Color = Palette.secondaryText
    var icon: String?

    var body: some View {
        HStack(spacing: 4) {
            if let icon { Text(icon) }
            Text(text)
        }
        .font(.caption2.weight(.medium))
        .foregroundStyle(tint)
        .padding(.horizontal, 8)
        .padding(.vertical, 4)
        .background(tint.opacity(0.14), in: Capsule())
    }
}

/// A labelled figure.
struct StatTile: View {
    let label: String
    let value: String
    var tint: Color = .primary
    var alignment: HorizontalAlignment = .leading

    var body: some View {
        VStack(alignment: alignment, spacing: 2) {
            Text(label)
                .font(.system(size: 9).weight(.medium))
                .foregroundStyle(Palette.mutedText)
                .textCase(.uppercase)
            Text(value)
                .font(.caption.monospacedDigit())
                .foregroundStyle(tint)
        }
    }
}

/// The server's freshness verdict, shown rather than hidden.
///
/// `status` distinguishes a live quote from a session close from genuinely stale data
/// — a distinction `freshness.py` works to compute and every web page surfaces.
/// Reproducing it is the point: the risk in a native app is a number that reads as
/// current merely because it is on screen.
struct FreshnessBanner: View {
    let meta: Meta
    @Environment(Localization.self) private var loc

    private var isStale: Bool { meta.stale == true || meta.status == "stale" }

    /// The verdict, or nil when this endpoint does not publish one.
    ///
    /// Not every `meta` carries `status`: `/api/markets` does, `/api/fedwatch` sends
    /// only the cache age. The first version printed "Unknown" in that case, which
    /// reads as *we could not establish how fresh this is* — an alarming claim to make
    /// about a perfectly good payload. Absent means the question does not apply, so
    /// the age is shown on its own.
    ///
    /// An unrecognised status is passed through untranslated rather than dropped: it
    /// is a value the server invented after this app was built, and showing it is more
    /// useful than hiding it.
    private var label: String? {
        switch meta.status {
        case "realtime":      return loc(S.live)
        case "session_close": return loc(S.sessionClose)
        case "stale":         return loc(S.stale)
        case let other?  where !other.isEmpty: return other.capitalized
        default:              return nil
        }
    }

    /// Cache age in the reader's language.
    ///
    /// `meta.age_label` arrives already rendered in English, which is the one
    /// piece of chrome on these screens the language toggle could not reach. The
    /// same payload carries `age_seconds`, so the app says it itself. The
    /// server's string is the fallback: a stale-looking label beats showing no
    /// freshness at all, which is the thing every one of these banners exists
    /// to prevent.
    private var age: String? {
        guard let seconds = meta.ageSeconds else { return meta.ageLabel }
        switch seconds {
        case ..<60:    return loc(S.ageJustNow)
        case ..<3600:  return loc(S.ageMinutes(seconds / 60))
        case ..<86400: return loc(S.ageHours(seconds / 3600))
        default:       return loc(S.ageDays(seconds / 86400))
        }
    }

    var body: some View {
        HStack(spacing: 8) {
            Circle()
                .fill(isStale ? Palette.down
                      : (meta.marketOpen == true ? Palette.up : Palette.secondaryText))
                .frame(width: 7, height: 7)
            if let label {
                Text(label)
                    .font(.caption.weight(.medium))
                    .foregroundStyle(isStale ? Palette.down : Palette.secondaryText)
            }
            if let age {
                // The separator belongs to the age only when something precedes it.
                Text(label == nil ? age : "· \(age)")
                    .font(.caption)
                    .foregroundStyle(Palette.secondaryText)
            }
            Spacer()
        }
        .padding(.horizontal, 4)
    }
}

/// Loading and failure presentation, shared so every screen fails the same way.
///
/// `warming` is a distinct case on purpose. Several endpoints answer 202 with no
/// payload while a cache rebuilds, and the web pages poll rather than give up;
/// presenting that as an error would tell the reader something is broken when the
/// correct reading is "not yet".
struct LoadFailure: View {
    let error: Error
    let retry: () -> Void
    @Environment(Localization.self) private var loc

    private var isWarming: Bool {
        if case APIError.warming = error { return true }
        return false
    }

    /// `APIError.warming`'s own description is English, built where there is no
    /// environment to read a language from — so the localized copy is substituted
    /// here, at the one place it is shown. Every other error keeps its own message,
    /// which is usually the server's and already in the reader's language.
    private var message: String {
        isWarming ? loc(S.warming) : error.localizedDescription
    }

    var body: some View {
        VStack(spacing: 14) {
            Image(systemName: isWarming ? "hourglass" : "exclamationmark.triangle")
                .font(.largeTitle)
                .foregroundStyle(isWarming ? Palette.warn : Palette.down)
            Text(message)
                .font(.footnote)
                .foregroundStyle(Palette.secondaryText)
                .multilineTextAlignment(.center)
            Button(isWarming ? loc(S.checkAgain) : loc(S.retry), action: retry)
                .buttonStyle(.borderedProminent)
                .tint(Palette.brand)
        }
        .padding(32)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

/// A centred spinner on the app background.
struct LoadingPane: View {
    var label: String?

    var body: some View {
        VStack(spacing: 10) {
            ProgressView().tint(Palette.brand)
            if let label {
                Text(label)
                    .font(.caption)
                    .foregroundStyle(Palette.secondaryText)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

/// An explicit "there is nothing here", used instead of an empty scroll view.
struct EmptyPane: View {
    let icon: String
    let title: String
    var detail: String?

    var body: some View {
        VStack(spacing: 10) {
            Image(systemName: icon)
                .font(.largeTitle)
                .foregroundStyle(Palette.mutedText)
            Text(title)
                .font(.subheadline.weight(.medium))
                .foregroundStyle(Palette.secondaryText)
            if let detail {
                Text(detail)
                    .font(.caption)
                    .foregroundStyle(Palette.mutedText)
                    .multilineTextAlignment(.center)
            }
        }
        .padding(32)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

/// Colour for a run's headline verdict.
///
/// A direct port of `report_email._decision_chip`, and it has to stay one: the same
/// run must not be amber in the completion mail and green in the app. Three details
/// carried over deliberately, each of which was wrong in the first draft here:
///
///   * **The fallback is amber, not grey.** The server has no neutral bucket. "Hold",
///     "Overweight" and "Underweight" all land here — and the sampled feed is full of
///     the latter two — so a grey default quietly drained the colour out of most of
///     the list.
///   * **The Chinese terms are matched.** Reports are written in the language they
///     were requested in, and a Chinese run says 买入 / 卖出, never "buy". Matching
///     only the English would render a whole language's reports in the fallback
///     colour. Note 增持 / 减持 (overweight / underweight) *are* mapped to green and
///     red server-side even though their English equivalents are not; that asymmetry
///     is reproduced rather than corrected, because the point is to agree with the
///     mail, not to be independently sensible.
///   * **Only the first line, capped.** The field is free text written by a model and
///     sometimes carries a sentence of rationale after the verdict, which turns a chip
///     into a paragraph.
enum Decision {
    static func tint(_ raw: String?) -> Color {
        let first = headline(raw)
        guard !first.isEmpty else { return Palette.secondaryText }
        let low = first.lowercased()
        if low.contains("buy") || first.contains("买入") || first.contains("增持") {
            return Palette.up
        }
        if low.contains("sell") || first.contains("卖出") || first.contains("减持") {
            return Palette.down
        }
        return Palette.warn
    }

    static func label(_ raw: String?, fallback: String) -> String {
        let first = headline(raw)
        return first.isEmpty ? fallback : first
    }

    /// The first non-empty line, trimmed and capped at 120 characters — the same
    /// slice `_decision_chip` takes.
    private static func headline(_ raw: String?) -> String {
        guard let raw else { return "" }
        let first = raw.split(separator: "\n", omittingEmptySubsequences: false)
            .first.map(String.init) ?? raw
        return String(first.trimmingCharacters(in: .whitespaces).prefix(120))
    }
}
