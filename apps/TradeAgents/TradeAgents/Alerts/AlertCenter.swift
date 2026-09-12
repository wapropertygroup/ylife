import Foundation
import Observation

/// Something that happened which the reader would want to know about.
struct AppAlert: Identifiable, Hashable {
    enum Kind: String {
        case reportFinished
        case largeMove
        case dataStale
    }

    let id: String
    let kind: Kind
    let title: String
    let detail: String
    let date: Date
    var isRead: Bool = false

    var tintsDown: Bool { kind == .dataStale }
}

/// In-app alerts for the handful of events worth interrupting for.
///
/// Deliberately **in-app only**. A system notification via `UNUserNotificationCenter`
/// would need the app to be properly signed to register reliably, and — more to the
/// point — it would promise something this app cannot deliver: notifications while it
/// is closed. Nothing here runs in the background, so every event below is noticed
/// only because a screen or the menu-bar popover happened to be loading anyway. An
/// alert that silently stops arriving when the app is quit is worse than one that
/// never claimed to.
///
/// Three events, chosen because they are derivable from data the app already fetches:
///
///   * **A report finished** — the showcase gained a job id not seen before. This is
///     the one that matters: a deep run takes tens of minutes, which is exactly why
///     `report_email.py` exists on the server side.
///   * **A large index move** — an index whose day change clears `moveThreshold`.
///   * **Market data went stale** — the server's own freshness verdict, which is a
///     claim about the upstream feed rather than about the cache being old.
@Observable
@MainActor
final class AlertCenter {
    private static let seenKey = "TA.seenReportIDs"
    private static let seenCap = 200

    /// A day move at or beyond this is worth surfacing. 2% on a major index is roughly
    /// a two-standard-deviation session — frequent enough to be useful, rare enough
    /// not to fire most days.
    static let moveThreshold = 2.0

    private(set) var alerts: [AppAlert] = []

    /// Report ids already announced, oldest first, persisted so a relaunch does not
    /// re-announce the same ten reports.
    ///
    /// An ordered array rather than a `Set` because it is capped: trimming a Set means
    /// discarding *arbitrary* ids, since a Set has no order, so the cap would silently
    /// evict recent reports and let them be announced a second time. Membership is a
    /// linear scan over at most `seenCap` entries, which is nothing next to the HTTP
    /// call that produced the list.
    private var seenReports: [String]

    /// Whether a showcase payload has been observed yet *this launch*.
    ///
    /// The load-bearing flag. On a cold start every report in the feed is "new", so
    /// without this the reader's first action produces ten notifications about runs
    /// that finished days ago. The first payload therefore seeds the seen-set silently
    /// and announces nothing; only what appears *after* that is news.
    private var hasSeededReports = false

    var unread: [AppAlert] { alerts.filter { !$0.isRead } }

    init() {
        seenReports = UserDefaults.standard.stringArray(forKey: Self.seenKey) ?? []
    }

    // MARK: - Sources

    func observe(showcase jobs: [AgentJob], language: Language) {
        let finished = jobs.filter { $0.status == "done" }
        guard hasSeededReports else {
            for job in finished where !seenReports.contains(job.id) {
                seenReports.append(job.id)
            }
            persistSeen()
            hasSeededReports = true
            return
        }
        for job in finished where !seenReports.contains(job.id) {
            seenReports.append(job.id)
            let verdict = Decision.label(job.decision, fallback: S.noDecision(language))
            raise(AppAlert(
                id: "report:\(job.id)",
                kind: .reportFinished,
                title: S.notifyNewReport(language),
                detail: "\(job.ticker) · \(verdict)",
                date: Format.parseISO(job.finishedAt) ?? .now
            ))
        }
        persistSeen()
    }

    func observe(markets: MarketsResponse, language: Language) {
        // Keyed by symbol and rounded change, not by symbol alone: the same index
        // drifting from -2.1% to -2.4% during a session is one event, not two, but a
        // genuine reversal the next day should still be able to raise a new one.
        for instrument in markets.indices.values {
            guard let change = instrument.dayChange,
                  abs(change) >= Self.moveThreshold else { continue }
            let bucket = Int(change.rounded())
            raise(AppAlert(
                id: "move:\(instrument.symbol):\(bucket)",
                kind: .largeMove,
                title: S.notifyBigMove(language),
                detail: "\(instrument.name) \(Format.signedPercent(change))",
                date: .now
            ))
        }

        if markets.meta?.stale == true || markets.meta?.status == "stale" {
            // One id for the whole condition, so a stale feed raises a single alert
            // however many times a screen reloads while it stays stale.
            raise(AppAlert(
                id: "stale:markets",
                kind: .dataStale,
                title: S.notifyStale(language),
                detail: markets.meta?.ageLabel ?? "",
                date: .now
            ))
        }
    }

    // MARK: - Mutation

    /// Adds an alert unless one with the same id is already present.
    ///
    /// Dedupe is on identity rather than recency because every source here is polled:
    /// the same condition is re-observed on each load, and appending on every poll
    /// would turn one stale feed into a list of hundreds.
    private func raise(_ alert: AppAlert) {
        guard !alerts.contains(where: { $0.id == alert.id }) else { return }
        alerts.insert(alert, at: 0)
        if alerts.count > 50 { alerts.removeLast(alerts.count - 50) }
    }

    func markAllRead() {
        for index in alerts.indices { alerts[index].isRead = true }
    }

    func dismissAll() {
        alerts.removeAll()
    }

    private func persistSeen() {
        // Bounded, because this grows forever otherwise and is written to disk on
        // every poll. Dropping from the front keeps the most recently announced ids,
        // which is the half that matters: an old report cannot reappear in the sample
        // without being announced again first.
        if seenReports.count > Self.seenCap {
            seenReports.removeFirst(seenReports.count - Self.seenCap)
        }
        UserDefaults.standard.set(seenReports, forKey: Self.seenKey)
    }
}
