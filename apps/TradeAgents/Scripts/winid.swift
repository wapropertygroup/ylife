import CoreGraphics
import Foundation
// Print "<windowID> <width>x<height>" for the largest on-screen window owned by
// the named app. Used instead of a screen rect: `screencapture -l<id>` captures
// that window's own pixels, so it cannot photograph whatever happens to be on
// top of it — which is exactly how a rect-based capture caught another app's
// document twice.
let want = CommandLine.arguments.dropFirst().first ?? "TradeAgents"
guard let list = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]] else { exit(1) }
var best: (id: Int, w: Int, h: Int)? = nil
for w in list {
    guard (w[kCGWindowOwnerName as String] as? String) == want,
          let id = w[kCGWindowNumber as String] as? Int,
          let b = w[kCGWindowBounds as String] as? [String: Any],
          let width = b["Width"] as? Double, let height = b["Height"] as? Double
    else { continue }
    if best == nil || Int(width * height) > best!.w * best!.h {
        best = (id, Int(width), Int(height))
    }
}
if let best { print("\(best.id) \(best.w)x\(best.h)") } else { exit(2) }
