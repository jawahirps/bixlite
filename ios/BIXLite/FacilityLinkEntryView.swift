import SwiftUI

struct FacilityLinkEntryView: View {
    @EnvironmentObject private var state: AppState
    @Environment(\.dismiss) private var dismiss
    @State private var text = ""
    @State private var error: String?

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("https://reports.ghafservices.com/portal/dashboard?token=…",
                              text: $text, axis: .vertical)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .keyboardType(.URL)
                        .lineLimit(2...5)
                    Button {
                        if let s = UIPasteboard.general.string { text = s }
                    } label: { Label("Paste from clipboard", systemImage: "doc.on.clipboard") }
                } header: {
                    Text("Facility dashboard link")
                } footer: {
                    Text("Paste the link Ghaf shared with your facility. It is stored only in this device's Keychain.")
                }
                if let error {
                    Section { Text(error).foregroundStyle(.red) }
                }
            }
            .navigationTitle("Facility link")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Open") {
                        if state.saveFacilityLink(text) {
                            dismiss()
                        } else {
                            error = "That doesn't look like a BIX Lite facility dashboard link."
                        }
                    }
                    .disabled(text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                }
            }
        }
    }
}
