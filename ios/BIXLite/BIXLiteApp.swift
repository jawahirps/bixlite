import SwiftUI

@main
struct BIXLiteApp: App {
    @StateObject private var state = AppState()

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environmentObject(state)
                .onOpenURL { url in state.handleIncoming(url) }
        }
    }
}
