import Foundation

struct CasinoExitReport: Decodable {
    struct Discovery: Decodable {
        let lastRun: String
        let exitCode: Int
        let sources: [String]
        let lastSuccess: String?
    }

    struct Research: Decodable {
        let status: String
        let checkedAt: String
    }

    let status: String
    let service: String
    let version: String
    let commit: String
    let browserReady: Bool
    let canRun: Bool
    let platforms: [String]
    let outreachResearchInstalled: Bool
    let discoveryScheduled: Bool
    let failures: Int
    let unconfirmed: Int
    let lastNotionSync: String?
    let lastNotionRead: String?
    let discovery: Discovery?
    let lastResearchCheck: Research?

    var check: (Health, String) {
        let attention = !canRun || !outreachResearchInstalled || !discoveryScheduled
            || failures > 0 || unconfirmed > 0 || (discovery?.exitCode ?? 0) != 0
            || lastResearchCheck?.status == "unavailable"
        let sourceRun = discovery.map { "Last discovery: \($0.lastRun), exit \($0.exitCode)" }
            ?? "Last discovery: None recorded"
        let research = lastResearchCheck.map { "\($0.status.replacingOccurrences(of: "_", with: " ")) on \($0.checkedAt)" }
            ?? (outreachResearchInstalled ? "Installed, awaiting first check" : "Unavailable")
        let detail = [
            browserReady ? "The Pi backend answered and Chromium is installed." : "The Pi backend answered. Chromium is unavailable.",
            "Version \(version) (\(commit.prefix(7)))",
            "Can start an application: \(canRun ? "Yes" : "No")",
            "Runner platforms: \(platforms.joined(separator: ", "))",
            "Casino Exit discovery: \(discoveryScheduled ? "Scheduled" : "Unscheduled on the Pi")",
            sourceRun,
            "Last successful discovery: \(discovery?.lastSuccess ?? "None recorded")",
            "Opportunity Discovery contact research: \(research)",
            "Failed runs: \(failures). Unconfirmed submissions: \(unconfirmed).",
            "Last application sync: \(lastNotionSync ?? "None recorded")",
            "Last verified job read: \(lastNotionRead ?? "None recorded")",
        ].joined(separator: "\n")
        return (!browserReady ? .unavailable : attention ? .attention : .healthy, detail)
    }
}
