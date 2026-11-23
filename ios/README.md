# BIX Lite for iOS

Native SwiftUI wrapper for the BIX Lite dashboards (Ghaf Medical Billing Services).
iOS 17+, iPhone and iPad. No analytics or third-party SDKs.

## What it does
- **Ghaf team sign-in**: opens https://reports.ghafservices.com/ in a web view. Sign-in and 2FA are handled by the web app. Cookies and local storage persist in the default WebKit data store.
- **Facility dashboard link**: paste a `https://reports.ghafservices.com/portal/dashboard?token=...` link. The app checks the host and path, then saves the link in the **iOS Keychain** (this device only). You can remove it in Settings.
- **Deep link**: `bixlite://open?url=<percent-encoded facility link>` (the app asks for confirmation before saving).
- Pull-to-refresh, progress bar, back/forward swipe, and an offline screen with Retry. Pages on other hosts open in Safari.
- Settings (gear button): switch mode, replace or remove the link, clear website data (signs you out).

## Open and run
1. Install Xcode 16 or later from the Mac App Store.
2. Open `ios/BIXLite.xcodeproj`. If it does not open, run `brew install xcodegen && cd ios && xcodegen generate`, then open the project it generates.
3. Select the **BIXLite** target > Signing & Capabilities > choose your **Team**. A free Apple ID works for simulator and personal device builds.
4. Pick an iPhone or iPad simulator and press Run (Cmd+R).
5. To test the deep link in the simulator:
   `xcrun simctl openurl booted "bixlite://open?url=https%3A%2F%2Freports.ghafservices.com%2Fportal%2Fdashboard%3Ftoken%3DTEST"`

## Release notes
- TestFlight and the App Store need a **paid Apple Developer Program** account (USD 99/year). Then use Product > Archive > Distribute App.
- Bump `MARKETING_VERSION` / `CURRENT_PROJECT_VERSION` for each upload.
- App Review may reject apps that are only a website wrapper. The native home screen, Keychain link storage and offline handling help, but expect questions. Give the reviewer a demo login.
- Universal links (`https://reports.ghafservices.com/...` opening the app) are not set up. They need the Associated Domains entitlement (paid account) and an `apple-app-site-association` file on the server.
- `ITSAppUsesNonExemptEncryption` is set to NO because the app only uses standard HTTPS.
