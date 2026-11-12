#if os(iOS)
import AVFoundation
import SwiftUI
import UIKit

struct PairingScanner: View {
    let completion: (Result<String, Error>) -> Void
    @Environment(\.dismiss) private var dismiss
    var body: some View {
        NavigationStack {
            ScannerCamera(completion: completion)
                .ignoresSafeArea(edges: .bottom)
                .navigationTitle("Scan Pi Invitation")
                .toolbar { Button("Cancel") { dismiss() } }
        }
    }
}

private struct ScannerCamera: UIViewControllerRepresentable {
    let completion: (Result<String, Error>) -> Void
    func makeUIViewController(context: Context) -> ScannerController { ScannerController(completion: completion) }
    func updateUIViewController(_ controller: ScannerController, context: Context) {}
    static func dismantleUIViewController(_ controller: ScannerController, coordinator: ()) { controller.stop() }
}

private final class ScannerController: UIViewController, AVCaptureMetadataOutputObjectsDelegate {
    private let session = AVCaptureSession()
    private let queue = DispatchQueue(label: "HomeServicesConnection.QR")
    private var layer: AVCaptureVideoPreviewLayer?
    private var finished = false
    private var active = true
    private let completion: (Result<String, Error>) -> Void
    init(completion: @escaping (Result<String, Error>) -> Void) { self.completion = completion; super.init(nibName: nil, bundle: nil) }
    required init?(coder: NSCoder) { fatalError("init(coder:) is unavailable") }

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = .black
        guard let message = Bundle.main.object(forInfoDictionaryKey: "NSCameraUsageDescription") as? String, !message.isEmpty else {
            fail("This app needs a camera usage description to scan QR codes. Paste the invitation instead."); return
        }
        AVCaptureDevice.requestAccess(for: .video) { allowed in
            DispatchQueue.main.async {
                guard self.active else { return }
                if allowed { self.configure() }
                else { self.fail("Camera access is off. Enable it in Settings or paste the pairing invitation.") }
            }
        }
    }

    private func configure() {
        guard let device = AVCaptureDevice.default(for: .video), let input = try? AVCaptureDeviceInput(device: device),
              session.canAddInput(input) else { fail("No camera is available. Paste the invitation instead."); return }
        session.addInput(input)
        let output = AVCaptureMetadataOutput()
        guard session.canAddOutput(output) else { fail("QR scanning is unavailable. Paste the invitation instead."); return }
        session.addOutput(output)
        output.setMetadataObjectsDelegate(self, queue: .main)
        output.metadataObjectTypes = [.qr]
        let preview = AVCaptureVideoPreviewLayer(session: session)
        preview.videoGravity = .resizeAspectFill
        preview.frame = view.bounds
        view.layer.addSublayer(preview)
        layer = preview
        queue.async { self.session.startRunning() }
    }

    override func viewDidLayoutSubviews() { super.viewDidLayoutSubviews(); layer?.frame = view.bounds }
    func stop() {
        active = false
        queue.async { if self.session.isRunning { self.session.stopRunning() } }
    }
    private func fail(_ message: String) {
        guard !finished else { return }
        finished = true
        completion(.failure(NSError(domain: "HomeServicesScanner", code: 1, userInfo: [NSLocalizedDescriptionKey: message])))
    }
    func metadataOutput(_ output: AVCaptureMetadataOutput, didOutput metadataObjects: [AVMetadataObject], from connection: AVCaptureConnection) {
        guard active, !finished, let code = metadataObjects.compactMap({ ($0 as? AVMetadataMachineReadableCodeObject)?.stringValue }).first else { return }
        finished = true
        stop()
        completion(.success(code))
    }
}
#endif
