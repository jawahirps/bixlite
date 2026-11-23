import SwiftUI
import WebKit

@MainActor
final class WebViewModel: NSObject, ObservableObject, WKNavigationDelegate, WKUIDelegate {
    let webView: WKWebView
    @Published var progress: Double = 0
    @Published var isLoading = false
    @Published var canGoBack = false
    @Published var loadError: String?
    @Published var externalURL: URL?

    private let startURL: URL
    private var observers: [NSKeyValueObservation] = []
    private let refreshControl = UIRefreshControl()

    init(startURL: URL) {
        self.startURL = startURL
        let config = WKWebViewConfiguration()
        config.websiteDataStore = .default()   // persistent cookies + localStorage
        config.allowsInlineMediaPlayback = true
        webView = WKWebView(frame: .zero, configuration: config)
        super.init()
        webView.navigationDelegate = self
        webView.uiDelegate = self
        webView.allowsBackForwardNavigationGestures = true
        webView.scrollView.contentInsetAdjustmentBehavior = .automatic
        refreshControl.addTarget(self, action: #selector(pullToRefresh), for: .valueChanged)
        webView.scrollView.refreshControl = refreshControl

        observers = [
            webView.observe(\.estimatedProgress, options: [.new]) { [weak self] wv, _ in
                let p = wv.estimatedProgress
                Task { @MainActor in self?.progress = p }
            },
            webView.observe(\.isLoading, options: [.new]) { [weak self] wv, _ in
                let l = wv.isLoading
                Task { @MainActor in self?.isLoading = l }
            },
            webView.observe(\.canGoBack, options: [.new]) { [weak self] wv, _ in
                let b = wv.canGoBack
                Task { @MainActor in self?.canGoBack = b }
            }
        ]
        load(startURL)
    }

    func load(_ url: URL) {
        loadError = nil
        webView.load(URLRequest(url: url))
    }

    func retry() {
        loadError = nil
        if let current = webView.url, AppLinks.isInternal(current) {
            webView.reload()
        } else {
            load(startURL)
        }
    }

    func goBack() { if webView.canGoBack { webView.goBack() } }

    @objc private func pullToRefresh() {
        if webView.url == nil { load(startURL) } else { webView.reload() }
    }

    // MARK: WKNavigationDelegate

    func webView(_ webView: WKWebView,
                 decidePolicyFor navigationAction: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = navigationAction.request.url else { decisionHandler(.cancel); return }
        let scheme = url.scheme?.lowercased() ?? ""
        let isMainFrame = navigationAction.targetFrame?.isMainFrame ?? true

        if scheme == "about" || scheme == "blob" || scheme == "data" {
            decisionHandler(.allow); return
        }
        if AppLinks.isInternal(url) || !isMainFrame && (scheme == "https") {
            decisionHandler(.allow); return
        }
        // Other hosts and schemes (mailto:, tel:, http, third-party sites) leave the app.
        decisionHandler(.cancel)
        if scheme == "https" || scheme == "http" {
            externalURL = url
        } else {
            UIApplication.shared.open(url)
        }
    }

    func webView(_ webView: WKWebView, didStartProvisionalNavigation navigation: WKNavigation!) {
        loadError = nil
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        refreshControl.endRefreshing()
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!,
                 withError error: Error) {
        handle(error)
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        handle(error)
    }

    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
        webView.reload()
    }

    private func handle(_ error: Error) {
        refreshControl.endRefreshing()
        let ns = error as NSError
        if ns.domain == NSURLErrorDomain && ns.code == NSURLErrorCancelled { return }
        if ns.domain == "WebKitErrorDomain" && ns.code == 102 { return } // frame load interrupted
        loadError = ns.localizedDescription
    }

    // MARK: WKUIDelegate (target=_blank / window.open)

    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for navigationAction: WKNavigationAction,
                 windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = navigationAction.request.url {
            if AppLinks.isInternal(url) { webView.load(URLRequest(url: url)) }
            else if ["http", "https"].contains(url.scheme?.lowercased() ?? "") { externalURL = url }
        }
        return nil
    }
}

struct WebView: UIViewRepresentable {
    let model: WebViewModel
    func makeUIView(context: Context) -> WKWebView { model.webView }
    func updateUIView(_ uiView: WKWebView, context: Context) {}
}
