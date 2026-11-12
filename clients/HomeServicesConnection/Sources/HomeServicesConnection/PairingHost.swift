import SwiftUI

/// App-owned provider integration. The callback uses the existing adapter's
/// persistence API. Failures stay visible and can be retried without re-pairing.
public struct PiConnectionHost: View {
    private let preset: String
    private let service: String
    private let profile: String
    private let provider: PairingProvider
    private let apply: (PairedCredential?) async throws -> Void
    @State private var client: PairingClient?
    @State private var error: String?
    @Environment(\.scenePhase) private var scenePhase

    public init(preset: String, keychainService: String, profile: String = "default",
                provider: PairingProvider = .homeServices,
                apply: @escaping (PairedCredential?) async throws -> Void) {
        self.preset = preset; service = keychainService; self.profile = profile
        self.provider = provider; self.apply = apply
    }
    public var body: some View {
        VStack(spacing: 0) {
            if let error {
                Text(error).foregroundStyle(.red).padding()
                Button("Retry Applying Connection") { Task { await applySaved() } }
            }
            if let client {
                PiConnectionView(client: client, deviceName: ProcessInfo.processInfo.hostName)
                    .task(id: client.credential) { await applySaved() }
            } else {
                ProgressView("Reading saved connection…")
                if error != nil { Button("Retry Keychain") { initialize() } }
            }
        }
        .task { initialize() }
        .onChange(of: scenePhase) { _, phase in
            guard phase == .active, let client, client.credential != nil else { return }
            Task { do { try await client.refresh() } catch { self.error = error.localizedDescription } }
        }
    }
    private func initialize() {
        guard client == nil else { return }
        do { client = try PairingClient(expectedPreset: preset, keychainService: service, profile: profile, provider: provider); error = nil }
        catch { self.error = error.localizedDescription }
    }
    private func applySaved() async {
        guard let client else { return }
        do { try await apply(client.credential); error = nil }
        catch { self.error = "Connection saved, but this app could not apply it: " + error.localizedDescription }
    }
}
