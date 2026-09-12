import Foundation

// Decoded from GET /api/markets on the existing Flask backend. The shapes here were
// read off the live response rather than inferred from routes.py, because the web
// dashboards are the only existing consumer and they access fields dynamically —
// there is no schema to copy.
//
// Two things about that payload drive the design:
//
//  * Almost every numeric field can be absent or null. `pe` is null for a
//    commodity, `ytd` is missing on some instruments, and `prices` carries nulls
//    where a session had no print (the web charts pass `spanGaps: true` over
//    exactly these). So the optionality below is not defensive habit, it is the
//    actual contract.
//  * `meta` is the freshness verdict, and it is not decoration. The backend
//    already separates cache age from market-hours staleness from a dead upstream
//    (freshness.py), and every web page surfaces it. A native client that dropped
//    it would be the one consumer presenting an hour-old quote as current.

struct MarketsResponse: Decodable, Sendable {
    let indices: [String: Instrument]
    let sectors: [Sector]
    let vix: Vix?
    let meta: Meta?
}

struct Instrument: Decodable, Sendable, Identifiable {
    let name: String
    let symbol: String
    let current: Double?
    let dayChange: Double?
    let ytd: Double?
    let hi52: Double?
    let lo52: Double?
    let ma50: Double?
    let ma200: Double?
    let pe: Double?
    let rsi14: Double?
    let volume: Int?
    let daily: Series?
    let weekly: Series?
    let monthly: Series?

    var id: String { symbol }

    enum CodingKeys: String, CodingKey {
        case name, symbol, current, ytd, hi52, lo52, ma50, ma200, pe, rsi14, volume
        case daily, weekly, monthly
        case dayChange = "day_chg"
    }
}

/// A dated series. `prices` is parallel to `dates` and may hold nulls.
struct Series: Decodable, Sendable {
    let dates: [String]
    let prices: [Double?]

    /// Date/price pairs with the gaps dropped, ready to plot.
    ///
    /// Parsing is done once here rather than in the view body: a SwiftUI body can
    /// be evaluated many times per second, and re-parsing 250 ISO dates each time
    /// is the classic way a chart screen turns janky.
    var points: [PricePoint] {
        let cal = Self.formatter
        return zip(dates, prices).compactMap { date, price in
            guard let price, let parsed = cal.date(from: date) else { return nil }
            return PricePoint(date: parsed, price: price)
        }
    }

    /// `yyyy-MM-dd` with a fixed locale and UTC. A user-locale formatter would fail
    /// to parse on a non-Gregorian calendar, which is a real device setting and
    /// would present as an empty chart rather than an error.
    private static let formatter: DateFormatter = {
        let f = DateFormatter()
        f.calendar = Calendar(identifier: .gregorian)
        f.locale = Locale(identifier: "en_US_POSIX")
        f.timeZone = TimeZone(identifier: "UTC")
        f.dateFormat = "yyyy-MM-dd"
        return f
    }()
}

struct PricePoint: Identifiable, Sendable {
    let date: Date
    let price: Double
    var id: Date { date }
}

struct Sector: Decodable, Sendable, Identifiable {
    let ticker: String
    let label: String
    let dayChange: Double?
    let weekChangePct: Double?

    var id: String { ticker }

    enum CodingKeys: String, CodingKey {
        case ticker, label
        case dayChange = "day_chg"
        case weekChangePct = "week_chg_pct"
    }
}

struct Vix: Decodable, Sendable {
    let current: Double?
    let dayChange: Double?
    let vix3m: Double?
    let vvix: Double?
    let termRatio: Double?

    enum CodingKeys: String, CodingKey {
        case current, vix3m, vvix
        case dayChange = "day_chg"
        case termRatio = "term_ratio"
    }
}

/// The server's own freshness verdict. See freshness.py: `status` is one of
/// `realtime`, `session_close` or `stale`, which is a different question from how
/// old the cache is (`ageLabel`).
struct Meta: Decodable, Sendable {
    let ageLabel: String?
    let ageSeconds: Int?
    let fetchedAt: String?
    let marketOpen: Bool?
    let stale: Bool?
    let status: String?

    enum CodingKeys: String, CodingKey {
        case status, stale
        case ageLabel = "age_label"
        case ageSeconds = "age_seconds"
        case fetchedAt = "fetched_at"
        case marketOpen = "market_open"
    }
}
