import SwiftUI

@main
struct TradeAgentsApp: App {
    #if os(macOS)
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    #endif

    var body: some Scene {
        WindowGroup {
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
            // Replaces the "New Window" item, which for a single-account dashboard
            // opens a second copy of the same thing with its own independent fetches.
            CommandGroup(replacing: .newItem) {}
        }
        #endif
    }
}
