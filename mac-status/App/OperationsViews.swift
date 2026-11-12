import AppKit
import SwiftUI

struct ConsoleIntegration: Identifiable {
    let id: String
    let title: String
    let symbol: String
    let bundle: String
    let description: String
    let groups: [String]

    static let catalog = [
        Self(id: "camera-monitor", title: "Camera Monitor", symbol: "video",
             bundle: "com.zacharysturman.cameramonitor",
             description: "Home Services records the Pi camera. Camera Monitor owns live viewing, playback, and direct camera connections.",
             groups: ["cameras"]),
        Self(id: "files-backups", title: "Files & Backups", symbol: "externaldrive.badge.timemachine",
             bundle: "com.zacharysturman.filesbackups",
             description: "The Mac companion writes its Workspace backups. Home Services serves managed files and exposes supported snapshots for recovery.",
             groups: ["backups", "storage"]),
        Self(id: "content-manager", title: "Content Manager", symbol: "doc.text",
             bundle: "com.zacharysturman.ContentManager.mobile",
             description: "The Pi holds cached article reviews and queues edits. The Mac companion applies them to the canonical drafts.",
             groups: [])
    ]

    var installedApp: URL? { NSWorkspace.shared.urlForApplication(withBundleIdentifier: bundle) }

    func health(in snapshot: StatusSnapshot?) -> Health {
        guard let snapshot else { return .unknown }
        if id == "content-manager" {
            guard let relay = snapshot.hostOverview?.relay, let last = relay.lastContact else { return .unknown }
            return snapshot.checkedAt.timeIntervalSince(last) > 600 ? .attention : .healthy
        }
        return Health.rollup(snapshot.groups.filter { groups.contains($0.id) }.map(\.health))
    }
}

struct ConsoleSection<Content: View>: View {
    let title: String
    var note: String? = nil
    @ViewBuilder var content: Content

    var body: some View {
        VStack(alignment: .leading, spacing: 11) {
            VStack(alignment: .leading, spacing: 3) {
                Text(title).font(.headline)
                if let note { Text(note).font(.subheadline).foregroundStyle(.secondary) }
            }
            content
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

struct ConsoleCard<Content: View>: View {
    @ViewBuilder var content: Content

    var body: some View {
        content
            .padding(14)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(.quaternary.opacity(0.50), in: RoundedRectangle(cornerRadius: 12))
    }
}

struct HealthBadge: View {
    var health: Health
    var stale = false

    var body: some View {
        Label(stale ? "Last known · \(health.title)" : health.title, systemImage: health.symbol)
            .font(.caption.weight(.semibold))
            .foregroundStyle(stale ? Color.secondary : health.tint)
    }
}

struct DiskCards: View {
    let disks: [DiskCapacity]

    var body: some View {
        if disks.isEmpty {
            Text("Disk capacity is unavailable. Refresh the Pi connection to retry.")
                .foregroundStyle(.secondary)
        }
        ForEach(disks) { disk in
            ConsoleCard {
                VStack(alignment: .leading, spacing: 9) {
                    Label(disk.title, systemImage: "externaldrive.fill").font(.headline)
                    if let capacity = disk.capacity {
                        HStack(alignment: .firstTextBaseline, spacing: 5) {
                            Text(StatusFormat.bytes(capacity.free_bytes)).font(.title2.bold())
                            Text("available of \(StatusFormat.bytes(capacity.total_bytes))").foregroundStyle(.secondary)
                        }
                        ProgressView(value: capacity.usedFraction) {
                            Text("Space used or reserved")
                        } currentValueLabel: {
                            Text(capacity.usedFraction, format: .percent.precision(.fractionLength(0)))
                        }
                        .tint(capacity.usedFraction >= 0.9 ? .orange : .accentColor)
                    } else {
                        Label("Capacity unavailable", systemImage: "exclamationmark.triangle")
                            .foregroundStyle(.orange)
                    }
                    Text(disk.reason + " · " + disk.mount).font(.caption).foregroundStyle(.secondary)
                }
            }
        }
    }
}

struct ScheduleCards: View {
    let items: [ScheduledTask]
    var compact = false

    var body: some View {
        if items.isEmpty { Text("No schedule data yet.").foregroundStyle(.secondary) }
        ForEach(items.sorted { ($0.next ?? .distantFuture) < ($1.next ?? .distantFuture) }) { item in
            ConsoleCard {
                VStack(alignment: .leading, spacing: 6) {
                    HStack(alignment: .firstTextBaseline) {
                        Text(item.name).font(.headline)
                        Spacer()
                        Text(item.state).font(.caption.weight(.semibold))
                            .foregroundStyle(["Scheduled", "Running now", "Estimated"].contains(item.state) ? Color.secondary : .orange)
                    }
                    if let next = item.next, next > .now {
                        HStack(spacing: 5) {
                            Text("Next").foregroundStyle(.secondary)
                            Text(StatusFormat.date(next)).fontWeight(.semibold)
                            Text(next, style: .relative).foregroundStyle(.secondary)
                        }
                    } else {
                        Text(item.next == nil ? "Next run unavailable" : "Scheduled time passed · refresh to check")
                            .foregroundStyle(.secondary)
                    }
                    if !compact {
                        Text(item.owner + (item.last.map { " · Last \(item.owner == "Raspberry Pi" ? "trigger" : "success") \(StatusFormat.date($0))" } ?? ""))
                            .foregroundStyle(.secondary)
                        if let detail = item.detail { Text(detail).foregroundStyle(.secondary) }
                    }
                }
            }
        }
    }
}

struct RelayCard: View {
    let relay: RelayReading?

    var body: some View {
        ConsoleCard {
            VStack(alignment: .leading, spacing: 8) {
                Label("Article review relay", systemImage: "arrow.left.arrow.right").font(.headline)
                if let relay {
                    Text("\(relay.cached) cached articles · \(relay.queued) waiting for the Mac")
                        .font(.title3.weight(.medium))
                    Text(relay.lastContact.map { "Last Mac contact \(StatusFormat.date($0))" } ?? "No Mac contact recorded")
                    if let oldest = relay.oldestQueued {
                        Text("Oldest queued edit \(StatusFormat.date(oldest))")
                    }
                } else {
                    Text("The Pi did not return relay metadata. Check the Pi connection and whether the relay is set up.")
                }
            }
            .foregroundStyle(.primary)
        }
    }
}
