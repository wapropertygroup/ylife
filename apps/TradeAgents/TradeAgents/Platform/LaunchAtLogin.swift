#if os(macOS)
import AppKit
import Observation
import ServiceManagement

/// "Open at login", backed by `SMAppService`.
///
/// `SMAppService.mainApp` (macOS 13+) rather than the old
/// `SMLoginItemSetEnabled` helper-bundle dance: the modern API registers the app
/// itself, needs no separate login-item target, and — importantly — is the one whose
/// state the user can see and revoke in System Settings ▸ General ▸ Login Items.
///
/// **This will usually fail for a Debug build**, and that is expected rather than a
/// bug: macOS will only launch a registered app that is signed and installed in a
/// stable location, and `dev.sh` builds an ad-hoc-signed bundle inside `build/`.
/// The failure is surfaced verbatim instead of being swallowed, because a toggle that
/// silently flips back is worse than one that says why it would not stick.
@Observable
@MainActor
final class LaunchAtLogin {
    /// Mirrors `SMAppService`, which is the source of truth — the user can revoke the
    /// registration in System Settings without the app being involved, so this is
    /// re-read rather than cached in `UserDefaults`.
    private(set) var status: SMAppService.Status = .notRegistered

    /// Set when the last register/unregister threw. Cleared on the next success.
    private(set) var failure: String?

    var isEnabled: Bool { status == .enabled }

    /// macOS accepted the registration but the user has not approved it yet. A
    /// distinct state on purpose: "off" and "waiting for you in System Settings" call
    /// for completely different next actions from the reader.
    var needsApproval: Bool { status == .requiresApproval }

    init() { refresh() }

    func refresh() {
        status = SMAppService.mainApp.status
    }

    func set(_ enabled: Bool) {
        do {
            if enabled {
                try SMAppService.mainApp.register()
            } else {
                try SMAppService.mainApp.unregister()
            }
            failure = nil
        } catch {
            failure = error.localizedDescription
        }
        refresh()
    }

    /// Opens the Login Items pane, for the `requiresApproval` case.
    func openSystemSettings() {
        let url = URL(string: "x-apple.systempreferences:com.apple.LoginItems-Settings.extension")
        guard let url else { return }
        NSWorkspace.shared.open(url)
    }
}
#endif
