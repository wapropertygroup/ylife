import SwiftUI

/// The kiosk itself: one dashboard, full screen, rotating.
///
/// Deliberately thin. Every section it shows is the same view the app already
/// renders, so there is one implementation of each dashboard and the kiosk cannot
/// drift from the app — the alternative, a second set of TV-shaped screens, would
/// be eight more things to keep correct and eight more places for a number to be
/// stale in only one of them.
struct KioskView: View {
    @Environment(Localization.self) private var loc
    let kiosk: Kiosk

    /// One timer, not two. It ticks every second and the two schedules are
    /// derived from elapsed time, because a pair of `Timer.publish` streams at 30
    /// and 300 seconds drift against each other and produce a double rebuild
    /// whenever they coincide — visible as a flicker every tenth rotation.
    private let tick = Timer.publish(every: 1, on: .main, in: .common).autoconnect()
    @State private var heldFor: Double = 0
    @State private var sinceRefresh: Double = 0
    @State private var now = Date()

    var body: some View {
        ZStack(alignment: .top) {
            Palette.background.ignoresSafeArea()

            // Lay the dashboard out into a canvas shrunk by the zoom factor, then
            // scale it back up to fill the screen. The content therefore reflows
            // for the smaller canvas — which is the point — instead of being
            // magnified and clipped.
            GeometryReader { geo in
                kiosk.current.destination
                    // Rebuilding on identity is what re-runs each screen's
                    // `.task`. Without this the kiosk would show the first
                    // payload it ever fetched, for as long as it was left running.
                    .id(kiosk.token)
                    .frame(width: geo.size.width / kiosk.zoom,
                           height: geo.size.height / kiosk.zoom)
                    .scaleEffect(kiosk.zoom, anchor: .topLeading)
            }
            .padding(.top, 34)

            header
        }
        .onReceive(tick) { _ in step() }
        // Escape leaves, which is what a full-screen Mac window trains you to
        // expect. Also the only way out on a machine with no visible chrome.
        .background(KioskKeyCatcher { kiosk.stop() })
        .onDisappear { kiosk.stop() }
    }

    /// A slim bar: what this is, whether it is live, and where in the loop.
    ///
    /// A wall display has to answer "is this thing still working?" without anyone
    /// touching it, so the refresh age is shown rather than implied. A frozen
    /// dashboard and a quiet market look identical otherwise.
    private var header: some View {
        HStack(spacing: 12) {
            Text(loc(kiosk.current.title))
                .font(.headline)
            Circle()
                .fill(kiosk.paused ? Palette.warn : Palette.up)
                .frame(width: 6, height: 6)
            Text(ageLabel)
                .font(.caption.monospacedDigit())
                .foregroundStyle(Palette.secondaryText)

            Spacer()

            // Rotation position. Dots rather than "3 / 8" because it reads at a
            // glance from across a room, which text of this size does not.
            HStack(spacing: 4) {
                ForEach(Kiosk.rotation.indices, id: \.self) { i in
                    Capsule()
                        .fill(i == kiosk.index ? Palette.brand : Palette.border)
                        .frame(width: i == kiosk.index ? 14 : 5, height: 5)
                }
            }

            Text(now, format: .dateTime.hour().minute())
                .font(.caption.monospacedDigit())
                .foregroundStyle(Palette.secondaryText)
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 8)
        .background(.ultraThinMaterial)
    }

    private var ageLabel: String {
        let secs = Int(now.timeIntervalSince(kiosk.lastRefresh))
        if secs < 60 { return loc(S.kioskJustNow) }
        return "\(secs / 60)m"
    }

    private func step() {
        now = Date()
        guard !kiosk.paused else { return }
        heldFor += 1
        sinceRefresh += 1
        if heldFor >= kiosk.dwell {
            heldFor = 0
            // Advancing rebuilds the next screen, which re-fetches it, so the
            // refresh clock restarts too. Not doing this would rebuild a screen
            // that had just been built.
            sinceRefresh = 0
            kiosk.advance()
        } else if sinceRefresh >= kiosk.refresh {
            sinceRefresh = 0
            kiosk.reload()
        }
    }
}

#if os(macOS)
import AppKit

/// Escape to leave.
///
/// An `NSView` rather than SwiftUI's `.onKeyPress`, which needs macOS 14's focus
/// system to have given this view focus — and in a kiosk the focused thing is
/// whatever chart or picker the rotating dashboard happens to contain, so the
/// keypress never arrives. A local event monitor sees it regardless of focus,
/// which is the behaviour a full-screen escape hatch has to have: if it depends
/// on where focus landed, it is not an escape hatch.
private struct KioskKeyCatcher: NSViewRepresentable {
    let onEscape: () -> Void

    func makeNSView(context: Context) -> NSView {
        context.coordinator.install(onEscape)
        return NSView(frame: .zero)
    }

    func updateNSView(_ nsView: NSView, context: Context) {}

    func makeCoordinator() -> Coordinator { Coordinator() }

    static func dismantleNSView(_ nsView: NSView, coordinator: Coordinator) {
        coordinator.remove()
    }

    final class Coordinator {
        private var monitor: Any?

        func install(_ onEscape: @escaping () -> Void) {
            guard monitor == nil else { return }
            monitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { event in
                if event.keyCode == 53 {           // Escape
                    onEscape()
                    return nil                      // swallow it
                }
                return event
            }
        }

        func remove() {
            if let monitor { NSEvent.removeMonitor(monitor) }
            monitor = nil
        }

        deinit { remove() }
    }
}
#else
/// iOS leaves by tapping, handled in the view below. Nothing to catch.
private struct KioskKeyCatcher: View {
    let onEscape: () -> Void
    var body: some View { Color.clear.allowsHitTesting(false) }
}
#endif
