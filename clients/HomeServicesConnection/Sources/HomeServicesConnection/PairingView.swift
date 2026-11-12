import SwiftUI

/// Embed this screen in an app's connection settings. The host app still owns
/// its provider adapter and must observe the published credential/settings.
public struct PiConnectionView: View {
    @ObservedObject private var client: PairingClient
    private let deviceName: String
    @State private var text = ""
    @State private var preview: PairingPreview?
    @State private var busy = false
    @State private var error: String?
    @State private var scanning = false
    @State private var disconnecting = false
    @State private var forgetting = false
    @State private var browsing = false
    @State private var folder = ""
    @State private var folders: [PairingClient.Folder] = []
    @State private var cameras: [PairingClient.Camera] = []

    public init(client: PairingClient, deviceName: String) {
        self.client = client
        self.deviceName = deviceName
    }

    public var body: some View {
        Form {
            if let error { Section { Text(error).foregroundStyle(.red) } }
            if busy { ProgressView("Connecting…") }
            if let credential = client.credential, let connection = client.connection {
                Section("Saved connection") {
                    LabeledContent("Connection", value: connection.token.name)
                    LabeledContent("Server", value: credential.base_url)
                    LabeledContent("Device", value: connection.device_name)
                    Text(client.status).font(.caption).foregroundStyle(.secondary)
                    Button("Check Connection") { perform { try await client.refresh(); try await loadChoices() } }
                }
                Section("Use this Pi for") {
                    ForEach(connection.allowed_scopes, id: \.self) { scope in
                        Toggle(permissionName(scope), isOn: Binding(get: { connection.token.scopes.contains(scope) }, set: { enabled in
                            var scopes = Set(connection.token.scopes)
                            if enabled { scopes.insert(scope) } else { scopes.remove(scope) }
                            perform { try await client.update(scopes: scopes); try await loadChoices() }
                        }))
                    }
                }
                if connection.token.scopes.contains("transfers:write") {
                    Section("Save uploads to") {
                        Text(connection.preferences["upload_folder"].flatMap { $0.isEmpty ? "Files" : "Files/" + $0 } ?? "Choose in app")
                        if connection.token.scopes.contains("storage:read") {
                            Button("Choose Folder") {
                                // Start at Files so a moved/deleted old default
                                // never prevents choosing a replacement.
                                folder = ""
                                perform { folders = try await client.folders(at: folder); browsing = true }
                            }
                        }
                        Text("This is the app’s default upload folder. It does not restrict access to other folders.")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                }
                if connection.token.scopes.contains("cameras:read") {
                    Section("Default camera") {
                        Picker("Camera", selection: Binding(get: { connection.preferences["camera_id"] ?? "" }, set: { camera in
                            var preferences = connection.preferences
                            preferences["camera_id"] = camera.isEmpty ? nil : camera
                            perform { try await client.update(preferences: preferences) }
                        })) {
                            Text("Choose in app").tag("")
                            ForEach(cameras) { camera in Text(camera.name).tag(camera.id) }
                        }
                    }
                }
                Section {
                    Button("Disconnect from Pi", role: .destructive) { disconnecting = true }
                    Button("Forget Saved Connection", role: .destructive) { forgetting = true }
                }
            } else {
                Section("Connect to Pi") {
                    Text("Generate a QR code for this app in Home Services Status on your Mac. Both devices need access to the Pi’s private network.")
                    TextField("Paste pairing invitation", text: $text, axis: .vertical)
                        .autocorrectionDisabled()
                        .onChange(of: text) { _, _ in preview = nil }
                    #if os(iOS)
                    Button("Scan QR Code", systemImage: "qrcode.viewfinder") { scanning = true }
                    #endif
                    Button("Review Connection") { perform { preview = try await client.preview(text) } }
                        .disabled(text.isEmpty)
                    if client.hasInterruptedPairing {
                        Button("Retry Interrupted Connection") { perform { try await client.retryInterruptedPairing(); text = "" } }
                    }
                }
                if let preview {
                    Section("Connect to " + preview.name) {
                        Text(preview.base_url)
                        ForEach(preview.scopes, id: \.self) { scope in Label(permissionName(scope), systemImage: "checkmark") }
                        Button("Connect") { perform { try await client.connect(text, deviceName: deviceName); text = ""; try await loadChoices() } }
                    }
                }
            }
        }
        .formStyle(.grouped)
        .navigationTitle("Pi Connection")
        .disabled(busy)
        .task {
            if client.credential != nil { perform { try await client.refresh(); try await loadChoices() } }
        }
        #if os(iOS)
        .sheet(isPresented: $scanning) {
            PairingScanner { result in
                scanning = false
                switch result {
                case .success(let code): text = code; perform { preview = try await client.preview(code) }
                case .failure(let issue): error = issue.localizedDescription
                }
            }
        }
        #endif
        .sheet(isPresented: $browsing) {
            NavigationStack {
                List {
                    Text(folder.isEmpty ? "Files" : "Files/" + folder).font(.headline)
                    if !folder.isEmpty {
                        Button("Parent Folder", systemImage: "arrow.up") {
                            let parent = folder.split(separator: "/").dropLast().joined(separator: "/")
                            perform { let next = try await client.folders(at: parent); folder = parent; folders = next }
                        }
                    }
                    ForEach(folders, id: \.path) { entry in
                        Button(entry.name, systemImage: "folder") {
                            perform { let next = try await client.folders(at: entry.path); folder = entry.path; folders = next }
                        }
                    }
                    Button("Use This Folder") {
                        guard var preferences = client.connection?.preferences else { return }
                        preferences["upload_folder"] = folder
                        perform { try await client.update(preferences: preferences); browsing = false }
                    }
                    Button("Cancel") { browsing = false }
                    if busy { ProgressView() }
                    if let error { Text(error).foregroundStyle(.red) }
                }.navigationTitle("Upload Folder").disabled(busy)
            }.frame(minWidth: 300, minHeight: 360)
        }
        .confirmationDialog("Disconnect this app?", isPresented: $disconnecting) {
            Button("Disconnect", role: .destructive) { perform { try await client.disconnect() } }
        } message: { Text("The Pi will revoke this device’s access. Use a new invitation to reconnect.") }
        .confirmationDialog("Forget this connection?", isPresented: $forgetting) {
            Button("Forget Connection", role: .destructive) { perform { try client.forget() } }
        } message: { Text("Removes the saved credential here. Revoke it in the Mac connection manager to also disable it on the Pi.") }
    }

    private func perform(_ operation: @escaping () async throws -> Void) {
        guard !busy else { return }
        busy = true
        error = nil
        Task { @MainActor in
            defer { busy = false }
            do { try await operation() } catch { self.error = error.localizedDescription }
        }
    }

    private func loadChoices() async throws {
        if client.connection?.token.scopes.contains("cameras:read") == true { cameras = try await client.cameras() }
    }

    private func permissionName(_ scope: String) -> String {
        switch scope {
        case "runner:read": "Read runner state, research and resumes"
        case "runner:control": "Prepare and supervise application runs"
        case "runner:submit": "Confirm submission after reviewing the application"
        case "review:read": "Read opportunities and backend status"
        case "review:write": "Edit and review opportunities"
        case "outbox:schedule": "Approve and schedule reviewed emails"
        case "discovery:refresh": "Refresh discovery sources"
        case "status:read": "Service status"
        case "storage:read": "Browse and download files"
        case "storage:write": "Organize files and use Trash"
        case "transfers:write": "Upload files"
        case "jobs:read": "Read background task status"
        case "jobs:write": "Create and cancel maintenance tasks"
        case "backups:read": "Browse and download backups"
        case "backups:restore": "Restore backup files"
        case "backups:report": "Report backup runs"
        case "cameras:read": "List cameras"
        case "cameras:control": "View and control cameras"
        case "media:read": "View archived camera media"
        case "articles:read": "Read article review copies"
        case "articles:review": "Queue article edits and review decisions"
        case "articles:sync": "Synchronize articles from the Mac"
        case "opportunities:read": "Read opportunity review copies"
        case "opportunities:review": "Queue opportunity review changes"
        case "opportunities:sync": "Synchronize opportunities from the Mac"
        case "chewsense:deposit": "Deposit encrypted recordings"
        case "chewsense:collect": "Collect encrypted recordings on the Mac"
        case "legacy:storage-contract": "Read Home Platform storage status"
        default: scope
        }
    }
}
