import Foundation
import Security

enum OperatorError: LocalizedError {
    case noConnection, missingToken, invalidToken, keychain(OSStatus), response(Int), invalidResponse

    var errorDescription: String? {
        switch self {
        case .noConnection: "Set a private HTTPS gateway in Settings."
        case .missingToken: "Connect a scoped operator token in Settings to read and manage Home Services jobs."
        case .invalidToken: "Enter a Home Services bearer token."
        case .keychain(let code): "The operator token could not be accessed in Keychain (\(code))."
        case .response(401): "The gateway rejected the token. Check that it is current and belongs to this Pi."
        case .response(403): "The operator token lacks the scope for this action."
        case .response(409): "This job already finished or changed state. Refresh to see its current status."
        case .response(let status): "The gateway returned HTTP \(status)."
        case .invalidResponse: "The gateway returned an unrecognized response."
        }
    }
}

enum OperatorCredential {
    private static func query(_ endpoint: URL) -> [String: Any] {
        [kSecClass as String: kSecClassGenericPassword,
         kSecAttrService as String: "com.zacharysturman.homeservices.status.operator",
         kSecAttrAccount as String: endpoint.absoluteString]
    }

    static func read(_ endpoint: URL) throws -> String? {
        var fields = query(endpoint)
        fields[kSecReturnData as String] = true
        fields[kSecMatchLimit as String] = kSecMatchLimitOne
        var result: CFTypeRef?
        let status = SecItemCopyMatching(fields as CFDictionary, &result)
        if status == errSecItemNotFound { return nil }
        guard status == errSecSuccess, let data = result as? Data else { throw OperatorError.keychain(status) }
        return String(data: data, encoding: .utf8)
    }

    static func save(_ token: String, endpoint: URL) throws {
        let token = token.trimmingCharacters(in: .whitespacesAndNewlines)
        guard token.count <= 256, token.range(of: #"^hst_tok_[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+$"#,
                                             options: .regularExpression) != nil else { throw OperatorError.invalidToken }
        let query = query(endpoint)
        let data = [kSecValueData as String: Data(token.utf8)]
        var status = SecItemUpdate(query as CFDictionary, data as CFDictionary)
        if status == errSecItemNotFound {
            status = SecItemAdd(query.merging(data) { _, new in new } as CFDictionary, nil)
        }
        guard status == errSecSuccess else { throw OperatorError.keychain(status) }
    }

    static func remove(_ endpoint: URL) throws {
        let status = SecItemDelete(query(endpoint) as CFDictionary)
        guard status == errSecSuccess || status == errSecItemNotFound else { throw OperatorError.keychain(status) }
    }
}

struct ServiceJob: Decodable, Identifiable {
    struct Timestamp: Decodable { let at: String? }
    struct JobError: Decodable { let message: String? }

    let id: String
    let kind: String
    let state: String
    let cancel_requested: Bool
    let created_at: String
    let progress: Timestamp?
    let heartbeat: Timestamp?
    let error: JobError?

    var active: Bool { ["waiting", "blocked", "running"].contains(state) }
    var canCancel: Bool {
        !cancel_requested && (state == "waiting" || state == "blocked" || (state == "running" && kind == "backup.restore"))
    }
    var title: String {
        switch kind {
        case "backup.restore": "Restore backup files"
        case "camera.export", "camera.export_mp4": "Export a recording"
        case "storage.cleanup": "Storage maintenance"
        default: kind.replacingOccurrences(of: ".", with: " ").replacingOccurrences(of: "_", with: " ").capitalized
        }
    }
    var label: String {
        if cancel_requested && active { return "Cancellation requested" }
        return switch state {
        case "waiting": "Queued"
        case "blocked": "Waiting on a dependency"
        case "succeeded": "Completed"
        default: state.capitalized
        }
    }
}

struct OperatorJobs {
    let active: [ServiceJob]
    let recent: [ServiceJob]
    let mayCancel: Bool
}

struct OperatorAPI {
    private final class NoRedirect: NSObject, URLSessionTaskDelegate {
        func urlSession(_ session: URLSession, task: URLSessionTask,
                        willPerformHTTPRedirection response: HTTPURLResponse, newRequest request: URLRequest,
                        completionHandler: @escaping (URLRequest?) -> Void) {
            completionHandler(nil)
        }
    }

    private struct Bootstrap: Decodable {
        struct Token: Decodable { let scopes: [String] }
        let token: Token
    }

    private struct JobList: Decodable { let jobs: [ServiceJob] }
    private struct Readiness: Decodable { let modules: [String: Module] }
    private struct Module: Decodable { let state: String }
    private struct Receipt: Decodable { let id: String }
    private struct DeliveryList: Decodable {
        struct Message: Decodable { let id: String; let state: String }
        let messages: [Message]
    }

    let endpoint: URL
    let token: String

