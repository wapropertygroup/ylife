import Foundation

// Decoded from the dashboard endpoints behind the 13F, Fed, Valuation and DCA
// screens. Read off the live responses, like `Models.swift`, because the web
// pages access these dynamically and there is no schema to copy.
//
// The same rule applies throughout: almost every numeric is optional. A fund can
// file with no comparable prior quarter, a FRED series can be mid-revision, and a
// DCA row can be unscorable — the engine returns `V: null` rather than a zero, and
// flattening that to 0 here would turn "could not be measured" into "at its most
// expensive ever", which is the one mistake the server takes care never to make.

// MARK: - 13F

struct ThirteenF: Decodable, Sendable {
    let cik: String?
    let filingDate: String?
    let periodOfReport: String?
    let totalHoldings: Int?
    let totalValueMillions: Double?
    let holdings: [Holding]
    let error: String?

    enum CodingKeys: String, CodingKey {
        case cik, holdings, error
        case filingDate = "filing_date"
        case periodOfReport = "period_of_report"
        case totalHoldings = "total_holdings"
        case totalValueMillions = "total_value_millions"
    }
}

struct Holding: Decodable, Sendable, Identifiable {
    let ticker: String?
    let name: String
    let cusip: String?
    let pctPortfolio: Double?
    let valueMillions: Double?
    let shares: Double?
    let change: String?
    let changePct: Double?

    /// CUSIP is the stable key; a fund can hold two share classes of one issuer
    /// and `name` would collide. Falls back to the name so a row with no CUSIP
    /// still renders rather than being silently merged with its neighbour.
    var id: String { cusip ?? name }

    enum CodingKeys: String, CodingKey {
        case ticker, name, cusip, shares, change
        case pctPortfolio = "pct_portfolio"
        case valueMillions = "value_millions"
        case changePct = "change_pct"
    }
}

// MARK: - Fed

struct FedResponse: Decodable, Sendable {
    let status: String?
    let series: [String: FedSeries]
    let meta: Meta?
}

/// A FRED series as parallel arrays, which is how the endpoint sends it.
struct FedSeries: Decodable, Sendable {
    let dates: [String]
    let values: [Double?]

    /// Zipped and gap-dropped, parsed once. Same reasoning as `Series.points`: a
    /// SwiftUI body runs many times a second and re-parsing 1,200 ISO dates in it
    /// is how a chart screen turns janky.
    var points: [PricePoint] {
        zip(dates, values).compactMap { date, value in
            guard let value, let parsed = DateParse.iso(date) else { return nil }
            return PricePoint(date: parsed, price: value)
        }
    }
}

// MARK: - Valuation (index multiples)

struct Multiples: Decodable, Sendable {
    let status: String?
    let asOf: String?
    let headline: MultiplesHeadline?
    let forwardHistory: [ForwardPoint]
    let meta: Meta?

    enum CodingKeys: String, CodingKey {
        case status, headline, meta
        case asOf = "as_of"
        case forwardHistory = "forward_history"
    }
}

struct MultiplesHeadline: Decodable, Sendable {
    let spxCape: HeadlineMetric?
    let spxCapePercentile: HeadlineMetric?
    let spxFwdRealized: HeadlineMetric?
    let qqqForwardPe: HeadlineMetric?
    let soxForwardPe: HeadlineMetric?
    let n225ForwardPe: HeadlineMetric?

    enum CodingKeys: String, CodingKey {
        case spxCape = "spx_cape"
        case spxCapePercentile = "spx_cape_percentile"
        case spxFwdRealized = "spx_fwd_realized"
        case qqqForwardPe = "qqq_forward_pe"
        case soxForwardPe = "sox_forward_pe"
        case n225ForwardPe = "n225_forward_pe"
    }
}

/// One headline figure and the provenance the endpoint ships with it.
///
/// Modelled as an object because that is what it is. The first version here
/// assumed a bare `Double`, compiled, and failed at runtime with a type
/// mismatch — a mistake invisible until the screen was actually rendered.
///
/// `coveragePct` and `constituents` earn their place: a QQQ forward P/E computed
/// from 55 of 97 members is a weaker claim than a Nikkei one from 40 of 40, and
/// showing the number without the coverage presents them as equals.
struct HeadlineMetric: Decodable, Sendable {
    let value: Double?
    let asOf: String?
    let source: String?
    let unit: String?
    let yoy: Double?
    let coveragePct: Double?
    let constituents: String?

