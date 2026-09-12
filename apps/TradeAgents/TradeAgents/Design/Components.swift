import SwiftUI

// Shared chrome. These live here rather than as private types inside one screen
// because the second screen that needs a card or a freshness badge is where the two
// silently drift apart — and a freshness badge that means something different on two
// screens is worse than not having one.

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

    private var isStale: Bool { meta.stale == true || meta.status == "stale" }

    /// The verdict, or nil when this endpoint does not publish one.
    ///
    /// Not every `meta` carries `status`: `/api/markets` does, `/api/fedwatch` sends
    /// only the cache age. The first version printed "Unknown" in that case, which
    /// reads as *we could not establish how fresh this is* — an alarming claim to make
    /// about a perfectly good payload. Absent means the question does not apply, so
    /// the age is shown on its own.
    private var label: String? {
        switch meta.status {
        case "realtime":      return "Live"
        case "session_close": return "At session close"
        case "stale":         return "Stale"
        case let other?  where !other.isEmpty: return other.capitalized
        default:              return nil
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
            if let age = meta.ageLabel {
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

    private var isWarming: Bool {
        if case APIError.warming = error { return true }
        return false
    }

    var body: some View {
        VStack(spacing: 14) {
            Image(systemName: isWarming ? "hourglass" : "exclamationmark.triangle")
                .font(.largeTitle)
                .foregroundStyle(isWarming ? Palette.warn : Palette.down)
            Text(error.localizedDescription)
                .font(.footnote)
                .foregroundStyle(Palette.secondaryText)
                .multilineTextAlignment(.center)
            Button(isWarming ? "Check again" : "Retry", action: retry)
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

    static func label(_ raw: String?) -> String {
        let first = headline(raw)
        return first.isEmpty ? "No decision" : first
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
