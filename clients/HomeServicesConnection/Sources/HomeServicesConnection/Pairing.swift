import Combine
import Foundation
import Security

/// Providers bind credentials to their own server and API. An invitation cannot
/// choose a provider dynamically or redirect an app's saved bearer credential.
public struct PairingProvider: Equatable, Sendable {
    public let id: String
    public let tokenPrefix: String
    public static let homeServices = Self(id: "home-services", tokenPrefix: "hst_")
    public static let casinoExitRunner = Self(id: "casino-exit-runner", tokenPrefix: "cet_")
    public static let opportunityReview = Self(id: "opportunity-review", tokenPrefix: "odt_")
    public init(id: String, tokenPrefix: String) { self.id = id; self.tokenPrefix = tokenPrefix }
}

public enum PairingError: LocalizedError {
    case invalidInvitation, wrongApp, expired, serverMismatch, response(Int, String), keychain(OSStatus), invalidResponse
    public var errorDescription: String? {
        switch self {
        case .invalidInvitation: "Paste a complete Pi connection invitation."
        case .wrongApp: "This invitation is for a different app or service. Generate one for this app."
        case .expired: "This invitation expired. Generate a new QR code on the Mac."
        case .serverMismatch: "The server identity or connection address did not match the invitation."
        case .response(let status, let code):
            switch code {
            case "pairing_expired": "This invitation expired or was cancelled. Generate a new code."
            case "unauthenticated" where status == 401: "This connection was disconnected or its credential expired. Generate a new invitation, or forget it here."
            case "forbidden": "This action needs permission. Review the access switches in connection settings."
            case "pairing_used": "This invitation was used by another connection. Generate a new code."
            default: "The Pi returned HTTP \(status) (\(code))."
            }
        case .keychain(let status): "Could not save or read this connection in Keychain (\(status))."
        case .invalidResponse: "The Pi returned an invalid response."
        }
    }
}

public struct PairingInvitation: Codable, Equatable {
    public let protocolVersion: Int
    public let provider: String
    public let base_url: String
    public let server_id: String
    public let invitation_id: String
    public let secret: String
    public let preset: String
    public let expires_at: String
    enum CodingKeys: String, CodingKey {
        case protocolVersion = "protocol"
        case provider, base_url, server_id, invitation_id, secret, preset, expires_at
    }

    public static func parse(_ text: String, expectedPreset: String, expectedProvider: String = "home-services", now: Date = .now) throws -> Self {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard trimmed.count <= 8192, let uri = URLComponents(string: trimmed), uri.scheme == "homeservices", uri.host == "pair",
              uri.path.isEmpty, uri.query == nil, uri.user == nil, uri.password == nil, uri.port == nil,
              let fragment = uri.fragment, fragment.range(of: #"^[A-Za-z0-9_-]+$"#, options: .regularExpression) != nil else {
            throw PairingError.invalidInvitation
        }
        var encoded = fragment.replacingOccurrences(of: "-", with: "+").replacingOccurrences(of: "_", with: "/")
        encoded += String(repeating: "=", count: (4 - encoded.count % 4) % 4)
        guard let data = Data(base64Encoded: encoded), let invitation = try? JSONDecoder().decode(Self.self, from: data),
              invitation.protocolVersion == 1, secureOrigin(invitation.base_url) != nil,
              invitation.invitation_id.range(of: #"^pair_[A-Za-z0-9_-]{1,64}$"#, options: .regularExpression) != nil,
              (16...128).contains(invitation.secret.count), !invitation.server_id.isEmpty,
              let expires = timestamp(invitation.expires_at) else { throw PairingError.invalidInvitation }
        guard invitation.provider == expectedProvider, invitation.preset == expectedPreset else { throw PairingError.wrongApp }
        guard expires > now else { throw PairingError.expired }
        return invitation
    }

    private static func timestamp(_ value: String) -> Date? {
        let fractional = ISO8601DateFormatter()
        fractional.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return fractional.date(from: value) ?? ISO8601DateFormatter().date(from: value)
    }

    public static func secureOrigin(_ string: String) -> URL? {
        guard let url = URLComponents(string: string), url.scheme == "https", let host = url.host, !host.isEmpty,
              !string.contains(where: { $0.isWhitespace }), url.user == nil, url.password == nil,
              url.query == nil, url.fragment == nil, url.path.isEmpty || url.path == "/" else { return nil }
        return url.url
    }
}

public struct ConnectionState: Codable, Equatable, Hashable, Identifiable {
    public struct Token: Codable, Equatable, Hashable {
        public let id: String
        public let name: String
        public let scopes: [String]
        public let revoked_at: String?
    }
    public let id: String
    public let preset: String
    public let device_name: String
    public let allowed_scopes: [String]
    public let preferences: [String: String]
    public let token: Token
}

public struct PairedCredential: Codable, Equatable, Hashable {
    public let token: String
    public let base_url: String
    public let server_id: String
    public let connection: ConnectionState
}

public struct PairingPreview: Decodable {
    public let server_id: String
    public let preset: String
    public let name: String
    public let scopes: [String]
    public let base_url: String
}

private struct PendingPairing: Codable {
    let text: String
    let clientSecret: String
    let deviceName: String
}

private struct CredentialVault {
    let service: String
    let account: String
    private var query: [String: Any] {
        [kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: service, kSecAttrAccount as String: account]
    }
    func read<T: Decodable>(_ type: T.Type) throws -> T? {
        var fields = query
        fields[kSecReturnData as String] = true
        fields[kSecMatchLimit as String] = kSecMatchLimitOne
        var result: CFTypeRef?
        let status = SecItemCopyMatching(fields as CFDictionary, &result)
        if status == errSecItemNotFound { return nil }
        guard status == errSecSuccess, let data = result as? Data else { throw PairingError.keychain(status) }
        return try JSONDecoder().decode(T.self, from: data)
    }
    func save<T: Encodable>(_ value: T) throws {
        let attributes: [String: Any] = [kSecValueData as String: try JSONEncoder().encode(value),
            kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly]
        var status = SecItemUpdate(query as CFDictionary, attributes as CFDictionary)
        if status == errSecItemNotFound { status = SecItemAdd(query.merging(attributes) { _, new in new } as CFDictionary, nil) }
        guard status == errSecSuccess else { throw PairingError.keychain(status) }
    }
    func remove() throws {
        let status = SecItemDelete(query as CFDictionary)
        guard status == errSecSuccess || status == errSecItemNotFound else { throw PairingError.keychain(status) }
    }
}

/// One instance per app connection/profile. Credentials are isolated in Keychain.
@MainActor public final class PairingClient: ObservableObject {
    @Published public private(set) var credential: PairedCredential?
    @Published public private(set) var connection: ConnectionState?
    public let expectedPreset: String
    public let provider: PairingProvider
    @Published public private(set) var status = "Saved connection; checking availability…"
    private let vault: CredentialVault
    private let pendingVault: CredentialVault
    private let session: URLSession
    private var generation = 0

