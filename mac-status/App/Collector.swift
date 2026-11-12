import Darwin
import Foundation

enum Collector {
    // The LaunchAgent and the foreground refresh share one writer. After a settings
    // change, a late result from the previous connection is ignored by readers.
    static func refresh() throws -> StatusSnapshot {
        let lock = try StatusStore.directory().appendingPathComponent("collector.lock")
        let fd = open(lock.path, O_CREAT | O_RDWR | O_CLOEXEC, 0o600)
        guard fd >= 0 else { throw POSIXError(.EIO) }
        defer { close(fd) }
        guard flock(fd, LOCK_EX) == 0 else { throw POSIXError(.EIO) }
        let configuration = StatusStore.configuration()
        let snapshot = collect(configuration)
        try StatusStore.save(snapshot, to: "status.json")
        return snapshot
    }

    private static func collect(_ configuration: Configuration) -> StatusSnapshot {
        let groups = ServiceGroup.catalog
        let probes = groups.flatMap(\.checks).map(\.probe)
        let units = Set(probes.flatMap { probe -> [String] in
            if case .unit(let unit, let job) = probe { return [unit] + [job].compactMap { $0 } }
            return []
        })
        let needsObservation = probes.contains {
            switch $0 {
            case .readiness, .backups, .cameras, .outbox: true
            default: false
            }
        }
        let pi = PiReport.read(host: configuration.host, units: units, observation: needsObservation)
        let agents = probes.contains { if case .agent = $0 { return true }; return false }
            ? Tool.output("/bin/launchctl", ["list"], timeout: 3) : nil

        let reports = groups.map { group in
            GroupReport(id: group.id, title: group.title, symbol: group.symbol, checks: group.checks.map { check in
                let result: (Health, String)
                switch check.probe {
                case .gateway: result = gateway(configuration.gateway)
                case let .unit(unit, job): result = pi.unit(unit, job: job)
                case let .agent(label, scheduled): result = agent(label, scheduled: scheduled, output: agents)
                case .readiness(let keys): result = pi.observed { $0.roots(keys) }
                case .backups: result = pi.observed { $0.backupStatus }
                case .cameras: result = pi.observed { $0.cameraStatus }
                case .outbox: result = pi.observed { $0.notificationStatus }
                case .opportunityReview: result = pi.opportunityStatus
                case .casinoExit: result = pi.casinoExitStatus
                }
                return CheckResult(name: check.name, health: result.0, detail: result.1,
                                   summary: summary(check.probe, health: result.0, details: result.1),
                                   nextStep: Troubleshooting.step(for: check.probe, group: group.id, health: result.0, pi: pi))
            })
        }
        let macBackup = reports.first { $0.id == "mac" }?.checks.first { $0.name == "Files & Backups scheduler" }
        let schedules = pi.schedules + HostDiagnostics.macBackupSchedules(
            agentLoaded: macBackup.map { [.healthy, .idle, .attention].contains($0.health) })
        return StatusSnapshot(configuration: configuration, checkedAt: .now, groups: reports,
                              hostOverview: pi.hostOverview, schedules: schedules)
    }

    private static func summary(_ probe: Probe, health: Health, details: String) -> String {
        let firstLine = String(details.prefix(while: { !$0.isNewline }))
        switch probe {
        case .unit where health == .unknown: return firstLine
        case .unit(_, .some):
            return switch health {
            case .healthy: "The Pi is managing this schedule."
            case .attention: "This schedule or its last run needs attention."
            default: "This schedule is inactive on the Pi."
            }
        case .unit:
            return switch health {
            case .healthy: "Running on the Pi."
            case .attention: "The Pi service is changing state."
            default: "The Pi service has stopped or failed."
            }
        case .readiness where health == .healthy: return "Home Services verified this in its latest health sample."
        case .readiness where health == .idle: return "Disabled on the Pi."
        case .readiness where health == .unavailable || health == .attention: return "One or more checks failed. Expand Technical details for the affected items."
        default: return firstLine
        }
    }

    private static func gateway(_ input: String) -> (Health, String) {
        guard let url = Configuration.secureURL(input) else { return (.unknown, "Set the gateway URL in Settings.") }
        let endpoint = url.appendingPathComponent("healthz")
        guard let output = Tool.output("/usr/bin/curl", ["--disable", "--fail", "--silent", "--max-time", "5",
                                                        "--max-filesize", "4096", endpoint.absoluteString], timeout: 6),
              let data = output.data(using: .utf8),
              let response = try? JSONDecoder().decode([String: String].self, from: data),
              response["status"] == "ok" else {
            return (.unavailable, "The HTTPS health check failed. Check Tailscale and the gateway URL.")
        }
        return (.healthy, "The private HTTPS endpoint responded.")
    }

