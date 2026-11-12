import SwiftUI

extension Health {
    var tint: Color {
        switch self {
        case .healthy: .green
        case .attention: .orange
        case .unavailable: .red
        case .unknown, .idle: .secondary
        }
    }
}
