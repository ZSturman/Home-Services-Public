import AppKit
import Combine
import SwiftUI
import WidgetKit

@main
enum EntryPoint {
    static func main() {
        let arguments = Array(CommandLine.arguments.dropFirst())
        do {
            switch arguments.first {
            case "--collect":
                let previous = StatusStore.snapshot(for: StatusStore.configuration())
                let current = try Collector.refresh()
                if current.shouldReloadWidget(previous: previous) {
                    WidgetCenter.shared.reloadTimelines(ofKind: widgetKind)
                }
            case "--configure":
                guard arguments.count == 4 else { throw CocoaError(.validationMissingMandatoryProperty) }
                var configuration = StatusStore.configuration()
                configuration.host = arguments[1]
                configuration.gateway = arguments[2]
                configuration.dashboard = arguments[3]
                guard configuration.validationMessage == nil else { throw POSIXError(.EINVAL) }
                try StatusStore.save(configuration, to: "settings.json")
            case "--connect-operator":
                guard let endpoint = Configuration.secureURL(StatusStore.configuration().gateway) else {
                    throw OperatorError.noConnection
                }
                guard let token = readLine() else { throw OperatorError.invalidToken }
                try OperatorCredential.save(token, endpoint: endpoint)
            default:
                StatusApp.main()
            }
        } catch {
            FileHandle.standardError.write(Data("Home Services Status: \(error.localizedDescription)\n".utf8))
            exit(1)
        }
    }
}

struct StatusApp: App {
    var body: some Scene {
        Window("Home Services Status", id: "dashboard") {
            Dashboard()
                .frame(minWidth: 760, minHeight: 500)
        }
        .defaultSize(width: 930, height: 690)
    }
}

struct Dashboard: View {
    @Environment(\.openURL) private var openURL
    @State private var configuration = StatusStore.configuration()
    @State private var snapshot: StatusSnapshot?
    @State private var selected: String? = "overview"
    @State private var settings = false
    @State private var refreshing = false
    @State private var error: String?
    @State private var testResults: [String: String] = [:]
    @State private var apiChecking = false
    @State private var apiResult: DiagnosticResult?
    @State private var notificationPending = false
    @State private var notificationID: String?
    @State private var recentWarnings: DiagnosticResult?
    @State private var loadingWarnings = false
    @State private var credentialRevision = 0
    private let update = Timer.publish(every: 10, on: .main, in: .common).autoconnect()

    private var stale: Bool { snapshot.map { $0.expiresAt <= .now } ?? false }
    private var page: String { selected ?? "overview" }
    private var groups: [ServiceGroup] { ServiceGroup.catalog }
    private var group: GroupReport? { snapshot?.groups.first { $0.id == page } }
    private var integration: ConsoleIntegration? { ConsoleIntegration.catalog.first { $0.id == page } }
    private var schedules: [ScheduledTask] { snapshot?.schedules ?? [] }
    private var disks: [DiskCapacity] { snapshot?.hostOverview?.disks ?? [] }

