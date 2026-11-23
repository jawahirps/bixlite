import SwiftUI

struct WelcomeView: View {
    @EnvironmentObject private var state: AppState

    var body: some View {
        ZStack {
            Brand.gradient.ignoresSafeArea()
            VStack(spacing: 28) {
                Spacer()
                ZStack {
                    RoundedRectangle(cornerRadius: 28, style: .continuous)
                        .fill(.white.opacity(0.12))
                        .frame(width: 104, height: 104)
                    Text("B")
                        .font(.system(size: 64, weight: .black, design: .rounded))
                        .foregroundStyle(.white)
                }
                VStack(spacing: 8) {
                    Text("BIX Lite")
                        .font(.system(size: 44, weight: .heavy, design: .rounded))
                    Text("Ghaf Medical Billing Services")
                        .font(.subheadline.weight(.medium))
                        .opacity(0.8)
                }
                .foregroundStyle(.white)
                Spacer()
                VStack(spacing: 14) {
                    Button { state.useTeam() } label: {
                        Label("Ghaf team sign-in", systemImage: "person.badge.key.fill")
                    }
                    .buttonStyle(BigButtonStyle(filled: true))

                    Button {
                        if state.facilityURL != nil { state.setMode(.facility) }
                        else { state.showLinkEntry = true }
                    } label: {
                        Label(state.facilityURL != nil ? "Open saved facility dashboard"
                                                       : "Open facility dashboard link",
                              systemImage: "link")
                    }
                    .buttonStyle(BigButtonStyle(filled: false))

                    Button("Settings") { state.showSettings = true }
                        .font(.footnote.weight(.semibold))
                        .foregroundStyle(.white.opacity(0.8))
                        .padding(.top, 6)
                }
                .frame(maxWidth: 460)
                .padding(.horizontal, 28)
                .padding(.bottom, 32)
            }
        }
    }
}
