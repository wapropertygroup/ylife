import Foundation
import OSLog

/// Request logging, readable with
/// `log stream --predicate 'subsystem == "com.trade-agents.TradeAgents"'`.
///
/// A GUI app's `print` goes nowhere once it is launched by Finder or `open`, which is
/// how it is actually run — so a failure that only reproduces outside Xcode is
/// otherwise invisible. Status codes are logged because this API answers 202 to mean
/// "not ready", and telling that apart from a genuine failure is the single most
/// common question when a screen shows the warming state.
let apiLog = Logger(subsystem: "com.trade-agents.TradeAgents", category: "api")

/// Which deployment the app talks to.
///
/// `trade-agents.com` and `stock.li-family.us` are the *same* Flask app behind two
/// nginx vhosts — the former proxies every path to ystocker:8000 rather than
/// redirecting, so the API is identical on both. The default is the brand host
/// because that is the one this app is named after; the alternate is kept because it
/// is the host the backend's own notes record as verified from a development machine,
/// and having somewhere to switch to is what makes a DNS or certificate problem
/// diagnosable from inside the app instead of looking like the app being broken.
enum AppHost: String, CaseIterable, Identifiable, Sendable {
    case tradeAgents = "trade-agents.com"
    case liFamily = "stock.li-family.us"

    var id: String { rawValue }
    var url: URL { URL(string: "https://\(rawValue)")! }

    var label: String {
        switch self {
        case .tradeAgents: return "trade-agents.com"
        case .liFamily:    return "stock.li-family.us"
        }
    }
}

/// Small persisted preferences. Deliberately not a `@Observable` model: the host is
/// read by an actor on a background task, and routing that through main-actor state
/// would mean either a hop per request or an isolation violation.
enum AppSettings {
    private static let hostKey = "TA.host"

    static var host: AppHost {
        get {
            guard let raw = UserDefaults.standard.string(forKey: hostKey),
                  let hit = AppHost(rawValue: raw) else { return .tradeAgents }
            return hit
        }
        set { UserDefaults.standard.set(newValue.rawValue, forKey: hostKey) }
    }

    static var baseURL: URL { host.url }
}

