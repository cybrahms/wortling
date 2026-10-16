import SwiftUI

@main
struct Harness: App {
  var body: some Scene {
    WindowGroup {
      VStack(spacing: 30) {
        Text("Wortling isolated Shortcut test")
        Button("Download test Shortcut") {
          UIApplication.shared.open(URL(string: "http://127.0.0.1:8791/test/shortcut")!)
        }
        Button("Run filename") { run(text: "IMG_1898") }
        Button("Run German text") { run(text: "Bitte Material sammeln.") }
      }
    }
  }
  private func run(text: String) {
    let args = ProcessInfo.processInfo.arguments
    guard let index = args.firstIndex(of: "--shortcut-name"), index + 1 < args.count else { return }
    var url = URLComponents(string: "shortcuts://run-shortcut")!
    url.queryItems = [
      URLQueryItem(name: "name", value: args[index + 1]),
      URLQueryItem(name: "input", value: "text"),
      URLQueryItem(name: "text", value: text),
    ]
    UIApplication.shared.open(url.url!)
  }
}
