import SwiftUI

/// The sections of the app.
///
/// Ordered by what a reader opens most, not by how the web nav is arranged: Agents is
/// first because it is what `trade-agents.com` exists to serve, even though Markets
/// was the first screen built here.
enum AppSection: String, CaseIterable, Identifiable, Hashable {
    case agents
    case markets
    case rates
    case sentiment
    case settings

    var id: String { rawValue }

    /// The tab's name, in both languages. Resolved by the caller, which has the
    /// environment; an enum has no access to one.
    var title: LocalizedString {
        switch self {
        case .agents:    return S.agents
        case .markets:   return S.markets
        case .rates:     return S.rates
        case .sentiment: return S.sentiment
        case .settings:  return S.settings
        }
    }

    var icon: String {
        switch self {
        case .agents:    return "person.3.sequence.fill"
        case .markets:   return "chart.line.uptrend.xyaxis"
        case .rates:     return "building.columns"
        case .sentiment: return "gauge.with.dots.needle.33percent"
        case .settings:  return "gearshape"
        }
    }

    @ViewBuilder
    var destination: some View {
        switch self {
        case .agents:    AgentsView()
        case .markets:   MarketsView()
        case .rates:     RatesView()
        case .sentiment: SentimentView()
        case .settings:  SettingsView()
        }
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
    @State private var selection: AppSection = .agents
    @State private var session = Session()

    var body: some View {
        content
            .environment(session)
            .task { await session.refresh() }
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
                    .toolbar { ToolbarItem(placement: .primaryAction) { AlertsButton() } }
            }
        }
        #else
        TabView(selection: $selection) {
            ForEach(AppSection.allCases) { section in
                NavigationStack {
                    section.destination
                        .toolbar { ToolbarItem(placement: .primaryAction) { AlertsButton() } }
                }
                .tabItem { Label(loc(section.title), systemImage: section.icon) }
                .tag(section)
            }
        }
        .tint(Palette.brand)
        #endif
    }
}
