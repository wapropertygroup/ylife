import CoreGraphics
import Foundation

// Post scroll-wheel events at a point. Used by dev.sh to reach cards below the fold:
// SwiftUI's ScrollView takes no keyboard focus, so `key code 121` (page down) does
// nothing, and there is no accessibility action to scroll it either.
let args = CommandLine.arguments
guard args.count >= 4,
      let x = Double(args[1]), let y = Double(args[2]), let ticks = Int(args[3]) else {
    FileHandle.standardError.write("usage: scroll <x> <y> <ticks>\n".data(using: .utf8)!)
    exit(2)
}
// The cursor must be over the scroll view: a wheel event is delivered by location,
// not to the focused view.
CGWarpMouseCursorPosition(CGPoint(x: x, y: y))
usleep(200_000)
for _ in 0..<abs(ticks) {
    if let e = CGEvent(scrollWheelEvent2Source: nil, units: .line, wheelCount: 1,
                       wheel1: Int32(ticks > 0 ? -3 : 3), wheel2: 0, wheel3: 0) {
        e.post(tap: .cghidEventTap)
    }
    usleep(40_000)
}
