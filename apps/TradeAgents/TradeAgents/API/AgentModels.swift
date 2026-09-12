import Foundation

// Decoded from the /agents endpoints.
//
// The two showcase routes are the only part of /agents that needs no sign-in, which
// is what makes a working report reader possible in this app today: submitting a run
// needs a Google iOS OAuth client ID that can only be created in a web console, but
// *reading* finished reports needs nothing. See routes.py — `api_agents_showcase`
// and `api_agents_showcase_job` are deliberately unauthenticated.

/// `GET /api/agents/showcase`
struct ShowcaseList: Decodable, Sendable {
    let jobs: [AgentJob]
    let enabled: Bool
}

/// A run, as published to an unauthenticated reader.
///
/// Mirrors `agents._PUBLIC_FIELDS`. Everything past the first four is optional
/// because that tuple has grown over time and older records simply lack the newer
/// keys — a job written before the model picker existed carries no `provider`, and
/// `job_models()` turns that absence back into the deployment default rather than
/// treating it as an error.
struct AgentJob: Decodable, Sendable, Identifiable, Hashable {
    let id: String
    let ticker: String
    let date: String?
    let status: String?
    let decision: String?

    let createdAt: String?
    let startedAt: String?
    let finishedAt: String?
    let elapsedSec: Double?

    /// Whether the run had to fall back to a cheaper model, or recovered from a
    /// failure. Published for a specific reason, recorded in routes.py: a sampled
    /// report must not be able to pass itself off as better than it is.
    let degraded: Bool?
    let recovered: Bool?
    let fallbackModels: [String]?

    let provider: String?
    let deepModel: String?
    let quickModel: String?
    let thinking: String?

    let hasReport: Bool?

    enum CodingKeys: String, CodingKey {
        case id, ticker, date, status, decision, degraded, recovered, provider, thinking
        case createdAt = "created_at"
        case startedAt = "started_at"
        case finishedAt = "finished_at"
        case elapsedSec = "elapsed_sec"
        case fallbackModels = "fallback_models"
        case deepModel = "deep_model"
        case quickModel = "quick_model"
        case hasReport = "has_report"
    }
}

/// `GET /api/agents/showcase/<id>` — one report, already split into agent turns.
///
/// The split is done server-side by `agent_roles.split_sections`, and that is worth
/// keeping: the awkward cases (a preamble with no role, a team divider, a model that
/// emitted its heading in Chinese) have one implementation there rather than a second
/// one here that would drift.
struct ShowcaseReport: Decodable, Sendable {
    let id: String
    let ticker: String
    let date: String?
    let status: String?
    let decision: String?
    let createdAt: String?
    let finishedAt: String?
    let hasReport: Bool?
    let sections: [ReportSection]

    enum CodingKeys: String, CodingKey {
        case id, ticker, date, status, decision, sections
        case createdAt = "created_at"
        case finishedAt = "finished_at"
        case hasReport = "has_report"
    }

    /// Stamps each section with its position.
    ///
    /// Done here rather than left to the views because `ReportSection.id` defaults to
    /// 0, so a decoded report would otherwise hand `ForEach` sixteen sections that all
    /// claim the same identity — which SwiftUI resolves by rendering one of them and
    /// silently dropping the rest. Keying on the role instead is not an option: a role
    /// legitimately takes more than one turn in a report.
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(String.self, forKey: .id)
        ticker = try c.decode(String.self, forKey: .ticker)
        date = try c.decodeIfPresent(String.self, forKey: .date)
        status = try c.decodeIfPresent(String.self, forKey: .status)
        decision = try c.decodeIfPresent(String.self, forKey: .decision)
        createdAt = try c.decodeIfPresent(String.self, forKey: .createdAt)
        finishedAt = try c.decodeIfPresent(String.self, forKey: .finishedAt)
        hasReport = try c.decodeIfPresent(Bool.self, forKey: .hasReport)
        let decoded = try c.decodeIfPresent([ReportSection].self, forKey: .sections) ?? []
        sections = decoded.enumerated().map { index, section in
            var copy = section
            copy.id = index
            return copy
        }
    }
}

/// One agent's turn.
///
/// `role` and `team` are both nullable and the first section usually has neither —
/// it is the report's preamble. Rendering that as a turn by an agent called "null"
/// is the obvious failure, so `AgentRole?` is honoured rather than defaulted.
struct ReportSection: Decodable, Sendable, Identifiable {
    let body: String
    let role: AgentRole?
    let team: String?
    let teamZh: String?

    /// Positional, assigned after decode. The payload carries no per-section id, and
    /// two turns by the same role in one report are legitimate, so keying a ForEach
    /// on the role would collapse them.
    var id: Int = 0

    enum CodingKeys: String, CodingKey {
        case body, role, team
        case teamZh = "team_zh"
    }
}

/// `POST /api/agents/run` → 202.
struct RunAccepted: Decodable, Sendable {
    let jobId: String
    let status: String?

    enum CodingKeys: String, CodingKey {
        case status
        case jobId = "job_id"
    }
}

/// The agent that wrote a section. Every field is served by `agent_roles.py`,
/// including the colour — so the app's role colours cannot drift from the web app's
/// or the completion email's.
struct AgentRole: Decodable, Sendable, Hashable {
    let key: String
    let name: String
    let short: String?
    let zh: String?
    let icon: String?
    let color: String?
    let group: String?
}