    var body: some View {
        NavigationSplitView {
            List(selection: $selected) {
                Section("Operations") {
                    nav("Overview", "square.grid.2x2", "overview")
                    nav("Pi Storage", "internaldrive", "storage", status("storage"))
                    nav("Schedules", "calendar", "maintenance", status("maintenance"))
                    nav("Activity", "list.bullet.rectangle", "activity")
                    nav("Diagnostics", "stethoscope", "diagnostics")
                    nav("App Connections", "qrcode", "connections")
                }
                Section("Connected apps") {
                    ForEach(ConsoleIntegration.catalog) { item in
                        nav(item.title, item.symbol, item.id, item.health(in: stale ? nil : snapshot))
                    }
                }
                Section("Infrastructure") {
                    ForEach(groups.filter { !["storage", "maintenance"].contains($0.id) }) { item in
                        nav(item.title, item.symbol, item.id, status(item.id))
                    }
                }
            }
            .listStyle(.sidebar)
            .navigationSplitViewColumnWidth(min: 190, ideal: 220, max: 270)
        } detail: {
            ScrollView {
                VStack(alignment: .leading, spacing: 23) {
                    header
                    pageContent
                }
                .frame(maxWidth: 860, alignment: .leading)
                .padding(26)
                .frame(maxWidth: .infinity)
            }
            .defaultScrollAnchor(.top)
            .toolbar {
                ToolbarItemGroup {
                    if let dashboard = Configuration.secureURL(configuration.dashboard) {
                        Button("Open Web Dashboard", systemImage: "arrow.up.right.square") { openURL(dashboard) }
                            .help("Open Web Dashboard")
                    }
                    Button("Refresh Status", systemImage: "arrow.clockwise") { refresh() }
                        .disabled(refreshing)
                        .help("Refresh Status")
                    Button("Settings", systemImage: "gearshape") { settings = true }
                        .help("Settings")
                }
            }
        }
        .sheet(isPresented: $settings) {
            StatusSettings(configuration: configuration, savedGateway: configuration.gateway,
                           credentialsChanged: { credentialRevision += 1 }) { updated in
                try StatusStore.save(updated, to: "settings.json")
                configuration = updated
                testResults.removeAll()
                apiResult = nil
                recentWarnings = nil
                notificationID = nil
                snapshot = StatusStore.snapshot(for: updated)
                refresh()
            }
        }
        .onOpenURL { url in
            guard url.scheme == "home-services-status", url.host == "details" else { return }
            let id = url.lastPathComponent
            selected = groups.contains(where: { $0.id == id }) ? id : "overview"
            refresh()
        }
        .onReceive(update) { _ in
            if let new = StatusStore.snapshot(for: configuration) { snapshot = new }
            if page == "maintenance", let snapshot,
               Date.now.timeIntervalSince(snapshot.checkedAt) > 60,
               schedules.contains(where: { $0.owner == "Raspberry Pi" && ($0.next ?? .distantFuture) <= .now }) {
                refresh(reloadWidget: false)
            }
        }
        .task {
            snapshot = StatusStore.snapshot(for: configuration)
            if snapshot?.schedules == nil || stale { refresh() }
        }
        .confirmationDialog("Send a real test notification?", isPresented: $notificationPending, titleVisibility: .visible) {
            Button("Send Test Notification") { runAPI(send: true) }
        } message: {
            Text("Home Services will send to its configured notification topic. You can check delivery in the notification app.")
        }
    }

    private func status(_ id: String) -> Health? {
        snapshot?.groups.first(where: { $0.id == id }).map { stale ? .unknown : $0.health }
    }

