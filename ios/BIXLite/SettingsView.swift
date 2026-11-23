import SwiftUI

struct SettingsView: View {
    @EnvironmentObject private var state: AppState
    @Environment(\.dismiss) private var dismiss
    @State private var confirmSignOut = false
    @State private var confirmRemove = false

    var body: some View {
        NavigationStack {
            Form {
                Section("Mode") {
                    Button { state.useTeam(); dismiss() } label: {
                        row("Ghaf team sign-in", "person.badge.key", selected: state.mode == .team)
                    }
                    Button {
                        if state.facilityURL != nil { state.setMode(.facility); dismiss() }
                        else { dismiss(); state.showLinkEntry = true }
                    } label: {
                        row("Facility dashboard", "link", selected: state.mode == .facility)
                    }
                    Button { state.goHome(); dismiss() } label: {
                        row("Home screen", "house", selected: state.mode == .none)
                    }
                }
                Section("Facility link") {
                    if state.facilityURL != nil {
                        Label("A link is saved in the Keychain", systemImage: "lock.fill")
                        Button("Replace link") { dismiss(); state.showLinkEntry = true }
                        Button("Remove saved link", role: .destructive) { confirmRemove = true }
                    } else {
                        Button("Add facility link") { dismiss(); state.showLinkEntry = true }
                    }
                }
                Section {
                    Button("Clear website data (sign out)", role: .destructive) { confirmSignOut = true }
                } footer: {
                    Text("Removes cookies, local storage and cache for the web dashboards on this device.")
                }
                Section {
                    LabeledContent("Version", value: appVersion)
                    LabeledContent("Publisher", value: "Ghaf Medical Billing Services")
                }
            }
            .navigationTitle("Settings")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() } } }
            .confirmationDialog("Remove the saved facility link?", isPresented: $confirmRemove,
                                titleVisibility: .visible) {
                Button("Remove", role: .destructive) { state.removeFacilityLink() }
            }
            .confirmationDialog("Sign out and clear website data?", isPresented: $confirmSignOut,
                                titleVisibility: .visible) {
                Button("Clear and sign out", role: .destructive) {
                    Task { await state.clearWebsiteData() }
                }
            }
        }
    }

    private func row(_ title: String, _ icon: String, selected: Bool) -> some View {
        HStack {
            Label(title, systemImage: icon)
            Spacer()
            if selected { Image(systemName: "checkmark").foregroundStyle(Brand.teal) }
        }
    }

    private var appVersion: String {
        let v = Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "?"
        let b = Bundle.main.infoDictionary?["CFBundleVersion"] as? String ?? "?"
        return "\(v) (\(b))"
    }
}
