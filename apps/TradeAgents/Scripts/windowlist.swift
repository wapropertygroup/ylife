// Prints the on-screen window ids owned by a named application, one per line as
// `id<TAB>widthxheight<TAB>title`.
//
// This exists so `dev.sh` can call `screencapture -l <id>`, which captures exactly one
// window wherever it is. The alternative, `screencapture -R x,y,w,h`, takes a rect in
// the *main* display's coordinate space — on a multi-display machine a window on a
// secondary display is simply unreachable, and the capture silently returns whatever
// happens to be on the main display instead. That failure produced screenshots of
// unrelated applications more than once while this app was being built.
//
// Run with `swift Scripts/windowlist.swift TradeAgents`. Deliberately a script rather
// than a compiled helper: it runs a handful of times during development and is not
// worth a build product, and CoreGraphics is in the system frameworks so it needs no
// package dependency.

import CoreGraphics
import Foundation

let wanted = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "TradeAgents"

// .optionOnScreenOnly excludes windows that exist but are not displayed, which is what
// keeps a closed popover from being captured as a blank rectangle.
guard let raw = CGWindowListCopyWindowInfo(
    [.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID
) as? [[String: Any]] else {
    FileHandle.standardError.write(Data("could not read the window list\n".utf8))
    exit(1)
}

var found = false
for window in raw {
    guard let owner = window[kCGWindowOwnerName as String] as? String, owner == wanted,
          let id = window[kCGWindowNumber as String] as? Int else { continue }

    let bounds = window[kCGWindowBounds as String] as? [String: Any] ?? [:]
    let w = Int(bounds["Width"] as? Double ?? 0)
    let h = Int(bounds["Height"] as? Double ?? 0)

    // Skip the 1x1 and zero-size helper windows AppKit keeps around; capturing one
    // yields a valid but empty PNG, which looks like a rendering bug rather than a
    // wrong target.
    guard w > 20, h > 20 else { continue }

    let title = window[kCGWindowName as String] as? String ?? ""
    print("\(id)\t\(w)x\(h)\t\(title)")
    found = true
}

if !found {
    FileHandle.standardError.write(Data("no on-screen windows for \(wanted)\n".utf8))
    exit(2)
}
