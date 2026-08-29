import Foundation

/// Talks to the existing Flask backend. There is no new server: every screen here
/// reads the same `/api/*` endpoints the web dashboards do.
actor APIClient {
    static let shared = APIClient()

    /// Defaults to the li-family host because that is the one reachable and verified
    /// from this development sandbox. `trade-agents.com` serves the identical app
    /// (nginx proxies every path to ystocker:8000) and is the brand-correct host to
    /// ship against — switch this and re-check, rather than assuming, since that
    /// vhost answers only to its own `server_name`.
    static let baseURL = URL(string: "https://stock.li-family.us")!

    private let session: URLSession

    init() {
        let config = URLSessionConfiguration.default
        // The dashboards are cache-peeking endpoints that can still take seconds on
        // a cold cache. No request should hang forever, though: the equivalent
        // mistake on the server side (yf.Ticker with no timeout) parked a background
        // thread indefinitely with nothing in the log.
        config.timeoutIntervalForRequest = 30
        config.waitsForConnectivity = true
        // URLSession keeps cookies by default, which is what the existing session
        // auth needs: /login verifies a Google ID token and replies with a Flask
        // session cookie, so a native sign-in can reuse that endpoint unchanged.
        config.httpCookieAcceptPolicy = .always
        // Never let the URL cache answer a market-data request. Same reasoning as the
        // service worker on the web side: a plausible-looking stale quote with no
        // indication is worse than a visible failure.
        config.requestCachePolicy = .reloadIgnoringLocalCacheData
        config.urlCache = nil
        self.session = URLSession(configuration: config)
    }

    func markets() async throws -> MarketsResponse {
        try await get("/api/markets")
    }

    private func get<T: Decodable>(_ path: String) async throws -> T {
        guard let url = URL(string: path, relativeTo: Self.baseURL) else {
            throw APIError.badURL(path)
        }
        let (data, response) = try await session.data(from: url)
        guard let http = response as? HTTPURLResponse else {
            throw APIError.notHTTP
        }
        // 202 is a real answer here, not an error: several endpoints reply
        // {"status":"warming"} with no payload while their cache rebuilds, and the
        // web pages poll rather than give up. Surfaced distinctly so a caller can do
        // the same instead of showing a decode failure.
        if http.statusCode == 202 {
            throw APIError.warming
        }
        guard (200...299).contains(http.statusCode) else {
            throw APIError.http(status: http.statusCode)
        }
        do {
            return try JSONDecoder().decode(T.self, from: data)
        } catch {
            throw APIError.decoding(underlying: error)
        }
    }
}

enum APIError: LocalizedError {
    case badURL(String)
    case notHTTP
    case warming
    case http(status: Int)
    case decoding(underlying: Error)

    var errorDescription: String? {
        switch self {
        case .badURL(let path):
            return "Bad URL: \(path)"
        case .notHTTP:
            return "Unexpected non-HTTP response"
        case .warming:
            return "The server is warming its cache. Try again shortly."
        case .http(let status):
            return "Server returned HTTP \(status)"
        case .decoding(let underlying):
            // Kept verbose deliberately: the payload is loosely typed and evolves
            // with the web dashboards, so a field changing shape is the most likely
            // failure and the least self-evident from a generic message.
            return "Could not read the response: \(underlying)"
        }
    }
}
