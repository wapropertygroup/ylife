#if os(macOS)
import SwiftUI

/// The status-bar popover.
///
/// What belongs in a menu bar is the thing you would otherwise open the app to check,
/// and for this product that is *what the agents decided*, not a price. Prices are
/// everywhere; a finished research report with a verdict on it is the thing this
/// project produces. So the decisions lead and a short market line follows.
///
/// **Loads on open, never on a timer.** Menu-bar apps that poll in the background are
/// how a dashboard turns into a battery complaint, and the content is only observable
/// while the popover is showing. `/api/markets` is ~148 KB, so a one-minute timer
/// would be roughly 200 MB a day to keep a panel warm that nobody is looking at.
struct MenuBarView: View {
    @Environment(\.openWindow) private var openWindow
    @Environment(Localization.self) private var loc
    @Environment(AlertCenter.self) private var alerts

    @State private var jobs: [AgentJob] = []
    @State private var markets: MarketsResponse?
    @State private var loading = true
    @State private var failure: String?

    /// Index rows worth a glance, in preference order.
    ///
    /// Resolved against what the payload actually contains rather than indexed
    /// directly, and `ndx` is **not** in it: `/api/markets` keys the Nasdaq as `ixic`.
    /// Asking for a key that does not exist rendered a silently empty third column —
    /// the same trap `MarketsView.preferredOrder` is immune to only because it appends
    /// whatever it did not recognise instead of leaving a hole.
    private static let glancePreference = ["spx", "ixic", "dji", "n225", "gold"]
    private static let glanceCount = 3

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            header
            Divider().overlay(Palette.border)

            if loading {
                ProgressView()
                    .controlSize(.small)
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 18)
            } else if let failure {
                Text(failure)
                    .font(.caption)
                    .foregroundStyle(Palette.secondaryText)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(12)
            } else {
                if let markets { glance(markets) ; Divider().overlay(Palette.border) }
                decisions
            }

            Divider().overlay(Palette.border)
            footer
        }
        .frame(width: 320)
        // The popover's own backing is a system material that follows the *system*
        // appearance, so `.preferredColorScheme(.dark)` does not reach it — on a Mac in
        // light mode the panel came out light grey with this app's dark-only palette
        // painted onto it, and `Palette.up` (emerald-400, chosen to glow against
        // near-black) measured as an unreadable pale green. Painting the background
        // explicitly is the same decision the web app makes: dark-only, everywhere.
        .background(Palette.background)
        .task { await load() }
    }

    private var header: some View {
        HStack {
            Text("trade-agents")
                .font(.caption.weight(.semibold))
                .foregroundStyle(.white)
            Spacer()
            if !alerts.unread.isEmpty {
                Circle()
                    .fill(Palette.warn)
                    .frame(width: 6, height: 6)
            }
            if let label = markets?.meta?.ageLabel {
                Text(label)
                    .font(.caption2)
                    .foregroundStyle(Palette.secondaryText)
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
    }

    private func glance(_ data: MarketsResponse) -> some View {
        let shown = Self.glancePreference
            .compactMap { data.indices[$0] }
            .prefix(Self.glanceCount)
        return HStack(spacing: 0) {
            ForEach(Array(shown)) { instrument in
                VStack(alignment: .leading, spacing: 1) {
                    Text(instrument.symbol)
                        .font(.system(size: 9))
                        .foregroundStyle(Palette.mutedText)
                    Text(Format.signedPercent(instrument.dayChange))
                        .font(.caption.weight(.medium).monospacedDigit())
                        .foregroundStyle(Format.tint(instrument.dayChange))
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
    }

    @ViewBuilder
    private var decisions: some View {
        if jobs.isEmpty {
            Text(loc(S.noFinished))
                .font(.caption)
                .foregroundStyle(Palette.secondaryText)
                .padding(12)
        } else {
            VStack(spacing: 0) {
                ForEach(jobs.prefix(5)) { job in
                    HStack(spacing: 8) {
                        Text(job.ticker)
                            .font(.caption.monospaced().weight(.medium))
                            .foregroundStyle(.white)
                            .lineLimit(1)
                        Spacer()
                        Text(Format.day(job.date))
                            .font(.system(size: 9))
                            .foregroundStyle(Palette.mutedText)
                        Text(Decision.label(job.decision, fallback: loc(S.noDecision)))
                            .font(.system(size: 10, weight: .semibold))
                            .foregroundStyle(Decision.tint(job.decision))
                            .lineLimit(1)
                    }
                    .padding(.horizontal, 12)
                    .padding(.vertical, 6)
                }
            }
            .padding(.vertical, 2)
        }
    }

    private var footer: some View {
        HStack(spacing: 12) {
            Button(loc(S.open)) { open() }
            Button(loc(S.refresh)) { Task { await load() } }
            Spacer()
            // Explicit, because a menu-bar app with no visible window is otherwise
            // impossible to quit without Activity Monitor -- and closing the main
            // window already terminates this one (see AppDelegate), so the Dock
            // shortcut is not always available either.
            Button(loc(S.quit)) { NSApp.terminate(nil) }
        }
        .buttonStyle(.plain)
        .foregroundStyle(Palette.brand)
        .font(.caption)
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
    }

    /// Brings the main window forward, reopening it if it was closed.
    ///
    /// `NSApp.activate` is needed as well as `openWindow`: the app is not frontmost
    /// while a menu-bar popover is showing, so without it the window is ordered in
    /// behind whatever the reader was actually using.
    private func open() {
        openWindow(id: TradeAgentsApp.mainWindowID)
        NSApp.activate(ignoringOtherApps: true)
    }

    private func load() async {
        loading = true
        defer { loading = false }
        failure = nil
        // Independent, so a cold markets cache answering 202 does not also hide the
        // decisions -- which are the reason this popover exists.
        async let showcase = try? await APIClient.shared.showcase()
        async let quotes = try? await APIClient.shared.markets()
        let (list, data) = await (showcase, quotes)
        jobs = list?.jobs ?? []
        markets = data
        if list == nil && data == nil {
            failure = loc(S.unreachable)
        }
        // The popover is often the only surface a reader opens all day, so it feeds
        // the alert centre too -- otherwise a report that finished while the main
        // window was closed would never be noticed at all.
        if let list { alerts.observe(showcase: list.jobs, language: loc.language) }
        if let data { alerts.observe(markets: data, language: loc.language) }
    }
}
#endif