    enum CodingKeys: String, CodingKey {
        case value, source, unit, yoy, constituents
        case asOf = "as_of"
        case coveragePct = "coverage_pct"
    }
}

/// One dated row of the banked forward-P/E series. SPY and QQQ are the two the
/// server has banked longest; the rest of the row is ignored rather than modelled,
/// because a key added upstream must not fail decoding of the whole screen.
struct ForwardPoint: Decodable, Sendable, Identifiable {
    let date: String
    let spy: Double?
    let qqq: Double?

    var id: String { date }
    var parsedDate: Date? { DateParse.iso(date) }

    enum CodingKeys: String, CodingKey { case date, spy = "SPY", qqq = "QQQ" }
}

// MARK: - DCA

struct DcaList: Decodable, Sendable {
    let rows: [DcaRow]
    let scored: Int?
    let universe: Int?
    let baseDca: Double?
    let maxMultiplier: Double?
    let warming: Bool?
    let dcfEnabled: Bool?

    enum CodingKeys: String, CodingKey {
        case rows, scored, universe, warming
        case baseDca = "base_dca"
        case maxMultiplier = "max_multiplier"
        case dcfEnabled = "dcf_enabled"
    }
}

struct DcaRow: Decodable, Sendable, Identifiable {
    let ticker: String
    let name: String?
    let sector: String?
    let model: String?
    let v: Double?
    let vRel: Double?
    let vDcf: Double?
    let wDcf: Double?
    let blended: Bool?
    let band: String?
    let mValuation: Double?
    let mEarnings: Double?
    let mPortfolio: Double?
    let multiplier: Double?
    let amount: Double?
    let capped: Bool?
    let years: Double?
    let vintages: Int?
    let stale: Bool?

    var id: String { ticker }

    enum CodingKeys: String, CodingKey {
        case ticker, name, sector, model, band, multiplier, amount, capped
        case years, vintages, stale, blended
        case v = "V"
        case vRel = "V_rel"
        case vDcf = "V_dcf"
        case wDcf = "w_dcf"
        case mValuation = "m_valuation"
        case mEarnings = "m_earnings"
        case mPortfolio = "m_portfolio"
    }
}

// MARK: - Dated series

/// Parallel `dates` / `values` arrays, which is how most of these endpoints ship a
/// series. Shared rather than re-declared per payload.
struct DateSeries: Decodable, Sendable {
    let dates: [String]
    let values: [Double?]

    /// Zipped and gap-dropped, parsed once — a SwiftUI body runs many times a second
    /// and re-parsing a few hundred ISO dates inside one is how a chart screen turns
    /// janky. A null value is dropped rather than plotted as zero: these are yields
    /// and percentages where 0 is a real, and very wrong, reading.
    var points: [PricePoint] {
        zip(dates, values).compactMap { date, value in
            guard let value, let parsed = DateParse.iso(date) else { return nil }
            return PricePoint(date: parsed, price: value)
        }
    }

    /// The last `count` points. Most of these series run to 500-2,500 observations
    /// and the recent window is what a reader is asking about.
    func tail(_ count: Int) -> [PricePoint] {
        let all = points
        return all.count > count ? Array(all.suffix(count)) : all
    }
}

// MARK: - Yield curves

struct YieldCurves: Decodable, Sendable {
    let us: YieldCurve?
    let cn: YieldCurve?
    let jp: YieldCurve?
    let spxPe: Double?

    enum CodingKeys: String, CodingKey {
        case us, cn, jp
        case spxPe = "spx_pe"
    }
}

struct YieldCurve: Decodable, Sendable {
    let current: [String: Double]
    let history10y: DateSeries?
    let spread10y3m: Double?

    enum CodingKeys: String, CodingKey {
        case current
        case history10y = "history_10y"
        case spread10y3m = "spread_10y_3m"
    }

