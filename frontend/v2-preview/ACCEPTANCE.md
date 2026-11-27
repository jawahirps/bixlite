# BIX Lite polished workspace acceptance

## Automated acceptance (synthetic only)

Run from the workspace:
```
node bix-polish-qa/regress.cjs /path/to/final.html
```
The harness runs inline JS with a light mocked DOM, no requests, no browser, no files modified. It reports nonzero exit on failures. The integrated older prototype passes the first six groups but deliberately fails the identity-control requirement; the identity-profile intermediate passes all seven groups. The final file must be run separately. The harness is not proof of browser rendering, production logic, security or actual DHA reconciliation.

| Test | Acceptance |
|---|---|
| Monetary partition | Each unique synthetic facility + claim: claimed net = paid + denied not resubmitted + resubmission outstanding + awaiting initial RA. Nonnegative amounts, allocated exactly once. Historical submission events not additional balance. |
| First view | Claimed and four allocated balances shown; no decorative sparklines; demo marker and non-overlap definition visible. |
| Grouped comparison | One stacked column per month; claimed total is height and clickable label, four non-overlapping allocations Paid, Denied, Resubmitted, Awaiting RA. All segments and month labels are accessible actions; hover/focus and drill retain applied scope. Non-color separation and patterns reinforce segments. |
| Scope | Facility and other category filters, date enable/disable, clear/select all, no implicit widening when drilling; applied state restored on reload for the assigned profile. |
| Claim evidence | Original claim → initial rejection → first RS / RA → second RS / RA → decision; pending RA stops pathway until evidence exists; sample report and XML marked not for submission; worklist supports pagination. |
| Draft guard | No reconciliation draft for paid, initial awaiting, or pending repeated RA. Second/repeated rejection or reconciliation-review only, human review required. No recipients, sending, submission or payer connectivity; local-only status. |
| Identity | No normal dashboard role/tenant/plan selector. Assigned frozen fixture yields scope and permissions; out-of-scope direct claim cannot open. No client-only enforcement claimed as production security. |

## Manual real-browser acceptance (required for final approval)
- Desktop 1440/1024 and mobile 390/320: no page-level overflow, legible currency and dates, intentional glass effect that preserves contrast, visible demo banner; sidebar/top menu collapses correctly.
- Tab cycle reaches each metric, chart bar, month label, filter, evidence and modal control. Focus visible; touch hit-target adequate (or provide larger alternative control); Escape closes modal; chart scrolls horizontally on mobile.
- Select a subset of a permitted facility, date range and payer; apply; verify exact selected scope, then drill from KPI and grouped chart; evidence/table values reconcile to displayed scoped totals. Clear chart selection without clearing applied filters. Reload, confirm profile-scoped preferences.
- Click a first-RA-pending claim and fully paid claim: reconciliation action visibly disabled. Repeated rejection path: draft becomes available, wording says draft-only, downloaded XML explicitly non-submittable. Open last worklist page and direct evidence link; return to original filtered worklist.
- Verify all user-facing profile/tenant/plan controls are read-only or absent; UI never lets the user acquire elevated permissions. Test separate fixed-profile builds for Viewer/Analyst/SME/Owner in staging if implemented.
- Performance: UI loads from local fixture without network; resize preferences persist; no stale scope appears after any filter change. Check console errors and no outgoing requests.
- Do not mark data sync healthy, regulatory rules validated, or production deployed based on synthetic prototype.

## Theme checks
- Use the top-navigation colour control to switch Aurora, Oceanic, Amethyst, Solar, including mobile and keyboard focus. All figures, filter scope, selected claim and capability gates remain unchanged. Reload with the same local profile and verify the chosen theme restores. Inspect text contrast, small stack segments and touch on narrow viewports.

## Production gate (outside prototype)
Server-side verified identity, tenant and per-resource facility authorization, plan/feature entitlements, audited PHI access, safe export, secure draft lifecycle and separate authorized sending, real source manifests and deduped ERA/RA lineage, independent KPI and performance verification. Client-side mock guards are not an access control.

