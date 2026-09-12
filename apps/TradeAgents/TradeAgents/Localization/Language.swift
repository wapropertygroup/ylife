import Foundation
import Observation

/// The two languages the whole product speaks.
///
/// Matched to the web app, which ships EN + ZH in `static/i18n.js` and toggles with
/// `I18n.toggle()`. An explicit in-app toggle rather than following the system locale,
/// for the same reason the site has one: a reader whose Mac is in English may well
/// want the Chinese copy, and the report they run is written in the language they
/// chose — `/api/agents/run` takes `lang`, and the choice is frozen onto the job.
enum Language: String, CaseIterable, Identifiable, Sendable {
    case en
    case zh

    var id: String { rawValue }

    /// The name of each language *in that language*, which is the convention a
    /// language picker has to follow — "Chinese" is no use to somebody who cannot
    /// read the current language.
    var endonym: String {
        switch self {
        case .en: return "English"
        case .zh: return "中文"
        }
    }

    /// Used for dates and numbers, so a Chinese reader gets 2026年8月28日 rather than
    /// Aug 28, 2026 under Chinese UI copy.
    var locale: Locale {
        switch self {
        case .en: return Locale(identifier: "en_US")
        case .zh: return Locale(identifier: "zh_Hans_CN")
        }
    }

    /// What to send as `lang` on `/api/agents/run`.
    var apiCode: String { rawValue }

    /// The best match for the system's preferred languages, used the first time the
    /// app runs and never again — after that the reader's explicit choice wins.
    static var systemDefault: Language {
        for code in Locale.preferredLanguages {
            if code.hasPrefix("zh") { return .zh }
            if code.hasPrefix("en") { return .en }
        }
        return .en
    }
}

/// A string in both languages.
///
/// A struct with two non-optional fields rather than two parallel dictionaries. The
/// web side needs a *test* to assert that neither language has a key the other lacks
/// (CLAUDE.md records this for the DCA band and factor strings, which are composed in
/// JS where `I18n.apply()` cannot reach them). Here the compiler enforces it: there is
/// no way to write one half and forget the other.
struct LocalizedString: Sendable, Hashable {
    let en: String
    let zh: String

    init(_ en: String, _ zh: String) {
        self.en = en
        self.zh = zh
    }

    func callAsFunction(_ language: Language) -> String { value(in: language) }

    func value(in language: Language) -> String {
        switch language {
        case .en: return en
        case .zh: return zh
        }
    }
}

/// The current language, and the thing views read to resolve a string.
@Observable
@MainActor
final class Localization {
    private static let key = "TA.language"

    var language: Language {
        didSet {
            guard language != oldValue else { return }
            UserDefaults.standard.set(language.rawValue, forKey: Self.key)
            Format.locale = language.locale
        }
    }

    init() {
        let stored = UserDefaults.standard.string(forKey: Self.key)
        language = stored.flatMap(Language.init(rawValue:)) ?? .systemDefault
        Format.locale = language.locale
    }

    /// `loc(.someString)` at the call site.
    func callAsFunction(_ string: LocalizedString) -> String { string.value(in: language) }

    /// Picks between a server-supplied English string and its Chinese counterpart.
    ///
    /// The agents API already ships both — `role.name` / `role.zh`, `team` / `team_zh`
    /// — and the app ignored both Chinese fields before this existed. Falls back to
    /// whichever is present rather than showing an empty label, because a role with no
    /// name renders as a coloured bar with nothing beside it.
    func pick(_ english: String?, _ chinese: String?) -> String? {
        let preferred = language == .zh ? chinese : english
        let fallback = language == .zh ? english : chinese
        for candidate in [preferred, fallback] {
            if let candidate, !candidate.trimmingCharacters(in: .whitespaces).isEmpty {
                return candidate
            }
        }
        return nil
    }
}