    init(gateway: String) throws {
        guard let endpoint = Configuration.secureURL(gateway) else { throw OperatorError.noConnection }
        guard let token = try OperatorCredential.read(endpoint) else { throw OperatorError.missingToken }
        self.endpoint = endpoint
        self.token = token
    }

    private static let session: URLSession = {
        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = 8
        config.timeoutIntervalForResource = 12
        return URLSession(configuration: config, delegate: NoRedirect(), delegateQueue: nil)
    }()

    private func request<T: Decodable>(_ path: String, method: String = "GET", query: [URLQueryItem] = [],
                                       json: [String: String]? = nil, idempotent: Bool = false) async throws -> T {
        let url = endpoint.appendingPathComponent("api/v1").appendingPathComponent(path)
        guard var parts = URLComponents(url: url, resolvingAgainstBaseURL: false) else { throw OperatorError.invalidResponse }
        if !query.isEmpty { parts.queryItems = query }
        guard let target = parts.url else { throw OperatorError.invalidResponse }
        var request = URLRequest(url: target, cachePolicy: .reloadIgnoringLocalCacheData)
        request.httpMethod = method
        request.setValue("Bearer " + token, forHTTPHeaderField: "Authorization")
        request.setValue("1", forHTTPHeaderField: "Home-Services-Contract")
        if let json {
            request.httpBody = try JSONEncoder().encode(json)
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }
        if idempotent { request.setValue(UUID().uuidString, forHTTPHeaderField: "Idempotency-Key") }
        let (data, response) = try await Self.session.data(for: request)
        guard let http = response as? HTTPURLResponse else { throw OperatorError.invalidResponse }
        guard (200..<300).contains(http.statusCode) else { throw OperatorError.response(http.statusCode) }
        guard let decoded = try? JSONDecoder().decode(T.self, from: data) else { throw OperatorError.invalidResponse }
        return decoded
    }

    func jobs() async throws -> OperatorJobs {
        let access: Bootstrap = try await request("bootstrap")
        async let recent: JobList = request("jobs", query: [.init(name: "limit", value: "50")])
        async let waiting: JobList = jobs(in: "waiting")
        async let blocked: JobList = jobs(in: "blocked")
        async let running: JobList = jobs(in: "running")
        let lists = try await [running, blocked, waiting]
        let latest = try await recent.jobs
        let finishedIDs = Set(latest.filter { !$0.active }.map(\.id))
        var seen = finishedIDs
        let active = lists.flatMap(\.jobs).filter { seen.insert($0.id).inserted }
        return OperatorJobs(active: active, recent: latest.filter { !$0.active },
                            mayCancel: access.token.scopes.contains("jobs:write"))
    }

    private func jobs(in state: String) async throws -> JobList {
        try await request("jobs", query: [.init(name: "state", value: state), .init(name: "limit", value: "100")])
    }

    func cancel(_ job: ServiceJob) async throws -> ServiceJob {
        guard job.canCancel, job.id.range(of: #"^job_[A-Za-z0-9_-]+$"#, options: .regularExpression) != nil else {
            throw OperatorError.invalidResponse
        }
        return try await request("jobs/\(job.id)/cancel", method: "POST")
    }

    func readiness() async throws -> DiagnosticResult {
        let snapshot: Readiness = try await request("readiness")
        guard !snapshot.modules.isEmpty else { throw OperatorError.invalidResponse }
        let affected = snapshot.modules.filter { !["ready", "configured", "disabled"].contains($0.value.state) }
        if affected.isEmpty {
            return DiagnosticResult(health: .healthy, message: "The authenticated API returned \(snapshot.modules.count) module checks.")
        }
        return DiagnosticResult(health: .attention,
                                message: "The API responded. \(affected.count) modules need attention: \(affected.keys.sorted().joined(separator: ", ")).",
                                nextStep: "Run System Checks and expand the affected groups for the failing checks and suggested fixes.")
    }

    func sendTestNotification() async throws -> String {
        let receipt: Receipt = try await request("notifications/test", method: "POST",
                                                json: ["message": "Test requested from the Home Services Mac console."], idempotent: true)
        return receipt.id
    }

    func deliveryStatus(_ id: String) async throws -> DiagnosticResult {
        let outbox: DeliveryList = try await request("notifications/outbox")
        guard let message = outbox.messages.first(where: { $0.id == id }) else {
            return DiagnosticResult(health: .unknown, message: "The test is outside the current outbox view.",
                                    nextStep: "Check the notification app for the original test before sending another one.")
        }
        switch message.state {
        case "delivered":
            return DiagnosticResult(health: .healthy, message: "The notifier recorded delivery to its configured topic.")
        case "pending":
            return DiagnosticResult(health: .idle, message: "The test is pending.",
                                    nextStep: "Wait for the notifier to process the queue, then choose Check Test Delivery again.")
        default:
            return DiagnosticResult(health: .attention, message: "Test notification: \(message.state).",
                                    nextStep: "Check Notifications and Recent Pi Warnings for a notifier failure. Verify the Pi can reach the configured topic.")
        }
    }
}
