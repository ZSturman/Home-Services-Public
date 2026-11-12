import Foundation

struct DiagnosticResult {
    let health: Health
    let message: String
    var nextStep: String? = nil
}

enum Troubleshooting {
    static let ssh = "Check that this Mac is on the same network or tailnet as the Pi. Verify the Pi SSH host in Settings and that SSH can sign in without a prompt."
    static let sample = "Open Schedules and check Health sampling. Load Recent Warnings for its last error, then check that the Pi’s clock is correct."
    static let drive = "Check the backup drive’s power, USB connection, and configured mount. If the wrong drive is mounted, reconnect the expected drive before retrying."
    static let widget = "Open Edit Widgets on the desktop and add Home Services Status. If an existing widget stays stale, remove and add that instance again."
    static let token = "In Settings → Operator access, store a current scoped token for the exact HTTPS gateway URL."
    static let relay = "Confirm Content Manager’s article sync is enabled, then check the Mac companions page and the Pi connection."

    static func step(for probe: Probe, group: String, health: Health, pi: PiReport) -> String? {
        guard [.unknown, .attention, .unavailable].contains(health) else { return nil }
        switch probe {
        case .gateway:
            return "Check Tailscale and the private HTTPS gateway URL in Settings. If SSH works, check the Gateway service and recent Pi warnings."
        case .agent(let label, _):
            if label == "com.zacharysturman.filesbackups.agent" {
                return "Open Files & Backups and verify its Workspace schedule and Mac companion are enabled."
            }
            if label == "com.zacharysturman.ContentManager.ArticleSync" { return relay }
            return "Check the Mac companion installation and login items, then refresh after the agent is running."
        default: break
        }
        if pi.issue != nil { return ssh }
        switch probe {
        case .unit(_, .some):
            return "Open Schedules to check the next trigger and the last run result. Load Recent Warnings for the service error before retrying."
        case .unit:
            return "Expand Technical details for the service name. Load Recent Warnings to see why it stopped or failed."
        case .gateway, .agent:
            return nil
        case .casinoExit:
            return "Expand Technical details for browser capacity, discovery, contact research, and the last synchronization."
        case .opportunityReview:
            return "Expand Technical details to see failed sources, database readiness and email setup."
        default: break
        }
        if pi.observationIssue != nil { return sample }
        switch group {
        case "storage": return drive
        case "backups":
            return "Check Pi Storage and the recovery repository, then check the last Workspace run in Files & Backups. A scheduled run can be missed when the Mac is asleep or the destination is unreachable."
        case "cameras":
            return "Check that the Pi camera stream and camera worker are running. Verify the camera connection and recording drive before testing playback in Camera Monitor."
        case "alerts":
            return "Check that the notifier is running and the Pi can reach the configured notification topic. Use the outbox delivery check to verify a new test."
        default:
            return "Load Recent Warnings and check that Home Services can read its application-data filesystem."
        }
    }

    static func api(_ error: Error, delivery: Bool) -> DiagnosticResult {
        let step: String
        switch error {
        case OperatorError.noConnection:
            step = "Save the Pi’s private HTTPS gateway URL in Settings and run the test again."
        case OperatorError.missingToken, OperatorError.invalidToken, OperatorError.response(401):
            step = token
        case OperatorError.keychain:
            step = "Unlock your login Keychain and allow Home Services Status to access its saved token, or store a replacement in Settings."
        case OperatorError.response(403):
            step = delivery ? "Use a console token with notifications:admin for sending tests and checking delivery." : token
        case OperatorError.response(404), OperatorError.invalidResponse:
            step = "Check that the URL points to the Home Services API gateway. The compatibility web dashboard has a separate address."
        default:
            step = "Check Tailscale and the Gateway page. Use Test API Access before retrying an operator action."
        }
        return DiagnosticResult(health: .attention, message: error.localizedDescription, nextStep: step)
    }
}
