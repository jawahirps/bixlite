import Foundation

enum AppLinks {
    static let host = "reports.ghafservices.com"
    static let teamURL = URL(string: "https://reports.ghafservices.com/")!
    static let facilityPath = "/portal/dashboard"
    static let scheme = "bixlite"

    /// True when the URL belongs to the BIX Lite web app (stays inside the app).
    static func isInternal(_ url: URL) -> Bool {
        url.scheme?.lowercased() == "https" && url.host?.lowercased() == host
    }

    /// Validates a facility dashboard link: https, exact host, exact path, non-empty token.
    static func validFacilityLink(_ raw: String) -> URL? {
        let trimmed = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let comps = URLComponents(string: trimmed),
              comps.scheme?.lowercased() == "https",
              comps.host?.lowercased() == host,
              comps.port == nil,
              comps.path == facilityPath || comps.path == facilityPath + "/",
              let token = comps.queryItems?.first(where: { $0.name == "token" })?.value,
              !token.isEmpty,
              let url = comps.url
        else { return nil }
        return url
    }

    /// Extracts a facility link from bixlite://open?url=<encoded> or a direct https link.
    static func facilityLink(fromIncoming url: URL) -> URL? {
        if url.scheme?.lowercased() == scheme {
            guard url.host?.lowercased() == "open",
                  let comps = URLComponents(url: url, resolvingAgainstBaseURL: false),
                  let inner = comps.queryItems?.first(where: { $0.name == "url" })?.value
            else { return nil }
            return validFacilityLink(inner)
        }
        return validFacilityLink(url.absoluteString)
    }
}
