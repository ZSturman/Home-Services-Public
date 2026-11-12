import XCTest

/// Opt-in acceptance against a connected phone and live Home Services. Run
/// alongside Files & Backups' UI test helpers in an isolated source export.
/// Saves the actual connection, relaunches with no test credentials, downloads
/// and restores one explicitly selected small archived file into a new folder.
final class MobileRecoveryTests: XCTestCase {
    func testSavedConnectionBrowsesAndRestoresCurrentMacBackup() throws {
        let env = ProcessInfo.processInfo.environment
        guard let url = env["FB_HS_TEST_URL"], let token = env["FB_HS_TEST_TOKEN"],
              let snapshot = env["FB_RECOVERY_SNAPSHOT"], let relative = env["FB_RECOVERY_RELATIVE_PATH"] else {
            throw XCTSkip("Live recovery environment is required")
        }
        let app = XCUIApplication()
        app.launchEnvironment = ["FB_UITEST_PREFILL": "1", "FB_UITEST_PREFILL_KIND": "homeServices",
                                 "FB_UITEST_NAME": "Pi Home Services", "FB_HS_TEST_URL": url,
                                 "FB_HS_TEST_TOKEN": token, "FB_HS_TEST_ROOT": ""]
        app.launch()
        let saved = app.buttons.containing(NSPredicate(format: "label CONTAINS 'Pi Home Services'")).firstMatch
        _ = saved.waitForExistence(timeout: 5)
        if !saved.exists {
            let add = app.buttons["addStorageEmpty"].exists ? app.buttons["addStorageEmpty"] : app.buttons["addStorage"]
            XCTAssertTrue(add.waitForExistence(timeout: 10))
            add.tap()
            reveal(app.buttons["testConnection"], in: app).tap()
            XCTAssertTrue(waitRevealing(app.staticTexts["Connected"], in: app, timeout: 45))
            reveal(app.buttons["saveConnection"], in: app).tap()
            XCTAssertTrue(saved.waitForExistence(timeout: 15))
        }
        // Reopen the production state with credentials loaded only from Keychain.
        app.terminate()
        app.launchEnvironment = [:]
        app.launchArguments = ["-FBInitialTab", "backups"]
        app.launch()
        let browse = app.buttons.containing(NSPredicate(format: "label BEGINSWITH 'Browse snapshots on Pi Home Services'")).firstMatch
        XCTAssertTrue(waitRevealing(browse, in: app, timeout: 45))
        browse.tap()
        let selected = app.staticTexts.containing(NSPredicate(format: "label CONTAINS %@", String(snapshot.prefix(8)))).firstMatch
        XCTAssertTrue(waitRevealing(selected, in: app, timeout: 60), "current Mac snapshot is visible")
        selected.tap()
        let parts = relative.split(separator: "/").map(String.init)
        for part in parts.dropLast() {
            let folder = app.staticTexts[part].firstMatch
            XCTAssertTrue(waitRevealing(folder, in: app, timeout: 45), "snapshot folder \(part)")
            folder.tap()
        }
        let name = try XCTUnwrap(parts.last)
        let file = app.staticTexts[name].firstMatch
        XCTAssertTrue(waitRevealing(file, in: app, timeout: 45))
        file.tap()
        let done = app.navigationBars.buttons["Done"]
        XCTAssertTrue(done.waitForExistence(timeout: 45), "archived file downloads and opens on phone")
        done.tap()
        XCTAssertTrue(file.waitForExistence(timeout: 15))
        file.swipeLeft()
        let restore = app.buttons["Restore"].firstMatch
        XCTAssertTrue(restore.waitForExistence(timeout: 10))
        restore.tap()
        let confirm = app.buttons["Restore to a New Folder"]
        XCTAssertTrue(confirm.waitForExistence(timeout: 10))
        confirm.tap()
        let restored = app.staticTexts.containing(NSPredicate(format: "label BEGINSWITH 'Restored '")).firstMatch
        XCTAssertTrue(restored.waitForExistence(timeout: 120), "server restore completes on phone")
        print("MOBILE_RECOVERY_RESULT: \(restored.label)")
        // Leave the normal installed application on its real Backups tab.
        app.terminate()
        app.launchArguments = ["-FBInitialTab", "backups"]
        app.launch()
    }
}
