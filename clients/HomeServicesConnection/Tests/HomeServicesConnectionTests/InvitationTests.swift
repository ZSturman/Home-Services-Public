import Foundation
import XCTest
@testable import HomeServicesConnection

final class InvitationTests: XCTestCase {
    private func uri(_ changes: [String: Any] = [:]) throws -> String {
        var payload: [String: Any] = ["protocol": 1, "provider": "home-services", "base_url": "https://host.example.net:8443",
            "server_id": "srv_fixture", "invitation_id": "pair_fixture", "secret": "fixture-secret-0001",
            "preset": "home-page", "expires_at": "2099-01-01T00:00:00Z"]
        payload.merge(changes) { _, new in new }
        let data = try JSONSerialization.data(withJSONObject: payload)
        let encoded = data.base64EncodedString().replacingOccurrences(of: "+", with: "-")
            .replacingOccurrences(of: "/", with: "_").replacingOccurrences(of: "=", with: "")
        return "homeservices://pair#" + encoded
    }

    func testSamePayloadAcceptedFromPasteOrScanner() throws {
        let code = try uri()
        let pasted = try PairingInvitation.parse(" \n" + code + "\n", expectedPreset: "home-page")
        XCTAssertEqual(pasted, try PairingInvitation.parse(code, expectedPreset: "home-page"))
        XCTAssertEqual(pasted.base_url, "https://host.example.net:8443")
        XCTAssertNoThrow(try PairingInvitation.parse(uri(["expires_at": "2099-01-01T00:00:00.123456Z"]), expectedPreset: "home-page"))
    }

    func testWrongAppProtocolProviderAndExpiredInvitationsRefused() throws {
        for change: [String: Any] in [["preset": "files-backups"], ["protocol": 2], ["provider": "other"],
                                     ["expires_at": "2020-01-01T00:00:00Z"], ["secret": "short"]] {
            XCTAssertThrowsError(try PairingInvitation.parse(uri(change), expectedPreset: "home-page"))
        }
    }

    func testCredentialAndQueryAndHttpOriginsRefused() throws {
        for url in ["http://home.example", "https://user:pass@home.example", "https://home.example/path",
                    "https://home.example?q=secret", "https://home.example#secret"] {
            XCTAssertNil(PairingInvitation.secureOrigin(url))
            XCTAssertThrowsError(try PairingInvitation.parse(uri(["base_url": url]), expectedPreset: "home-page"))
        }
    }

    func testMalformedOrOversizedCodesAndWrongSchemeRefused() throws {
        for code in ["123456", "https://pair#abcd", "homeservices://pair?token=abc#abcd", String(repeating: "a", count: 9000)] {
            XCTAssertThrowsError(try PairingInvitation.parse(code, expectedPreset: "home-page"))
        }
    }
    func testIndependentProvidersAreExplicitAndCannotCrossPair() throws {
        for provider in [PairingProvider.opportunityReview, .casinoExitRunner] {
            let code = try uri(["provider": provider.id])
            XCTAssertThrowsError(try PairingInvitation.parse(code, expectedPreset: "home-page"))
            XCTAssertNoThrow(try PairingInvitation.parse(code, expectedPreset: "home-page", expectedProvider: provider.id))
            XCTAssertThrowsError(try PairingInvitation.parse(uri(), expectedPreset: "home-page", expectedProvider: provider.id))
        }
    }

}
