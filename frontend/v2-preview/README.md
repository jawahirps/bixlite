# BIX Lite V2.0 — isolated UI development branch

This branch preserves the existing BIX Lite application. The approved standalone redesign lives at `frontend/public/v2/index.html` and is included as a static asset in Vite builds. Start the existing frontend with `npm ci && npm run dev` from `frontend`, then open `http://localhost:5173/v2/index.html`. The existing application is unchanged.

The V2.0 route uses **synthetic data only**; it does not authenticate, contact the backend, submit claims, send emails or access real records. The standalone entitlement fixture is for development only, not a public route. Production integration requires mapping the dashboard to validated server APIs, server-enforced tenant/permission checks, and integration testing with authorized non-production data.

## Test
From the repository root:
```sh
node frontend/v2-preview/regress.cjs frontend/public/v2/index.html
node frontend/v2-preview/patches/gestures/test.cjs
```
See HANDOFF.md and ACCEPTANCE.md for scope, UX, browser acceptance, and the remaining production gates. No deployment or main-branch merge is implied.
