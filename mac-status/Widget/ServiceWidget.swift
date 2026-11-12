import OSLog
import SwiftUI
import WidgetKit

private let overviewURL = URL(string: "home-services-status://details/overview")!

struct StatusEntry: TimelineEntry {
    var date: Date
    var snapshot: StatusSnapshot?
    var stale: Bool { snapshot.map { $0.expiresAt <= date } ?? false }
}

struct StatusProvider: TimelineProvider {
    func placeholder(in context: Context) -> StatusEntry { preview }

    func getSnapshot(in context: Context, completion: @escaping (StatusEntry) -> Void) {
        let current = read()
        completion(context.isPreview && current.snapshot == nil ? preview : current)
    }

    func getTimeline(in context: Context, completion: @escaping (Timeline<StatusEntry>) -> Void) {
        let current = read()
        let receipt = WidgetReadReceipt(readAt: current.date, snapshotAt: current.snapshot?.checkedAt,
                                        groupCount: current.snapshot?.groups.count ?? 0,
                                        summary: current.snapshot?.widgetSummary)
        do {
            try StatusStore.save(receipt, to: WidgetReadReceipt.filename)
        } catch {
            let logger = Logger(subsystem: "com.zacharysturman.homeservices.status", category: "widget")
            logger.error("Widget timeline receipt could not be written (code \((error as NSError).code, privacy: .public)).")
        }
        var entries = [current]
        if let snapshot = current.snapshot, snapshot.expiresAt > current.date {
            entries.append(StatusEntry(date: snapshot.expiresAt, snapshot: snapshot))
        }
        completion(Timeline(entries: entries, policy: .after(current.date.addingTimeInterval(15 * 60))))
    }

    private func read() -> StatusEntry {
        let configuration = StatusStore.configuration()
        let snapshot = StatusStore.snapshot(for: configuration)?.forWidget
        return StatusEntry(date: .now, snapshot: snapshot)
    }

    private var preview: StatusEntry {
        let groups = ServiceGroup.catalog.map { group in
            GroupReport(id: group.id, title: group.title, symbol: group.symbol,
                        checks: [CheckResult(name: group.title, health: group.id == "backups" ? .attention : .healthy, detail: "")])
        }
        return StatusEntry(date: .now, snapshot: StatusSnapshot(configuration: Configuration(), checkedAt: .now, groups: groups))
    }
}

struct ServiceWidgetView: View {
    @Environment(\.widgetFamily) private var family
    @Environment(\.widgetRenderingMode) private var renderingMode
    let entry: StatusEntry

    var body: some View {
        VStack(alignment: .leading, spacing: 7) {
            HStack(spacing: 5) {
                Image(systemName: "waveform.path.ecg")
                    .widgetAccentable()
                Text("Home Services")
                Spacer(minLength: 0)
                if family != .systemSmall { Image(systemName: "arrow.up.right").foregroundStyle(.secondary) }
            }
            .font(.headline)
            .lineLimit(1)
            if let snapshot = entry.snapshot, !snapshot.groups.isEmpty {
                if family == .systemSmall {
                    compact(snapshot)
                } else if family == .systemMedium {
                    grid(snapshot)
                } else {
                    let shown = snapshot.groups.count > 8 ? 7 : 8
                    ForEach(snapshot.groups.prefix(shown)) { group in row(group, showStatus: true) }
                    if snapshot.groups.count > shown { more(snapshot.groups.count - shown) }
                }
                Spacer(minLength: 0)
                footer(snapshot)
            } else {
                Spacer(minLength: 0)
                Text(entry.snapshot == nil ? "Open app to refresh status" : "All groups hidden")
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                Spacer(minLength: 0)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .containerBackground(.fill.tertiary, for: .widget)
        .widgetURL(overviewURL)
    }

    private func compact(_ snapshot: StatusSnapshot) -> some View {
        let total = snapshot.groups.count
        let rollup = Health.rollup(snapshot.groups.map(\.health))
        let health = entry.stale ? Health.unknown : rollup
        let status = switch health {
        case .healthy: "All healthy"
        case .attention, .unavailable: "\(snapshot.attentionCount) need attention"
        case .unknown: entry.stale ? "Status stale" : "\(snapshot.unknownCount) unknown"
        case .idle: "All idle"
        }
        return VStack(alignment: .leading, spacing: 4) {
            Spacer(minLength: 0)
            Image(systemName: health.symbol)
                .font(.title2)
                .foregroundStyle(tint(health))
                .accessibilityHidden(true)
            Text(status).font(.subheadline.weight(.semibold))
            Text("\(snapshot.healthyCount) of \(total) healthy")
                .font(.caption)
                .foregroundStyle(.secondary)
                .opacity(entry.stale ? 0 : 1)
                .accessibilityHidden(entry.stale)
        }
    }

    private func grid(_ snapshot: StatusSnapshot) -> some View {
        let shown = snapshot.groups.count > 8 ? 7 : 8
        return LazyVGrid(columns: [GridItem(.flexible()), GridItem(.flexible())], alignment: .leading, spacing: 4) {
            ForEach(snapshot.groups.prefix(shown)) { group in row(group, showStatus: false) }
            if snapshot.groups.count > shown { more(snapshot.groups.count - shown) }
        }
    }

    private func row(_ group: GroupReport, showStatus: Bool) -> some View {
        let health = entry.stale ? Health.unknown : group.health
        return Link(destination: group.link) {
            HStack(spacing: 6) {
                Image(systemName: health.symbol)
                    .foregroundStyle(tint(health))
                    .frame(width: 15)
                Text(group.title).lineLimit(1)
                Spacer(minLength: 0)
                if showStatus { Text(health.title).foregroundStyle(.secondary) }
            }
            .font(showStatus ? .subheadline : .caption)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .accessibilityLabel("\(group.title), \(health.title)")
    }

    private func more(_ count: Int) -> some View {
        Link("+\(count) in Overview", destination: overviewURL)
            .font(.caption)
    }

    private func footer(_ snapshot: StatusSnapshot) -> some View {
        HStack(spacing: 4) {
            if entry.stale { Text("Stale ·") }
            Text("Checked")
            Text(snapshot.checkedAt, style: .time)
                .monospacedDigit()
        }
        .font(.caption2)
        .foregroundStyle(.secondary)
        .lineLimit(1)
    }

    private func tint(_ health: Health) -> Color {
        renderingMode == .fullColor ? health.tint : .primary
    }
}

@main
struct HomeServicesWidget: Widget {
    var body: some WidgetConfiguration {
        StaticConfiguration(kind: widgetKind, provider: StatusProvider()) { entry in
            ServiceWidgetView(entry: entry)
        }
        .configurationDisplayName("Home Services Status")
        .description("See Pi services, backup protection, camera recording and Mac companions.")
        .supportedFamilies([.systemSmall, .systemMedium, .systemLarge])
    }
}
