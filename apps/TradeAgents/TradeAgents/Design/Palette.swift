import SwiftUI

/// Colours taken from the web app's Tailwind slate/indigo palette so the native
/// screens and the dashboards do not look like different products.
///
/// The web app is dark-only (`<html class="dark">`, `bg-slate-950`) and so is this.
/// That is a deliberate copy of the site's own decision, not an oversight: see
/// CLAUDE.md, where `prefers-color-scheme` is explicitly not consulted because it
/// would change the appearance for existing readers who never asked.
enum Palette {
    static let background     = Color(hex: 0x020617) // slate-950
    static let card           = Color(hex: 0x0F172A) // slate-900
    static let well           = Color(hex: 0x1E293B) // slate-800, for inset rows
    static let border         = Color(hex: 0x1E293B) // slate-800
    static let secondaryText  = Color(hex: 0x94A3B8) // slate-400
    static let mutedText      = Color(hex: 0x64748B) // slate-500
    static let up             = Color(hex: 0x34D399) // emerald-400
    static let down           = Color(hex: 0xF87171) // red-400
    static let warn           = Color(hex: 0xFBBF24) // amber-400
    static let brand          = Color(hex: 0x6366F1) // indigo-500
}

extension Color {
    /// `Color(hex: 0x34D399)`, for transcribing Tailwind values without turning each
    /// one into three divisions by 255 at the call site.
    init(hex: UInt32) {
        self.init(
            red:   Double((hex >> 16) & 0xFF) / 255,
            green: Double((hex >> 8) & 0xFF) / 255,
            blue:  Double(hex & 0xFF) / 255
        )
    }

    /// A `#rrggbb` string, as the server sends for each agent role.
    ///
    /// Returns nil rather than a default colour on anything unparseable. The caller
    /// decides what to substitute: silently falling back to grey here would make a
    /// server-side typo look like a deliberate design choice, and every role would
    /// quietly converge on the same colour as the palette drifted.
    init?(webHex: String?) {
        guard var raw = webHex?.trimmingCharacters(in: .whitespaces), !raw.isEmpty else {
            return nil
        }
        if raw.hasPrefix("#") { raw.removeFirst() }
        guard raw.count == 6, let value = UInt32(raw, radix: 16) else { return nil }
        self.init(hex: value)
    }
}
