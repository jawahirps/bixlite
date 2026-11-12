import { Routes, Route, Navigate } from "react-router-dom";
import Layout from "./components/Layout";
import ProtectedRoute from "./components/ProtectedRoute";
import Login from "./pages/Login";
import Dashboard from "./pages/Dashboard";
import ClinicalProfiles from "./pages/ClinicalProfiles";
import DhaSync from "./pages/DhaSync";
import Analytics from "./pages/Analytics";
import LoginActivity from "./pages/LoginActivity";
import FacilitySettings from "./pages/FacilitySettings";
import RemittancePortal from "./pages/portal/RemittancePortal";
import Jobs from "./pages/Jobs";
import DocReview from "./pages/DocReview";
import Scrubber from "./pages/Scrubber";
import Calibration from "./pages/Calibration";
import ResubWorklist from "./pages/ResubWorklist";
import ReportPacks from "./pages/ReportPacks";
import AccountSecurity from "./pages/AccountSecurity";

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />

      {/* Public portal — no auth required, token-gated by backend */}
      <Route path="/portal/remittance" element={<RemittancePortal />} />

      <Route element={<ProtectedRoute />}>
        <Route element={<Layout />}>
          <Route path="/" element={<Dashboard />} />
          <Route path="/clinical-profiles" element={<ClinicalProfiles />} />
          <Route path="/dha-sync" element={<DhaSync />} />
          <Route path="/analytics" element={<Analytics />} />
          <Route path="/report-packs" element={<ReportPacks />} />
          <Route path="/login-activity" element={<LoginActivity />} />
          <Route path="/account-security" element={<AccountSecurity />} />
          <Route path="/facility-settings" element={<FacilitySettings />} />
          <Route path="/jobs" element={<Jobs />} />
          <Route path="/scrubber" element={<Scrubber />} />
          <Route path="/doc-review" element={<DocReview />} />
          <Route path="/calibration" element={<Calibration />} />
          <Route path="/resub-worklist" element={<ResubWorklist />} />
        </Route>
      </Route>

      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
