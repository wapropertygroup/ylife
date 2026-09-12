import SwiftUI
import WebKit

/// The sign-in screen.
///
/// It hosts the server's own `/login` page in a web view rather than reimplementing
/// Google sign-in natively, because the native path is blocked on something that
/// cannot be produced from a checkout: a **Google OAuth client ID** for this app from
/// the Google Cloud console. `APIClient.signInWithGoogle` is the other half and is
/// already written — the server needs no change for either route.
///
/// **The known risk, stated rather than left for the reader to discover:** Google
/// refuses its sign-in inside embedded web views (`disallowed_useragent`), a
/// deliberate anti-phishing measure that also covers Google Identity Services.
/// Whether it fires here is Google's decision, not this code's, so the screen reports
/// what actually happened instead of assuming either outcome.
///
/// If it does work, the session is real: cookies set in the web view are copied into
/// `HTTPCookieStorage.shared`, which is the store `APIClient`'s `URLSession` reads, so
/// every later API call is authenticated. That copy is the whole trick — a web view
/// keeps its own cookie jar, and without it the reader would watch themselves sign in
/// and still find the app signed out.
struct LoginView: View {
    @Environment(\.dismiss) private var dismiss
    @Environment(Localization.self) private var loc
    @Environment(Session.self) private var session

    @State private var status: String?
    @State private var loading = true

    private var loginURL: URL {
        URL(string: "/login", relativeTo: AppSettings.baseURL) ?? AppSettings.baseURL
    }

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider().overlay(Palette.border)

            ZStack {
                LoginWebView(
                    url: loginURL,
                    onNavigation: { Task { await syncCookies() } },
                    onError: { status = $0 },
                    onLoadingChanged: { loading = $0 }
                )
                if loading {
                    ProgressView().tint(Palette.brand)
                }
            }

            if let status {
                Text(status)
                    .font(.caption)
                    .foregroundStyle(Palette.down)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(12)
            }

            Divider().overlay(Palette.border)
            footer
        }
        .frame(minWidth: 560, minHeight: 660)
        .background(Palette.background)
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(loc(S.signInTitle))
                .font(.headline)
            Text(loc(S.signInIntro))
                .font(.caption)
                .foregroundStyle(Palette.secondaryText)
                .fixedSize(horizontal: false, vertical: true)
            Text(loc(S.signInEmbedded))
                .font(.caption2)
                .foregroundStyle(Palette.warn)
                .fixedSize(horizontal: false, vertical: true)
            Text("\(loc(S.signInHost)) \(AppSettings.host.label)")
                .font(.caption2)
                .foregroundStyle(Palette.mutedText)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(14)
    }

    private var footer: some View {
        HStack {
            Button(loc(S.openInBrowser)) { openExternally() }
                .buttonStyle(.plain)
                .foregroundStyle(Palette.brand)
            Spacer()
            Button(loc(S.cancel)) { dismiss() }
            Button(loc(S.refresh)) { Task { await syncCookies() } }
                .buttonStyle(.borderedProminent)
                .tint(Palette.brand)
        }
        .font(.caption)
        .padding(12)
    }

    private func openExternally() {
        #if os(macOS)
        NSWorkspace.shared.open(loginURL)
        #else
        UIApplication.shared.open(loginURL)
        #endif
    }

    /// Copies the web view's cookies into the shared store, then re-reads the session.
    ///
    /// Run after every navigation rather than only on a "success" URL, because the page
    /// signs in with a `fetch` to `/api/auth/google` and then redirects — there is no
    /// single navigation that reliably means "done", and watching for one would also
    /// miss the case where the reader was already signed in and `/login` bounced
    /// straight to `/markets`.
    private func syncCookies() async {
        let cookies = await WKWebsiteDataStore.default().httpCookieStore.allCookies()
        let host = AppSettings.host.rawValue
        for cookie in cookies where host.hasSuffix(cookie.domain)
            || cookie.domain.hasSuffix(host)
            || cookie.domain == ".\(host)" {
            HTTPCookieStorage.shared.setCookie(cookie)
        }
        await session.refresh()
        if session.isSignedIn { dismiss() }
    }
}

/// A minimal WKWebView wrapper for both platforms.
private struct LoginWebView {
    let url: URL
    let onNavigation: () -> Void
    let onError: (String) -> Void
    let onLoadingChanged: (Bool) -> Void

    func makeCoordinator() -> Coordinator {
        Coordinator(onNavigation: onNavigation,
                    onError: onError,
                    onLoadingChanged: onLoadingChanged)
    }

    func makeWebView(context: Context) -> WKWebView {
        let config = WKWebViewConfiguration()
        // The *default* data store, not a non-persistent one: the cookies this view
        // acquires are the entire point, and an ephemeral store would discard them
        // before they could be copied across.
        config.websiteDataStore = .default()
        let view = WKWebView(frame: .zero, configuration: config)
        view.navigationDelegate = context.coordinator
        view.load(URLRequest(url: url))
        return view
    }

    /// Holds plain closures rather than a reference back to the SwiftUI view, so there
    /// is no retain cycle to break with a capture list — the closures are handed in at
    /// construction and own nothing.
    final class Coordinator: NSObject, WKNavigationDelegate {
        private let onNavigation: () -> Void
        private let onError: (String) -> Void
        private let onLoadingChanged: (Bool) -> Void

        init(onNavigation: @escaping () -> Void,
             onError: @escaping (String) -> Void,
             onLoadingChanged: @escaping (Bool) -> Void) {
            self.onNavigation = onNavigation
            self.onError = onError
            self.onLoadingChanged = onLoadingChanged
        }

        func webView(_ webView: WKWebView, didStartProvisionalNavigation navigation: WKNavigation!) {
            onLoadingChanged(true)
        }

        func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
            onLoadingChanged(false)
            onNavigation()
        }

        func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
            onLoadingChanged(false)
            onError(error.localizedDescription)
        }

        func webView(_ webView: WKWebView,
                     didFailProvisionalNavigation navigation: WKNavigation!,
                     withError error: Error) {
            onLoadingChanged(false)
            // Reported rather than swallowed: this is where Google's
            // `disallowed_useragent` refusal would surface, and a blank web view with
            // no explanation is the worst possible version of that.
            onError(error.localizedDescription)
        }
    }
}

#if os(macOS)
extension LoginWebView: NSViewRepresentable {
    func makeNSView(context: Context) -> WKWebView { makeWebView(context: context) }
    func updateNSView(_ nsView: WKWebView, context: Context) {}
}
#else
extension LoginWebView: UIViewRepresentable {
    func makeUIView(context: Context) -> WKWebView { makeWebView(context: context) }
    func updateUIView(_ uiView: WKWebView, context: Context) {}
}
#endif
