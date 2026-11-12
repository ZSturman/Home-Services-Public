import AppKit
import CoreImage.CIFilterBuiltins
import SwiftUI

struct ConnectionCatalog: Decodable {
    struct Preset: Decodable, Identifiable {
        let id: String
        let scopes: [String]
        var title: String {
            switch id {
            case "home-page": "Home Page"
            case "content-manager": "Content Manager"
            case "article-companion": "Content Manager · Mac companion"
            case "opportunity-review": "Opportunity Review · Home Services relay"
            case "opportunity-companion": "Opportunity Discovery · relay companion"
            case "chewsense-phone": "Chew Sense · iPhone"
            case "chewsense-mac": "Chew Sense · Mac"
            case "casino-exit": "Casino Exit · Pi runner"
            case "camera-monitor": "Camera Monitor"
            case "files-backups": "Files & Backups"
            case "files-backups-companion": "Files & Backups · reporting companion"
            case "home-platform-legacy": "Home Platform · storage status"
            default: id.replacingOccurrences(of: "-", with: " ").capitalized
            }
        }
    }
    struct Camera: Decodable, Identifiable { let id: String; let name: String }
    let presets: [Preset]
    let permissions: [String: String]
    let cameras: [Camera]
}

struct ManagedConnection: Decodable, Identifiable {
    struct Credential: Decodable, Identifiable {
        let id: String
        let name: String
        let scopes: [String]
        let revoked_at: String?
        let last_used_at: String?
    }
    let id: String
    let preset: String
    let device_name: String
    let allowed_scopes: [String]
    let preferences: [String: String]
    let token: Credential
}

struct ConnectionListing: Decodable {
    struct Pending: Decodable, Identifiable { let id: String; let name: String; let expires_at: String }
    let connections: [ManagedConnection]
    let existing_tokens: [ManagedConnection.Credential]
    let pending: [Pending]
}

struct ConnectionInvitation: Decodable {
    struct Invitation: Decodable { let invitation_id: String; let expires_at: String }
    let invitation: Invitation
    let pairing_uri: String
    let name: String
    var expires: Date {
        let fractional = ISO8601DateFormatter()
        fractional.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return fractional.date(from: invitation.expires_at) ?? ISO8601DateFormatter().date(from: invitation.expires_at) ?? .distantPast
    }
}

enum ConnectionBackend: String, CaseIterable, Identifiable {
    case homeServices = "Home Services"
    case casinoExit = "Casino Exit · Pi runner"
    case opportunityReview = "Opportunity Review · direct Pi backend"
    var id: String { rawValue }
}

enum PiConnectionAdmin {
    static func run<T: Decodable>(host: String, arguments: [String], backend: ConnectionBackend = .homeServices) async throws -> T {
        try await Task.detached { try runSync(host: host, arguments: arguments, backend: backend) }.value
    }

    private static func runSync<T: Decodable>(host: String, arguments: [String], backend: ConnectionBackend) throws -> T {
        // Existing SSH trust, no new administrative HTTP credential or public listener.
        let command: [String]
        switch backend {
        case .homeServices: command = ["/opt/home-services/current/venv/bin/home-services", "--config", "/etc/home-services/config.toml", "connections"]
        case .opportunityReview: command = ["sudo", "-n", "-u", "opportunity-review", "env", "OD_DB_PATH=/var/lib/opportunity-review/opportunities.db",
               "/usr/bin/python3", "/opt/opportunity-review/current/pi_connections.py"]
        case .casinoExit: command = ["sh", "-c", "CASINO_CONNECTIONS_PATH=\"$HOME/casino-exit-runner/data/connections.sqlite3\" \"$HOME/casino-exit-runner/venv/bin/python\" -m casino_exit.companion.pi_connections \"$@\"", "pairing"]
        }
        let args = command + arguments
        guard let output = PiReport.ssh(host: host, command: args.map(PiReport.shellQuote).joined(separator: " "), timeout: 15),
              let data = output.data(using: .utf8), let result = try? JSONDecoder().decode(T.self, from: data) else {
            throw NSError(domain: "HomeServicesConnections", code: 1, userInfo: [NSLocalizedDescriptionKey:
                "The Pi could not complete this connection request. Check SSH access and that the gateway release includes connections. Folder selections must exist on the mounted disk."])
        }
        return result
    }
}

