import Foundation

// Decoded from the dashboard endpoints. Shapes were read off the live responses, not
// inferred from routes.py: the web pages access these payloads dynamically, so there
// is no schema to copy and the field names are genuinely inconsistent between
// endpoints (`day_chg` vs `day_chg_pct`, `pct` vs `pct_dec`) — the same trap
// tests/test_brief_formatters.py exists to catch on the server side.

// MARK: - /api/fedwatch

/// What the market expects the Fed to do, from fed funds futures.
struct FedWatch: Decodable, Sendable {
    let asOf: String?
    let status: String?
    let current: FedCurrent?
    let meetings: [FedMeeting]
    let meta: Meta?

    enum CodingKeys: String, CodingKey {
        case status, current, meetings, meta
        case asOf = "as_of"
    }
}

/// Today's target range. `effr` is the effective rate, which sits inside it.
struct FedCurrent: Decodable, Sendable {
    let label: String?
    let lower: Double?
    let upper: Double?
    let mid: Double?
    let effr: Double?
}

/// One FOMC meeting's implied outcome.
struct FedMeeting: Decodable, Sendable, Identifiable {
    let date: String
    let label: String?
    let effective: String?
    let impliedRate: Double?
    let changeBp: Double?
    let cutProb: Double?
    let holdProb: Double?
    let hikeProb: Double?
    let method: String?
    let note: String?
    let outcomes: [FedOutcome]?

    var id: String { date }

    enum CodingKeys: String, CodingKey {
        case date, label, effective, method, note, outcomes
        case impliedRate = "implied_rate"
        case changeBp = "change_bp"
        case cutProb = "cut_prob"
        case holdProb = "hold_prob"
        case hikeProb = "hike_prob"
    }
}

/// One possible target range at a meeting, with its probability.
struct FedOutcome: Decodable, Sendable, Identifiable {
    let lower: Double?
    let upper: Double?
    let prob: Double?
    let steps: Int?

    var id: String { "\(lower ?? -1)-\(upper ?? -1)" }
}

// MARK: - /api/fear-greed

/// CNN-style fear & greed composite.
struct FearGreed: Decodable, Sendable {
    let score: Double?
    let rating: String?
    let prevClose: Double?
    let prevWeek: Double?
    let prevMonth: Double?
    let prevYear: Double?
    let history: [FearGreedPoint]

    enum CodingKeys: String, CodingKey {
        case score, rating, history
        case prevClose = "prev_close"
        case prevWeek = "prev_week"
        case prevMonth = "prev_month"
        case prevYear = "prev_year"
    }
}

/// A dated reading. `t` is epoch **milliseconds**, which is what the web charts are
/// given; treating it as seconds silently plots every point in 1970.
struct FearGreedPoint: Decodable, Sendable, Identifiable {
    let t: Double
    let y: Double
    let rating: String?

    var id: Double { t }
    var date: Date { Date(timeIntervalSince1970: t / 1000) }
}
