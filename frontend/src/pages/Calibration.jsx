/**
 * Calibration.jsx — Layer 3: Denial Intelligence
 *
 * Three panels:
 *  1. Rule Precision — per scrub rule: how often a triggered rule led to an actual RA denial
 *  2. Denial Queue   — claims denied AND flagged by scrubber → resubmission candidates
 *  3. Top Denial Codes — most frequent RA denial codes + amounts
 */
import { useEffect, useState } from "react";
import apiClient from "../api/client";
import { useAuth } from "../context/AuthContext";
import { readFacility, saveFacility } from "../utils/facility";

const PRIORITY_STYLE = {
  CRITICAL:   "bg-red-100 text-red-800 font-bold",
  HIGH:       "bg-orange-100 text-orange-700 font-semibold",
  MEDIUM:     "bg-amber-100 text-amber-700",
  PROACTIVE:  "bg-purple-100 text-purple-800 font-semibold",
};

const SEV_STYLE = {
  BLOCK: "bg-red-100 text-red-700",
  WARN:  "bg-amber-100 text-amber-700",
  PASS:  "bg-emerald-100 text-emerald-700",
};

function fmt(n) {
  if (n == null) return "—";
  return Number(n).toLocaleString("en-AE", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function PrecisionBar({ pct }) {
  if (pct == null) return <span className="text-slate-600 text-xs">no RA yet</span>;
  const color = pct >= 70 ? "bg-red-500" : pct >= 40 ? "bg-amber-400" : "bg-emerald-400";
  return (
    <div className="flex items-center gap-2">
      <div className="w-20 rounded-full bg-slate-200 h-2">
        <div className={`h-2 rounded-full ${color}`} style={{ width: `${Math.min(pct, 100)}%` }} />
      </div>
      <span className="text-xs font-medium text-slate-700">{pct}%</span>
    </div>
  );
}

export default function Calibration() {
  const { user } = useAuth();
  const [facilities, setFacilities] = useState([]);
  const [facilityId, setFacilityId] = useState(() => {
    const saved = readFacility();
    return saved ? Number(saved) : (user?.facility_id || 13);
  });

  const [tab, setTab] = useState("files"); // files | queue | codes | rules
  const [queueView, setQueueView] = useState("claims"); // claims | by_file
  const [expandedFiles, setExpandedFiles] = useState(new Set());
  const [ruleStats, setRuleStats]     = useState([]);
  const [denialQueue, setDenialQueue] = useState([]);
  const [denialCodes, setDenialCodes] = useState([]);
  const [fileSummary, setFileSummary] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  // Recall queue date filter
  const [queueDateFrom, setQueueDateFrom] = useState("");
  const [queueDateTo,   setQueueDateTo]   = useState("");
  const [queueLoading,  setQueueLoading]  = useState(false);
  const [exportingExcel, setExportingExcel] = useState(false);

  useEffect(() => {
    apiClient.get("/facilities")
      .then(r => setFacilities(Array.isArray(r.data) ? r.data : []))
      .catch(err => console.error("Failed to load facilities", err));
    loadAll(facilityId);
  }, []);

  async function loadQueue(fid, dateFrom, dateTo) {
    setQueueLoading(true);
    try {
      const params = { facility_id: fid, limit: 500 };
      if (dateFrom) params.date_from = dateFrom;
      if (dateTo)   params.date_to   = dateTo;
      const r = await apiClient.get("/calibration/denial-queue", { params });
      setDenialQueue(r.data);
    } catch (e) {
      setError(e.response?.data?.detail || e.message);
    } finally { setQueueLoading(false); }
  }

  async function downloadRecallExcel() {
    setExportingExcel(true);
    try {
      const params = { facility_id: facilityId };
      if (queueDateFrom) params.date_from = queueDateFrom;
      if (queueDateTo)   params.date_to   = queueDateTo;
      const r = await apiClient.get("/calibration/export-recall-excel", { params, responseType: "blob" });
      const url = URL.createObjectURL(r.data);
      const a = document.createElement("a"); a.href = url;
      a.download = `recall_queue_${facilityId}.xlsx`; a.click();
      setTimeout(() => URL.revokeObjectURL(url), 5000);
    } catch (e) {
      setError("Export failed: " + (e.response?.data?.detail || e.message));
    } finally { setExportingExcel(false); }
  }

  async function loadAll(fid) {
    setLoading(true); setError(null);
    try {
      const [r1, r3, r4] = await Promise.all([
        apiClient.get("/calibration/rule-stats",         { params: { facility_id: fid } }),
        apiClient.get("/calibration/denial-codes",       { params: { facility_id: fid } }),
        apiClient.get("/calibration/file-denial-summary",{ params: { facility_id: fid } }),
      ]);
      setRuleStats(r1.data);
      setDenialCodes(r3.data);
      setFileSummary(r4.data);
    } catch (e) {
      setError(e.response?.data?.detail || e.message);
    } finally {
      setLoading(false);
      await loadQueue(fid, queueDateFrom, queueDateTo);
    }
  }

  function handleFacilityChange(id) {
    setFacilityId(id);
    saveFacility(id);
    loadAll(id);
  }

  // Summary counts
  const upgradeSuggestions = ruleStats.filter(r => r.suggested_severity === "BLOCK").length;
  const criticalQueue      = denialQueue.filter(d => d.priority === "CRITICAL").length;
  const totalDenied        = denialQueue.reduce((s, d) => s + d.denial_amount, 0);

  return (
    <div className="space-y-6">
      {/* Header */}
      <div>
        <p className="gh-eyebrow">Denials</p>
        <h1 className="gh-h1">Denial Intelligence</h1>
        <p className="text-sm text-slate-500">
          Layer 3 — Correlates scrub findings with RA denial outcomes to calibrate rule weights
          and surface resubmission candidates.
        </p>
      </div>

      {/* Facility + refresh */}
      <div className="gh-card p-4 flex flex-wrap items-end gap-3">
        <div className="flex-1 min-w-48">
          <label className="mb-1 block text-xs font-medium text-slate-600">Facility</label>
          {facilities.length > 0 ? (
            <select
              value={facilityId}
              onChange={e => handleFacilityChange(Number(e.target.value))}
              className="gh-input w-full py-1.5"
            >
              {facilities.map(f => (
                <option key={f.id} value={f.id}>{f.facility_name} ({f.dhpo_provider_id || "—"})</option>
              ))}
            </select>
          ) : (
            <input type="number" value={facilityId}
              onChange={e => handleFacilityChange(Number(e.target.value))}
              className="gh-input w-40 py-1.5" />
          )}
        </div>
        <button
          onClick={() => apiClient.post("/calibration/refresh", null, { params: { facility_id: facilityId } })
            .catch(() => {}).finally(() => loadAll(facilityId))}
          disabled={loading}
          className="gh-btn gh-btn-sm"
        >
          {loading ? "Loading…" : "Refresh"}
        </button>
      </div>

      {error && <div className="rounded-lg bg-red-50 p-3 text-sm text-red-700">{error}</div>}

      {/* KPI cards */}
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        {[
          { label: "Files to Resubmit",  value: fileSummary.filter(f => f.window_status !== "expired").length, color: "bg-amber-50 text-amber-700" },
          { label: "Urgent (≤5 days)",  value: fileSummary.filter(f => f.window_status === "urgent").length,  color: fileSummary.filter(f=>f.window_status==="urgent").length > 0 ? "bg-red-50 text-red-700" : "bg-slate-50 text-slate-400" },
          { label: "Recall Queue",      value: denialQueue.length,     color: denialQueue.length > 0 ? "bg-orange-50 text-orange-700" : "bg-slate-50 text-slate-400" },
          { label: "AED Denied",        value: fmt(totalDenied),       color: totalDenied > 0 ? "bg-rose-50 text-rose-700" : "bg-slate-50 text-slate-400" },
        ].map(s => (
          <div key={s.label} className={`rounded-lg border border-hairline p-4 ${s.color}`}>
            <p className="text-2xl font-bold">{s.value}</p>
            <p className="text-xs font-medium mt-0.5 opacity-70">{s.label}</p>
          </div>
        ))}
      </div>

      {/* Tabs */}
      <div className="flex gap-1 overflow-x-auto border-b border-slate-200">
        {[
              { key: "files",  label: `Resubmission Files (${fileSummary.length})` },
          { key: "queue",  label: `Recall Queue (${denialQueue.length})` },
          { key: "codes",  label: `Denial Codes (${denialCodes.length})` },
          { key: "rules",  label: `Rule Precision (${ruleStats.length})` },
        ].map(t => (
          <button key={t.key} onClick={() => setTab(t.key)}
            className={`shrink-0 whitespace-nowrap px-4 py-2 text-sm font-medium border-b-2 transition-colors ${
              tab === t.key
                ? "border-ghaf-600 text-ghaf-700"
                : "border-transparent text-slate-500 hover:text-slate-700"
            }`}>
            {t.label}
          </button>
        ))}
      </div>

      {/* ── Tab: Resubmission Files ────────────────────────────────────────── */}
      {tab === "files" && (
        <div className="space-y-3">
          <div className="rounded-xl bg-amber-50 border border-amber-200 px-4 py-3">
            <p className="text-xs text-amber-900">
              <strong>21-day resubmission window</strong> — DHA allows resubmission within 21 days of
              the RemittanceAdvice date. Each row below is one ClaimSubmission file with denied claims.
              Correct the flagged issues and resubmit the full file via eClaimLink before the deadline.
            </p>
          </div>

          {fileSummary.length === 0 && !loading ? (
            <div className="gh-card p-6 text-center text-slate-600 text-sm">
              No files with denied claims found — either no RA data yet or all claims were paid.
            </div>
          ) : (
            <div className="gh-card overflow-hidden">
              <div className="overflow-x-auto">
                <table className="min-w-full divide-y divide-slate-200 text-sm">
                  <thead className="bg-slate-50">
                    <tr>
                      <th className="px-3 py-2 text-left font-semibold text-slate-600">Window</th>
                      <th className="px-3 py-2 text-left font-semibold text-slate-600">File</th>
                      <th className="px-3 py-2 text-left font-semibold text-slate-600">Sub. Date</th>
                      <th className="px-3 py-2 text-right font-semibold text-slate-600">Claims</th>
                      <th className="px-3 py-2 text-right font-semibold text-slate-600">Denied</th>
                      <th className="px-3 py-2 text-right font-semibold text-slate-600">AED Denied</th>
                      <th className="px-3 py-2 text-left font-semibold text-slate-600">RA Received</th>
                      <th className="px-3 py-2 text-left font-semibold text-slate-600">Days Left</th>
                      <th className="px-3 py-2 text-left font-semibold text-slate-600">Denial Codes</th>
                      <th className="px-3 py-2 text-left font-semibold text-slate-600">Scrub Rules</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-100">
                    {fileSummary.map(f => {
                      const windowStyle =
                        f.window_status === "urgent"  ? "bg-red-50" :
                        f.window_status === "open"    ? "bg-amber-50/40" :
                        f.window_status === "expired" ? "bg-slate-50"    : "";

                      const daysChip = (() => {
                        if (f.days_remaining == null)  return <span className="text-slate-600 text-xs">no RA</span>;
                        if (f.days_remaining < 0)      return <span className="gh-chip gh-chip-neutral">Expired</span>;
                        if (f.days_remaining <= 5)     return <span className="gh-chip gh-chip-denied font-bold">{f.days_remaining}d left ⚠</span>;
                        return <span className="gh-chip gh-chip-paid">{f.days_remaining}d left</span>;
                      })();

                      return (
                        <tr key={f.raw_xml_id} className={`hover:brightness-95 ${windowStyle}`}>
                          <td className="px-3 py-2">
                            {f.window_status === "urgent"  && <span className="gh-chip gh-chip-denied font-bold">URGENT</span>}
                            {f.window_status === "open"    && <span className="gh-chip gh-chip-pending">OPEN</span>}
                            {f.window_status === "expired" && <span className="gh-chip gh-chip-neutral">EXPIRED</span>}
                            {f.window_status === "no_ra"   && <span className="gh-chip gh-chip-brand">NO RA</span>}
                          </td>
                          <td className="px-3 py-2 text-slate-800 text-xs max-w-xs truncate font-medium" title={f.source_filename}>
                            {f.source_filename || `raw_${f.raw_xml_id}`}
                          </td>
                          <td className="px-3 py-2 text-slate-500 text-xs">{f.submission_date || "—"}</td>
                          <td className="px-3 py-2 text-right text-slate-600">{f.total_claims}</td>
                          <td className="px-3 py-2 text-right font-semibold text-red-700">{f.denied_claims}</td>
                          <td className="px-3 py-2 text-right font-bold text-red-700">{fmt(f.total_denial_amount)}</td>
                          <td className="px-3 py-2 text-slate-600 text-xs">{f.earliest_ra_date || "—"}</td>
                          <td className="px-3 py-2">{daysChip}</td>
                          <td className="px-3 py-2">
                            <div className="flex flex-wrap gap-1">
                              {(f.denial_codes || []).slice(0, 3).map(c => (
                                <span key={c} className="gh-chip gh-chip-pending">{c}</span>
                              ))}
                              {(f.denial_codes || []).length > 3 && (
                                <span className="text-xs text-slate-600">+{f.denial_codes.length - 3}</span>
                              )}
                            </div>
                          </td>
                          <td className="px-3 py-2">
                            <div className="flex flex-wrap gap-1">
                              {(f.scrub_rules || []).slice(0, 2).map(r => (
                                <span key={r} className="rounded bg-slate-100 px-1.5 py-0.5 text-xs font-mono text-slate-600">{r}</span>
                              ))}
                              {(f.scrub_rules || []).length > 2 && (
                                <span className="text-xs text-slate-600">+{f.scrub_rules.length - 2}</span>
                              )}
                            </div>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
              <div className="border-t border-slate-100 bg-slate-50 px-4 py-2">
                <p className="text-xs text-slate-500">
                  Sorted by AED denied descending. Correct scrub findings, rebuild the ClaimSubmission XML, and resubmit via eClaimLink before the window closes.
                  Expired files may still be worth appealing directly with the payer.
                </p>
              </div>
            </div>
          )}
        </div>
      )}

      {/* ── Tab: Rule Precision ─────────────────────────────────────────────── */}
      {tab === "rules" && (
        <div className="gh-card overflow-hidden">
          {ruleStats.length === 0 && !loading ? (
            <p className="p-6 text-center text-slate-600 text-sm">
              No scrub findings matched to RA outcomes yet — fetch more dates and re-scrub.
            </p>
          ) : (
            <div className="overflow-x-auto">
              <table className="min-w-full divide-y divide-slate-200 text-sm">
                <thead className="bg-slate-50">
                  <tr>
                    <th className="px-3 py-2 text-left font-semibold text-slate-600">Rule</th>
                    <th className="px-3 py-2 text-left font-semibold text-slate-600">Current</th>
                    <th className="px-3 py-2 text-right font-semibold text-slate-600">Triggers</th>
                    <th className="px-3 py-2 text-right font-semibold text-slate-600">With RA</th>
                    <th className="px-3 py-2 text-right font-semibold text-slate-600">Denied</th>
                    <th className="px-3 py-2 text-right font-semibold text-slate-600">Paid</th>
                    <th className="px-3 py-2 text-left font-semibold text-slate-600">Precision</th>
                    <th className="px-3 py-2 text-right font-semibold text-slate-600">AED Denied</th>
                    <th className="px-3 py-2 text-left font-semibold text-slate-600">Suggest</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {ruleStats.map(r => (
                    <tr key={r.rule_id} className={`hover:bg-slate-50 ${r.suggested_severity === "BLOCK" ? "bg-red-50/30" : ""}`}>
                      <td className="px-3 py-2 font-mono text-xs text-slate-800">{r.rule_id}</td>
                      <td className="px-3 py-2">
                        <span className={`gh-chip ${SEV_STYLE[r.current_severity] || "bg-slate-100 text-slate-600"}`}>
                          {r.current_severity}
                        </span>
                      </td>
                      <td className="px-3 py-2 text-right text-slate-700">{r.total_triggers}</td>
                      <td className="px-3 py-2 text-right text-slate-600">{r.with_ra}</td>
                      <td className="px-3 py-2 text-right font-medium text-red-700">{r.denied}</td>
                      <td className="px-3 py-2 text-right text-emerald-700">{r.paid}</td>
                      <td className="px-3 py-2"><PrecisionBar pct={r.precision_pct} /></td>
                      <td className="px-3 py-2 text-right text-slate-700">{fmt(r.total_denial_amount)}</td>
                      <td className="px-3 py-2">
                        {r.suggested_severity ? (
                          <span className={`gh-chip font-bold ${SEV_STYLE[r.suggested_severity]}`}>
                            → {r.suggested_severity}
                          </span>
                        ) : <span className="text-slate-300 text-xs">—</span>}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          <div className="border-t border-slate-100 bg-slate-50 px-4 py-2">
            <p className="text-xs text-slate-500">
              <strong>Precision</strong> = denied ÷ (denied + paid) among claims with a known RA outcome.
              Rules with precision ≥ 70% and ≥ 3 denials are suggested for upgrade to BLOCK.
            </p>
          </div>
        </div>
      )}

      {/* ── Tab: Recall Queue ──────────────────────────────────────────────── */}
      {tab === "queue" && (
        <div className="space-y-3">
          {/* Date filter + view toggle + Excel download toolbar */}
          <div className="gh-card p-4 flex flex-wrap items-end gap-3">
            <div>
              <label className="mb-1 block text-xs text-slate-500">RA Date From</label>
              <input type="date" value={queueDateFrom}
                onChange={e => setQueueDateFrom(e.target.value)}
                className="gh-input py-1.5" />
            </div>
            <div>
              <label className="mb-1 block text-xs text-slate-500">RA Date To</label>
              <input type="date" value={queueDateTo}
                onChange={e => setQueueDateTo(e.target.value)}
                className="gh-input py-1.5" />
            </div>
            <button
              onClick={() => loadQueue(facilityId, queueDateFrom, queueDateTo)}
              disabled={queueLoading}
              className="gh-btn gh-btn-sm"
            >
              {queueLoading ? "Loading…" : "Apply"}
            </button>
            {(queueDateFrom || queueDateTo) && (
              <button onClick={() => { setQueueDateFrom(""); setQueueDateTo(""); loadQueue(facilityId, "", ""); }}
                className="gh-btn-secondary gh-btn-sm">
                Clear
              </button>
            )}
            {/* View toggle */}
            <div className="flex overflow-hidden rounded border border-sage-line text-sm">
              {[["claims","By Claim"],["by_file","By RA File"]].map(([v, label]) => (
                <button key={v} onClick={() => setQueueView(v)}
                  className={`px-3 py-1.5 font-medium transition-colors ${
                    queueView === v ? "bg-ghaf-600 text-white" : "bg-white text-slate-600 hover:bg-slate-50"
                  }`}>
                  {label}
                </button>
              ))}
            </div>
            <button
              onClick={downloadRecallExcel}
              disabled={exportingExcel || denialQueue.length === 0}
              className="ml-auto rounded-lg bg-emerald-600 px-4 py-1.5 text-sm font-semibold text-white hover:bg-emerald-700 disabled:opacity-50"
            >
              {exportingExcel ? "Exporting…" : `⬇ Export ${denialQueue.length} claims (.xlsx)`}
            </button>
          </div>

          {/* ── By Claim view ── */}
          {queueView === "claims" && (
            <div className="gh-card overflow-hidden">
              <div className="px-4 py-3 border-b border-amber-100 bg-amber-50">
                <p className="text-xs text-amber-900">
                  Claims <strong>denied by payer AND flagged by the scrubber</strong> — sorted by RA date (newest first).
                  Correct findings, rebuild the file, and resubmit within 21 days of the RA date.
                </p>
              </div>
              {denialQueue.length === 0 && !queueLoading ? (
                <p className="p-6 text-center text-slate-600 text-sm">No recall claims found for the selected period.</p>
              ) : (
                <div className="overflow-x-auto">
                  <table className="min-w-full divide-y divide-slate-200 text-sm">
                    <thead className="bg-slate-50">
                      <tr>
                        <th className="px-3 py-2 text-left font-semibold text-slate-600">Priority</th>
                        <th className="px-3 py-2 text-left font-semibold text-slate-600">Claim ID</th>
                        <th className="px-3 py-2 text-left font-semibold text-slate-600">Encounter</th>
                        <th className="px-3 py-2 text-left font-semibold text-slate-600">Payer</th>
                        <th className="px-3 py-2 text-left font-semibold text-slate-600">RA Date</th>
                        <th className="px-3 py-2 text-left font-semibold text-slate-600">RA File</th>
                        <th className="px-3 py-2 text-right font-semibold text-slate-600">Claimed</th>
                        <th className="px-3 py-2 text-right font-semibold text-slate-600">Denied</th>
                        <th className="px-3 py-2 text-left font-semibold text-slate-600">Scrub Rules</th>
                        <th className="px-3 py-2 text-left font-semibold text-slate-600">Denial Codes</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-slate-100">
                      {queueLoading ? (
                        <tr><td colSpan={10} className="px-3 py-6 text-center text-slate-600">Loading…</td></tr>
                      ) : denialQueue.map(d => (
                        <tr key={`${d.claim_id}-${d.remittance_date}`} className="hover:bg-slate-50">
                          <td className="px-3 py-2">
                            <div className="flex flex-col gap-1">
                              <span className={`gh-chip w-fit ${PRIORITY_STYLE[d.priority] || "bg-slate-100 text-slate-600"}`}>
                                {d.priority}
                              </span>
                              {d.recall_flag && (
                                <span className="gh-chip w-fit bg-denied font-bold text-white">
                                  🚨 RECALL
                                </span>
                              )}
                              {d.recall_type === "PROACTIVE" && (
                                <span className="gh-chip w-fit bg-ink-deep font-semibold text-white">
                                  ⚡ PROACTIVE
                                </span>
                              )}
                            </div>
                          </td>
                          <td className="px-3 py-2 font-mono text-xs text-slate-800">{d.claim_id}</td>
                          <td className="px-3 py-2 text-slate-600 text-xs">
                            {d.encounter_date}<br />
                            <span className="text-slate-600">{d.encounter_type || "—"}</span>
                          </td>
                          <td className="px-3 py-2 text-slate-600 text-xs">{d.payer_id}</td>
                          <td className="px-3 py-2 text-slate-600 text-xs font-medium">{d.remittance_date || "—"}</td>
                          <td className="px-3 py-2 text-slate-500 text-xs max-w-[160px] truncate" title={d.ra_file_name || d.ra_file_id}>
                            {d.ra_file_name || d.ra_file_id || "—"}
                          </td>
                          <td className="px-3 py-2 text-right text-slate-700">{fmt(d.gross_amount)}</td>
                          <td className="px-3 py-2 text-right font-semibold text-red-700">{fmt(d.denial_amount)}</td>
                          <td className="px-3 py-2">
                            <div className="flex flex-wrap gap-1">
                              {(d.scrub_rules || []).map(rule => (
                                <span key={rule} className="rounded bg-slate-100 px-1.5 py-0.5 text-xs font-mono text-slate-700">{rule}</span>
                              ))}
                            </div>
                          </td>
                          <td className="px-3 py-2">
                            <div className="flex flex-col gap-0.5">
                              {(d.denial_codes || []).map(code => (
                                <span key={code} className="text-xs text-amber-700 font-medium">{code}</span>
                              ))}
                              {(d.denial_descriptions || []).slice(0, 1).map(desc => (
                                <span key={desc} className="text-xs text-slate-600 truncate max-w-xs" title={desc}>{desc}</span>
                              ))}
                            </div>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          )}

          {/* ── By RA File view ── */}
          {queueView === "by_file" && (() => {
            // Group denialQueue by ra_file_id, preserving newest-RA-first order
            const fileMap = new Map();
            for (const d of denialQueue) {
              const key = d.ra_file_id || "(no RA file)";
              if (!fileMap.has(key)) {
                fileMap.set(key, {
                  ra_file_id: key,
                  ra_file_name: d.ra_file_name || d.ra_file_id || key,
                  ra_date: d.remittance_date,  // first = newest (already sorted)
                  payer_id: d.payer_id,
                  claims: [],
                  total_claimed: 0,
                  total_denied: 0,
                  denial_codes: new Set(),
                  scrub_rules: new Set(),
                  priorities: new Set(),
                });
              }
              const g = fileMap.get(key);
              g.claims.push(d);
              g.total_claimed += Number(d.gross_amount) || 0;
              g.total_denied  += Number(d.denial_amount) || 0;
              (d.denial_codes || []).forEach(c => g.denial_codes.add(c));
              (d.scrub_rules  || []).forEach(r => g.scrub_rules.add(r));
              g.priorities.add(d.priority);
            }
            const groups = [...fileMap.values()];
            const topPriority = g => ["CRITICAL","HIGH","PROACTIVE","MEDIUM"].find(p => g.priorities.has(p)) || "MEDIUM";

            return (
              <div className="space-y-2">
                <p className="text-xs text-slate-500 px-1">
                  {groups.length} RA files · {denialQueue.length} recall claims · grouped by remittance file, newest first
                </p>
                {queueLoading ? (
                  <div className="gh-card p-6 text-center text-slate-600 text-sm">Loading…</div>
                ) : groups.length === 0 ? (
                  <div className="gh-card p-6 text-center text-slate-600 text-sm">No recall claims found for the selected period.</div>
                ) : groups.map(g => {
                  const isOpen = expandedFiles.has(g.ra_file_id);
                  const prio = topPriority(g);
                  return (
                    <div key={g.ra_file_id} className="gh-card overflow-hidden">
                      {/* File header row — clickable to expand */}
                      <button
                        className="w-full text-left px-4 py-3 flex flex-wrap items-center gap-3 hover:bg-slate-50 transition-colors"
                        onClick={() => setExpandedFiles(prev => {
                          const next = new Set(prev);
                          next.has(g.ra_file_id) ? next.delete(g.ra_file_id) : next.add(g.ra_file_id);
                          return next;
                        })}
                      >
                        <span className="text-slate-400 text-sm w-4">{isOpen ? "▾" : "▸"}</span>
                        <span className={`gh-chip ${PRIORITY_STYLE[prio]}`}>{prio}</span>
                        <span className="font-mono text-sm font-semibold text-slate-800 flex-1 truncate" title={g.ra_file_name || g.ra_file_id}>
                          {g.ra_file_name || g.ra_file_id}
                        </span>
                        <span className="text-xs text-slate-500">{g.ra_date || "—"}</span>
                        <span className="text-xs text-slate-500">{g.payer_id}</span>
                        <span className="gh-chip gh-chip-neutral">
                          {g.claims.length} claim{g.claims.length !== 1 ? "s" : ""}
                        </span>
                        <span className="text-xs text-slate-500">Claimed: <span className="font-semibold text-slate-700">{fmt(g.total_claimed)}</span></span>
                        <span className="text-xs">Denied: <span className="font-bold text-red-700">{fmt(g.total_denied)}</span></span>
                        {/* Denial codes + scrub rules chips */}
                        <div className="flex flex-wrap gap-1">
                          {[...g.denial_codes].slice(0, 3).map(c => (
                            <span key={c} className="gh-chip gh-chip-pending">{c}</span>
                          ))}
                          {g.denial_codes.size > 3 && <span className="text-xs text-slate-600">+{g.denial_codes.size - 3}</span>}
                          {[...g.scrub_rules].slice(0, 2).map(r => (
                            <span key={r} className="rounded bg-slate-100 px-1.5 py-0.5 text-xs font-mono text-slate-600">{r}</span>
                          ))}
                          {g.scrub_rules.size > 2 && <span className="text-xs text-slate-600">+{g.scrub_rules.size - 2}</span>}
                        </div>
                      </button>

                      {/* Expanded claim rows */}
                      {isOpen && (
                        <div className="border-t border-slate-100 overflow-x-auto">
                          <table className="min-w-full divide-y divide-slate-100 text-sm">
                            <thead className="bg-slate-50">
                              <tr>
                                <th className="pl-8 pr-3 py-2 text-left font-semibold text-slate-500 text-xs">Priority</th>
                                <th className="px-3 py-2 text-left font-semibold text-slate-500 text-xs">Claim ID</th>
                                <th className="px-3 py-2 text-left font-semibold text-slate-500 text-xs">Encounter</th>
                                <th className="px-3 py-2 text-left font-semibold text-slate-500 text-xs">Payer</th>
                                <th className="px-3 py-2 text-left font-semibold text-slate-500 text-xs">RA Date</th>
                                <th className="px-3 py-2 text-right font-semibold text-slate-500 text-xs">Claimed</th>
                                <th className="px-3 py-2 text-right font-semibold text-slate-500 text-xs">Denied</th>
                                <th className="px-3 py-2 text-left font-semibold text-slate-500 text-xs">Scrub Rules</th>
                                <th className="px-3 py-2 text-left font-semibold text-slate-500 text-xs">Denial Codes</th>
                              </tr>
                            </thead>
                            <tbody className="divide-y divide-slate-50 bg-white">
                              {g.claims.map(d => (
                                <tr key={`${d.claim_id}-${d.remittance_date}`} className="hover:bg-slate-50/60">
                                  <td className="pl-8 pr-3 py-2">
                                    <span className={`gh-chip ${PRIORITY_STYLE[d.priority]}`}>{d.priority}</span>
                                  </td>
                                  <td className="px-3 py-2 font-mono text-xs text-slate-800">{d.claim_id}</td>
                                  <td className="px-3 py-2 text-slate-600 text-xs">
                                    {d.encounter_date}<br />
                                    <span className="text-slate-600">{d.encounter_type || "—"}</span>
                                  </td>
                                  <td className="px-3 py-2 text-slate-600 text-xs">{d.payer_id}</td>
                                  <td className="px-3 py-2 text-slate-600 text-xs">{d.remittance_date || "—"}</td>
                                  <td className="px-3 py-2 text-right text-slate-700">{fmt(d.gross_amount)}</td>
                                  <td className="px-3 py-2 text-right font-semibold text-red-700">{fmt(d.denial_amount)}</td>
                                  <td className="px-3 py-2">
                                    <div className="flex flex-wrap gap-1">
                                      {(d.scrub_rules || []).map(rule => (
                                        <span key={rule} className="rounded bg-slate-100 px-1.5 py-0.5 text-xs font-mono text-slate-700">{rule}</span>
                                      ))}
                                    </div>
                                  </td>
                                  <td className="px-3 py-2">
                                    <div className="flex flex-col gap-0.5">
                                      {(d.denial_codes || []).map(code => (
                                        <span key={code} className="text-xs text-amber-700 font-medium">{code}</span>
                                      ))}
                                    </div>
                                  </td>
                                </tr>
                              ))}
                              {/* File totals footer */}
                              <tr className="bg-slate-50 font-semibold">
                                <td colSpan={5} className="pl-8 pr-3 py-2 text-xs text-slate-500">
                                  File total — {g.claims.length} claims
                                </td>
                                <td className="px-3 py-2 text-right text-xs text-slate-700">{fmt(g.total_claimed)}</td>
                                <td className="px-3 py-2 text-right text-xs text-red-700">{fmt(g.total_denied)}</td>
                                <td colSpan={2} />
                              </tr>
                            </tbody>
                          </table>
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            );
          })()}
        </div>
      )}

      {/* ── Tab: Top Denial Codes ──────────────────────────────────────────── */}
      {tab === "codes" && (
        <div className="gh-card overflow-hidden">
          {denialCodes.length === 0 && !loading ? (
            <p className="p-6 text-center text-slate-600 text-sm">
              No denial codes found in RA data — fetch RemittanceAdvice files first.
            </p>
          ) : (
            <div className="overflow-x-auto">
              <table className="min-w-full divide-y divide-slate-200 text-sm">
                <thead className="bg-slate-50">
                  <tr>
                    <th className="px-3 py-2 text-left font-semibold text-slate-600">Denial Code</th>
                    <th className="px-3 py-2 text-left font-semibold text-slate-600">Description</th>
                    <th className="px-3 py-2 text-left font-semibold text-slate-600">Type</th>
                    <th className="px-3 py-2 text-right font-semibold text-slate-600">Occurrences</th>
                    <th className="px-3 py-2 text-right font-semibold text-slate-600">Claims</th>
                    <th className="px-3 py-2 text-right font-semibold text-slate-600">AED Denied</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {denialCodes.map((d, i) => (
                    <tr key={d.denial_code || i} className="hover:bg-slate-50">
                      <td className="px-3 py-2 font-mono text-xs font-semibold text-amber-700">
                        {d.denial_code || "—"}
                      </td>
                      <td className="px-3 py-2 text-slate-700 max-w-sm" title={d.denial_description}>
                        {d.denial_description || "—"}
                      </td>
                      <td className="px-3 py-2 text-slate-500 text-xs">{d.denial_type || "—"}</td>
                      <td className="px-3 py-2 text-right font-medium text-slate-800">{d.occurrences}</td>
                      <td className="px-3 py-2 text-right text-slate-700">{d.claims_affected}</td>
                      <td className="px-3 py-2 text-right font-semibold text-red-700">
                        {fmt(d.total_denied_amount)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          <div className="border-t border-slate-100 bg-slate-50 px-4 py-2">
            <p className="text-xs text-slate-500">
              Denial codes from RemittanceAdvice activity details. Use these to identify
              payer-specific patterns and cross-train your billing team.
            </p>
          </div>
        </div>
      )}
    </div>
  );
}
