import SwiftUI

@main
struct TradeAgentsApp: App {
    var body: some Scene {
        WindowGroup {
            MarketsView()
                // The web app is dark-only (`<html class="dark">`, bg-slate-950), and
                // the palette below is lifted from it so the two do not look like
                // different products. Forced rather than following the system, for the
                // same reason the site does not offer a light theme.
                .preferredColorScheme(.dark)
        }
    }
}

/// Colours taken from the web app's Tailwind slate/indigo palette so the native
/// screens match the dashboards exactly.
enum Palette {
    static let background = Color(red: 2 / 255, green: 6 / 255, blue: 23 / 255)        // slate-950
    static let card = Color(red: 15 / 255, green: 23 / 255, blue: 42 / 255)            // slate-900
    static let border = Color(red: 30 / 255, green: 41 / 255, blue: 59 / 255)          // slate-800
    static let secondaryText = Color(red: 148 / 255, green: 163 / 255, blue: 184 / 255) // slate-400
    static let up = Color(red: 52 / 255, green: 211 / 255, blue: 153 / 255)            // emerald-400
    static let down = Color(red: 248 / 255, green: 113 / 255, blue: 113 / 255)         // red-400
    static let brand = Color(red: 99 / 255, green: 102 / 255, blue: 241 / 255)         // indigo-500
}