    private final class NoRedirect: NSObject, URLSessionTaskDelegate {
        func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse,
                        newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) { completionHandler(nil) }
    }

    public init(expectedPreset: String, keychainService: String, profile: String = "default", provider: PairingProvider = .homeServices) throws {
        self.expectedPreset = expectedPreset
        self.provider = provider
        vault = CredentialVault(service: keychainService, account: (provider == .homeServices ? "" : provider.id + ":") + expectedPreset + ":" + profile)
        pendingVault = CredentialVault(service: keychainService, account: (provider == .homeServices ? "" : provider.id + ":") + expectedPreset + ":" + profile + ":pending")
        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = 15
        config.timeoutIntervalForResource = 20
        config.httpShouldSetCookies = false
        session = URLSession(configuration: config, delegate: NoRedirect(), delegateQueue: nil)
        credential = try vault.read(PairedCredential.self)
        connection = credential?.connection
    }

    public func preview(_ text: String) async throws -> PairingPreview {
        let invite = try PairingInvitation.parse(text, expectedPreset: expectedPreset, expectedProvider: provider.id)
        let value: PairingPreview = try await send(base: invite.base_url, path: "pairing/preview", method: "POST",
            body: ["invitation_id": invite.invitation_id, "secret": invite.secret])
        guard value.server_id == invite.server_id, value.base_url == invite.base_url, value.preset == expectedPreset else {
            throw PairingError.serverMismatch
        }
        return value
    }

    public func connect(_ text: String, deviceName: String) async throws {
        generation += 1
        let revision = generation
        let text = text.trimmingCharacters(in: .whitespacesAndNewlines)
        let invite = try PairingInvitation.parse(text, expectedPreset: expectedPreset, expectedProvider: provider.id)
        var pending = try pendingVault.read(PendingPairing.self)
        if pending?.text != text {
            var bytes = [UInt8](repeating: 0, count: 32)
            let status = SecRandomCopyBytes(kSecRandomDefault, bytes.count, &bytes)
            guard status == errSecSuccess else { throw PairingError.keychain(status) }
            let secret = Data(bytes).base64EncodedString().replacingOccurrences(of: "+", with: "-")
                .replacingOccurrences(of: "/", with: "_").replacingOccurrences(of: "=", with: "")
            pending = PendingPairing(text: text, clientSecret: secret, deviceName: deviceName)
            try pendingVault.save(pending!)
        }
        guard let pending else { throw PairingError.invalidInvitation }
        let result: PairedCredential = try await send(base: invite.base_url, path: "pairing/exchange", method: "POST",
            body: ["invitation_id": invite.invitation_id, "secret": invite.secret,
                   "client_secret": pending.clientSecret, "device_name": pending.deviceName])
        guard result.server_id == invite.server_id, result.base_url == invite.base_url,
              result.connection.preset == expectedPreset,
              result.token == "\(provider.tokenPrefix)\(result.connection.token.id).\(pending.clientSecret)" else {
            throw PairingError.serverMismatch
        }
        guard revision == generation else { throw CancellationError() }
        try vault.save(result)
        credential = result
        connection = result.connection
        status = "Connected"
        try pendingVault.remove()
    }

