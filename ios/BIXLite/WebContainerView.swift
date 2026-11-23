import SwiftUI
import SafariServices

struct WebContainerView: View {
    @EnvironmentObject private var state: AppState
    @StateObject private var model: WebViewModel

    init(startURL: URL) {
        _model = StateObject(wrappedValue: WebViewModel(startURL: startURL))
    }

    var body: some View {
        ZStack(alignment: .top) {
            WebView(model: model)
                .ignoresSafeArea(edges: .bottom)

            if model.isLoading && model.progress < 1 {
                ProgressView(value: model.progress)
                    .progressViewStyle(.linear)
                    .tint(Brand.teal)
                    .frame(height: 3)
            }

            if let message = model.loadError {
                OfflineView(message: message) { model.retry() }
            }
        }
        .overlay(alignment: .bottomTrailing) {
            Button { state.showSettings = true } label: {
                Image(systemName: "gearshape.fill")
                    .font(.system(size: 18, weight: .semibold))
                    .foregroundStyle(.white)
                    .frame(width: 46, height: 46)
                    .background(Circle().fill(Brand.navy.opacity(0.85)))
                    .shadow(radius: 4, y: 2)
            }
            .accessibilityLabel("Settings")
            .padding(16)
        }
        .sheet(item: Binding(get: { model.externalURL.map(IdentifiedURL.init) },
                             set: { model.externalURL = $0?.url })) { item in
            SafariView(url: item.url).ignoresSafeArea()
        }
    }
}

struct IdentifiedURL: Identifiable {
    let url: URL
    var id: String { url.absoluteString }
}

struct OfflineView: View {
    let message: String
    let retry: () -> Void

    var body: some View {
        ZStack {
            Brand.gradient.ignoresSafeArea()
            VStack(spacing: 18) {
                Image(systemName: "wifi.exclamationmark")
                    .font(.system(size: 54, weight: .semibold))
                Text("Can't reach BIX Lite")
                    .font(.title2.bold())
                Text(message)
                    .font(.subheadline)
                    .multilineTextAlignment(.center)
                    .opacity(0.8)
                Button("Retry", action: retry)
                    .buttonStyle(BigButtonStyle(filled: true))
                    .frame(maxWidth: 260)
                    .padding(.top, 8)
            }
            .foregroundStyle(.white)
            .padding(32)
        }
    }
}

struct SafariView: UIViewControllerRepresentable {
    let url: URL
    func makeUIViewController(context: Context) -> SFSafariViewController {
        SFSafariViewController(url: url)
    }
    func updateUIViewController(_ vc: SFSafariViewController, context: Context) {}
}
