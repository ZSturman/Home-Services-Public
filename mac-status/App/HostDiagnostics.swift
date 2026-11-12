import Foundation

enum HostDiagnostics {
    // Fixed code sent through the existing trusted SSH connection. Load only
    // config/volume guards. Never initialize a service DB or read article bodies.
    static let command = """
        /opt/home-services/current/venv/bin/python - <<'HOME_SERVICES_MAC_STATUS'
        import json, os, sqlite3, subprocess, time
        from contextlib import closing
        from home_services.config import load_config
        from home_services.volumes import VolumeGuard

        disks = []
        try:
            stats = os.statvfs("/")
            disks.append({"id": "system", "mount": "/", "state": "verified", "reason": "Linux root filesystem",
                          "capacity": {"total_bytes": stats.f_blocks * stats.f_frsize,
                                       "free_bytes": stats.f_bavail * stats.f_frsize}})
        except OSError:
            pass
        config = None
        try:
            config = load_config("/etc/home-services/config.toml")
            guard = VolumeGuard(config)
            disks.extend(guard.status(volume).as_dict() | {"mount": volume.mount} for volume in config.volumes)
        except Exception:
            pass
        timers = None
        try:
            data = subprocess.check_output(
                ["systemctl", "list-timers", "--all", "--no-pager", "--output=json", "home-services-*.timer"],
                stderr=subprocess.DEVNULL, timeout=3)
            timers = json.loads(data)
        except (ValueError, OSError, subprocess.SubprocessError):
            pass
        relay = None
        if config:
            try:
                path = config.data_dir / "article-relay.sqlite3"
                with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
                    db.execute("BEGIN")
                    seen = db.execute("SELECT value FROM state WHERE key='companion_seen_epoch'").fetchone()
                    queued, oldest = db.execute("SELECT count(*), min(created_at) FROM reviews WHERE state='queued'").fetchone()
                    relay = {"cached": db.execute("SELECT count(*) FROM snapshots").fetchone()[0],
                             "queued": queued, "companion_seen_epoch": float(seen[0]) if seen else None,
                             "oldest_queued_epoch": oldest}
            except (ValueError, sqlite3.Error):
                pass
        cleanup_next = None
        try:
            from home_services.worker import CLEANUP_INTERVAL_SECONDS
            cleanup_next = (int(time.time()) // CLEANUP_INTERVAL_SECONDS + 1) * CLEANUP_INTERVAL_SECONDS
        except (ImportError, ValueError, ZeroDivisionError):
            pass
        print(json.dumps({"disks": disks, "timers": timers, "relay": relay, "cleanup_next_epoch": cleanup_next}))
        HOME_SERVICES_MAC_STATUS
        """

    static func recentWarnings(host: String) -> DiagnosticResult {
        let units = ServiceGroup.catalog.flatMap(\.checks).compactMap { check -> String? in
            guard case .unit(let unit, let job) = check.probe, unit.hasPrefix("home-services-") else { return nil }
            return job ?? unit
        }
        let filters = units.map { "-u " + PiReport.shellQuote($0) }.joined(separator: " ")
        let marker = "\n--HOME-SERVICES-JOURNAL-EXIT--\n"
        let command = """
            timeout 8s journalctl --no-pager --since=-24h --priority=warning --lines=30 --output=short-iso \(filters) 2>&1
            printf '\\n--HOME-SERVICES-JOURNAL-EXIT--\\n%s\\n' "$?"
            """
        let output = PiReport.ssh(host: host, command: command, timeout: 15)
            ?? PiReport.ssh(host: host, command: command, timeout: 15)
        guard let output, let boundary = output.range(of: marker, options: .backwards) else {
            return DiagnosticResult(health: .unknown, message: "Could not reach the Pi to read its journal.",
                                    nextStep: Troubleshooting.ssh)
        }
        let message = output[..<boundary.lowerBound].trimmingCharacters(in: .whitespacesAndNewlines)
        let exitCode = output[boundary.upperBound...].trimmingCharacters(in: .whitespacesAndNewlines)
        if exitCode == "124" {
            return DiagnosticResult(health: .unknown, message: "The Pi journal did not finish within 8 seconds.",
                                    nextStep: "Retry when the Pi is less busy. If it repeats, check Pi Storage and the system journal for I/O issues.")
        }
        if exitCode != "0" || message.localizedCaseInsensitiveContains("not seeing messages")
            || message.localizedCaseInsensitiveContains("insufficient permissions") {
            return DiagnosticResult(health: .attention, message: message.isEmpty ? "The Pi refused the journal read." : message,
                                    nextStep: "Ask the Pi administrator to verify that the configured SSH account can read the Home Services journal.")
        }
        if message.isEmpty || message == "-- No entries --" {
            return DiagnosticResult(health: .healthy, message: "No Home Services warnings or errors in the last 24 hours.")
        }
        return DiagnosticResult(health: .attention, message: message,
                                nextStep: "Use the service name and timestamp to match this warning to the affected check above.")
    }

    private struct BackupConfiguration: Decodable {
        struct Job: Decodable {
            struct Schedule: Decodable {
                let hour: Int
                let minute: Int
            }
            let id: String
            let name: String
            let enabled: Bool
            let schedule: Schedule
        }

        let jobs: [Job]
    }

    private struct BackupReceipt: Decodable {
        let state: String
        let scheduler: Scheduler?
        let lastCheckAt: String?
        let lastCheckPassed: Bool?

        struct Scheduler: Decodable { let lastSuccessAt: String? }
    }

    static func macBackupSchedules(agentLoaded: Bool?) -> [ScheduledTask] {
        let root = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/FilesBackups", isDirectory: true)
        guard let data = try? Data(contentsOf: root.appendingPathComponent("config.json")),
              let settings = try? JSONDecoder().decode(BackupConfiguration.self, from: data) else { return [] }
        return settings.jobs.filter(\.enabled).map { job in
            let next = Calendar.current.nextDate(after: .now, matching: DateComponents(hour: job.schedule.hour, minute: job.schedule.minute),
                                                 matchingPolicy: .nextTime)
            var receipt: BackupReceipt?
            if job.id.range(of: #"^[A-Za-z0-9_-]+$"#, options: .regularExpression) != nil,
               let data = try? Data(contentsOf: root.appendingPathComponent("status/\(job.id).json")) {
                receipt = try? JSONDecoder().decode(BackupReceipt.self, from: data)
            }
            var detail = "Daily Mac schedule. Wake or reconnect can trigger a catch-up."
            if let receipt {
                detail += " Last reported state: \(receipt.state.replacingOccurrences(of: "_", with: " "))."
                if let check = StatusFormat.apiDate(receipt.lastCheckAt), let passed = receipt.lastCheckPassed {
                    detail += " Repository check \(passed ? "passed" : "failed") on \(StatusFormat.date(check))."
                }
            }
            return ScheduledTask(id: "mac-backup:" + job.id, name: job.name, owner: "Files & Backups",
                                 state: agentLoaded == true ? "Scheduled" : "Scheduler unavailable",
                                 next: agentLoaded == true ? next : nil,
                                 last: StatusFormat.apiDate(receipt?.scheduler?.lastSuccessAt), detail: detail)
        }
    }
}
