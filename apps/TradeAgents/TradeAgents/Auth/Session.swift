import Foundation
import Observation

/// Who is signed in, if anyone.
///
/// Auth here is a **Flask session cookie**, not a bearer token: `/api/auth/google`
/// verifies a Google ID token and replies with `Set-Cookie`, and every gate on the
/// server keys off `session["user_email"]`. URLSession's shared cookie storage
/// therefore *is* the credential store, which is why there is no token to persist in
/// the Keychain here — and also why signing out has to be a server call rather than
/// just clearing local state.
@Observable
@MainActor
final class Session {
    private(set) var user: AuthUser = .anonymous
    private(set) var isLoading = false

    /// Set when the last refresh could not reach the server.
    ///
    /// Kept separate from `user` because "we do not know" and "signed out" are
    /// different, and conflating them would show a Sign in button to somebody whose
    /// session is perfectly valid but whose network blipped.
    private(set) var lastError: String?

    var isSignedIn: Bool { user.isSignedIn }

    func refresh() async {
        isLoading = true
        defer { isLoading = false }
        do {
            user = try await APIClient.shared.me().user
            lastError = nil
        } catch {
            lastError = error.localizedDescription
        }
    }

    func signOut() async {
        do {
            try await APIClient.shared.logout()
            user = .anonymous
            lastError = nil
        } catch {
            lastError = error.localizedDescription
        }
    }

    /// Why the sign-in button is not wired up.
    ///
    /// Native Google sign-in needs an **iOS OAuth client ID** created in the Google
    /// Cloud console, plus the GoogleSignIn SDK. Neither can be produced from a
    /// checkout, and the missing piece is a console registration rather than code:
    /// `APIClient.signInWithGoogle` already implements the half that lives here, and
    /// the server needs no change at all.
    ///
    /// Stated in the UI rather than shown as a dead button, on the same principle the
    /// backend applies to a cold data source: an unavailable thing is named, not
    /// quietly omitted.
    static let signInBlockedReason = """
        Native sign-in needs a Google iOS OAuth client ID, which has to be created in \
        the Google Cloud console. The server side already works — /api/auth/google \
        accepts an ID token and returns a session cookie.
        """
}
