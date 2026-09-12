import SwiftUI

/// The alert list, shown from the bell in the toolbar and from the menu bar.
struct AlertsView: View {
    @Environment(Localization.self) private var loc
    @Environment(AlertCenter.self) private var alerts

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack {
                Text(loc(S.notifications))
                    .font(.caption.weight(.semibold))
                Spacer()
                if !alerts.alerts.isEmpty {
                    Button(loc(S.dismissAll)) { alerts.dismissAll() }
                        .buttonStyle(.plain)
                        .font(.caption2)
                        .foregroundStyle(Palette.brand)
                }
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 9)

            Divider().overlay(Palette.border)

            if alerts.alerts.isEmpty {
                Text(loc(S.noAlerts))
                    .font(.caption)
                    .foregroundStyle(Palette.secondaryText)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(14)
            } else {
                ScrollView {
                    VStack(spacing: 0) {
                        ForEach(alerts.alerts) { alert in
                            AlertRow(alert: alert)
                        }
                    }
                }
                .frame(maxHeight: 280)
            }

            Divider().overlay(Palette.border)

            Text(loc(S.notificationsNote))
                .font(.system(size: 10))
                .foregroundStyle(Palette.mutedText)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.horizontal, 12)
                .padding(.vertical, 8)
        }
        .frame(width: 320)
        .background(Palette.background)
        // Reading the list is what makes it read. Doing this on appear rather than on
        // a per-row tap because the rows are not actionable -- there is nothing to
        // open, so requiring a click to clear the badge would be busywork.
        .onAppear { alerts.markAllRead() }
    }
}

private struct AlertRow: View {
    let alert: AppAlert

    private var tint: Color {
        switch alert.kind {
        case .reportFinished: return Palette.brand
        case .largeMove:      return Palette.warn
        case .dataStale:      return Palette.down
        }
    }

    private var icon: String {
        switch alert.kind {
        case .reportFinished: return "doc.text.fill"
        case .largeMove:      return "arrow.up.arrow.down"
        case .dataStale:      return "clock.badge.exclamationmark"
        }
    }

    var body: some View {
        HStack(alignment: .top, spacing: 9) {
            Image(systemName: icon)
                .font(.caption)
                .foregroundStyle(tint)
                .frame(width: 16)
            VStack(alignment: .leading, spacing: 2) {
                Text(alert.title)
                    .font(.caption.weight(.medium))
                    .foregroundStyle(.white)
                if !alert.detail.isEmpty {
                    Text(alert.detail)
                        .font(.caption2)
                        .foregroundStyle(Palette.secondaryText)
                }
            }
            Spacer()
            if !alert.isRead {
                Circle()
                    .fill(tint)
                    .frame(width: 6, height: 6)
                    .padding(.top, 4)
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
    }
}

/// The toolbar bell, with an unread count.
struct AlertsButton: View {
    @Environment(Localization.self) private var loc
    @Environment(AlertCenter.self) private var alerts
    @State private var showing = false

    var body: some View {
        Button {
            showing.toggle()
        } label: {
            Image(systemName: alerts.unread.isEmpty ? "bell" : "bell.badge.fill")
                .foregroundStyle(alerts.unread.isEmpty ? Palette.secondaryText : Palette.warn)
        }
        .help(loc(S.notifications))
        .popover(isPresented: $showing, arrowEdge: .bottom) {
            AlertsView()
                .environment(loc)
                .environment(alerts)
        }
    }
}
