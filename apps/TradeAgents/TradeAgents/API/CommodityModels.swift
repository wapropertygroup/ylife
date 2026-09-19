import Foundation

// Decoded from `/api/commodities`. Read off the live response, like the other
// dashboard models — the web page accesses these dynamically and there is no
// schema to copy.

/// One tracked commodity, with everything the endpoint already computes.
///
/// Sixteen of these arrive in one call, each carrying a year and five years of
/// closes, six return horizons, both moving averages, the 52-week range and RSI.
/// The screen shows all of it rather than a price and a day change, because none
/// of it costs a second request: `CLAUDE.md` records what this backend's Yahoo
/// budget does when callers multiply, and the cheapest new information is the
/// information already in a payload being fetched.
struct Commodity: Decodable, Sendable, Identifiable {
    let symbol: String
    let name: String
    let group: String?
    let unit: String?
    let emoji: String?

    let last: Double?
    let prev: Double?
    let dayChg: Double?
    let dayChgPct: Double?

    let ret1w: Double?
    let ret1m: Double?
    let ret3m: Double?
    let ret6m: Double?
    let retYtd: Double?
    let ret1y: Double?

    let ma50: Double?
    let ma200: Double?
    let aboveMa50: Bool?
    let aboveMa200: Bool?

    let high52: Double?
    let low52: Double?
    /// Where the last price sits in the 52-week range, 0-100.
    let pos52: Double?
    let rsi14: Double?

    let dates1y: [String]
    let prices1y: [Double?]
    let dates5y: [String]
    let prices5y: [Double?]

    var id: String { symbol }

    /// Zipped and gap-dropped once, not per body evaluation. Same reasoning as
    /// `FedSeries.points`: a SwiftUI body runs many times a second and re-parsing
    /// a year of ISO dates inside one is how a chart screen turns janky.
    func points(fiveYear: Bool) -> [PricePoint] {
        let dates = fiveYear ? dates5y : dates1y
        let prices = fiveYear ? prices5y : prices1y
        return zip(dates, prices).compactMap { date, price in
            guard let price, let parsed = DateParse.iso(date) else { return nil }
            return PricePoint(date: parsed, price: price)
        }
    }

    enum CodingKeys: String, CodingKey {
        case symbol, name, group, unit, emoji, last, prev
        case dayChg = "day_chg"
        case dayChgPct = "day_chg_pct"
        case ret1w = "ret_1w"
        case ret1m = "ret_1m"
        case ret3m = "ret_3m"
        case ret6m = "ret_6m"
        case retYtd = "ret_ytd"
        case ret1y = "ret_1y"
        case ma50 = "ma_50"
        case ma200 = "ma_200"
        case aboveMa50 = "above_ma_50"
        case aboveMa200 = "above_ma_200"
        case high52, low52, pos52
        case rsi14 = "rsi_14"
        case dates1y = "dates_1y"
        case prices1y = "prices_1y"
        case dates5y = "dates_5y"
        case prices5y = "prices_5y"
    }
}

/// A cross-commodity ratio — gold/silver, gold/oil — with its own history.
///
/// These earn a card of their own rather than being derivable on the client: the
/// endpoint already aligns the two series by date, and doing that here would mean
/// reimplementing the alignment and getting a different answer on any day one of
/// the two did not trade.
struct CommodityRatio: Decodable, Sendable, Identifiable {
    let label: String
    let desc: String?
    let current: Double?
    let dates: [String]
    let values: [Double?]

    var id: String { label }

    var points: [PricePoint] {
        zip(dates, values).compactMap { date, value in
            guard let value, let parsed = DateParse.iso(date) else { return nil }
            return PricePoint(date: parsed, price: value)
        }
    }
}

struct CommoditiesResponse: Decodable, Sendable {
    let commodities: [String: Commodity]
    let ratios: [String: CommodityRatio]
    let ts: Double?

    /// Grouped for display, in a fixed order.
    ///
    /// The payload is a dictionary, so iterating it directly would reshuffle the
    /// screen on every refresh — the same trap `MarketsView.preferredOrder` exists
    /// for. A group the server adds that is not listed here is appended rather
    /// than dropped, so new data shows up without a client release.
    static let groupOrder = ["energy", "metals", "industrial", "agri", "macro"]

    func grouped() -> [(String, [Commodity])] {
        var buckets: [String: [Commodity]] = [:]
        for item in commodities.values {
            buckets[item.group ?? "other", default: []].append(item)
        }
        for key in buckets.keys {
            buckets[key]?.sort { ($0.name) < ($1.name) }
        }
        var out: [(String, [Commodity])] = []
        for key in Self.groupOrder {
            if let hit = buckets.removeValue(forKey: key) { out.append((key, hit)) }
        }
        for key in buckets.keys.sorted() {
            out.append((key, buckets[key] ?? []))
        }
        return out
    }
}
