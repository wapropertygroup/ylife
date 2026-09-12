import SwiftUI

@main
struct TradeAgentsApp: App {
    #if os(macOS)
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate

    /// Shared so the menu-bar popover can reopen the main window by id rather than
    /// rummaging through `NSApp.windows`.
    static let mainWindowID = "main"
    #endif

    var body: some Scene {
        WindowGroup(id: Self.sceneID) {
            RootView()
                // The web app is dark-only (`<html class="dark">`, bg-slate-950) and
                // the palette is lifted from it so the two do not look like different
                // products. Forced rather than following the system, for the same
                // reason the site does not offer a light theme.
                .preferredColorScheme(.dark)
        }
        #if os(macOS)
        .defaultSize(width: 1280, height: 900)
        .commands {
            // Replaces "New Window", which for a single-account dashboard opens a
            // second copy of the same thing with its own independent fetches.
            CommandGroup(replacing: .newItem) {}
        }
        #endif

        // The status-bar item, as its own scene.
        //
        // It needs a second `#if` rather than sharing the one above: a conditional
        // that opens directly after a scene expression is parsed as applying to that
        // expression's modifier chain, so declaring a *new* scene inside it fails with
        // "unexpected tokens in '#if' expression body".
        //
        // `.window` rather than the default `.menu` because the content is a laid-out
        // panel — rows with colour-coded verdicts and a market line — and `.menu` style
        // only renders a list of menu items.
        #if os(macOS)
        MenuBarExtra("trade-agents", systemImage: "chart.line.uptrend.xyaxis") {
            MenuBarView()
                .preferredColorScheme(.dark)
        }
        .menuBarExtraStyle(.window)
        #endif
    }

    /// `WindowGroup(id:)` needs a literal on every platform, but the id is only
    /// referenced on macOS.
    private static var sceneID: String {
        #if os(macOS)
        return mainWindowID
        #else
        return "main"
        #endif
    }
}
