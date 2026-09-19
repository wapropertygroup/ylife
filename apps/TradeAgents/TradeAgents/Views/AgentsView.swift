import SwiftUI

/// The Agents screen: finished reports, readable without an account.
///
/// This is the sampled feed (`/api/agents/showcase`), which the server publishes
/// deliberately unauthenticated — `api_agents_showcase` in routes.py takes no gate.
/// That is what lets this screen be real rather than a placeholder: the part of
/// `/agents` that needs a Google iOS OAuth client ID is *submitting* a run, not
/// reading one.
struct AgentsView: View {
    @State private var state: LoadState = .loading
    @State private var composing = false
    @Environment(Localization.self) private var loc
    @Environment(AlertCenter.self) private var alerts

    enum LoadState {
        case loading
        case loaded(ShowcaseList)
        case failed(Error)
    }

    var body: some View {
        ZStack {
            Palette.background.ignoresSafeArea()
            content
        }
        .navigationTitle(loc(S.agents))
        .toolbar {
            ToolbarItem(placement: .primaryAction) {
                Button {
                    composing = true
                } label: {
                    Label(loc(S.newRun), systemImage: "plus.circle")
                }
            }
        }
        .sheet(isPresented: $composing) { RunComposer() }
        .task { await load() }
    }

    @ViewBuilder
    private var content: some View {
        switch state {
        case .loading:
            LoadingPane(label: loc(S.loadingReports))

        case .failed(let error):
            LoadFailure(error: error) { Task { await load() } }

        case .loaded(let list) where !list.enabled:
            // The server can turn the sample off entirely (`showcase_enabled`). Saying
            // so is better than an empty list, which reads as "no reports exist".
            EmptyPane(icon: "eye.slash",
                      title: loc(S.sampleOff),
                      detail: loc(S.sampleOffHint))

        case .loaded(let list) where list.jobs.isEmpty:
            EmptyPane(icon: "tray",
                      title: loc(S.noReports),
                      detail: loc(S.noReportsHint))

        case .loaded(let list):
            ScrollView {
                LazyVStack(spacing: Metrics.stackSpacing) {
                    SignInNotice()
                    ForEach(list.jobs) { job in
                        NavigationLink(value: job) {
                            JobCard(job: job)
                        }
                        .buttonStyle(.plain)
                    }
                }
                .padding(.horizontal, Metrics.screenH)
                .padding(.vertical, Metrics.screenV)
            }
            .refreshable { await load() }
            .navigationDestination(for: AgentJob.self) { job in
                ReportView(job: job)
            }
        }
    }

    private func load() async {
        do {
            let list = try await APIClient.shared.showcase()
            state = .loaded(list)
            // Raised from here rather than from inside the client: the language an
            // alert is written in is a reader preference, and `AlertCenter` has no
            // environment to read it from.
            alerts.observe(showcase: list.jobs, language: loc.language)
        } catch {
            state = .failed(error)
        }
    }
}

/// Explains what signing in would add, without pretending it is available.
private struct SignInNotice: View {
    @Environment(Session.self) private var session
    @Environment(Localization.self) private var loc

    var body: some View {
        if !session.isSignedIn {
            Card(padding: 12) {
                HStack(alignment: .top, spacing: 10) {
                    Image(systemName: "person.crop.circle.badge.questionmark")
                        .foregroundStyle(Palette.warn)
                    VStack(alignment: .leading, spacing: 3) {
                        Text(loc(S.readingSample))
                            .font(.caption.weight(.semibold))
                        Text(loc(S.readingHint))
                            .font(.caption2)
                            .foregroundStyle(Palette.secondaryText)
                    }
                    Spacer()
                }
            }
        }
    }
}

/// One report in the list.
private struct JobCard: View {
    let job: AgentJob
    @Environment(Localization.self) private var loc

    var body: some View {
        Card {
            VStack(alignment: .leading, spacing: 9) {
                HStack(alignment: .firstTextBaseline) {
                    Text(job.ticker)
                        .font(.headline.monospaced())
                    Spacer()
                    Chip(text: Decision.label(job.decision, fallback: loc(S.noDecision)),
                         tint: Decision.tint(job.decision))
                }

                HStack(spacing: 10) {
                    Label(Format.day(job.date), systemImage: "calendar")
                    if let elapsed = job.elapsedSec {
                        Label(Format.elapsed(elapsed), systemImage: "clock")
                    }
                    Spacer()
                }
                .font(.caption2)
                .foregroundStyle(Palette.mutedText)
                .labelStyle(.titleAndIcon)

                // Surfaced, not hidden. routes.py publishes these precisely so a
                // sampled report cannot pass itself off as better than it is: a run
                // that fell back to a cheaper model produced a different artefact from
                // one that did not.
                if job.degraded == true || job.recovered == true || job.provider != nil {
                    HStack(spacing: 6) {
                        if job.degraded == true {
                            Chip(text: loc(S.degraded), tint: Palette.warn)
                        }
                        if job.recovered == true {
                            Chip(text: loc(S.recovered), tint: Palette.warn)
                        }
                        if let provider = job.provider {
                            Chip(text: provider, tint: Palette.mutedText)
                        }
                        Spacer()
                    }
                }
            }
        }
    }
}
