import SwiftUI

struct ContentView: View {
    @EnvironmentObject private var state: AppState

    var body: some View {
        Group {
            if let url = state.currentURL {
                WebContainerView(startURL: url)
                    .id(state.reloadToken)
            } else {
                WelcomeView()
            }
        }
        .sheet(isPresented: $state.showSettings) { SettingsView() }
        .sheet(isPresented: $state.showLinkEntry) { FacilityLinkEntryView() }
        .alert("Open facility dashboard?",
               isPresented: Binding(get: { state.pendingFacilityLink != nil },
                                    set: { if !$0 { state.pendingFacilityLink = nil } })) {
            Button("Open and save") { state.confirmPendingLink() }
            Button("Cancel", role: .cancel) { state.pendingFacilityLink = nil }
        } message: {
            Text("This link will be saved securely in the Keychain on this device.")
        }
    }
}
