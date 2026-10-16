import XCTest

final class ShortcutTests: XCTestCase {
  func testImportAndPhotoCapture() throws {
    try exercise(mode: "photo")
  }

  func testFilenameRecovery() throws {
    try exercise(mode: "filename")
  }

  func testGermanTextCapture() throws {
    try exercise(mode: "text")
  }

  private func exercise(mode: String) throws {
    continueAfterFailure = false
    let before = try captures().count
    let shortcuts = XCUIApplication(bundleIdentifier: "com.apple.shortcuts")
    shortcuts.terminate()
    let photos = XCUIApplication(bundleIdentifier: "com.apple.mobileslideshow")
    photos.terminate()
    let app = XCUIApplication()
    app.launch()
    app.buttons["Download test Shortcut"].tap()
    let safari = XCUIApplication(bundleIdentifier: "com.apple.mobilesafari")
    XCTAssertTrue(safari.wait(for: .runningForeground, timeout: 30))
    XCTAssertTrue(safari.buttons["Download"].waitForExistence(timeout: 20))
    safari.buttons["Download"].tap()
    let files = XCUIApplication(bundleIdentifier: "com.apple.DocumentsApp")
    files.activate()
    _ = files.tabBars.firstMatch.waitForExistence(timeout: 20)
    let file = files.cells.matching(NSPredicate(format: "label BEGINSWITH 'Capture German Test'"))
      .firstMatch
    XCTAssertTrue(file.waitForExistence(timeout: 20))
    let shortcutName = file.label.components(separatedBy: ".shortcut")[0]
    file.tap()
    XCTAssertTrue(shortcuts.wait(for: .runningForeground, timeout: 30))
    XCTAssertTrue(shortcuts.buttons["Set Up Shortcut"].waitForExistence(timeout: 20))
    shortcuts.buttons["Set Up Shortcut"].tap()
    let key = shortcuts.textViews.firstMatch
    XCTAssertTrue(key.waitForExistence(timeout: 20))
    key.coordinate(withNormalizedOffset: CGVector(dx: 0.95, dy: 0.1)).tap()
    let existing = key.value as? String ?? ""
    key.typeText(String(repeating: XCUIKeyboardKey.delete.rawValue, count: existing.count))
    key.typeText("wortling-isolated-ios-test-" + String(repeating: "x", count: 40))
    XCTAssertEqual(
      key.value as? String, "wortling-isolated-ios-test-" + String(repeating: "x", count: 40))
    shortcuts.buttons["Close"].tap()
    shortcuts.buttons["Add Shortcut"].tap()
    XCTAssertTrue(shortcuts.buttons["Add Shortcut"].waitForNonExistence(timeout: 15))
    let host: XCUIApplication
    if mode == "photo" {
      photos.activate()
      if photos.buttons["Continue"].waitForExistence(timeout: 3) {
        photos.buttons["Continue"].tap()
      }
      _ = photos.buttons.firstMatch.waitForExistence(timeout: 10)
      if photos.buttons["Share"].exists {
        photos.coordinate(withNormalizedOffset: CGVector(dx: 0.09, dy: 0.095)).tap()
      }
      let fixture = photos.images.matching(identifier: "PXGGridLayout-Info").allElementsBoundByIndex
        .last!
      print("Selected fixture: \(fixture.label)")
      fixture.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5)).tap()
      XCTAssertTrue(photos.buttons["Share"].waitForExistence(timeout: 10))
      photos.buttons["Share"].tap()
      host = photos
      let shareAction = host.cells.matching(
        NSPredicate(format: "label BEGINSWITH %@", shortcutName)
      ).firstMatch
      for _ in 0..<6 {
        if shareAction.exists && shareAction.isHittable { break }
        host.swipeUp()
      }
      XCTAssertTrue(shareAction.exists, host.debugDescription)
      shareAction.tap()
    } else {
      app.terminate()
      app.launchArguments = ["--shortcut-name", shortcutName]
      app.launch()
      app.buttons[mode == "filename" ? "Run filename" : "Run German text"].tap()
      host = shortcuts
    }
    let deadline = Date().addingTimeInterval(45)
    var received: [[String: Any]] = []
    var choseRecoveryPhoto = false
    while Date() < deadline {
      received = try captures()
      if received.count > before { break }
      if mode == "filename" {
        for ui in [shortcuts, XCUIApplication(bundleIdentifier: "com.apple.springboard")] {
          let ok = ui.buttons["OK"].firstMatch
          if ok.exists && ok.isHittable { ok.tap() }
          if !choseRecoveryPhoto {
            let images = ui.images.matching(identifier: "PXGGridLayout-Info")
              .allElementsBoundByIndex
            if let image = images.first {
              image.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5)).tap()
              choseRecoveryPhoto = true
            }
          }
        }
      }
      for name in ["Always Allow", "Allow Once", "Allow"] {
        let button = XCUIApplication(bundleIdentifier: "com.apple.springboard").buttons[name]
          .firstMatch
        if button.exists && button.isHittable { button.tap() }
      }
      RunLoop.current.run(until: Date().addingTimeInterval(0.5))
    }
    XCTAssertGreaterThan(received.count, before, host.debugDescription)
    let capture = try XCTUnwrap(received.last)
    XCTAssertTrue((capture["idempotency_key"] as? String ?? "").hasPrefix("shortcut-v3:"))
    if mode == "text" {
      XCTAssertEqual(capture["text"] as? String, "Bitte Material sammeln.")
      XCTAssertNil(capture["image_path"] as? String)
      return
    }
    XCTAssertNotNil(capture["image_path"] as? String, "Photo became text: \(capture)")
    if mode == "filename" { XCTAssertTrue(choseRecoveryPhoto) }
    XCTAssertEqual(capture["format"] as? String, "JPEG")
    XCTAssertEqual(capture["width"] as? Int, 2000)
    XCTAssertEqual(capture["height"] as? Int, 1500)
    let screenshot = XCTAttachment(screenshot: XCUIScreen.main.screenshot())
    screenshot.lifetime = .keepAlways
    add(screenshot)
  }

  private func captures() throws -> [[String: Any]] {
    let semaphore = DispatchSemaphore(value: 0)
    var result: Result<Data, Error>?
    URLSession.shared.dataTask(with: URL(string: "http://127.0.0.1:8791/test/captures")!) {
      data, _, error in
      if let error = error { result = .failure(error) } else { result = .success(data!) }
      semaphore.signal()
    }.resume()
    XCTAssertEqual(semaphore.wait(timeout: .now() + 10), .success)
    return try XCTUnwrap(JSONSerialization.jsonObject(with: result!.get()) as? [[String: Any]])
  }
}
