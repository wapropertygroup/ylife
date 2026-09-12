import SwiftUI

/// Account, backend and build information.
struct SettingsView: View {
    @Environment(Session.self) private var session
    @State private var host = AppSettings.host
    @State private var signingOut = false

    var body: some View {
        Form {
            Section("Account") {
                if session.isSignedIn {
                    LabeledContent("Signed in as", value: session.user.email)
                    Button(signingOut ? "Signing out…" : "Sign out", role: .destructive) {
                        Task {
                            signingOut = true
                            await session.signOut()
                            signingOut = false
                        }
                    }
                    .disabled(signingOut)
                } else {
                    LabeledContent("Status", value: "Not signed in")
                    Text(Session.signInBlockedReason)
                        .font(.caption)
                        .foregroundStyle(Palette.secondaryText)
                }

                // Distinguished from being signed out on purpose. A network failure and
                // a missing session look identical in the UI otherwise, and telling
                // somebody with a perfectly good session to sign in again is the wrong
                // instruction.
                if let error = session.lastError {
                    Label(error, systemImage: "wifi.exclamationmark")
                        .font(.caption)
                        .foregroundStyle(Palette.warn)
                }

                Button("Refresh") { Task { await session.refresh() } }
                    .disabled(session.isLoading)
            }

            Section {
                Picker("Backend", selection: $host) {
                    ForEach(AppHost.allCases) { option in
                        Text(option.label).tag(option)
                    }
                }
                .onChange(of: host) { _, newValue in
                    AppSettings.host = newValue
                    // The session belongs to a host: cookies are scoped per domain, so
                    // switching backends means the previous sign-in does not travel.
                    // Re-reading it immediately keeps the Account section honest rather
                    // than showing the old host's user against the new one.
                    Task { await session.refresh() }
                }
            } header: {
                Text("Backend")
            } footer: {
                Text("Both hosts serve the same Flask app — trade-agents.com proxies every "
                     + "path to it rather than redirecting. Switching is for diagnosing a "
                     + "DNS or certificate problem, not for changing what you see.")
            }

            Section("About") {
                LabeledContent("Version", value: Self.version)
                LabeledContent("Platform", value: Self.platform)
            }
        }
        .formStyle(.grouped)
        .navigationTitle("Settings")
    }

    private static var version: String {
        let bundle = Bundle.main
        let short = bundle.infoDictionary?["CFBundleShortVersionString"] as? String ?? "—"
        let build = bundle.infoDictionary?["CFBundleVersion"] as? String ?? "—"
        return "\(short) (\(build))"
    }

    private static var platform: String {
        #if os(macOS)
        return "macOS"
        #else
        return "iOS"
        #endif
    }
}
