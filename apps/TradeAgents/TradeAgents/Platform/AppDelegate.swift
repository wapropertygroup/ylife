#if os(macOS)
import AppKit

/// macOS application delegate.
///
/// There was, briefly, deterministic window placement here — driven by a defaults key
/// so `Scripts/dev.sh` could screenshot a known rect. It is gone because it did not
/// work: SwiftUI positions the window *after* `applicationDidFinishLaunching`, so the
/// frame was overwritten every time. The build script now reads the window's real rect
/// through the accessibility API instead, which is both simpler and correct wherever
/// the window happens to be.
///
/// Worth recording rather than silently deleting: the appealing version of this
/// screenshot helper is one that assumes a fixed rect. That version does not fail
/// loudly when the assumption breaks — `screencapture -R` happily photographs whatever
/// is behind the app.
final class AppDelegate: NSObject, NSApplicationDelegate {
    /// A single-window dashboard: closing the last window should quit, not leave an
    /// empty menu bar behind with no way back to the UI.
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        true
    }
}
#endif
