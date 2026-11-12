import SwiftUI

struct ActivityView: View {
    var gateway: String
    var openSettings: () -> Void
    @State private var jobs: OperatorJobs?
    @State private var loadError: String?
    @State private var actionMessage: String?
    @State private var refreshing = false
    @State private var cancellation: ServiceJob?
    @State private var cancelling = false
    @State private var includeRoutineJobs = false

    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            HStack {
                Text("Restore and maintenance jobs handled by Home Services workers.")
                    .foregroundStyle(.secondary)
                Spacer()
                Button("Reload Jobs", systemImage: "arrow.clockwise") { Task { await reload() } }
                    .disabled(refreshing || cancelling)
            }
            if let loadError {
                ConsoleCard {
                    VStack(alignment: .leading, spacing: 8) {
                        Label(loadError, systemImage: "exclamationmark.circle")
                        Button("Open Settings", action: openSettings)
                    }
                }
            }
            if let actionMessage {
                Text(actionMessage).foregroundStyle(.secondary).textSelection(.enabled)
            }
            if let jobs {
                ConsoleSection(title: "In progress or queued",
                               note: jobs.mayCancel ? "Cancel requests go through the worker and may take time to finish."
                                   : "This token can view jobs. Cancellation requires the jobs:write scope.") {
                    if jobs.active.isEmpty { Text("No active Home Services jobs.").foregroundStyle(.secondary) }
                    ForEach(jobs.active) { job in jobCard(job, mayCancel: jobs.mayCancel && loadError == nil) }
                }
                ConsoleSection(title: "Recent results", note: "From the 50 newest jobs.") {
                    Toggle("Show completed storage maintenance", isOn: $includeRoutineJobs)
                    let results = jobs.recent.filter { includeRoutineJobs || $0.kind != "storage.cleanup" || $0.state != "succeeded" }
                    if results.isEmpty { Text("No matching recent jobs.").foregroundStyle(.secondary) }
                    ForEach(results) { job in jobCard(job, mayCancel: false) }
                }
            } else if refreshing {
                ProgressView("Loading jobs…")
            }
        }
        .task(id: gateway) {
            jobs = nil
            actionMessage = nil
            await reload()
            while !Task.isCancelled && loadError == nil {
                do { try await Task.sleep(for: .seconds(15)) } catch { break }
                await reload()
            }
        }
        .confirmationDialog("Request job cancellation?", isPresented: Binding(
            get: { cancellation != nil }, set: { if !$0 { cancellation = nil } }),
            titleVisibility: .visible, presenting: cancellation) { job in
                Button("Request Cancellation", role: .destructive) { Task { await cancel(job) } }
            } message: { job in
                Text("\(job.title) · \(job.id). Waiting jobs cancel immediately. Running jobs stop when the worker observes the request.")
            }
    }

    private func jobCard(_ job: ServiceJob, mayCancel: Bool) -> some View {
        ConsoleCard {
            VStack(alignment: .leading, spacing: 6) {
                HStack {
                    Text(job.title).font(.headline)
                    Spacer()
                    Text(job.label).font(.caption.weight(.semibold))
                        .foregroundStyle(["failed", "interrupted"].contains(job.state) ? Color.orange : .secondary)
                    if job.canCancel && mayCancel {
                        Button("Cancel Job") { cancellation = job }.disabled(cancelling)
                    }
                }
                if let created = StatusFormat.apiDate(job.created_at) {
                    Text("Requested \(StatusFormat.date(created))").foregroundStyle(.secondary)
                }
                if let heartbeat = StatusFormat.apiDate(job.heartbeat?.at), job.active {
                    Text("Worker last checked in \(StatusFormat.date(heartbeat))").foregroundStyle(.secondary)
                }
                if let progress = StatusFormat.apiDate(job.progress?.at), job.active {
                    Text("Work last advanced \(StatusFormat.date(progress))").foregroundStyle(.secondary)
                }
                if let message = job.error?.message { Text(message).foregroundStyle(.secondary) }
                if job.kind == "storage.cleanup", job.state == "running", !job.cancel_requested {
                    Text("Maintenance is already in progress. It finishes in the storage worker.")
                        .foregroundStyle(.secondary)
                }
                Text(job.id).font(.caption.monospaced()).foregroundStyle(.secondary).textSelection(.enabled)
            }
        }
    }

    private func reload() async {
        guard !refreshing, !cancelling else { return }
        refreshing = true
        defer { refreshing = false }
        do {
            let fresh = try await OperatorAPI(gateway: gateway).jobs()
            guard !Task.isCancelled else { return }
            jobs = fresh
            loadError = nil
        } catch {
            guard !Task.isCancelled else { return }
            loadError = error.localizedDescription
        }
    }

    private func cancel(_ job: ServiceJob) async {
        guard !cancelling else { return }
        cancelling = true
        actionMessage = nil
        do {
            let result = try await OperatorAPI(gateway: gateway).cancel(job)
            actionMessage = "\(result.title): \(result.label.lowercased())."
        } catch {
            actionMessage = error.localizedDescription
        }
        cancelling = false
        await reload()
    }
}
