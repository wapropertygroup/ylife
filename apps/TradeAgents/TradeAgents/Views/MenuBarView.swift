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

    @State private var jobs: [AgentJob] = []
    @State private var markets: MarketsResponse?
    @State private var loading = true
    @State private var failure: String?

    /// Index rows worth a glance, in this order. Deliberately three: the popover has a
    /// fixed width and this is a summary, not the Markets screen.
    private static let glanceKeys = ["spx", "ndx", "dji"]

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            header
            Divider()

            if loading {
                ProgressView()
                    .controlSize(.small)
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 18)
            } else if let failure {
                Text(failure)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(12)
            } else {
                if let markets { glance(markets) ; Divider() }
                decisions
            }

            Divider()
            footer
        }
        .frame(width: 320)
        .task { await load() }
    }

    private var header: some View {
        HStack {
            Text("trade-agents")
                .font(.caption.weight(.semibold))
            Spacer()
            if let label = markets?.meta?.ageLabel {
                Text(label)
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
    }

    private func glance(_ data: MarketsResponse) -> some View {
        HStack(spacing: 0) {
            ForEach(Self.glanceKeys, id: \.self) { key in
                if let instrument = data.indices[key] {
                    VStack(alignment: .leading, spacing: 1) {
                        Text(instrument.symbol)
                            .font(.system(size: 9))
                            .foregroundStyle(.secondary)
                        Text(Format.signedPercent(instrument.dayChange))
                            .font(.caption.weight(.medium).monospacedDigit())
                            .foregroundStyle(Format.tint(instrument.dayChange))
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
    }

    @ViewBuilder
    private var decisions: some View {
        if jobs.isEmpty {
            Text("No finished reports")
                .font(.caption)
                .foregroundStyle(.secondary)
                .padding(12)
        } else {
            VStack(spacing: 0) {
                ForEach(jobs.prefix(5)) { job in
                    HStack(spacing: 8) {
                        Text(job.ticker)
                            .font(.caption.monospaced().weight(.medium))
                            .lineLimit(1)
                        Spacer()
                        Text(Format.day(job.date))
                            .font(.system(size: 9))
                            .foregroundStyle(.tertiary)
                        Text(Decision.label(job.decision))
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
            Button("Open") { open() }
            Button("Refresh") { Task { await load() } }
            Spacer()
            // Explicit, because a menu-bar app with no visible window is otherwise
            // impossible to quit without Activity Monitor -- and closing the main
            // window already terminates this one (see AppDelegate), so the Dock
            // shortcut is not always available either.
            Button("Quit") { NSApp.terminate(nil) }
        }
        .buttonStyle(.link)
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
            failure = "Could not reach trade-agents."
        }
    }
}
#endif