/// Talks to the existing Flask backend. There is no new server: every screen reads
/// the same `/api/*` endpoints the web dashboards do.
actor APIClient {
    static let shared = APIClient()

    private let session: URLSession

    init() {
        let config = URLSessionConfiguration.default
        // The dashboards are cache-peeking endpoints that can still take seconds on a
        // cold cache. No request should hang forever, though: the equivalent mistake
        // on the server side (yf.Ticker with no timeout) parked a background thread
        // indefinitely with nothing in the log.
        config.timeoutIntervalForRequest = 30
        config.waitsForConnectivity = true
        // URLSession keeps cookies by default, which is what the existing session auth
        // needs: /api/auth/google verifies a Google ID token and replies with a Flask
        // session cookie, so a native sign-in can reuse that endpoint unchanged.
        config.httpCookieAcceptPolicy = .always
        config.httpShouldSetCookies = true
        // Never let the URL cache answer a market-data request. Same reasoning as the
        // service worker on the web side: a plausible-looking stale quote with no
        // indication is worse than a visible failure.
        config.requestCachePolicy = .reloadIgnoringLocalCacheData
        config.urlCache = nil
        self.session = URLSession(configuration: config)
    }

    // MARK: - Dashboards

    func markets() async throws -> MarketsResponse { try await get("/api/markets") }
    func fedwatch() async throws -> FedWatch { try await get("/api/fedwatch") }
    func fearGreed() async throws -> FearGreed { try await get("/api/fear-greed") }

    /// The research dashboards. All four are public and unauthenticated, exactly
    /// as their web pages are, so none needs a session.
    func fed() async throws -> FedResponse { try await get("/api/fed") }
    func multiples() async throws -> Multiples { try await get("/api/multiples") }
    func dca() async throws -> DcaList { try await get("/api/dca") }
    func yieldCurves() async throws -> YieldCurves { try await get("/api/yield-curve") }
    func yieldSpread() async throws -> YieldSpread { try await get("/api/yield-spread") }
    func breadth() async throws -> Breadth { try await get("/api/breadth") }
    func putCall() async throws -> PutCall { try await get("/api/put-call-ratio") }
    func skew() async throws -> Skew { try await get("/api/skew") }
    func commodities() async throws -> CommoditiesResponse { try await get("/api/commodities") }

    /// One fund's latest 13F. The slug is the fund name lowercased with spaces
    /// hyphenated — percent-encoded here anyway, because a name with a dot in it
    /// ("T. Rowe Price") would otherwise produce a path segment the router reads
    /// differently from the one intended.
    func thirteenF(slug: String) async throws -> ThirteenF {
        let safe = slug.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? slug
        return try await get("/api/13f/\(safe)")
    }

    // MARK: - Agents

    func showcase() async throws -> ShowcaseList { try await get("/api/agents/showcase") }

    func showcaseReport(id: String) async throws -> ShowcaseReport {
        let safe = id.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? id
        return try await get("/api/agents/showcase/\(safe)")
    }

    /// Queues a run. Answers 202 with a job id to poll, or 401 when signed out.
    ///
    /// Deliberately sends no `model` or `thinking`. routes.py treats both as absent-
    /// means-default, so omitting them runs the deployment's configured models — and
    /// the alternative, a model picker, would mean a second copy of
    /// `agent_models.CHOICES` living in Swift. That table carries each choice's
    /// accepted thinking levels, which differ per model and are clamped server-side;
    /// a stale client copy would offer a level the server silently rewrites, so the UI
    /// would be lying about what ran. Exposing `agent_models.options()` on a small
    /// `/api/agents/models` route is the right fix, and it is server work.
    func submitRun(ticker: String, date: String, language: String = "en") async throws -> RunAccepted {
        try await post("/api/agents/run", body: [
            "ticker": ticker,
            "date": date,
            "lang": language,
        ], accepts202: true)
    }

    /// Polls one run. Owner-or-VIP; 401 when signed out.
    func job(id: String) async throws -> AgentJob {
        let safe = id.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? id
        return try await get("/api/agents/job/\(safe)")
    }

    // MARK: - Auth

    func me() async throws -> AuthEnvelope { try await get("/api/auth/me") }

    @discardableResult
    func logout() async throws -> Bool {
        let _: EmptyResponse = try await post("/api/auth/logout", body: [String: String]())
        return true
    }

    /// Exchanges a Google ID token for a Flask session cookie.
    ///
    /// Unused until the app has an iOS OAuth client ID (see apps/README.md) — kept
    /// because the *server* side genuinely needs no change for native sign-in, and
    /// writing the call now is what makes that claim checkable rather than a guess.
    func signInWithGoogle(idToken: String) async throws -> AuthEnvelope {
        try await post("/api/auth/google", body: ["credential": idToken])
    }

    // MARK: - Transport

    private func get<T: Decodable>(_ path: String) async throws -> T {
        try await send(request(path, method: "GET"))
    }

    private func post<T: Decodable>(
        _ path: String,
        body: some Encodable,
        accepts202: Bool = false
    ) async throws -> T {
        var req = request(path, method: "POST")
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try JSONEncoder().encode(body)
        return try await send(req, accepts202: accepts202)
    }

    private func request(_ path: String, method: String) -> URLRequest {
        // Resolved per request rather than captured at init: the host is switchable at
        // runtime from Settings, and an actor built once at launch would otherwise go
        // on talking to the old one until the app was relaunched.
        let url = URL(string: path, relativeTo: AppSettings.baseURL) ?? AppSettings.baseURL
        var req = URLRequest(url: url)
        req.httpMethod = method
        req.setValue("application/json", forHTTPHeaderField: "Accept")
        return req
    }

    /// - Parameter accepts202: whether 202 carries a real payload for this endpoint.
    ///
    ///   202 means two different things in this API and the difference matters. On the
    ///   dashboards it means "cache warming, no payload yet, poll me". On
    ///   `/api/agents/run` it is the *success* response, carrying the job id — so
    ///   treating 202 as warming everywhere would report a successfully queued run as
    ///   a transient server error, and the reader would resubmit a run they had
    ///   already been charged for.
    private func send<T: Decodable>(_ req: URLRequest, accepts202: Bool = false) async throws -> T {
        let (data, response) = try await session.data(for: req)
        guard let http = response as? HTTPURLResponse else { throw APIError.notHTTP }

        apiLog.info("\(req.httpMethod ?? "?", privacy: .public) \(req.url?.absoluteString ?? "?", privacy: .public) -> \(http.statusCode, privacy: .public) (\(data.count, privacy: .public) bytes)")

        if http.statusCode == 202, !accepts202 { throw APIError.warming }

        // The backend answers 429 with a JSON body explaining *which* limit was hit
        // and whether more runs can be bought. Collapsing that into "HTTP 429" would
        // throw away the only actionable part of the response.
        if http.statusCode == 429 {
            throw APIError.rateLimited(detail: Self.errorDetail(data))
        }
        guard (200...299).contains(http.statusCode) else {
            throw APIError.http(status: http.statusCode, detail: Self.errorDetail(data))
        }
        do {
            return try JSONDecoder().decode(T.self, from: data)
        } catch {
            throw APIError.decoding(underlying: error)
        }
    }

    /// Pulls `error` out of a JSON error body, if there is one.
    private static func errorDetail(_ data: Data) -> String? {
        guard let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return nil }
        return object["error"] as? String
    }
}

/// `{"user": {...}}` — note the envelope is always present.
struct AuthEnvelope: Decodable, Sendable {
    let user: AuthUser
}

/// The signed-in user.
///
/// **An anonymous caller still gets a user object**, not null: the server replies
/// `{"email": "", "name": "Anonymous", "picture": ""}`. So "is there a user?" is the
/// wrong question and would report everyone as signed in — the test is a non-empty
/// email, which is also exactly what every gate on the server keys off.
struct AuthUser: Decodable, Sendable, Equatable {
    let email: String
    let name: String
    let picture: String

    var isSignedIn: Bool { !email.isEmpty }

    static let anonymous = AuthUser(email: "", name: "Anonymous", picture: "")
}

struct EmptyResponse: Decodable, Sendable {}

enum APIError: LocalizedError {
    case notHTTP
    case warming
    case rateLimited(detail: String?)
    case http(status: Int, detail: String?)
    case decoding(underlying: Error)

    var errorDescription: String? {
        switch self {
        case .notHTTP:
            return "Unexpected non-HTTP response"
        case .warming:
            return "The server is building this data. Try again shortly."
        case .rateLimited(let detail):
            return detail ?? "Rate limited. Try again later."
        case .http(let status, let detail):
            if let detail, !detail.isEmpty { return detail }
            return "Server returned HTTP \(status)"
        case .decoding(let underlying):
            // Kept verbose deliberately: the payload is loosely typed and evolves with
            // the web dashboards, so a field changing shape is the most likely failure
            // and the least self-evident from a generic message.
            return "Could not read the response: \(underlying)"
        }
    }
}