    /// The curve, short end first.
    ///
    /// The ordering is the whole chart, and the obvious spelling of it is wrong in a
    /// way that still draws. `current` is keyed by tenor *strings* — `3M`, `6M`,
    /// `1Y` … `30Y` — and sorting those as text puts `10Y` before `1Y`, `20Y` before
    /// `2Y`, and `3M` after `30Y`. The result is not an error or an empty chart: it
    /// is a plausible-looking line through the right values in the wrong order, on
    /// the one chart whose entire meaning is the shape of that line. So each label is
    /// converted to a number of months and sorted on that, and a tenor that does not
    /// parse is dropped rather than sorted to an arbitrary position.
    var ordered: [(label: String, months: Int, yield: Double)] {
        current.compactMap { label, yield in
            guard let months = Self.months(label) else { return nil }
            return (label, months, yield)
        }
        .sorted { $0.months < $1.months }
    }

    /// `3M` → 3, `10Y` → 120. Nil for anything else, which is how an unexpected key
    /// stays out of the chart instead of landing at position zero.
    static func months(_ tenor: String) -> Int? {
        let text = tenor.uppercased().trimmingCharacters(in: .whitespaces)
        guard let unit = text.last, let n = Int(text.dropLast()) else { return nil }
        switch unit {
        case "M": return n
        case "Y": return n * 12
        default:  return nil
        }
    }
}

/// The 10Y-3M spread, with the recession flag the endpoint ships beside it.
struct YieldSpread: Decodable, Sendable {
    let dates: [String]
    let spread: [Double?]
    let recession: [Int?]
}

// MARK: - Breadth

struct Breadth: Decodable, Sendable {
    let asof: String?
    let latest: [String: Double]
    let maPeriods: [Int]
    let pctAboveMa: [String: DateSeries]
    let rspSpy: DateSeries?
    let universe: Int?
    let stale: Bool?

    enum CodingKeys: String, CodingKey {
        case asof, latest, universe, stale
        case maPeriods = "ma_periods"
        case pctAboveMa = "pct_above_ma"
        case rspSpy = "rsp_spy"
    }

    /// `latest` keyed by an *integer-valued string*, so the same sort trap as the
    /// yield curve: "100" sorts before "20" as text.
    var orderedLatest: [(period: Int, pct: Double)] {
        latest.compactMap { key, value in Int(key).map { ($0, value) } }
            .sorted { $0.0 < $1.0 }
    }
}

// MARK: - Put/call ratio

struct PutCall: Decodable, Sendable {
    let dates: [String]
    let closes: [Double?]
    let current: Double?
    let dayChg: Double?
    let ma20: Double?

    enum CodingKeys: String, CodingKey {
        case dates, closes, current, ma20
        case dayChg = "day_chg"
    }

    var series: DateSeries { DateSeries(dates: dates, values: closes) }
}

// MARK: - SKEW

struct Skew: Decodable, Sendable {
    let dates: [String]
    let skew: [Double?]
    let vix: [Double?]
    let latest: SkewLatest?

    var skewSeries: DateSeries { DateSeries(dates: dates, values: skew) }
    var vixSeries: DateSeries { DateSeries(dates: dates, values: vix) }
}

struct SkewLatest: Decodable, Sendable {
    let band: String?
    let percentile: Double?
    let skew: Double?
    let skewDate: String?
    let vix: Double?
    let vvix: Double?

    enum CodingKeys: String, CodingKey {
        case band, percentile, skew, vix, vvix
        case skewDate = "skew_date"
    }
}

// MARK: - Shared date parsing
/// `yyyy-MM-dd` with a fixed locale and UTC, shared by every model here.
///
/// One formatter, not one per type: `DateFormatter` is expensive to build and
/// these are parsed in bulk. The fixed `en_US_POSIX` locale is not pedantry —
/// a device set to a non-Gregorian calendar parses nothing with a user-locale
/// formatter, and the symptom is an empty chart rather than an error.
enum DateParse {
    static func iso(_ string: String) -> Date? { formatter.date(from: string) }

    private static let formatter: DateFormatter = {
        let f = DateFormatter()
        f.calendar = Calendar(identifier: .gregorian)
        f.locale = Locale(identifier: "en_US_POSIX")
        f.timeZone = TimeZone(identifier: "UTC")
        f.dateFormat = "yyyy-MM-dd"
        return f
    }()
}