struct ConnectionsView: View {
    let configuration: Configuration
    @State private var backend: ConnectionBackend = .homeServices
    @State private var independentURL = ""
    @State private var catalog: ConnectionCatalog?
    @State private var listing: ConnectionListing?
    @State private var preset = "home-page"
    @State private var name = ""
    @State private var scopes: Set<String> = ["status:read"]
    @State private var invitation: ConnectionInvitation?
    @State private var busy = false
    @State private var error: String?
    @State private var revoking: ManagedConnection?

    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            Text("Connect an app by scanning or pasting a five-minute invitation. Each device receives its own revocable access.")
                .foregroundStyle(.secondary)
            if let error { Text(error).foregroundStyle(.red).textSelection(.enabled) }
            Picker("Backend", selection: $backend) {
                ForEach(ConnectionBackend.allCases) { backend in Text(backend.rawValue).tag(backend) }
            }.onChange(of: backend) { _, _ in
                invitation = nil; catalog = nil; listing = nil
                preset = backend == .homeServices ? "home-page" : (backend == .opportunityReview ? "opportunity-review" : "casino-exit")
                if var url = URLComponents(string: configuration.gateway) {
                    url.port = backend == .opportunityReview ? 8444 : 8768; independentURL = url.string ?? ""
                }
                refresh()
            }
            if backend != .homeServices {
                TextField("Backend HTTPS origin", text: $independentURL)
                Text("This backend owns separate credentials and permissions. Its connection does not use a Home Services token.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            HStack {
                Button("Refresh Connections", systemImage: "arrow.clockwise") { refresh() }
                if busy { ProgressView().controlSize(.small) }
            }
            if let catalog {
                GroupBox("Connect an app") {
                    VStack(alignment: .leading, spacing: 12) {
                        Picker("App / service", selection: $preset) {
                            ForEach(catalog.presets) { p in
                                Text(backend == .opportunityReview && p.id == "opportunity-review" ? "Opportunity Review" : p.title).tag(p.id)
                            }
                        }.onChange(of: preset) { _, new in
                            scopes = Set(catalog.presets.first { $0.id == new }?.scopes ?? [])
                        }
                        TextField("Connection name, such as Camera Monitor · iPhone", text: $name)
                        ForEach(catalog.presets.first { $0.id == preset }?.scopes ?? [], id: \.self) { scope in
                            Toggle(catalog.permissions[scope] ?? scope, isOn: Binding(
                                get: { scopes.contains(scope) },
                                set: { if $0 { scopes.insert(scope) } else { scopes.remove(scope) } }))
                        }
                        Button("Generate QR Code", systemImage: "qrcode") { generate() }
                            .disabled(name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || scopes.isEmpty)
                        Text("Choose Pi Connection in the receiving app. This backend keeps its own credentials and permissions.")
                            .font(.caption).foregroundStyle(.secondary)
                    }.padding(8)
                }
                if let invitation {
                    TimelineView(.periodic(from: .now, by: 1)) { context in
                        VStack(alignment: .leading, spacing: 10) {
                            Text(invitation.name).font(.headline)
                            if context.date < invitation.expires {
                                if let image = qr(invitation.pairing_uri) {
                                    Image(nsImage: image).interpolation(.none).resizable().frame(width: 260, height: 260)
                                        .padding(12).background(.white).accessibilityLabel("Pairing QR code")
                                }
                                Text("Open Connect to Pi in the receiving app, then scan this code or paste the invitation.")
                                Text("Expires in \(max(0, Int(invitation.expires.timeIntervalSince(context.date)))) seconds.")
                                    .font(.caption).foregroundStyle(.secondary)
                                Button("Copy Pairing Invitation") {
                                    let board = NSPasteboard.general
                                    board.clearContents()
                                    board.setString(invitation.pairing_uri, forType: .string)
                                    let changeCount = board.changeCount
                                    let delay = max(0, invitation.expires.timeIntervalSinceNow)
                                    DispatchQueue.main.asyncAfter(deadline: .now() + delay) {
                                        if board.changeCount == changeCount { board.clearContents() }
                                    }
                                }
                            } else { Text("Invitation expired. Generate a new code to connect.").foregroundStyle(.secondary) }
                        }.padding()
                    }
                }
                if let listing {
                    ForEach(listing.pending) { pending in
                        HStack {
                            Text("Waiting: " + pending.name)
                            Spacer()
                            Button("Cancel Invitation") {
                                perform {
                                    let _: [String: StringOrBool] = try await PiConnectionAdmin.run(host: configuration.host, arguments: ["cancel", pending.id], backend: backend)
                                }
                            }
                        }
                    }
                    Text("Paired apps").font(.title2.bold())
                    if listing.connections.isEmpty { Text("No managed connections yet.").foregroundStyle(.secondary) }
                    ForEach(listing.connections) { connection in
                        ConnectionAccessCard(connection: connection, catalog: catalog, update: { enabled, preferences in
                            perform {
                                var arguments = ["update", connection.id]
                                if let enabled {
                                    arguments += enabled.isEmpty ? ["--disable-all"] : enabled.sorted().flatMap { ["--scope", $0] }
                                }
                                if let preferences {
                                    arguments += ["--preferences-json", String(decoding: try JSONEncoder().encode(preferences), as: UTF8.self)]
                                }
                                let _: ManagedConnection = try await PiConnectionAdmin.run(host: configuration.host, arguments: arguments, backend: backend)
                            }
                        }, revoke: { revoking = connection })
                    }
                    DisclosureGroup("Existing credentials (\(listing.existing_tokens.count))") {
                        Text("Select the matching app preset above, then adopt a credential to manage it here. Its saved token continues working.")
                            .font(.caption).foregroundStyle(.secondary)
                        ForEach(listing.existing_tokens.filter { $0.revoked_at == nil }) { token in
                            HStack {
                                VStack(alignment: .leading) {
                                    Text(token.name)
                                    Text(token.scopes.joined(separator: ", ")).font(.caption).foregroundStyle(.secondary)
                                }
                                Spacer()
                                Button("Manage as \(preset)") {
                                    perform {
                                        let _: ManagedConnection = try await PiConnectionAdmin.run(host: configuration.host, arguments: ["adopt", token.id, "--preset", preset], backend: backend)
                                    }
                                }.disabled(!Set(token.scopes).isSubset(of: Set(catalog.presets.first { $0.id == preset }?.scopes ?? [])))
                            }.padding(.vertical, 5)
                        }
                    }
                }
            }
        }
        .disabled(busy)
        .task(id: configuration.host) { refresh() }
        .task(id: invitation?.invitation.invitation_id) {
            guard let current = invitation else { return }
            var failures = 0
            while Date.now < current.expires {
                do { try await Task.sleep(for: .seconds(5)) } catch { return }
                guard !busy, !Task.isCancelled else { continue }
                do {
                    let updated: ConnectionListing = try await PiConnectionAdmin.run(host: configuration.host, arguments: ["list"], backend: backend)
                    guard !Task.isCancelled else { return }
                    listing = updated
                    failures = 0
                    if !updated.pending.contains(where: { $0.id == current.invitation.invitation_id }) {
                        invitation = nil
                        return
                    }
                } catch {
                    failures += 1
                    if failures >= 3 { self.error = error.localizedDescription; return }
                }
            }
        }
        .confirmationDialog("Disconnect \(revoking?.token.name ?? "app")?", isPresented: Binding(
            get: { revoking != nil }, set: { if !$0 { revoking = nil } }), titleVisibility: .visible) {
                if let connection = revoking {
                    Button("Disconnect", role: .destructive) {
                        perform {
                            let _: ManagedConnection = try await PiConnectionAdmin.run(host: configuration.host, arguments: ["revoke", connection.id], backend: backend)
                        }
                    }
                }
        } message: { Text("This device will need a new invitation to connect again.") }
    }

    private func refresh() { perform {} }

    private func generate() {
        perform {
            guard let endpoint = Configuration.secureURL(backend == .homeServices ? configuration.gateway : independentURL) else { throw OperatorError.noConnection }
            let result: ConnectionInvitation = try await PiConnectionAdmin.run(host: configuration.host, arguments: ["invite", "--preset", preset, "--name", name,
                    "--base-url", endpoint.absoluteString] + scopes.sorted().flatMap { ["--scope", $0] }, backend: backend)
            invitation = result
        }
    }

    private func perform(_ operation: @escaping () async throws -> Void) {
        guard !busy else { return }
        busy = true
        error = nil
        Task { @MainActor in
            defer { busy = false }
            do {
                // SSH processes run off the main actor, including mutations.
                try await operation()
                let host = configuration.host
                let backend = backend
                let result = try await Task.detached {
                    let catalog: ConnectionCatalog = try await PiConnectionAdmin.run(host: host, arguments: ["catalog"], backend: backend)
                    let listing: ConnectionListing = try await PiConnectionAdmin.run(host: host, arguments: ["list"], backend: backend)
                    return (catalog, listing)
                }.value
                if catalog == nil { scopes = Set(result.0.presets.first { $0.id == preset }?.scopes ?? []) }
                catalog = result.0
                listing = result.1
                if let invitation, !result.1.pending.contains(where: { $0.id == invitation.invitation.invitation_id }) {
                    self.invitation = nil
                }
            } catch { self.error = error.localizedDescription }
        }
    }

    private func qr(_ string: String) -> NSImage? {
        let filter = CIFilter.qrCodeGenerator()
        filter.message = Data(string.utf8)
        filter.correctionLevel = "M"
        guard let output = filter.outputImage,
              let image = CIContext().createCGImage(output.transformed(by: .init(scaleX: 8, y: 8)),
                  from: output.extent.applying(.init(scaleX: 8, y: 8))) else { return nil }
        return NSImage(cgImage: image, size: NSSize(width: image.width, height: image.height))
    }
}