    private func nav(_ title: String, _ symbol: String, _ id: String, _ health: Health? = nil) -> some View {
        HStack {
            Label(title, systemImage: symbol)
            Spacer()
            if let health {
                Image(systemName: health.symbol)
                    .foregroundStyle(health.tint)
                    .accessibilityLabel(health.title)
            }
        }
        .tag(id)
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 5) {
            Text(pageTitle).font(.largeTitle.bold()).accessibilityAddTraits(.isHeader)
            if refreshing {
                Label("Checking the Pi and Mac…", systemImage: "arrow.clockwise").foregroundStyle(.secondary)
            } else if let snapshot {
                Label(stale ? "Status is stale · " + StatusFormat.date(snapshot.checkedAt) : "Checked " + StatusFormat.date(snapshot.checkedAt),
                      systemImage: stale ? "clock.badge.exclamationmark" : "clock")
                    .foregroundStyle(stale ? Color.orange : .secondary)
            } else {
                Text("Waiting for the first status check.").foregroundStyle(.secondary)
            }
            if let error { Label(error, systemImage: "exclamationmark.circle").foregroundStyle(.red) }
        }
    }

    private var pageTitle: String {
        switch page {
        case "overview": "Home Services"
        case "storage": "Pi Storage"
        case "maintenance": "Schedules"
        case "activity": "Activity"
        case "diagnostics": "Diagnostics"
        case "connections": "App Connections"
        default: integration?.title ?? groups.first(where: { $0.id == page })?.title ?? "Home Services"
        }
    }

    @ViewBuilder private var pageContent: some View {
        switch page {
        case "overview": overview
        case "storage":
            ConsoleSection(title: "Capacity", note: "Actual available space on the Pi and each verified Home Services volume.") {
                DiskCards(disks: disks)
            }
            if let group { groupDetail(group) }
        case "maintenance":
            ConsoleSection(title: "Upcoming and recent runs", note: "Times are shown in this Mac’s time zone. Pi last-trigger times show when work was started.") {
                ScheduleCards(items: schedules)
            }
            if let group { groupDetail(group) }
        case "activity":
            ActivityView(gateway: configuration.gateway, openSettings: { settings = true })
                .id(credentialRevision)
        case "diagnostics": diagnostics
        case "connections": ConnectionsView(configuration: configuration)
        default:
            if let integration { integrationPage(integration) }
            else if let group { groupDetail(group) }
        }
    }

    private var overview: some View {
        VStack(alignment: .leading, spacing: 22) {
            if let snapshot {
                let affected = snapshot.groups.filter { ![.healthy, .idle].contains($0.health) }
                let unhealthyApps = ConsoleIntegration.catalog.filter { [.attention, .unavailable].contains($0.health(in: snapshot)) }
                ConsoleSection(title: "Service health") {
                    ConsoleCard {
                        VStack(alignment: .leading, spacing: 9) {
                            Label(stale ? "Refresh to check current health" : "\(snapshot.healthyCount) of \(snapshot.groups.count) service groups healthy",
                                  systemImage: "waveform.path.ecg")
                                .font(.title3.weight(.semibold))
                            ForEach(affected) { report in
                                Button { selected = report.id } label: {
                                    HStack {
                                        Text(report.title)
                                        Spacer()
                                        HealthBadge(health: report.health, stale: stale)
                                        Image(systemName: "chevron.right").foregroundStyle(.secondary)
                                    }
                                }.buttonStyle(.plain)
                            }
                            ForEach(unhealthyApps) { item in
                                Button(item.title + " needs attention", systemImage: "exclamationmark.triangle") { selected = item.id }
                            }
                        }
                    }
                }
            }
            ConsoleSection(title: "Connected apps") {
                LazyVGrid(columns: [GridItem(.adaptive(minimum: 160))], alignment: .leading) {
                    ForEach(ConsoleIntegration.catalog) { item in
                        Button { selected = item.id } label: {
                            ConsoleCard {
                                VStack(alignment: .leading, spacing: 8) {
                                    Label(item.title, systemImage: item.symbol).font(.headline)
                                    HealthBadge(health: item.health(in: snapshot), stale: stale)
                                }
                                .frame(minHeight: 45, alignment: .leading)
                            }
                        }.buttonStyle(.plain)
                    }
                }
            }
            ConsoleSection(title: "Pi disk space") {
                DiskCards(disks: disks)
                Button("View Storage Details") { selected = "storage" }
            }
            ConsoleSection(title: "Next scheduled runs") {
                ScheduleCards(items: Array(schedules.filter { ($0.next ?? .distantPast) > .now }
                    .sorted { ($0.next ?? .distantFuture) < ($1.next ?? .distantFuture) }.prefix(3)), compact: true)
                Button("View Full Schedule") { selected = "maintenance" }
            }
        }
    }

    private func integrationPage(_ item: ConsoleIntegration) -> some View {
        VStack(alignment: .leading, spacing: 18) {
            Text(item.description).foregroundStyle(.secondary)
            HStack {
                if let app = item.installedApp {
                    Button("Open " + item.title, systemImage: "arrow.up.right.square") { NSWorkspace.shared.open(app) }
                } else {
                    Text("App unavailable on this Mac").foregroundStyle(.secondary)
                }
                Button("Run Checks", systemImage: "stethoscope") { refresh(test: item.id) }.disabled(refreshing)
            }
            if let result = testResults[item.id] { Text(result).foregroundStyle(.secondary) }
            if item.id == "content-manager" {
                RelayCard(relay: snapshot?.hostOverview?.relay)
                if item.health(in: snapshot) != .healthy {
                    DiagnosticCard(result: DiagnosticResult(health: .attention, message: "Check the article relay or Mac sync.",
                                                            nextStep: Troubleshooting.relay))
                }
            }
            if item.id == "files-backups" {
                ConsoleSection(title: "Mac backup schedule") {
                    ScheduleCards(items: schedules.filter { $0.owner == "Files & Backups" })
                }
            }
            ForEach(snapshot?.groups.filter { item.groups.contains($0.id) } ?? []) { report in
                groupDetail(report)
            }
        }
    }

    private var diagnostics: some View {
        VStack(alignment: .leading, spacing: 20) {
            ConsoleSection(title: "System checks",
                           note: "Check HTTPS and SSH reachability, disk identity, schedules, recording coverage, backup age and relay metadata.") {
                Button("Run System Checks", systemImage: "stethoscope") { refresh(test: "diagnostics") }
                    .disabled(refreshing)
                if let result = testResults["diagnostics"] { Text(result).foregroundStyle(.secondary) }
                if let hostIssue { DiagnosticCard(result: hostIssue) }
                ForEach(snapshot?.groups ?? []) { report in
                    DisclosureGroup {
                        groupDetail(report)
                    } label: {
                        HStack {
                            Label(report.title, systemImage: report.symbol)
                            Spacer()
                            HealthBadge(health: report.health, stale: stale)
                        }
                    }
                }
            }
            ConsoleSection(title: "Operator tests", note: "The readiness check makes an authenticated API request. A delivery test queues a real notification.") {
                HStack {
                    Button("Test API Access", systemImage: "checkmark.shield") { runAPI(send: false) }
                    Button("Send Test Notification…", systemImage: "bell") { notificationPending = true }
                    Button("Operator Settings") { settings = true }
                }
                .disabled(apiChecking)
                if apiChecking { ProgressView("Contacting the gateway…") }
                if let apiResult { DiagnosticCard(result: apiResult) }
                if let notificationID {
                    Button("Check Test Delivery") { runAPI(deliveryID: notificationID) }.disabled(apiChecking)
                }
            }
            ConsoleSection(title: "Recent Pi warnings", note: "Read up to 30 recent warnings and errors from the gateway, workers, notifier, and maintenance services.") {
                Button("Load Recent Warnings", systemImage: "exclamationmark.bubble") { loadWarnings() }
                    .disabled(loadingWarnings)
                if loadingWarnings { ProgressView("Reading Pi warnings…") }
                if let recentWarnings { DiagnosticCard(result: recentWarnings, monospaced: true) }
            }
            WidgetDiagnostics(snapshot: snapshot) { refresh() }
        }
    }

    private var hostIssue: DiagnosticResult? {
        guard let snapshot else { return nil }
        guard let host = snapshot.hostOverview else {
            return DiagnosticResult(health: .unknown, message: "Pi capacity and schedule details are unavailable.",
                                    nextStep: "Check SSH access and that the Home Services Python environment can read the Pi configuration.")
        }
        if host.disks.first(where: { $0.id == "system" })?.capacity == nil || host.timers == nil {
            return DiagnosticResult(health: .attention, message: "The Pi returned incomplete disk or timer data.",
                                    nextStep: "Check Pi Storage and Schedules, then load Recent Warnings for system or timer errors.")
        }
        if let disk = host.disks.first(where: { ($0.capacity?.usedFraction ?? 0) >= 0.9 }) {
            return DiagnosticResult(health: .attention, message: disk.title + " is at least 90% used or reserved.",
                                    nextStep: "Review Pi Storage and the retention schedule. Identify which data grew before removing files.")
        }
        return nil
    }

    private func groupDetail(_ report: GroupReport) -> some View {
        ConsoleSection(title: report.title) {
            ForEach(report.checks) { check in
                ConsoleCard {
                    VStack(alignment: .leading, spacing: 6) {
                        HStack(alignment: .firstTextBaseline) {
                            Text(check.name).font(.headline)
                            Spacer()
                            HealthBadge(health: check.health, stale: stale)
                        }
                        Text(check.summary ?? check.detail).foregroundStyle(.secondary)
                        if let nextStep = check.nextStep {
                            Label(nextStep, systemImage: "wrench.and.screwdriver")
                                .font(.subheadline)
                                .foregroundStyle(.secondary)
                        }
                        if let summary = check.summary, summary != check.detail {
                            DisclosureGroup("Technical details") {
                                Text(check.detail)
                                    .font(.callout.monospaced())
                                    .foregroundStyle(.secondary)
                                    .textSelection(.enabled)
                            }
                        }
                    }
                }
            }
        }
    }

    private func refresh(test: String? = nil, reloadWidget: Bool = true) {
        guard !refreshing else { return }
        refreshing = true
        error = nil
        if let test { testResults[test] = nil }
        let requested = configuration
        Task {
            defer {
                refreshing = false
                if requested != configuration && snapshot == nil { refresh() }
            }
            do {
                let fresh = try await Task.detached(priority: .utility) { try Collector.refresh() }.value
                guard fresh.configuration == configuration else { return }
                snapshot = fresh
                if reloadWidget { WidgetCenter.shared.reloadTimelines(ofKind: widgetKind) }
                if let test {
                    if let app = ConsoleIntegration.catalog.first(where: { $0.id == test }) {
                        let health = app.health(in: fresh)
                        testResults[test] = "Home Services checks: \(health.title.lowercased()) · \(StatusFormat.date(fresh.checkedAt))"
                    } else {
                        let unknown = fresh.unknownCount
                        let issues = fresh.attentionCount
                        testResults[test] = "\(fresh.healthyCount) healthy · \(issues) need attention · \(unknown) unknown"
                    }
                }
            } catch {
                self.error = "Could not update shared status. Check the app’s signing and App Group."
            }
        }
    }

    private func runAPI(send: Bool = false, deliveryID: String? = nil) {
        guard !apiChecking else { return }
        apiChecking = true
        apiResult = nil
        let gateway = configuration.gateway
        Task {
            defer { apiChecking = false }
            do {
                let api = try OperatorAPI(gateway: gateway)
                if send {
                    let id = try await api.sendTestNotification()
                    guard configuration.gateway == gateway else { return }
                    notificationID = id
                    apiResult = DiagnosticResult(health: .idle, message: "Test queued as " + id + ".",
                                                 nextStep: "Choose Check Test Delivery after the notifier has had time to process it.")
                } else {
                    let result: DiagnosticResult
                    if let deliveryID { result = try await api.deliveryStatus(deliveryID) }
                    else { result = try await api.readiness() }
                    if configuration.gateway == gateway { apiResult = result }
                }
            } catch {
                if configuration.gateway == gateway {
                    apiResult = Troubleshooting.api(error, delivery: send || deliveryID != nil)
                }
            }
        }
    }

    private func loadWarnings() {
        guard !loadingWarnings else { return }
        loadingWarnings = true
        recentWarnings = nil
        let host = configuration.host
        Task {
            defer { loadingWarnings = false }
            let warnings = await Task.detached(priority: .utility) {
                HostDiagnostics.recentWarnings(host: host)
            }.value
            if configuration.host == host { recentWarnings = warnings }
        }
    }
}

