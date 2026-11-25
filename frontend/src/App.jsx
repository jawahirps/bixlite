import { lazy, Suspense } from "react";
import { Routes, Route, Navigate } from "react-router-dom";
import Layout from "./components/Layout";
import ProtectedRoute from "./components/ProtectedRoute";
import Login from "./pages/Login";
const Dashboard = lazy(() => import("./pages/Dashboard"));
const ClinicalProfiles = lazy(() => import("./pages/ClinicalProfiles"));
const DhaSync = lazy(() => import("./pages/DhaSync"));
const Analytics = lazy(() => import("./pages/Analytics"));
const LoginActivity = lazy(() => import("./pages/LoginActivity"));
const FacilitySettings = lazy(() => import("./pages/FacilitySettings"));
const RemittancePortal = lazy(() => import("./pages/portal/RemittancePortal"));
const DashboardPortal = lazy(() => import("./pages/portal/DashboardPortal"));
const Jobs = lazy(() => import("./pages/Jobs"));
const DocReview = lazy(() => import("./pages/DocReview"));
const Scrubber = lazy(() => import("./pages/Scrubber"));
const Calibration = lazy(() => import("./pages/Calibration"));
const ScrubRules = lazy(() => import("./pages/ScrubRules"));
const ResubWorklist = lazy(() => import("./pages/ResubWorklist"));
const ReportPacks = lazy(() => import("./pages/ReportPacks"));
const Alerts = lazy(() => import("./pages/Alerts"));
const Turnaround = lazy(() => import("./pages/Turnaround"));
const DenialTrends = lazy(() => import("./pages/DenialTrends"));
const AccountSecurity = lazy(() => import("./pages/AccountSecurity"));
const Users = lazy(() => import("./pages/Users"));

// Pages load on demand, so the first screen downloads only what it needs.
function PageLoading() {
  return (
    <div className="flex items-center gap-3 py-16 text-sm text-slate-600" role="status">
      <span className="h-4 w-4 animate-spin rounded-full border-2 border-ghaf-200 border-t-ghaf-600" aria-hidden="true" />
      Loading…
    </div>
  );
}

export default function App() {
  return (
    <Suspense fallback={<PageLoading />}>
    <Routes>
      <Route path="/login" element={<Login />} />

      {/* Public portal — no auth required, token-gated by backend */}
      <Route path="/portal/remittance" element={<RemittancePortal />} />
      <Route path="/portal/dashboard" element={<DashboardPortal />} />

      <Route element={<ProtectedRoute />}>
        <Route element={<Layout />}>
          <Route path="/" element={<Dashboard />} />
          <Route path="/clinical-profiles" element={<ClinicalProfiles />} />
          <Route path="/dha-sync" element={<DhaSync />} />
          <Route path="/analytics" element={<Analytics />} />
          <Route path="/report-packs" element={<ReportPacks />} />
          <Route path="/login-activity" element={<LoginActivity />} />
          <Route path="/users" element={<Users />} />
          <Route path="/account-security" element={<AccountSecurity />} />
          <Route path="/facility-settings" element={<FacilitySettings />} />
          <Route path="/jobs" element={<Jobs />} />
          <Route path="/scrubber" element={<Scrubber />} />
          <Route path="/doc-review" element={<DocReview />} />
          <Route path="/calibration" element={<Calibration />} />
          <Route path="/scrub-rules" element={<ScrubRules />} />
          <Route path="/denial-trends" element={<DenialTrends />} />
          <Route path="/alerts" element={<Alerts />} />
          <Route path="/turnaround" element={<Turnaround />} />
          <Route path="/resub-worklist" element={<ResubWorklist />} />
        </Route>
      </Route>

      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
    </Suspense>
  );
}
