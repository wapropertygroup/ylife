import SwiftUI

/// Formatting helpers.
///
/// An absent value prints as an em dash rather than 0. The payload is full of
/// legitimate nulls — a commodity has no P/E, a session with no print has no price —
/// and rendering those as zero would be a false statement about the instrument
/// rather than a cosmetic one.
enum Format {
    /// The locale dates and numbers are rendered in.
    ///
    /// Set by `Localization` when the reader changes language, rather than threaded
    /// through every call site: a date appears in a dozen views and adding a `locale:`
    /// argument to each would be noise at every one of them. The cost is a piece of
    /// mutable global state — acceptable because it has exactly one writer, and the
    /// alternative (Chinese UI copy above `Aug 28, 2026`) is the visible bug.
    static var locale: Locale = .autoupdatingCurrent

    static func price(_ value: Double?) -> String {
        guard let value else { return "—" }
        return value.formatted(.number.precision(.fractionLength(2)).locale(locale))
    }

    static func number(_ value: Double?, places: Int = 1) -> String {
        guard let value else { return "—" }
        return value.formatted(.number.precision(.fractionLength(places)).locale(locale))
    }

    /// A large whole number with thousands separators and no decimals.
    ///
    /// Used where the *unit* is not reliably known — a 13F's reported value is
    /// labelled "millions" by the endpoint and is plainly not — so the figure is
    /// shown as the filing states it rather than scaled into a suffix. Printing
    /// "$299T" from a misread unit is a fabricated number; a grouped integer is
    /// just the filing's own.
    static func grouped(_ value: Double?) -> String {
        guard let value else { return "—" }
        return value.formatted(.number.precision(.fractionLength(0)).locale(locale))
    }

    static func signedPercent(_ value: Double?, places: Int = 2) -> String {
        guard let value else { return "—" }
        let sign = value >= 0 ? "+" : ""
        return sign + value.formatted(.number.precision(.fractionLength(places)).locale(locale)) + "%"
    }

    static func percent(_ value: Double?, places: Int = 1) -> String {
        guard let value else { return "—" }
        return value.formatted(.number.precision(.fractionLength(places)).locale(locale)) + "%"
    }

    static func tint(_ value: Double?) -> Color {
        guard let value else { return Palette.secondaryText }
        return value >= 0 ? Palette.up : Palette.down
    }

    /// An ISO-8601 timestamp from the backend, rendered in the reader's locale.
    ///
    /// Both spellings are tried because the API is not consistent: `created_at` on a
    /// job carries a `+00:00` offset while some cache stamps do not, and
    /// `ISO8601DateFormatter` fails rather than degrades when the option set does not
    /// match the string exactly.
    static func timestamp(_ raw: String?) -> String {
        guard let date = parseISO(raw) else { return raw ?? "—" }
        return date.formatted(Date.FormatStyle(date: .abbreviated, time: .shortened)
            .locale(locale))
    }

    static func day(_ raw: String?) -> String {
        guard let raw else { return "—" }
        guard let date = Self.dayParser.date(from: raw) else { return raw }
        return date.formatted(Date.FormatStyle(date: .abbreviated, time: .omitted)
            .locale(locale))
    }

    /// A short day label for a chart axis — month and day, no year.
    /// Exists because Swift Charts formats a date axis from the *system* locale,
    /// which this app's language toggle deliberately does not touch. A reader on
    /// Chinese therefore got `Aug 23` under a fully translated chart title, on the
    /// one element of the screen no `loc()` call reaches. The year is dropped
    /// because the surrounding series already establishes it and a three-part date
    /// is wide enough to force Charts to drop half the labels.
    static func axisDay(_ date: Date) -> String {
        date.formatted(.dateTime.month(.abbreviated).day().locale(locale))
    }

    /// Month and year, from a `yyyy-MM-dd` string.
    ///
    /// For the FOMC meeting cards, whose `label` the server builds with
    /// `strftime("%b %Y")` — English on every request, with no locale to negotiate.
    /// That label is derived entirely from the same date sent alongside it, so
    /// rebuilding it here loses no information and gains the reader's language;
    /// `nil` when the date will not parse, so the caller can fall back to the
    /// server's string rather than print nothing.
    static func monthYear(_ raw: String?) -> String? {
        guard let raw, let date = Self.dayParser.date(from: raw) else { return nil }
        return date.formatted(.dateTime.year().month(.abbreviated).locale(locale))
    }

    static func parseISO(_ raw: String?) -> Date? {
        guard let raw, !raw.isEmpty else { return nil }
        if let hit = internetDateTime.date(from: raw) { return hit }
        return withFractionalSeconds.date(from: raw)
    }

    /// A duration in seconds as "18m 04s". Used for how long a run took, where the
    /// figure is minutes-to-an-hour and a bare second count is unreadable.
    static func elapsed(_ seconds: Double?) -> String {
        guard let seconds, seconds > 0 else { return "—" }
        let total = Int(seconds.rounded())
        let h = total / 3600, m = (total % 3600) / 60, s = total % 60
        if h > 0 { return String(format: "%dh %02dm", h, m) }
        return String(format: "%dm %02ds", m, s)
    }

    private static let internetDateTime: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime]
        return f
    }()

    /// Tried second. `ISO8601DateFormatter` fails rather than degrades when the option
    /// set does not match the string exactly, so a stamp carrying milliseconds needs
    /// its own formatter — one parser would reject half the timestamps in this API.
    private static let withFractionalSeconds: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return f
    }()

    /// `yyyy-MM-dd` with a fixed locale and UTC. A user-locale formatter would fail to
    /// parse under a non-Gregorian calendar — a real device setting — and the symptom
    /// would be a blank date rather than an error.
    private static let dayParser: DateFormatter = {
        let f = DateFormatter()
        f.calendar = Calendar(identifier: .gregorian)
        f.locale = Locale(identifier: "en_US_POSIX")
        f.timeZone = TimeZone(identifier: "UTC")
        f.dateFormat = "yyyy-MM-dd"
        return f
    }()
}
