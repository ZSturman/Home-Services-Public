import Foundation

enum Probe {
    case unit(String, job: String? = nil)
    case agent(String, scheduled: Bool = false)
    case gateway, readiness([String]), backups, cameras, outbox, opportunityReview, casinoExit
}

struct Check {
    let name: String
    let probe: Probe

    init(_ name: String, _ probe: Probe) {
        self.name = name
        self.probe = probe
    }
}

struct ServiceGroup: Identifiable {
    let id: String
    let title: String
    let symbol: String
    let checks: [Check]

    init(_ id: String, _ title: String, _ symbol: String, _ checks: [Check]) {
        self.id = id
        self.title = title
        self.symbol = symbol
        self.checks = checks
    }

    static let catalog = [
        Self("gateway", "Gateway", "server.rack", [
            Check("Private HTTPS API", .gateway),
            Check("API gateway", .unit("home-services-gateway.service")),
            Check("State store", .readiness(["state_store"]))
        ]),
        Self("storage", "Storage", "externaldrive", [
            Check("Disk and storage roots", .readiness(["volume:", "storage:"])),
            Check("Storage worker", .unit("home-services-worker@storage.service"))
        ]),
        Self("backups", "Backups", "clock.arrow.circlepath", [
            Check("Current backup series", .backups),
            Check("Recovery repositories", .readiness(["backups", "backups:"])),
            Check("Backup worker", .unit("home-services-worker@backup.service")),
            Check("Recovery browser", .unit("home-services-backup-browser.service"))
        ]),
        Self("cameras", "Cameras", "video", [
            Check("Recording coverage", .cameras),
            Check("Camera worker", .unit("home-services-worker@camera.service")),
            Check("USB camera stream", .unit("home-services-camera-stream.service"))
        ]),
        Self("alerts", "Notifications", "bell", [
            Check("Delivery queue", .outbox),
            Check("Notifier", .unit("home-services-notifier.service"))
        ]),
        Self("opportunities", "Opportunity Review", "tray.full", [
            Check("Backend", .unit("home-services-opportunity-review.service")),
            Check("Discovery scheduler", .unit("home-services-opportunity-discovery.timer", job: "home-services-opportunity-job@discover.service")),
            Check("Outreach scheduler", .unit("home-services-opportunity-outbox.timer", job: "home-services-opportunity-mail@dispatch.service")),
            Check("Discovery and outreach", .opportunityReview)
        ]),
        Self("casino-exit", "Casino Exit", "briefcase", [
            Check("Application runner and outreach", .casinoExit)
        ]),
        Self("maintenance", "Maintenance", "wrench.and.screwdriver", [
            timer("Health sampling", "observe"),
            timer("Backup disk guard", "backup-disk-guard"),
            timer("Repository retention", "backup-retention"),
            timer("Repository integrity", "backup-check"),
            timer("Network recovery", "network-guard")
        ]),
        Self("network", "Network & sharing", "network", [
            Check("Compatibility dashboard", .unit("home-services-compat-dashboard.service")),
            Check("Tailscale", .unit("tailscaled.service")),
            Check("Network Manager", .unit("NetworkManager.service")),
            Check("SMB recovery share", .unit("smbd.service"))
        ]),
        Self("mac", "Mac companions", "laptopcomputer", [
            Check("Home Services bridge", .agent("com.zacharysturman.homeservices.macbridge")),
            Check("Files & Backups scheduler", .agent("com.zacharysturman.filesbackups.agent", scheduled: true)),
            Check("Article review sync", .agent("com.zacharysturman.ContentManager.ArticleSync"))
        ])
    ]

    private static func timer(_ title: String, _ suffix: String) -> Check {
        let unit = "home-services-" + suffix
        return Check(title, .unit(unit + ".timer", job: unit + ".service"))
    }
}
