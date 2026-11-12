import Foundation

let widgetKind = "HomeServicesStatus"
let agentLabel = "com.zacharysturman.homeservices.status.collector"

enum Health: String, Codable, CaseIterable {
    case healthy, attention, unavailable, unknown, idle

    var title: String {
        switch self {
        case .healthy: "Healthy"
        case .attention: "Attention"
        case .unavailable: "Down"
        case .unknown: "Unknown"
        case .idle: "Idle"
        }
    }

    var symbol: String {
        switch self {
        case .healthy: "checkmark.circle.fill"
        case .attention: "exclamationmark.triangle.fill"
        case .unavailable: "xmark.circle.fill"
        case .unknown: "questionmark.circle"
        case .idle: "minus.circle"
        }
    }

    static func rollup(_ states: [Self]) -> Self {
        for state in [Self.unavailable, .attention, .unknown, .healthy] where states.contains(state) {
            return state
        }
        return states.isEmpty ? .unknown : .idle
    }
}

struct CheckResult: Codable, Identifiable {
    var name: String
    var health: Health
    var detail: String
    var summary: String? = nil
    var nextStep: String? = nil
    var id: String { name }
}

struct GroupReport: Codable, Identifiable {
    var id: String
    var title: String
    var symbol: String
    var checks: [CheckResult]
    var health: Health { Health.rollup(checks.map(\.health)) }

    var link: URL {
        var url = URLComponents()
        url.scheme = "home-services-status"
        url.host = "details"
        url.path = "/" + id
        return url.url!
    }
}

struct Configuration: Codable, Equatable {
    var host = ""
    var gateway = ""
    var dashboard = ""
    var hidden: Set<String> = []

    static func secureURL(_ input: String) -> URL? {
        guard let parts = URLComponents(string: input), parts.scheme == "https",
              let host = parts.host, !host.isEmpty, parts.user == nil, parts.password == nil,
              parts.query == nil, parts.fragment == nil else { return nil }
        return parts.url
    }

    var validationMessage: String? {
        if !host.isEmpty && host.range(of: #"^([a-zA-Z0-9_][a-zA-Z0-9_.-]*@)?[a-zA-Z0-9][a-zA-Z0-9_.-]*$"#,
                                      options: .regularExpression) == nil {
            return "Use an SSH host alias or a user@hostname address."
        }
        if [gateway, dashboard].contains(where: { !$0.isEmpty && Self.secureURL($0) == nil }) {
            return "Use HTTPS URLs without credentials, query strings, or fragments."
        }
        return nil
    }
}

struct StatusSnapshot: Codable {
    static let staleAfter: TimeInterval = 35 * 60

    var configuration: Configuration
    var checkedAt: Date
    var groups: [GroupReport]
    var hostOverview: HostOverview? = nil
    var schedules: [ScheduledTask]? = nil
    var expiresAt: Date { checkedAt.addingTimeInterval(Self.staleAfter) }
    var attentionCount: Int { groups.filter { [.unavailable, .attention].contains($0.health) }.count }
    var healthyCount: Int { groups.filter { $0.health == .healthy }.count }
    var unknownCount: Int { groups.filter { $0.health == .unknown }.count }
}

enum StatusStore {
    static func directory() throws -> URL {
        guard let group = Bundle.main.object(forInfoDictionaryKey: "StatusAppGroup") as? String,
              let url = FileManager.default.containerURL(forSecurityApplicationGroupIdentifier: group) else {
            throw CocoaError(.fileNoSuchFile)
        }
        let directory = url.appendingPathComponent("HomeServicesStatus", isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true,
                                                attributes: [.posixPermissions: 0o700])
        return directory
    }

    static func configuration() -> Configuration {
        read(Configuration.self, "settings.json") ?? Configuration()
    }

    static func snapshot(for configuration: Configuration) -> StatusSnapshot? {
        guard let snapshot = read(StatusSnapshot.self, "status.json"),
              snapshot.configuration == configuration else { return nil }
        return snapshot
    }

    static func read<T: Decodable>(_ type: T.Type, _ file: String) -> T? {
        guard let url = try? directory().appendingPathComponent(file),
              let data = try? Data(contentsOf: url) else { return nil }
        return try? JSONDecoder().decode(type, from: data)
    }

    static func save<T: Encodable>(_ value: T, to file: String) throws {
        let url = try directory().appendingPathComponent(file)
        try JSONEncoder().encode(value).write(to: url, options: .atomic)
    }
}