    private static func agent(_ label: String, scheduled: Bool, output: String?) -> (Health, String) {
        guard let output else { return (.unknown, "Could not read this Mac’s LaunchAgents.") }
        let fields = output.split(whereSeparator: \.isNewline).lazy
            .map { $0.split(whereSeparator: \.isWhitespace) }
            .first { $0.count == 3 && $0[2] == label }
        guard let fields else { return (.unknown, "LaunchAgent is not loaded.\n" + label) }
        if fields[0] != "-" { return (.healthy, "Running · PID \(fields[0])\n" + label) }
        if fields[1] != "0" { return (.attention, "Last exit status: \(fields[1])\n" + label) }
        if scheduled { return (.idle, "Loaded and waiting for its next scheduled run.\n" + label) }
        return (.unavailable, "Loaded but stopped.\n" + label)
    }
}

enum Tool {
    static func output(_ executable: String, _ arguments: [String], timeout: TimeInterval) -> String? {
        let process = Process()
        let pipe = Pipe()
        process.executableURL = URL(fileURLWithPath: executable)
        process.arguments = arguments
        process.standardInput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        process.standardOutput = pipe
        do { try process.run() } catch { return nil }
        let deadline = DispatchWorkItem { if process.isRunning { process.terminate() } }
        DispatchQueue.global().asyncAfter(deadline: .now() + timeout, execute: deadline)
        defer { deadline.cancel() }
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        guard process.terminationStatus == 0 else { return nil }
        return String(data: data, encoding: .utf8)
    }
}

struct PiReport {
    var units: [String: [String: String]] = [:]
    var observation: Observation?
    var hostOverview: HostOverview?
    var opportunity: OpportunityReport?
    var casinoExit: CasinoExitReport?
    var issue: String?

    static let separator = "\n--OBSERVATION--\n"
    static let hostSeparator = "\n--HOST--\n"
    static let opportunitySeparator = "\n--OPPORTUNITY--\n"
    static let casinoExitSeparator = "\n--CASINO-EXIT--\n"

    static func read(host: String, units: Set<String>, observation: Bool) -> Self {
        if units.isEmpty && !observation { return Self() }
        guard !host.isEmpty else { return Self(issue: "Set the Pi’s SSH host in Settings.") }
        guard Configuration(host: host).validationMessage == nil else { return Self(issue: "Invalid SSH host.") }
        // Unit names come only from the bundled catalog. Keep the remote command
        // read-only and let OpenSSH enforce the user's existing host trust.
        let unitList = units.sorted().map(shellQuote)
        let unitCommand = unitList.isEmpty ? "" : """
            LC_ALL=C systemctl show --no-pager \
            -p Id,LoadState,ActiveState,SubState,Result,NRestarts,NextElapseUSecRealtime,ExecMainExitTimestamp -- \(unitList.joined(separator: " "));
            """
        let sampleCommand = observation ? """
            for sample in /var/lib/home-services/observation/*.jsonl; do latest="$sample"; done;
            if [ -r "$latest" ]; then tail -n 1 -- "$latest"; fi;
            """ : ""
        let command = unitCommand + " printf '\\n--OBSERVATION--\\n'; " + sampleCommand
            + " printf '\\n--HOST--\\n';\n" + HostDiagnostics.command
            + "\nprintf '\\n--OPPORTUNITY--\\n'; curl -sS --max-time 4 http://127.0.0.1:8089/api/operator/status 2>/dev/null; :"
        let casinoCommand = """
            printf '\n--CASINO-EXIT--\n';
            name=$(tailscale status --json 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))');
            if [ -n "$name" ]; then
              curl --fail --silent --max-time 4 --max-filesize 4096 "https://$name:8768/health";
            fi; :
            """
        guard let output = ssh(host: host, command: command + "; " + casinoCommand, timeout: 15) else {
            return Self(issue: "Pi status is unavailable over SSH. Check Tailscale and your SSH connection.")
        }
        return parse(output)
    }

    static func shellQuote(_ value: String) -> String {
        "'" + value.replacingOccurrences(of: "'", with: "'\\''") + "'"
    }

    static func ssh(host: String, command: String, timeout: TimeInterval) -> String? {
        guard !host.isEmpty, Configuration(host: host).validationMessage == nil else { return nil }
        return Tool.output("/usr/bin/ssh", [
            "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=4",
            "-o", "ServerAliveInterval=3", "-o", "ServerAliveCountMax=1", "--", host, command
        ], timeout: timeout)
    }

