import CoreGraphics
import Foundation

// Post real HID-level pointer events, for verifying interactive charts.
//
// `CGWarpMouseCursorPosition` alone is not enough: it relocates the cursor
// without generating a mouse-moved event, so SwiftUI's `onContinuousHover`
// never fires and a screenshot shows the chart in its resting state — which
// looks exactly like a hover readout that does not work. The warp and the
// event are both required.
//
//   pointer move <x> <y>
//   pointer click <x> <y>
//   pointer scroll <x> <y> <ticks>
let a = CommandLine.arguments
func need(_ i: Int) -> Double { Double(a[i]) ?? 0 }
guard a.count >= 4 else {
    FileHandle.standardError.write("usage: pointer move|click|scroll <x> <y> [ticks]\n".data(using: .utf8)!)
    exit(2)
}
let verb = a[1], pt = CGPoint(x: need(2), y: need(3))

CGWarpMouseCursorPosition(pt)
usleep(120_000)

switch verb {
case "move":
    CGEvent(mouseEventSource: nil, mouseType: .mouseMoved,
            mouseCursorPosition: pt, mouseButton: .left)?.post(tap: .cghidEventTap)
case "click":
    CGEvent(mouseEventSource: nil, mouseType: .mouseMoved,
            mouseCursorPosition: pt, mouseButton: .left)?.post(tap: .cghidEventTap)
    usleep(60_000)
    CGEvent(mouseEventSource: nil, mouseType: .leftMouseDown,
            mouseCursorPosition: pt, mouseButton: .left)?.post(tap: .cghidEventTap)
    usleep(60_000)
    CGEvent(mouseEventSource: nil, mouseType: .leftMouseUp,
            mouseCursorPosition: pt, mouseButton: .left)?.post(tap: .cghidEventTap)
case "scroll":
    let ticks = a.count > 4 ? Int(a[4]) ?? 3 : 3
    for _ in 0..<abs(ticks) {
        CGEvent(scrollWheelEvent2Source: nil, units: .line, wheelCount: 1,
                wheel1: Int32(ticks > 0 ? -3 : 3), wheel2: 0, wheel3: 0)?
            .post(tap: .cghidEventTap)
        usleep(40_000)
    }
default:
    FileHandle.standardError.write("unknown verb \(verb)\n".data(using: .utf8)!)
    exit(2)
}
usleep(150_000)