    public func refresh() async throws {
        guard let credential else { return }
        let revision = generation
        let value: ConnectionState = try await send(base: credential.base_url, path: "connection", token: credential.token)
        guard value.id == credential.connection.id, value.preset == expectedPreset else { throw PairingError.serverMismatch }
        guard revision == generation else { return }
        try saveConnection(value)
    }

    public var hasInterruptedPairing: Bool { (try? pendingVault.read(PendingPairing.self)) != nil }

    public func retryInterruptedPairing() async throws {
        guard let pending = try pendingVault.read(PendingPairing.self) else { return }
        try await connect(pending.text, deviceName: pending.deviceName)
    }

    public func update(scopes: Set<String>? = nil, preferences: [String: String]? = nil) async throws {
        guard let credential else { throw PairingError.invalidInvitation }
        generation += 1
        let revision = generation
        var body: [String: Any] = [:]
        if let scopes { body["scopes"] = scopes.sorted() }
        if let preferences { body["preferences"] = preferences }
        let value: ConnectionState = try await send(base: credential.base_url, path: "connection", method: "PATCH",
            token: credential.token, body: body)
        guard revision == generation else { return }
        try saveConnection(value)
    }

    private func saveConnection(_ value: ConnectionState) throws {
        guard value.id == credential?.connection.id, value.preset == expectedPreset,
              value.token.id == credential?.connection.token.id,
              Set(value.token.scopes).isSubset(of: Set(value.allowed_scopes)) else { throw PairingError.serverMismatch }
        guard let credential else { throw PairingError.invalidInvitation }
        let updated = PairedCredential(token: credential.token, base_url: credential.base_url,
                                       server_id: credential.server_id, connection: value)
        try vault.save(updated)
        self.credential = updated
        connection = value
        status = "Connected"
    }

    public func disconnect() async throws {
        guard let credential else { return }
        generation += 1
        let revision = generation
        let _: ConnectionState = try await send(base: credential.base_url, path: "connection", method: "DELETE", token: credential.token)
        guard revision == generation else { return }
        try vault.remove()
        try pendingVault.remove()
        self.credential = nil
        connection = nil
    }

    /// Use after a token was revoked elsewhere or a server was permanently removed.
    public func forget() throws {
        generation += 1
        try vault.remove()
        try pendingVault.remove()
        credential = nil
        connection = nil
    }

    public struct Camera: Decodable, Identifiable { public let id: String; public let name: String }
    public func cameras() async throws -> [Camera] {
        struct List: Decodable { let cameras: [Camera] }
        guard let credential else { return [] }
        let value: List = try await send(base: credential.base_url, path: "cameras", token: credential.token)
        return value.cameras
    }

    public struct Folder: Decodable { public let path: String; public let name: String; public let kind: String }
    public func folders(at path: String) async throws -> [Folder] {
        struct List: Decodable { let entries: [Folder]; let next_cursor: String? }
        guard let credential else { return [] }
        var folders: [Folder] = [], cursor: String?
        repeat {
            var query = [URLQueryItem(name: "path", value: path), URLQueryItem(name: "limit", value: "500")]
            if let cursor { query.append(.init(name: "cursor", value: cursor)) }
            let page: List = try await send(base: credential.base_url, path: "storage/files/entries", token: credential.token, query: query)
            folders += page.entries.filter { $0.kind == "directory" }
            cursor = page.next_cursor
        } while cursor != nil
        return folders
    }

    private func send<T: Decodable>(base: String, path: String, method: String = "GET", token: String? = nil,
                                     body: [String: Any]? = nil, query: [URLQueryItem] = []) async throws -> T {
        guard let origin = PairingInvitation.secureOrigin(base),
              var parts = URLComponents(url: origin.appendingPathComponent("api/v1").appendingPathComponent(path), resolvingAgainstBaseURL: false) else {
            throw PairingError.invalidInvitation
        }
        if !query.isEmpty { parts.queryItems = query }
        guard let url = parts.url else { throw PairingError.invalidInvitation }
        var request = URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData)
        request.httpMethod = method
        request.setValue("1", forHTTPHeaderField: "Home-Services-Contract")
        if let token { request.setValue("Bearer " + token, forHTTPHeaderField: "Authorization") }
        if let body {
            request.httpBody = try JSONSerialization.data(withJSONObject: body)
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }
        let data: Data
        let response: URLResponse
        do { (data, response) = try await session.data(for: request) }
        catch {
            status = "Pi unavailable. Saved connection kept; check Wi-Fi or Tailscale and retry."
            throw error
        }
        guard let http = response as? HTTPURLResponse else { throw PairingError.invalidResponse }
        guard (200..<300).contains(http.statusCode) else {
            let envelope = try? JSONSerialization.jsonObject(with: data) as? [String: [String: Any]]
            let code = envelope?["error"]?["code"] as? String ?? "request_failed"
            if http.statusCode == 401, token != nil { status = "Disconnected or credential expired. Generate a new invitation." }
            throw PairingError.response(http.statusCode, code)
        }
        return try JSONDecoder().decode(T.self, from: data)
    }
}
