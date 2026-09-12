import SwiftUI

/// Account, language, backend and build information.
struct SettingsView: View {
    @Environment(Session.self) private var session
    @Environment(Localization.self) private var loc
    @State private var host = AppSettings.host
    @State private var signingOut = false
    @State private var signingIn = false
    #if os(macOS)
    @State private var launch = LaunchAtLogin()
    #endif

    var body: some View {
        @Bindable var localization = loc

        Form {
            Section(loc(S.account)) {
                if session.isSignedIn {
                    LabeledContent(loc(S.signedInAs), value: session.user.email)
                    Button(signingOut ? loc(S.signingOut) : loc(S.signOut), role: .destructive) {
                        Task {
                            signingOut = true
                            await session.signOut()
                            signingOut = false
                        }
                    }
                    .disabled(signingOut)
                } else {
                    LabeledContent(loc(S.status), value: loc(S.notSignedIn))
                    Button(loc(S.signIn)) { signingIn = true }
                        .buttonStyle(.borderedProminent)
                        .tint(Palette.brand)
                    Text(loc(S.signInBlocked))
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

                Button(loc(S.refresh)) { Task { await session.refresh() } }
                    .disabled(session.isLoading)
            }

            Section {
                Picker(loc(S.language), selection: $localization.language) {
                    ForEach(Language.allCases) { option in
                        // Each language named in itself. "Chinese" is no use to
                        // somebody who cannot read the language currently showing.
                        Text(option.endonym).tag(option)
                    }
                }
            } header: {
                Text(loc(S.language))
            } footer: {
                Text(loc(S.languageNote))
            }

            Section {
                Picker(loc(S.backend), selection: $host) {
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
                Text(loc(S.backend))
            } footer: {
                Text(loc(S.backendNote))
            }

            Section {
                Text(loc(S.notificationsNote))
                    .font(.caption)
                    .foregroundStyle(Palette.secondaryText)
            } header: {
                Text(loc(S.notifications))
            }

            #if os(macOS)
            Section {
                Toggle(loc(S.launchAtLogin), isOn: Binding(
                    get: { launch.isEnabled },
                    set: { launch.set($0) }
                ))

                if launch.needsApproval {
                    // A distinct state from "off": macOS accepted the registration but
                    // is waiting for the user to allow it, and nothing this app does
                    // can complete that step.
                    Label(loc(S.launchNeedsApproval), systemImage: "hand.raised")
                        .font(.caption)
                        .foregroundStyle(Palette.warn)
                    Button(loc(S.openLoginItems)) { launch.openSystemSettings() }
                }

                if let failure = launch.failure {
                    VStack(alignment: .leading, spacing: 4) {
                        Label(loc(S.launchFailed), systemImage: "exclamationmark.triangle")
                            .font(.caption)
                            .foregroundStyle(Palette.down)
                        Text(failure)
                            .font(.caption2)
                            .foregroundStyle(Palette.mutedText)
                    }
                }
            } header: {
                Text(loc(S.launchAtLogin))
            } footer: {
                Text(loc(S.launchAtLoginNote))
            }
            #endif

            Section(loc(S.about)) {
                LabeledContent(loc(S.version), value: Self.version)
                LabeledContent(loc(S.platform), value: Self.platform)
            }
        }
        .formStyle(.grouped)
        .navigationTitle(loc(S.settings))
        .sheet(isPresented: $signingIn) {
            LoginView()
                .environment(loc)
                .environment(session)
        }
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
