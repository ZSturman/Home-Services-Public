import SwiftUI
import WidgetKit

struct WidgetDiagnostics: View {
    let snapshot: StatusSnapshot?
    var refresh: () -> Void
    @State private var result: DiagnosticResult?
    @State private var checking = false
    @State private var receipt: WidgetReadReceipt?

    var body: some View {
        ConsoleSection(title: "Desktop widgets", note: "Compare the status collected by the Mac with the last timeline read by WidgetKit.") {
            HStack {
                Button("Check Widget Status", systemImage: "rectangle.on.rectangle") {
                    Task { await check() }
                }
                .disabled(checking)
                Button("Refresh Widget Data", systemImage: "arrow.clockwise", action: refresh)
            }
            if let snapshot {
                Text("Latest collected status · " + StatusFormat.date(snapshot.checkedAt)).foregroundStyle(.secondary)
            }
            if let read = receipt, let time = read.snapshotAt {
                Text("WidgetKit last loaded \(read.groupCount) groups from the \(StatusFormat.date(time)) snapshot.")
                    .foregroundStyle(.secondary)
            }
            if let result { DiagnosticCard(result: result) }
        }
        .onAppear { readReceipt() }
        .onChange(of: snapshot?.checkedAt) { _, _ in readReceipt() }
    }

    private func readReceipt() {
        receipt = StatusStore.read(WidgetReadReceipt.self, WidgetReadReceipt.filename)
    }

    private func check() async {
        checking = true
        defer { checking = false }
        readReceipt()
        do {
            let count: Int = try await withCheckedThrowingContinuation { continuation in
                WidgetCenter.shared.getCurrentConfigurations { result in
                    continuation.resume(with: result.map { $0.filter { $0.kind == widgetKind }.count })
                }
            }
            guard count > 0 else {
                result = DiagnosticResult(health: .idle, message: "Home Services Status has no placed widgets.",
                                          nextStep: Troubleshooting.widget)
                return
            }
            let placement = count == 1 ? "1 widget configured." : "\(count) widgets configured."
            guard let time = receipt?.snapshotAt,
                  time.addingTimeInterval(StatusSnapshot.staleAfter) > .now else {
                result = DiagnosticResult(health: .attention, message: placement + " No recent widget timeline receipt.",
                                          nextStep: "Choose Refresh Widget Data. Give macOS time to reload it, then check widget status again. " + Troubleshooting.widget)
                return
            }
            if let snapshot, receipt?.summary != snapshot.widgetSummary {
                result = DiagnosticResult(health: .attention, message: placement + " The latest group status is still waiting for WidgetKit.",
                                          nextStep: "Choose Refresh Widget Data, then check again after macOS reloads the timeline.")
                return
            }
            result = DiagnosticResult(health: .healthy, message: placement + " WidgetKit read current group status.")
        } catch {
            result = DiagnosticResult(health: .unknown, message: "macOS could not report widget placement.",
                                      nextStep: Troubleshooting.widget)
        }
    }
}

struct DiagnosticCard: View {
    let result: DiagnosticResult
    var monospaced = false

    var body: some View {
        ConsoleCard {
            VStack(alignment: .leading, spacing: 8) {
                HealthBadge(health: result.health)
                Text(result.message)
                    .font(monospaced ? .callout.monospaced() : .body)
                    .textSelection(.enabled)
                if let next = result.nextStep {
                    Label(next, systemImage: "wrench.and.screwdriver")
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                }
            }
        }
    }
}
