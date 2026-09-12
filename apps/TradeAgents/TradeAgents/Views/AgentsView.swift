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
        .navigationTitle("Agents")
        .toolbar {
            ToolbarItem(placement: .primaryAction) {
                Button {
                    composing = true
                } label: {
                    Label("New run", systemImage: "plus.circle")
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
            LoadingPane(label: "Loading reports")

        case .failed(let error):
            LoadFailure(error: error) { Task { await load() } }

        case .loaded(let list) where !list.enabled:
            // The server can turn the sample off entirely (`showcase_enabled`). Saying
            // so is better than an empty list, which reads as "no reports exist".
            EmptyPane(icon: "eye.slash",
                      title: "The public sample is switched off",
                      detail: "Finished reports are still readable on the web app when signed in.")

        case .loaded(let list) where list.jobs.isEmpty:
            EmptyPane(icon: "tray",
                      title: "No sampled reports yet",
                      detail: "A run has to finish before it can appear here.")

        case .loaded(let list):
            ScrollView {
                LazyVStack(spacing: 12) {
                    SignInNotice()
                    ForEach(list.jobs) { job in
                        NavigationLink(value: job) {
                            JobCard(job: job)
                        }
                        .buttonStyle(.plain)
                    }
                }
                .padding(.horizontal, 16)
                .padding(.vertical, 12)
            }
            .refreshable { await load() }
            .navigationDestination(for: AgentJob.self) { job in
                ReportView(job: job)
            }
        }
    }

    private func load() async {
        do {
            state = .loaded(try await APIClient.shared.showcase())
        } catch {
            state = .failed(error)
        }
    }
}

/// Explains what signing in would add, without pretending it is available.
private struct SignInNotice: View {
    @Environment(Session.self) private var session

    var body: some View {
        if !session.isSignedIn {
            Card(padding: 12) {
                HStack(alignment: .top, spacing: 10) {
                    Image(systemName: "person.crop.circle.badge.questionmark")
                        .foregroundStyle(Palette.warn)
                    VStack(alignment: .leading, spacing: 3) {
                        Text("Reading the public sample")
                            .font(.caption.weight(.semibold))
                        Text("Your own runs and reports need sign-in, which is not wired up yet.")
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

    var body: some View {
        Card {
            VStack(alignment: .leading, spacing: 9) {
                HStack(alignment: .firstTextBaseline) {
                    Text(job.ticker)
                        .font(.headline.monospaced())
                    Spacer()
                    Chip(text: Decision.label(job.decision), tint: Decision.tint(job.decision))
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
                            Chip(text: "Degraded", tint: Palette.warn)
                        }
                        if job.recovered == true {
                            Chip(text: "Recovered", tint: Palette.warn)
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