    static func parse(_ output: String) -> Self {
        let parts = output.components(separatedBy: separator)
        guard parts.count == 2 else { return Self(issue: "The Pi returned an unrecognized response.") }
        var units: [String: [String: String]] = [:]
        for block in parts[0].components(separatedBy: "\n\n") {
            var properties: [String: String] = [:]
            for line in block.split(whereSeparator: \.isNewline) {
                let pair = line.split(separator: "=", maxSplits: 1, omittingEmptySubsequences: false)
                if pair.count == 2 { properties[String(pair[0])] = String(pair[1]) }
            }
            if let id = properties["Id"] { units[id] = properties }
        }
        let payloads = parts[1].components(separatedBy: hostSeparator)
        let decoder = JSONDecoder()
        let sample = try? decoder.decode(Observation.self, from: Data(payloads[0].utf8))
        let extras = payloads.count == 2 ? payloads[1].components(separatedBy: opportunitySeparator) : []
        let host = extras.first.flatMap { try? decoder.decode(HostOverview.self, from: Data($0.utf8)) }
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        let serviceReports = extras.count == 2 ? extras[1].components(separatedBy: casinoExitSeparator) : []
        let opportunity = serviceReports.first.flatMap { try? decoder.decode(OpportunityReport.self, from: Data($0.utf8)) }
        let casinoExit = serviceReports.count == 2
            ? try? decoder.decode(CasinoExitReport.self, from: Data(serviceReports[1].utf8)) : nil
        return Self(units: units, observation: sample, hostOverview: host, opportunity: opportunity, casinoExit: casinoExit)
    }

    var casinoExitStatus: (Health, String) {
        if let issue { return (.unknown, issue) }
        guard let casinoExit, casinoExit.status == "ok", casinoExit.service == "casino-exit-runner" else {
            return (.unavailable, "The Casino Exit runner did not pass its HTTP health check.")
        }
        return casinoExit.check
    }

    var opportunityStatus: (Health, String) {
        if let issue { return (.unknown, issue) }
        guard let opportunity else { return (.unavailable, "The Pi backend did not return a status report.") }
        return opportunity.check
    }

    var schedules: [ScheduledTask] {
        guard let maintenance = ServiceGroup.catalog.first(where: { $0.id == "maintenance" }) else { return [] }
        var result: [ScheduledTask] = maintenance.checks.compactMap { check in
            guard case let .unit(timer, job?) = check.probe else { return nil }
            let timing = hostOverview?.timers?.first { $0.unit == timer }
            let state = switch units[timer]?["ActiveState"] {
            case "active": units[job]?["ActiveState"] == "activating" ? "Running now" : "Scheduled"
            case "failed": "Failed"
            case "inactive": units[timer]?["LoadState"] == "loaded" ? "Inactive" : "Unavailable"
            default: "Unavailable"
            }
            let lastResult = units[job]?["Result"]
            let detail = if let lastResult, lastResult != "success" { "Last run reported \(lastResult)." } else { "" }
            return ScheduledTask(id: timer, name: check.name, owner: "Raspberry Pi", state: state,
                                 next: timing?.nextRun, last: timing?.lastTrigger,
                                 detail: detail.isEmpty ? nil : detail)
        }
        if let next = hostOverview?.cleanup_next_epoch {
            let worker = units["home-services-worker@storage.service"]?["ActiveState"] == "active"
            result.append(ScheduledTask(id: "storage.cleanup", name: "Storage cleanup", owner: "Raspberry Pi",
                                        state: worker ? "Estimated" : "Worker unavailable",
                                        next: worker ? Date(timeIntervalSince1970: next) : nil, last: nil,
                                        detail: "The storage worker queues cleanup on the hour. Find job results in Activity."))
        }
        return result
    }

    func unit(_ name: String, job: String?) -> (Health, String) {
        if let issue { return (.unknown, issue) }
        guard let unit = units[name], let active = unit["ActiveState"], unit["LoadState"] == "loaded" else {
            return (.unknown, "Unit is missing or its state is unavailable.\n" + name)
        }
        var details = [name, "Systemd: \(active) / \(unit["SubState"] ?? "unknown")"]
        var health: Health
        switch active {
        case "active": health = .healthy
        case "activating", "deactivating", "reloading": health = .attention
        case "inactive", "failed": health = .unavailable
        default: health = .unknown
        }
        if let restarts = unit["NRestarts"], !restarts.isEmpty { details.append("Restarts: " + restarts) }
        if let next = unit["NextElapseUSecRealtime"], !next.isEmpty, next != "n/a" {
            details.append("Next run: \(next) (Pi time)")
        }
        if let job, let last = units[job], let result = last["Result"] {
            if result != "success" {
                if health == .healthy { health = .attention }
                details.append("Last run result: " + result)
            }
            if let exited = last["ExecMainExitTimestamp"], !exited.isEmpty, exited != "n/a" {
                details.append("Last exit: \(exited) (Pi time)")
            }
        }
        return (health, details.joined(separator: "\n"))
    }

