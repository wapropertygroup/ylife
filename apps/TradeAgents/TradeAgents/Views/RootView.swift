import SwiftUI

/// The sections of the app.
///
/// Ordered by what a reader opens most, not by how the web nav is arranged: Agents is
/// first because it is what `trade-agents.com` exists to serve, even though Markets
/// was the first screen built here.
enum AppSection: String, CaseIterable, Identifiable, Hashable {
    case agents
    case markets
    case dca
    case valuation
    case holdings13f
    case fed
    case rates
    case commodities
    case sentiment
    case settings

    var id: String { rawValue }

    /// The tab's name, in both languages. Resolved by the caller, which has the
    /// environment; an enum has no access to one.
    var title: LocalizedString {
        switch self {
        case .agents:      return S.agents
        case .markets:     return S.markets
        case .dca:         return S.dca
        case .valuation:   return S.valuation
        case .holdings13f: return S.holdings13f
        case .fed:         return S.fed
        case .rates:       return S.rates
        case .commodities: return S.commodities
        case .sentiment:   return S.sentiment
        case .settings:    return S.settings
        }
    }

    var icon: String {
        switch self {
        case .agents:      return "person.3.sequence.fill"
        case .markets:     return "chart.line.uptrend.xyaxis"
        case .dca:         return "calendar.badge.plus"
        case .valuation:   return "function"
        case .holdings13f: return "building.2"
        case .fed:         return "banknote"
        case .rates:       return "building.columns"
        case .commodities: return "drop.fill"
        case .sentiment:   return "gauge.with.dots.needle.33percent"
        case .settings:    return "gearshape"
        }
    }

    @ViewBuilder
    var destination: some View {
        switch self {
        case .agents:      AgentsView()
        case .markets:     MarketsView()
        case .dca:         DcaView()
        case .valuation:   ValuationView()
        case .holdings13f: ThirteenFView()
        case .fed:         FedView()
        case .rates:       RatesView()
        case .commodities: CommoditiesView()
        case .sentiment:   SentimentView()
        case .settings:    SettingsView()
        }
    }

    /// The section named by `-TradeAgentsSection` or `$TRADEAGENTS_SECTION`, for
    /// screenshot automation.
    ///
    /// This exists because there is no reliable way to drive the sidebar from
    /// outside the process. SwiftUI's `List(selection:)` rows expose no `AXPress`
    /// action, `set selected of row N to true` mutates the accessibility tree
    /// without moving the actual selection, and a synthetic click at the row's own
    /// AX coordinates is ignored too — all three *report success*, which is how a
    /// screenshot of the wrong screen ends up labelled as the right one.
    ///
    /// So the section is chosen before the window exists, by the one mechanism that
    /// cannot half-work: if the value names a section, that is the section that
    /// renders. An unset or unrecognised value falls through to the normal default
    /// rather than failing, so this is inert in a normal launch.
    ///
    /// Two channels because the launcher decides which is available. A sandboxed app
    /// has to be started with `open`, which passes arguments but not environment —
    /// and AppKit folds `--args -Key value` straight into `UserDefaults`. The
    /// environment variable is kept for a direct `swift run`-style launch.
    static var launchSection: AppSection? {
        let raw = UserDefaults.standard.string(forKey: "TradeAgentsSection")
            ?? ProcessInfo.processInfo.environment["TRADEAGENTS_SECTION"]
        guard let raw else { return nil }
        return AppSection(rawValue: raw.trimmingCharacters(in: .whitespaces))
    }
}

/// Platform-appropriate navigation.
///
/// A single `TabView` for both would have been less code, and `.sidebarAdaptable`
/// exists to do exactly that — but it needs iOS 18 / macOS 15, which would raise the
/// floor purely for chrome. The two shapes below are what each platform's readers
/// expect anyway: a Mac dashboard with five sections wants a source list, and a phone
/// wants a tab bar.
struct RootView: View {
    @Environment(Localization.self) private var loc
    @State private var selection: AppSection = AppSection.launchSection ?? .agents
    @State private var session = Session()
    @State private var kiosk = Kiosk()
    /// `-TradeAgentsKiosk 1` starts straight in kiosk mode. Same reasoning as
    /// `launchSection`: the toolbar button cannot be driven from outside the
    /// process any more reliably than the sidebar could.
    private static var launchKiosk: Bool {
        UserDefaults.standard.string(forKey: "TradeAgentsKiosk") == "1"
    }

    var body: some View {
        Group {
            if kiosk.active {
                // Replaces the whole hierarchy rather than covering it. A sheet or
                // an overlay leaves the sidebar and toolbar alive underneath, still
                // laying out and still reachable by keyboard — which on a wall
                // display is a set of controls nobody can see but a passing cat can
                // still trigger.
                kioskSurface
            } else {
                content
            }
        }
        .environment(session)
        .task { await session.refresh() }
        .task {
            if Self.launchKiosk && !kiosk.active {
                kiosk.index = Kiosk.rotation.firstIndex(of: selection) ?? 0
                kiosk.start()
            }
        }
    }

    @ViewBuilder
    private var kioskSurface: some View {
        let view = KioskView(kiosk: kiosk)
        #if os(macOS)
        view
        #else
        // No keyboard on a phone or a TV-mirrored iPad, so the gesture is the way
        // out. Two taps, not one: a kiosk is exactly the surface a sleeve or a
        // passing hand brushes, and dropping out of full screen by accident is
        // the one failure that needs somebody to walk over and fix it.
        view.onTapGesture(count: 2) { kiosk.stop() }
        #endif
    }

    /// Enters the kiosk. Exposed on the toolbar and bound to a shortcut.
    private var kioskButton: some View {
        Button {
            kiosk.index = Kiosk.rotation.firstIndex(of: selection) ?? 0
            kiosk.start()
        } label: {
            Label(loc(S.kioskStart), systemImage: "tv")
        }
        .help(loc(S.kioskStart))
        .keyboardShortcut("k", modifiers: [.command, .shift])
    }

    @ViewBuilder
    private var content: some View {
        #if os(macOS)
        NavigationSplitView {
            // Plain rows bound to `selection`, not NavigationLinks. A
            // NavigationSplitView sidebar drives the detail column through its
            // selection; adding a NavigationLink as well gives the same tap two
            // meanings — select this section, and push it onto the detail stack — and
            // the visible symptom is a sidebar whose highlight disagrees with what the
            // detail pane is showing.
            List(AppSection.allCases, selection: $selection) { section in
                Label(loc(section.title), systemImage: section.icon)
                    .tag(section)
            }
            .navigationSplitViewColumnWidth(min: 170, ideal: 190, max: 240)
            .listStyle(.sidebar)
        } detail: {
            NavigationStack {
                selection.destination
                    .background(Palette.background)
                    .toolbar {
                        ToolbarItem(placement: .primaryAction) { kioskButton }
                        ToolbarItem(placement: .primaryAction) { AlertsButton() }
                    }
            }
        }
        #else
        TabView(selection: $selection) {
            ForEach(AppSection.allCases) { section in
                NavigationStack {
                    section.destination
                        .toolbar {
                            ToolbarItem(placement: .primaryAction) { kioskButton }
                            ToolbarItem(placement: .primaryAction) { AlertsButton() }
                        }
                }
                .tabItem { Label(loc(section.title), systemImage: section.icon) }
                .tag(section)
            }
        }
        .tint(Palette.brand)
        #endif
    }
}
