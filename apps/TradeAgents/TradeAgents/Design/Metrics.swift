import SwiftUI

/// The app's spacing vocabulary, in one place.
///
/// These were 66 hardcoded literals spread across twelve files — `.padding(14)`,
/// `cornerRadius: 14`, `LazyVStack(spacing: 12)` — which made "make it tighter" a
/// 66-site edit that would inevitably be applied unevenly. Unevenly is worse than
/// loose: a 14pt card beside an 11pt one reads as a mistake, while two 14pt cards
/// just read as roomy.
///
/// Sized for a dashboard rather than a reading app. Every screen here is numbers a
/// reader scans and compares, so fitting one more row above the fold is worth more
/// than the breathing room a text layout would want.
///
/// The floor is legibility and the tap target. `cardPadding` stops at 11 because
/// below that a value sits close enough to the card border to read as clipped, and
/// nothing here shrinks the 44pt minimum for anything tappable — the range buttons
/// under a chart are already at that limit.
enum Metrics {
    /// Inside a card, between its border and its content.
    static let cardPadding: CGFloat = 11
    /// Between cards in a scrolling column.
    static let stackSpacing: CGFloat = 8
    /// Card corner radius. Tracks padding — a large radius on a tight card crowds
    /// the corners of whatever sits inside it.
    static let cardRadius: CGFloat = 12
    /// Screen edge insets for a scroll view's content.
    static let screenH: CGFloat = 14
    static let screenV: CGFloat = 9
    /// Between a label and the figure it describes.
    static let labelGap: CGFloat = 2
    /// Between items in a row of stat tiles.
    static let tileGap: CGFloat = 12
}
