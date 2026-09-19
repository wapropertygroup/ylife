import SwiftUI

#if os(macOS)
import AppKit
#else
import UIKit
#endif

/// Kiosk state: which dashboards rotate, how long each is held, and when the
/// data behind them is thrown away and re-fetched.
///
/// The web app already has this shape — `stock.li-family.us/tv`, a standalone
/// kiosk with a domain short enough to type on a television remote — and the
/// reasoning carries over: nobody scrolls a TV, nobody taps it, and the only
/// interaction is that it must stay correct without one.
@Observable
final class Kiosk {
    /// The dashboards worth putting on a wall.
    ///
    /// Eight of the ten sections, and the two omissions are deliberate. Agents is
    /// a form that spends credits and takes tens of minutes — a button nobody can
    /// press is worse on a kiosk than absent. Settings configures the thing
    /// showing it. Both would also sit there unrefreshed, since neither has data
    /// that changes on its own.
    static let rotation: [AppSection] = [
        .markets, .dca, .valuation, .holdings13f,
        .fed, .rates, .commodities, .sentiment,
    ]

    var active = false
    var paused = false
    var index = 0

    /// Seconds per dashboard. Long enough to read a chart across a room, short
    /// enough that the full loop is a few minutes.
    var dwell: Double = 30

    /// How often the *visible* dashboard is rebuilt from the network.
    ///
    /// Rotating already refreshes — advancing recreates the destination view, so
    /// its `.task` runs again and re-fetches. This timer exists for the case
    /// rotation cannot cover: a kiosk parked on one dashboard, which would
    /// otherwise show the same numbers until somebody touched it. Five minutes
    /// matches the shortest server-side cache on anything here, so a faster
    /// setting would spend requests to be told the same thing.
    var refresh: Double = 300

    /// Bumped to force the current destination to be rebuilt. Views key off it
    /// with `.id`, which is the one reliable way to make a `.task` run again —
    /// SwiftUI will not re-run it for a state change inside the same identity.
    private(set) var token = UUID()
    private(set) var lastRefresh = Date()

    /// How much bigger everything is drawn. A dashboard read from a desk and one
    /// read from a sofa are not the same design, and everything else in this app
    /// is deliberately tight.
    ///
    /// A zoom factor rather than `dynamicTypeSize`, which was the obvious choice
    /// and is **inert on macOS** — measured, not assumed: rendering the same
    /// `Text` at `.large`, `.xxLarge` and `.accessibility3` gives an identical
    /// fitting size of 76x14 every time. Shipping it would have been a TV setting
    /// that quietly did nothing.
    ///
    /// The factor is applied by laying the content out into a *smaller* canvas
    /// and scaling the result up, so the layout genuinely reflows — cards get
    /// wider relative to the screen, charts get taller, and a row that fitted
    /// eight columns now fits five. Simply magnifying a desk-sized layout would
    /// crop it instead.
    var zoom: Double = 1.4
    static let zoomChoices: [Double] = [1.0, 1.2, 1.4, 1.7, 2.0]

    var current: AppSection { Self.rotation[min(index, Self.rotation.count - 1)] }

    func advance() {
        index = (index + 1) % Self.rotation.count
        reload()
    }

    func back() {
        index = (index - 1 + Self.rotation.count) % Self.rotation.count
        reload()
    }

    func reload() {
        token = UUID()
        lastRefresh = Date()
    }

    func start() {
        active = true
        paused = false
        reload()
        holdDisplayAwake(true)
        setFullScreen(true)
    }

    func stop() {
        active = false
        holdDisplayAwake(false)
        setFullScreen(false)
    }

    // MARK: - Keeping the screen on

    #if os(macOS)
    private var activity: NSObjectProtocol?
    #endif

    /// A television that blanks after ten minutes is not a dashboard.
    private func holdDisplayAwake(_ on: Bool) {
        #if os(macOS)
        if on {
            guard activity == nil else { return }
            activity = ProcessInfo.processInfo.beginActivity(
                options: [.idleDisplaySleepDisabled, .idleSystemSleepDisabled],
                reason: "TradeAgents kiosk display")
        } else if let activity {
            ProcessInfo.processInfo.endActivity(activity)
            self.activity = nil
        }
        #else
        UIApplication.shared.isIdleTimerDisabled = on
        #endif
    }

    /// iOS has no concept to toggle — an app is already the whole screen, and the
    /// kiosk view hides the tab bar itself.
    private func setFullScreen(_ on: Bool) {
        #if os(macOS)
        guard let window = Self.contentWindow() else { return }
        let isFull = window.styleMask.contains(.fullScreen)
        if isFull != on { window.toggleFullScreen(nil) }
        #endif
    }

    #if os(macOS)
    /// The main content window, which is not necessarily the first one.
    ///
    /// This app installs a menu-bar extra, and that is an `NSWindow` too — so
    /// `NSApp.windows.first` returns a 39pt-tall status item panel and
    /// `toggleFullScreen` on it silently does nothing. Observed exactly that:
    /// the kiosk started, reported success and stayed in a normal window.
    ///
    /// Filtered on `.titled` rather than picked by index, because the ordering of
    /// `NSApp.windows` is not something to rely on, and `keyWindow` is nil
    /// whenever another application is frontmost — which on a kiosk being set up
    /// is most of the time.
    static func contentWindow() -> NSWindow? {
        if let key = NSApp.keyWindow, key.styleMask.contains(.titled) { return key }
        return NSApp.windows.first { $0.styleMask.contains(.titled) && $0.isVisible }
    }
    #endif
}