private struct StatusSettings: View {
    @Environment(\.dismiss) private var dismiss
    @State var configuration: Configuration
    @State private var error: String?
    @State private var token = ""
    @State private var tokenMessage: String?
    let savedGateway: String
    var credentialsChanged: () -> Void
    var save: (Configuration) throws -> Void

    var body: some View {
        Form {
            Section("Connections") {
                TextField("Pi SSH host", text: $configuration.host, prompt: Text("user@hostname"))
                TextField("Gateway HTTPS URL", text: $configuration.gateway, prompt: Text("https://pi.example:8443"))
                TextField("Web dashboard URL", text: $configuration.dashboard, prompt: Text("Optional"))
            }
            Section("Operator access") {
                Text("A scoped API token enables Activity and authenticated tests. It stays in your Mac’s Keychain.")
                    .foregroundStyle(.secondary)
                if let endpoint = Configuration.secureURL(savedGateway), configuration.gateway == savedGateway {
                    SecureField("Operator token", text: $token)
                    HStack {
                        Button("Store Token") {
                            updateCredential("Token stored in Keychain.") {
                                try OperatorCredential.save(token, endpoint: endpoint)
                            }
                        }
                        .disabled(token.isEmpty)
                        Button("Remove Stored Token") {
                            updateCredential("Token removed from this Mac.") {
                                try OperatorCredential.remove(endpoint)
                            }
                        }
                    }
                    if let tokenMessage { Text(tokenMessage).foregroundStyle(.secondary) }
                } else {
                    Text("Save the HTTPS gateway before storing its token.").foregroundStyle(.secondary)
                }
            }
            Section("Widget groups") {
                ForEach(ServiceGroup.catalog) { group in
                    Toggle(group.title, isOn: Binding(
                        get: { !configuration.hidden.contains(group.id) },
                        set: { visible in
                            if visible { configuration.hidden.remove(group.id) }
                            else { configuration.hidden.insert(group.id) }
                        }
                    ))
                }
            }
            if let message = configuration.validationMessage ?? error {
                Text(message).foregroundStyle(.red)
            }
            HStack {
                Spacer()
                Button("Cancel") { dismiss() }.keyboardShortcut(.cancelAction)
                Button("Save") {
                    do {
                        try save(configuration)
                        dismiss()
                    } catch { self.error = "Could not save settings to the shared App Group." }
                }
                .keyboardShortcut(.defaultAction)
                .disabled(configuration.validationMessage != nil)
            }
        }
        .formStyle(.grouped)
        .frame(width: 520, height: 640)
    }

    private func updateCredential(_ message: String, _ action: () throws -> Void) {
        do {
            try action()
            token = ""
            tokenMessage = message
            credentialsChanged()
        } catch { tokenMessage = error.localizedDescription }
    }
}
