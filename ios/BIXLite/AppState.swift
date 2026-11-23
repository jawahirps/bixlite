import SwiftUI
import WebKit

enum AppMode: String {
    case none, team, facility
}

@MainActor
final class AppState: ObservableObject {
    /// Only the mode name is kept in UserDefaults (not secret). The link lives in the Keychain.
    @Published private(set) var mode: AppMode
    @Published private(set) var facilityURL: URL?
    @Published var pendingFacilityLink: URL?
    @Published var showSettings = false
    @Published var showLinkEntry = false
    @Published var reloadToken = UUID()

    private static let modeKey = "appMode"

    init() {
        let saved = KeychainStore.load().flatMap(AppLinks.validFacilityLink)
        facilityURL = saved
        var m = AppMode(rawValue: UserDefaults.standard.string(forKey: Self.modeKey) ?? "") ?? .none
        if m == .facility && saved == nil { m = .none }
        mode = m
    }

    var currentURL: URL? {
        switch mode {
        case .team: return AppLinks.teamURL
        case .facility: return facilityURL
        case .none: return nil
        }
    }

    func setMode(_ newMode: AppMode) {
        mode = newMode
        UserDefaults.standard.set(newMode.rawValue, forKey: Self.modeKey)
        reloadToken = UUID()
    }

    func useTeam() { setMode(.team) }

    @discardableResult
    func saveFacilityLink(_ raw: String) -> Bool {
        guard let url = AppLinks.validFacilityLink(raw),
              KeychainStore.save(url.absoluteString) else { return false }
        facilityURL = url
        setMode(.facility)
        return true
    }

    func removeFacilityLink() {
        KeychainStore.delete()
        facilityURL = nil
        if mode == .facility { setMode(.none) }
    }

    func goHome() { setMode(.none) }

    /// Deep links never auto-save: the user confirms first.
    func handleIncoming(_ url: URL) {
        if let link = AppLinks.facilityLink(fromIncoming: url) {
            pendingFacilityLink = link
        }
    }

    func confirmPendingLink() {
        if let link = pendingFacilityLink { _ = saveFacilityLink(link.absoluteString) }
        pendingFacilityLink = nil
    }

    /// Signs out of the web app by clearing cookies, local storage and caches.
    func clearWebsiteData() async {
        let store = WKWebsiteDataStore.default()
        await store.removeData(ofTypes: WKWebsiteDataStore.allWebsiteDataTypes(),
                               modifiedSince: .distantPast)
        reloadToken = UUID()
    }
}
