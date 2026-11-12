import Foundation

struct WidgetReadReceipt: Codable {
    let readAt: Date
    let snapshotAt: Date?
    let groupCount: Int
    let summary: [String]?

    static let filename = "widget-read.json"
}

extension StatusSnapshot {
    var forWidget: Self {
        var visible = self
        visible.groups.removeAll { configuration.hidden.contains($0.id) }
        return visible
    }

    var widgetSummary: [String] {
        forWidget.groups.map { $0.id + ":" + $0.health.rawValue }
    }

    func shouldReloadWidget(previous: Self?) -> Bool {
        let current = widgetSummary
        guard !current.isEmpty else { return false }
        guard let previous, previous.expiresAt > checkedAt else { return true }
        return current != previous.widgetSummary
    }
}
