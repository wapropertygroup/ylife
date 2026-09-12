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
// Run with `swift Scripts/windowlist.swift TradeAgents` to list, or
// `swift Scripts/windowlist.swift TradeAgents <out.png> <maxWidth>` to also capture the
// first matching window narrower than maxWidth.
//
// The capture is folded in rather than left to a second `screencapture -l <id>` call
// because a menu-bar popover is an NSPopover: it closes as soon as it stops being the
// key window, so anything that happens between finding its id and photographing it can
// dismiss it first.
//
// Deliberately a script rather than a compiled helper: it runs a handful of times
// during development and is not worth a build product, and CoreGraphics is in the
// system frameworks so it needs no package dependency.

import CoreGraphics
import Foundation
import ImageIO
import UniformTypeIdentifiers

let args = CommandLine.arguments
let wanted = args.count > 1 ? args[1] : "TradeAgents"
let outPath: String? = args.count > 2 ? args[2] : nil
let maxWidth = args.count > 3 ? (Int(args[3]) ?? Int.max) : Int.max

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

    guard let outPath, w <= maxWidth else { continue }
    // `screencapture -l` rather than CGWindowListCreateImage, which macOS 26 removed
    // ("Please use ScreenCaptureKit instead"). ScreenCaptureKit would work but is async
    // and needs its own permission prompt, where the command-line tool already has the
    // grant this terminal was given. Spawning it from here still counts as one
    // invocation from the shell's point of view, which is the property that matters:
    // the popover must not lose key status between being found and being photographed.
    let task = Process()
    task.executableURL = URL(fileURLWithPath: "/usr/sbin/screencapture")
    task.arguments = ["-x", "-o", "-l", String(id), outPath]
    do {
        try task.run()
        task.waitUntilExit()
    } catch {
        FileHandle.standardError.write(Data("screencapture failed: \(error)\n".utf8))
        exit(3)
    }
    guard task.terminationStatus == 0 else {
        FileHandle.standardError.write(Data("screencapture exited \(task.terminationStatus)\n".utf8))
        exit(3)
    }
    print("captured \(id) -> \(outPath)")
    exit(0)
}

if !found {
    FileHandle.standardError.write(Data("no on-screen windows for \(wanted)\n".utf8))
    exit(2)
}