    var observationIssue: String? {
        if let issue { return issue }
        guard let observation else { return "No readable Home Services observation sample." }
        let date = Date(timeIntervalSince1970: observation.ts)
        guard (-300...1800).contains(Date.now.timeIntervalSince(date)) else {
            return "The observation sample is stale or the Pi clock is out of sync.\nSample: " + StatusFormat.date(date)
        }
        return nil
    }

    func observed(_ status: (Observation) -> (Health, String)) -> (Health, String) {
        if let observationIssue { return (.unknown, observationIssue) }
        guard let observation else { return (.unknown, "No readable Home Services observation sample.") }
        let timestamp = StatusFormat.date(Date(timeIntervalSince1970: observation.ts))
        let (health, detail) = status(observation)
        return (health, detail + "\nSample: " + timestamp)
    }

}

struct Observation: Decodable {
    struct Series: Decodable {
        var host: String
        var tags: [String]
        var state: String
        var age_hours: Double
        var empty: Bool?
        var retired: Bool?
    }

    struct Camera: Decodable {
        var state: String
        var covered_seconds: Double
        var motion_events: Int
    }

    var ts: Double
    var readiness: [String: String]?
    var backups: [Series]?
    var cameras: [String: Camera]?
    var outbox: [String: Int]?

    // Older samples and per-module failures must not hide other evidence.
    init(from decoder: Decoder) throws {
        let data = try decoder.container(keyedBy: CodingKeys.self)
        ts = try data.decode(Double.self, forKey: .ts)
        readiness = try? data.decode([String: String].self, forKey: .readiness)
        backups = try? data.decode([Series].self, forKey: .backups)
        cameras = try? data.decode([String: Camera].self, forKey: .cameras)
        outbox = try? data.decode([String: Int].self, forKey: .outbox)
    }

    enum CodingKeys: String, CodingKey { case ts, readiness, backups, cameras, outbox }

    func roots(_ keys: [String]) -> (Health, String) {
        let matches = (readiness ?? [:]).filter { entry in
            keys.contains { $0.hasSuffix(":") ? entry.key.hasPrefix($0) : entry.key == $0 }
        }.sorted { $0.key < $1.key }
        guard !matches.isEmpty else { return (.unknown, "No matching readiness data.") }
        let health = matches.map { entry -> Health in
            switch entry.value {
            case "ready": .healthy
            case "disabled": .idle
            case "unavailable", "unsafe": .unavailable
            case "degraded": .attention
            default: .unknown
            }
        }
        return (Health.rollup(health), matches.map { "\($0.key): \($0.value)" }.joined(separator: "\n"))
    }

    var backupStatus: (Health, String) {
        guard let backups else { return (.unknown, "Repository protection data is unavailable.") }
        let current = backups.filter { $0.retired != true }
        guard !current.isEmpty else { return (.unknown, "No active backup series in the sample.") }
        let health = current.map { $0.state == "current" && $0.empty != true ? Health.healthy : .attention }
        let details = current.map {
            let series = $0.tags.isEmpty ? $0.host : $0.tags.joined(separator: ", ")
            return "\(series): \($0.empty == true ? "empty" : $0.state) · \($0.age_hours.formatted(.number.precision(.fractionLength(1)))) h old"
        }
        return (Health.rollup(health), details.joined(separator: "\n"))
    }

    var cameraStatus: (Health, String) {
        guard let cameras, !cameras.isEmpty else {
            return readiness?["cameras"] == "disabled" ? (.idle, "Recording is disabled.") : (.unknown, "No recording coverage in the sample.")
        }
        let health = cameras.values.map { $0.state == "recording" && $0.covered_seconds >= 855 ? Health.healthy : .attention }
        let details = cameras.sorted(by: { $0.key < $1.key }).map { name, camera in
            let coverage = min(1, camera.covered_seconds / 900).formatted(.percent.precision(.fractionLength(0)))
            return "\(name): \(camera.state) · \(coverage) coverage in 15 min · \(camera.motion_events) motion events"
        }
        return (Health.rollup(health), details.joined(separator: "\n"))
    }

    var notificationStatus: (Health, String) {
        if readiness?["notifications"] == "disabled" { return (.idle, "Delivery is disabled.") }
        guard let outbox, let failed = outbox["failed"], let pending = outbox["pending"] else {
            return (.unknown, "No delivery queue data in the sample.")
        }
        return (failed > 0 ? .attention : .healthy, "\(pending) pending · \(failed) failed · \(outbox["delivered"] ?? 0) delivered")
    }
}
