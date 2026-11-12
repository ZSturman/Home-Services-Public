import Foundation

struct DiskCapacity: Codable, Identifiable {
    struct Space: Codable {
        let total_bytes: Int64
        let free_bytes: Int64

        var usedFraction: Double {
            guard total_bytes > 0 else { return 0 }
            return max(0, min(1, 1 - Double(free_bytes) / Double(total_bytes)))
        }
    }

    let id: String
    let mount: String
    let state: String
    let reason: String
    let capacity: Space?

    var title: String { id == "system" ? "Pi system disk" : id == "backup-hdd" ? "Backup drive" : id }
}

struct TimerReading: Codable {
    let unit: String
    let next: UInt64?
    let last: UInt64?

    private func date(_ microseconds: UInt64?) -> Date? {
        guard let microseconds, (1...253_402_300_799_000_000).contains(microseconds) else { return nil }
        return Date(timeIntervalSince1970: Double(microseconds) / 1_000_000)
    }

    var nextRun: Date? { date(next) }
    var lastTrigger: Date? { date(last) }
}

struct ScheduledTask: Codable, Identifiable {
    let id: String
    let name: String
    let owner: String
    let state: String
    let next: Date?
    let last: Date?
    let detail: String?
}

struct RelayReading: Codable {
    let cached: Int
    let queued: Int
    let companion_seen_epoch: Double?
    let oldest_queued_epoch: Double?

    var lastContact: Date? { companion_seen_epoch.map(Date.init(timeIntervalSince1970:)) }
    var oldestQueued: Date? { oldest_queued_epoch.map(Date.init(timeIntervalSince1970:)) }
}

struct HostOverview: Codable {
    let disks: [DiskCapacity]
    let timers: [TimerReading]?
    let relay: RelayReading?
    let cleanup_next_epoch: Double?
}

enum StatusFormat {
    static func bytes(_ count: Int64) -> String {
        ByteCountFormatter.string(fromByteCount: count, countStyle: .decimal)
    }

    static func date(_ date: Date) -> String {
        date.formatted(date: .abbreviated, time: .shortened)
    }

    static func apiDate(_ input: String?) -> Date? {
        guard let input else { return nil }
        return (try? Date(input, strategy: .iso8601.year().month().day().time(includingFractionalSeconds: true).timeZone(separator: .colon)))
            ?? ISO8601DateFormatter().date(from: input)
    }
}