private enum StringOrBool: Decodable { case string(String), bool(Bool)
    init(from decoder: Decoder) throws {
        let value = try decoder.singleValueContainer()
        if let string = try? value.decode(String.self) { self = .string(string) }
        else { self = .bool(try value.decode(Bool.self)) }
    }
}

private struct ConnectionAccessCard: View {
    let connection: ManagedConnection
    let catalog: ConnectionCatalog
    let update: (Set<String>?, [String: String]?) -> Void
    let revoke: () -> Void
    @State private var folder = ""

    var body: some View {
        GroupBox {
            VStack(alignment: .leading, spacing: 10) {
                HStack {
                    VStack(alignment: .leading) {
                        Text(connection.token.name).font(.headline)
                        Text(connection.device_name + " · " + connection.preset).foregroundStyle(.secondary)
                    }
                    Spacer()
                    if connection.token.revoked_at == nil { Button("Disconnect", role: .destructive, action: revoke) }
                    else { Text("Disconnected").foregroundStyle(.secondary) }
                }
                ForEach(connection.allowed_scopes, id: \.self) { scope in
                    Toggle(catalog.permissions[scope] ?? scope, isOn: Binding(
                        get: { connection.token.scopes.contains(scope) },
                        set: { enabled in
                            var selected = Set(connection.token.scopes)
                            if enabled { selected.insert(scope) } else { selected.remove(scope) }
                            update(selected, nil)
                        }))
                }
                if connection.allowed_scopes.contains("transfers:write") {
                    HStack {
                        TextField("Default upload folder (relative to Files)", text: $folder)
                        Button("Save Folder") { update(nil, connection.preferences.merging(["upload_folder": folder]) { _, new in new }) }
                    }
                }
                if connection.allowed_scopes.contains("cameras:read") {
                    Picker("Default camera", selection: Binding(get: { connection.preferences["camera_id"] ?? "" }, set: { selected in
                        var preferences = connection.preferences
                        preferences["camera_id"] = selected.isEmpty ? nil : selected
                        update(nil, preferences)
                    })) {
                        Text("Choose in app").tag("")
                        ForEach(catalog.cameras) { camera in Text(camera.name).tag(camera.id) }
                    }
                }
                if connection.allowed_scopes.contains("transfers:write") || connection.allowed_scopes.contains("cameras:read") {
                    Text("Folder and camera choices are app defaults. Permission toggles are enforced by the server.")
                        .font(.caption).foregroundStyle(.secondary)
                }
                Text("Last used: " + (connection.token.last_used_at ?? "No request yet")).font(.caption).foregroundStyle(.secondary)
            }.padding(8)
        }
        .disabled(connection.token.revoked_at != nil)
        .onAppear { folder = connection.preferences["upload_folder"] ?? "" }
    }
}
