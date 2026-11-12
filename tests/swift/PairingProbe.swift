import Foundation

@main enum PairingProbe {
    static func main() {
        let data = FileHandle.standardInput.readDataToEndOfFile()
        guard let text = String(data: data, encoding: .utf8) else { exit(1) }
        do {
            let invitation = try PairingInvitation.parse(text, expectedPreset: "home-page")
            print(invitation.server_id)
        } catch { exit(1) }
    }
}
