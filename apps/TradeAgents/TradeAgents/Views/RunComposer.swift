import SwiftUI

/// The new-run sheet.
///
/// The form is real and so is the call behind it — `APIClient.submitRun` posts exactly
/// what `/api/agents/run` expects. What is missing is a way to *be* signed in: native
/// Google sign-in needs an iOS OAuth client ID from the Google Cloud console, which
/// cannot be created from a checkout.
///
/// So the button stays enabled and the failure is shown verbatim. Disabling it with a
/// vague tooltip would hide which half is unfinished; letting the request go and
/// surfacing the server's own `{"error": "Sign in required"}` proves the client half
/// works and names the missing piece. The server needs no change whatsoever.
struct RunComposer: View {
    @Environment(\.dismiss) private var dismiss
    @Environment(Session.self) private var session

    @State private var ticker = ""
    @State private var date = Date()
    @State private var submitting = false
    @State private var result: Result<RunAccepted, Error>?

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("Ticker", text: $ticker)
                        .font(.body.monospaced())
                        #if os(iOS)
                        .textInputAutocapitalization(.characters)
                        .autocorrectionDisabled()
                        #endif
                    DatePicker("Analysis date", selection: $date, displayedComponents: .date)
                } header: {
                    Text("Run")
                } footer: {
                    // Naming the omission rather than quietly running on defaults. The
                    // reader is entitled to know a choice exists that this client
                    // cannot yet offer.
                    Text("Uses the deployment's default models. Choosing a model needs a "
                         + "server endpoint that exposes the model table; duplicating it "
                         + "here would drift from what actually runs.")
                }

                if !session.isSignedIn {
                    Section {
                        Label {
                            Text(Session.signInBlockedReason)
                                .font(.caption)
                                .foregroundStyle(Palette.secondaryText)
                        } icon: {
                            Image(systemName: "exclamationmark.triangle")
                                .foregroundStyle(Palette.warn)
                        }
                    }
                }

                if let result {
                    Section {
                        switch result {
                        case .success(let accepted):
                            Label("Queued as \(accepted.jobId)", systemImage: "checkmark.circle")
                                .foregroundStyle(Palette.up)
                                .font(.caption)
                        case .failure(let error):
                            Label(error.localizedDescription, systemImage: "xmark.octagon")
                                .foregroundStyle(Palette.down)
                                .font(.caption)
                        }
                    }
                }
            }
            .formStyle(.grouped)
            .navigationTitle("New run")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Close") { dismiss() }
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button(submitting ? "Submitting…" : "Run") { Task { await submit() } }
                        .disabled(submitting || trimmedTicker.isEmpty)
                }
            }
        }
        .frame(minWidth: 420, minHeight: 380)
    }

    private var trimmedTicker: String {
        ticker.trimmingCharacters(in: .whitespacesAndNewlines).uppercased()
    }

    private func submit() async {
        submitting = true
        defer { submitting = false }
        do {
            let accepted = try await APIClient.shared.submitRun(
                ticker: trimmedTicker,
                date: Self.dayFormatter.string(from: date)
            )
            result = .success(accepted)
        } catch {
            result = .failure(error)
        }
    }

    /// `yyyy-MM-dd`, fixed locale and UTC — the format `submit()` parses. A
    /// user-locale formatter would emit `09/12/2026` under some regions, and a
    /// non-Gregorian calendar would emit a year the server cannot read at all.
    private static let dayFormatter: DateFormatter = {
        let f = DateFormatter()
        f.calendar = Calendar(identifier: .gregorian)
        f.locale = Locale(identifier: "en_US_POSIX")
        f.timeZone = TimeZone(identifier: "UTC")
        f.dateFormat = "yyyy-MM-dd"
        return f
    }()
}
