import Foundation

struct OpportunityReport: Decodable {
    struct Schedule: Decodable {
        var active: Bool?
        var nextRun: String?
    }

    struct Discovery: Decodable {
        var lastSuccess: String?
        var sourceFailures: [String]
        var newOpportunities: Int?
        var matchedExisting: Int?
        var expiredInArchive: Int?
        var timer: Schedule
    }

    struct Outreach: Decodable {
        var drafts: Int
        var waiting: Int
        var pendingFollowups: Int
        var failedOrUncertainAttempts: Int
        var emailIntegration: String
        var emailCheckedAt: String?
        var lastSubmission: String?
    }

    var database: String
    var macRequired: Bool
    var discovery: Discovery?
    var outreach: Outreach?

    var check: (Health, String) {
        guard database == "available", let discovery, let outreach else {
            return (.unavailable, "The Opportunity Review database is unavailable.")
        }
        let healthy = discovery.sourceFailures.isEmpty && discovery.lastSuccess != nil &&
            outreach.emailIntegration == "authenticated" && outreach.failedOrUncertainAttempts == 0
        var lines = [
            healthy ? "Discovery and Gmail sign-in are healthy." : "Discovery or email needs attention.",
            "Database: available",
            "Mac required for the backend: \(macRequired ? "yes" : "no")",
            "Last successful source refresh: \(discovery.lastSuccess ?? "Awaiting refresh")",
            "Next scheduled refresh: \(discovery.timer.nextRun ?? "Unavailable")",
            "Email integration: \(outreach.emailIntegration)",
            "Drafts waiting for review: \(outreach.drafts)",
            "Approved messages waiting to send: \(outreach.waiting)",
            "Last email submission: \(outreach.lastSubmission ?? "None on record")",
            "Failed or uncertain attempts: \(outreach.failedOrUncertainAttempts)",
            "Pending follow-ups: \(outreach.pendingFollowups)"
        ]
        if let checked = outreach.emailCheckedAt {
            let parser = ISO8601DateFormatter()
            parser.formatOptions.insert(.withFractionalSeconds)
            lines.append("Gmail sign-in checked: \(parser.date(from: checked)?.formatted(date: .abbreviated, time: .shortened) ?? checked)")
        }
        if let count = discovery.newOpportunities { lines.append("New opportunities: \(count)") }
        if let count = discovery.matchedExisting { lines.append("Matched existing records: \(count)") }
        if let count = discovery.expiredInArchive { lines.append("Older or expired in archive: \(count)") }
        if !discovery.sourceFailures.isEmpty {
            lines.append("Source failures: \(discovery.sourceFailures.joined(separator: ", "))")
        }
        return (healthy ? .healthy : .attention, lines.joined(separator: "\n"))
    }
}
