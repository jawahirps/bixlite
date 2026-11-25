import { useEffect, useRef, useState } from "react";
import apiClient from "../api/client";
import { useAuth } from "../context/AuthContext";
import { readFacility, saveFacility } from "../utils/facility";
import useFacilityDefault from "../hooks/useFacilityDefault";

const statusColors = {
  pending: "gh-chip-pending",
  parsed: "gh-chip-paid",
  failed: "gh-chip-denied",
  quarantined: "gh-chip-denied",
  running: "gh-chip-brand",
  success: "gh-chip-paid",
  partial: "gh-chip-pending",
};

function StatusBadge({ status }) {
  return (
    <span className={`gh-chip ${statusColors[status] || "gh-chip-neutral"}`}>
      {status}
    </span>
  );
}

// Layer 2: scrub quality badge fetched lazily per ClaimSubmission row
const scrubCache = {};  // raw_xml_id → summary

function ScrubBadge({ rawId }) {
  const [summary, setSummary] = useState(scrubCache[rawId] ?? null);
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    if (scrubCache[rawId] !== undefined) { setSummary(scrubCache[rawId]); return; }
    apiClient.get(`/dha-sync/raw-xml/${rawId}/xml-scrub-summary`)
      .then(r => {
        scrubCache[rawId] = r.data;
        if (mounted.current) setSummary(r.data);
      })
      .catch(() => { scrubCache[rawId] = null; });
    return () => { mounted.current = false; };
  }, [rawId]);

  if (!summary) return <span className="text-slate-300 text-xs">—</span>;
  if (!summary.scrubbed) return <span className="text-slate-600 text-xs">not scrubbed</span>;

  const { status, blocks, warnings, risk_level } = summary;
  if (status === "BLOCK") {
    return (
      <span title={`${blocks} block(s) · risk: ${risk_level}`}
            className="gh-chip gh-chip-denied">
        ✗ {blocks} block{blocks !== 1 ? "s" : ""}
      </span>
    );
  }
  if (status === "WARN") {
    return (
      <span title={`${warnings} warning(s) · risk: ${risk_level}`}
            className="gh-chip gh-chip-pending">
        ⚠ {warnings} warn{warnings !== 1 ? "s" : ""}
      </span>
    );
  }
  return (
    <span className="gh-chip gh-chip-paid">
      ✓ Pass
    </span>
  );
}

export default function DhaSync() {
  const { user } = useAuth();

  const [rawXml, setRawXml] = useState([]);
  const [fetchLogs, setFetchLogs] = useState([]);
  const [scrubFilter, setScrubFilter] = useState("all");
  const [expandedRaw, setExpandedRaw] = useState(null);
  const [findings, setFindings] = useState({});
  const [batchScrubbing, setBatchScrubbing] = useState(false);
  const [batchResult, setBatchResult] = useState(null);
  const [loadingTables, setLoadingTables] = useState(false);
  const [error, setError] = useState(null);
  // scrubSummaryVersion increments when scrubCache is updated, triggering re-render of filters
  const [scrubSummaryVersion, setScrubSummaryVersion] = useState(0);

  // Date filter
  const [dateFrom, setDateFrom] = useState("");
  const [dateTo, setDateTo] = useState("");

  // Row selection for bulk export
  const [selectedRows, setSelectedRows] = useState(new Set());
  const [downloadingId, setDownloadingId] = useState(null);
  const [bulkDownloading, setBulkDownloading] = useState(false);

  // Facility selector
  const [facilities, setFacilities] = useState([]);
  const [selectedFacilityId, setSelectedFacilityId] = useState(() => {
    const saved = readFacility();
    return saved ? Number(saved) : (user?.facility_id || 13);
  });
  const isSuperAdmin = useFacilityDefault(facilities, selectedFacilityId, setSelectedFacilityId);
  const [facilitiesError, setFacilitiesError] = useState(null);

  const activeFacilityId = selectedFacilityId || user?.facility_id;

  // Pre-load all scrub summaries for a facility so filters work immediately
  const loadScrubSummaries = async (facilityId) => {
    const fid = facilityId ?? activeFacilityId;
    if (!fid) return;
    try {
      const r = await apiClient.get("/dha-sync/scrub-summaries", { params: { facility_id: fid } });
      // Merge into module-level cache
      Object.entries(r.data).forEach(([id, summary]) => {
        scrubCache[Number(id)] = summary;
      });
      setScrubSummaryVersion(v => v + 1);
    } catch {
      // Non-fatal — badges will still load individually
    }
  };

  const loadTables = async (facilityId) => {
    const fid = facilityId ?? activeFacilityId;
    setLoadingTables(true);
    setError(null);
    try {
      const params = fid ? { facility_id: fid } : {};
      const [rawRes, logRes] = await Promise.all([
        apiClient.get("/dha-sync/raw-xml", { params: { ...params, limit: 500 } }),
        apiClient.get("/dha-sync/fetch-logs", { params }),
        loadScrubSummaries(fid),  // fire in parallel
      ]);
      setRawXml(rawRes.data);
      setFetchLogs(logRes.data);
      setSelectedRows(new Set());
    } catch (err) {
      setError(err.response?.data?.detail || "Failed to load DHA sync history");
    } finally {
      setLoadingTables(false);
    }
  };

  const handleFacilityChange = (id) => {
    setSelectedFacilityId(id);
    saveFacility(id);
  };

  useEffect(() => {
    apiClient.get("/facilities")
      .then(r => setFacilities(Array.isArray(r.data) ? r.data : []))
      .catch(err => { setFacilitiesError(err.response ? `HTTP ${err.response.status}` : err.message); });
    loadTables(selectedFacilityId);
  }, []);

  async function runBatchScrub() {
    if (!activeFacilityId) return;
    setBatchScrubbing(true); setBatchResult(null);
    try {
      const r = await apiClient.post(`/dha-sync/batch-scrub?facility_id=${activeFacilityId}&limit=500`);
      const d = r.data;
      setBatchResult(`✅ Scrubbed ${d.scrubbed} file(s).${d.failed > 0 ? ` ⚠ ${d.failed} failed.` : ""}${d.total === 0 ? " All files already scrubbed." : ""}`);
      Object.keys(scrubCache).forEach(k => delete scrubCache[k]);
      await loadTables();
    } catch (e) {
      setBatchResult("❌ Batch scrub failed: " + (e.response?.data?.detail || e.message));
    } finally { setBatchScrubbing(false); }
  }

  async function toggleFindings(rawId) {
    if (expandedRaw === rawId) { setExpandedRaw(null); return; }
    setExpandedRaw(rawId);
    if (findings[rawId]) return;
    try {
      const r = await apiClient.get(`/dha-sync/raw-xml/${rawId}/xml-findings`);
      setFindings(prev => ({ ...prev, [rawId]: r.data }));
    } catch {
      setFindings(prev => ({ ...prev, [rawId]: [] }));
    }
  }

  // ── Excel export helpers ────────────────────────────────────────────────────
  function triggerBlobDownload(blob, filename) {
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = filename; a.click();
    setTimeout(() => URL.revokeObjectURL(url), 5000);
  }

  async function downloadSingleExcel(rawId, filename) {
    setDownloadingId(rawId);
    try {
      const r = await apiClient.get(`/dha-sync/raw-xml/${rawId}/export-excel`, { responseType: "blob" });
      const safe = (filename || `raw_${rawId}`).replace(".xml", "");
      triggerBlobDownload(r.data, `scrub_${safe}.xlsx`);
    } catch (e) {
      setError("Excel export failed: " + (e.response?.data?.detail || e.message));
    } finally { setDownloadingId(null); }
  }

  async function downloadBulkExcel() {
    if (selectedRows.size === 0) return;
    setBulkDownloading(true);
    try {
      const r = await apiClient.post("/dha-sync/export-excel-bulk",
        { raw_xml_ids: [...selectedRows] },
        { responseType: "blob" },
      );
      triggerBlobDownload(r.data, "scrub_findings_export.xlsx");
    } catch (e) {
      setError("Bulk export failed: " + (e.response?.data?.detail || e.message));
    } finally { setBulkDownloading(false); }
  }

  // ── Filtering ───────────────────────────────────────────────────────────────
  const claimRows = rawXml.filter(r => r.xml_type === "ClaimSubmission" || r.xml_type === "RiayatiClaimSubmission");

  // Apply date filter then scrub filter
  // scrubSummaryVersion in the dep chain ensures re-evaluation when cache fills
  const filteredRaw = rawXml.filter(r => {
    // Date range filter on submission_date
    if (dateFrom && r.submission_date && r.submission_date < dateFrom) return false;
    if (dateTo   && r.submission_date && r.submission_date > dateTo)   return false;
    // Scrub status filter
    if (scrubFilter === "scrubbed")     return scrubCache[r.id]?.scrubbed;
    if (scrubFilter === "not_scrubbed") return (r.xml_type === "ClaimSubmission" || r.xml_type === "RiayatiClaimSubmission") && !scrubCache[r.id]?.scrubbed;
    if (scrubFilter === "block")  return scrubCache[r.id]?.status === "BLOCK";
    if (scrubFilter === "warn")   return scrubCache[r.id]?.status === "WARN";
    if (scrubFilter === "pass")   return scrubCache[r.id]?.status === "PASS";
    return true;
  });
  // eslint-disable-next-line react-hooks/exhaustive-deps
  void scrubSummaryVersion; // consumed so React re-renders when cache updates

  const visibleClaimIds = filteredRaw
    .filter(r => r.xml_type === "ClaimSubmission" || r.xml_type === "RiayatiClaimSubmission")
    .map(r => r.id);
  const allVisibleSelected = visibleClaimIds.length > 0 && visibleClaimIds.every(id => selectedRows.has(id));

  function toggleSelectAll() {
    if (allVisibleSelected) {
      setSelectedRows(prev => {
        const next = new Set(prev);
        visibleClaimIds.forEach(id => next.delete(id));
        return next;
      });
    } else {
      setSelectedRows(prev => new Set([...prev, ...visibleClaimIds]));
    }
  }

  function toggleRow(id) {
    setSelectedRows(prev => {
      const next = new Set(prev);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });
  }

  // Scrub stat counts
  const scrubbedRows = claimRows.filter(r => scrubCache[r.id]?.scrubbed);
  const blockCount   = scrubbedRows.filter(r => scrubCache[r.id]?.status === "BLOCK").length;
  const warnCount    = scrubbedRows.filter(r => scrubCache[r.id]?.status === "WARN").length;
  const passCount    = scrubbedRows.filter(r => scrubCache[r.id]?.status === "PASS").length;

  const SEVERITY_STYLE = {
    BLOCK: "gh-chip-denied",
    WARN:  "gh-chip-pending",
    PASS:  "gh-chip-paid",
  };

  return (
    <div className="space-y-6">
      <div>
        <p className="gh-eyebrow">Submission quality</p>
        <h1 className="gh-h1">XML Scrub Monitor</h1>
        <p className="text-sm text-slate-500">
          Quality check on fetched ClaimSubmission files — click any row to see per-claim findings.
        </p>
      </div>

      {/* Facility selector + actions */}
      <div className="gh-card p-4">
        <label className="mb-1 block text-xs font-medium text-slate-600">Facility</label>
        {facilitiesError && (
          <p className="mb-2 text-xs text-red-600">Could not load facilities: {facilitiesError}</p>
        )}
        <div className="flex flex-wrap items-center gap-2">
          {facilities.length > 0 ? (
            <select
              value={selectedFacilityId}
              onChange={e => handleFacilityChange(Number(e.target.value))}
              className="gh-input w-full max-w-sm py-1.5"
            >
              {facilities.map(f => (
                <option key={f.id} value={f.id}>
                  {f.facility_name} ({f.dhpo_provider_id || "no provider ID"})
                </option>
              ))}
            </select>
          ) : (
            <input
              type="number"
              placeholder="Facility ID"
              value={selectedFacilityId}
              onChange={e => handleFacilityChange(Number(e.target.value))}
              className="gh-input w-full max-w-xs py-1.5"
            />
          )}
          <button
            onClick={() => loadTables(selectedFacilityId)}
            disabled={loadingTables}
            className="gh-btn gh-btn-sm"
          >
            {loadingTables ? "Loading…" : "Refresh"}
          </button>
          <button
            onClick={runBatchScrub}
            disabled={batchScrubbing}
            title="Scrub all ClaimSubmission files that haven't been checked yet"
            className="gh-btn-secondary gh-btn-sm disabled:opacity-60"
          >
            {batchScrubbing ? "Scrubbing…" : "Re-scrub All"}
          </button>
          {selectedRows.size > 0 && (
            <button
              onClick={downloadBulkExcel}
              disabled={bulkDownloading}
              className="rounded-lg bg-emerald-600 px-4 py-1.5 text-sm font-semibold text-white hover:bg-emerald-700 disabled:opacity-60 flex items-center gap-1.5"
            >
              {bulkDownloading ? "Exporting…" : `⬇ Export ${selectedRows.size} file${selectedRows.size !== 1 ? "s" : ""} (.xlsx)`}
            </button>
          )}
        </div>
        {batchResult && <p className="mt-2 text-xs text-slate-600">{batchResult}</p>}
      </div>

      {/* Scrub summary stats */}
      {claimRows.length > 0 && (
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          {[
            { label: "Total Files", value: claimRows.length, color: "bg-slate-50 text-slate-800" },
            { label: "Blocks", value: blockCount, color: blockCount > 0 ? "bg-red-50 text-red-700" : "bg-slate-50 text-slate-400" },
            { label: "Warnings", value: warnCount, color: warnCount > 0 ? "bg-amber-50 text-amber-700" : "bg-slate-50 text-slate-400" },
            { label: "Pass", value: passCount, color: passCount > 0 ? "bg-emerald-50 text-emerald-700" : "bg-slate-50 text-slate-400" },
          ].map(s => (
            <div key={s.label} className={`rounded-lg border border-hairline p-4 ${s.color}`}>
              <p className="text-2xl font-bold">{s.value}</p>
              <p className="text-xs font-medium mt-0.5 opacity-70">{s.label}</p>
            </div>
          ))}
        </div>
      )}

      {error && <div className="rounded-lg bg-red-50 p-3 text-sm text-red-700">{error}</div>}

      {/* Date filter */}
      <div className="gh-card p-4">
        <p className="gh-label mb-2">Filter by Submission Date</p>
        <div className="flex flex-wrap items-end gap-3">
          <div>
            <label className="mb-1 block text-xs text-slate-500">From</label>
            <input
              type="date"
              value={dateFrom}
              onChange={e => setDateFrom(e.target.value)}
              className="gh-input py-1.5"
            />
          </div>
          <div>
            <label className="mb-1 block text-xs text-slate-500">To</label>
            <input
              type="date"
              value={dateTo}
              onChange={e => setDateTo(e.target.value)}
              className="gh-input py-1.5"
            />
          </div>
          {(dateFrom || dateTo) && (
            <button
              onClick={() => { setDateFrom(""); setDateTo(""); }}
              className="gh-btn-secondary gh-btn-sm"
            >
              Clear dates
            </button>
          )}
          <p className="text-xs text-slate-600 self-end pb-1.5">
            Showing {filteredRaw.length} of {rawXml.length} files
            {scrubFilter !== "all" && ` (${scrubFilter.replace("_", " ")} filter active)`}
          </p>
        </div>
      </div>

      {/* Filter chips */}
      <div className="flex flex-wrap gap-2">
        {[
          { key: "all", label: "All Files" },
          { key: "block", label: "✗ Blocks" },
          { key: "warn", label: "⚠ Warnings" },
          { key: "pass", label: "✓ Pass" },
          { key: "not_scrubbed", label: "Not scrubbed" },
        ].map(f => (
          <button key={f.key} onClick={() => setScrubFilter(f.key)}
            className={`rounded-full px-3 py-1 text-xs font-medium transition-colors ${
              scrubFilter === f.key ? "bg-ghaf-600 text-white" : "bg-slate-100 text-slate-600 hover:bg-slate-200"
            }`}>
            {f.label}
          </button>
        ))}
      </div>

      {/* Main table */}
      <div className="gh-card overflow-hidden">
        <div className="overflow-x-auto">
          <table className="min-w-full divide-y divide-slate-200 text-sm">
            <thead className="bg-slate-50">
              <tr>
                {/* Select-all checkbox */}
                <th className="w-8 px-2 py-2">
                  <input
                    type="checkbox"
                    checked={allVisibleSelected}
                    onChange={toggleSelectAll}
                    title="Select all visible ClaimSubmission rows"
                    className="rounded border-slate-300 text-ghaf-600 focus:ring-ghaf-500"
                  />
                </th>
                <th className="w-6" />
                <th className="px-3 py-2 text-left font-semibold text-slate-600">File</th>
                <th className="px-3 py-2 text-left font-semibold text-slate-600">Type</th>
                <th className="px-3 py-2 text-left font-semibold text-slate-600">Submission Date</th>
                <th className="px-3 py-2 text-left font-semibold text-slate-600">Fetched</th>
                <th className="px-3 py-2 text-left font-semibold text-slate-600">Status</th>
                <th className="px-3 py-2 text-right font-semibold text-slate-600">Records</th>
                <th className="px-3 py-2 text-left font-semibold text-slate-600">Scrub</th>
                <th className="px-3 py-2 text-left font-semibold text-slate-600">Export</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {filteredRaw.length === 0 && (
                <tr>
                  <td colSpan={10} className="px-3 py-6 text-center text-slate-600">
                    {loadingTables ? "Loading…" : "No files match the current filter."}
                  </td>
                </tr>
              )}
              {filteredRaw.map(r => {
                const isExpanded = expandedRaw === r.id;
                const isClaimSub = r.xml_type === "ClaimSubmission" || r.xml_type === "RiayatiClaimSubmission";
                const rowFindings = findings[r.id] || [];
                const isSelected = selectedRows.has(r.id);
                return (
                  <>
                    <tr key={r.id}
                      className={`transition-colors ${isClaimSub ? "hover:bg-ghaf-50" : "hover:bg-slate-50"} ${isExpanded ? "bg-ghaf-50" : ""} ${isSelected ? "bg-ghaf-50/60" : ""}`}
                    >
                      {/* Checkbox */}
                      <td className="px-2 py-2 text-center" onClick={e => e.stopPropagation()}>
                        {isClaimSub ? (
                          <input
                            type="checkbox"
                            checked={isSelected}
                            onChange={() => toggleRow(r.id)}
                            className="rounded border-slate-300 text-ghaf-600 focus:ring-ghaf-500"
                          />
                        ) : null}
                      </td>
                      {/* Expand toggle */}
                      <td className="pl-1 text-slate-600 text-xs cursor-pointer"
                          onClick={() => isClaimSub && toggleFindings(r.id)}>
                        {isClaimSub ? (isExpanded ? "▾" : "▸") : ""}
                      </td>
                      <td className="px-3 py-2 text-slate-800 max-w-xs truncate cursor-pointer"
                          title={r.source_filename}
                          onClick={() => isClaimSub && toggleFindings(r.id)}>
                        {r.source_filename || "—"}
                      </td>
                      <td className="px-3 py-2 text-slate-500 text-xs">{r.xml_type}</td>
                      <td className="px-3 py-2 text-slate-600">{r.submission_date || "—"}</td>
                      <td className="px-3 py-2 text-slate-500 text-xs">{new Date(r.fetched_at).toLocaleString()}</td>
                      <td className="px-3 py-2"><StatusBadge status={r.parse_status} /></td>
                      <td className="px-3 py-2 text-right text-slate-600">{r.records_parsed ?? "—"}</td>
                      <td className="px-3 py-2">
                        {isClaimSub ? <ScrubBadge rawId={r.id} /> : <span className="text-slate-300 text-xs">—</span>}
                      </td>
                      {/* Per-row Excel download */}
                      <td className="px-3 py-2">
                        {isClaimSub ? (
                          <button
                            onClick={e => { e.stopPropagation(); downloadSingleExcel(r.id, r.source_filename); }}
                            disabled={downloadingId === r.id}
                            title="Download findings as Excel"
                            className="rounded px-2 py-0.5 text-xs font-medium text-emerald-700 border border-emerald-300 hover:bg-emerald-50 disabled:opacity-50"
                          >
                            {downloadingId === r.id ? "…" : "⬇ xlsx"}
                          </button>
                        ) : <span className="text-slate-300 text-xs">—</span>}
                      </td>
                    </tr>
                    {isExpanded && (
                      <tr key={`${r.id}-findings`} className="bg-slate-50">
                        <td colSpan={10} className="px-6 py-3">
                          {rowFindings.length === 0 ? (
                            <p className="text-xs text-slate-600 italic">Loading findings…</p>
                          ) : rowFindings.length === 0 ? (
                            <p className="text-xs text-slate-600 italic">No findings (file passed scrub).</p>
                          ) : (
                            <table className="w-full text-xs">
                              <thead>
                                <tr className="text-slate-500 border-b border-slate-200">
                                  <th className="pb-1 text-left font-semibold">Severity</th>
                                  <th className="pb-1 text-left font-semibold">Rule</th>
                                  <th className="pb-1 text-left font-semibold">Claim ID</th>
                                  <th className="pb-1 text-left font-semibold">Enc. Date</th>
                                  <th className="pb-1 text-left font-semibold">Activity</th>
                                  <th className="pb-1 text-left font-semibold w-1/2">Message</th>
                                </tr>
                              </thead>
                              <tbody className="divide-y divide-slate-100">
                                {rowFindings.map(f => (
                                  <tr key={f.id}>
                                    <td className="py-1 pr-3">
                                      <span className={`rounded-full px-2 py-0.5 font-medium ${SEVERITY_STYLE[f.severity] || "bg-slate-100 text-slate-600"}`}>
                                        {f.severity}
                                      </span>
                                    </td>
                                    <td className="py-1 pr-3 font-mono text-slate-700">{f.rule_id}</td>
                                    <td className="py-1 pr-3 text-slate-600">{f.claim_id || "—"}</td>
                                    <td className="py-1 pr-3 text-slate-500">{f.encounter_date || "—"}</td>
                                    <td className="py-1 pr-3 text-slate-600">{f.activity_id || "—"}</td>
                                    <td className="py-1 text-slate-700">{f.message}</td>
                                  </tr>
                                ))}
                              </tbody>
                            </table>
                          )}
                        </td>
                      </tr>
                    )}
                  </>
                );
              })}
            </tbody>
          </table>
        </div>
        {/* Bulk export bar — shows when rows are selected */}
        {selectedRows.size > 0 && (
          <div className="flex items-center justify-between border-t border-slate-200 bg-ghaf-50 px-4 py-2">
            <span className="text-xs text-slate-600">
              {selectedRows.size} file{selectedRows.size !== 1 ? "s" : ""} selected
            </span>
            <div className="flex gap-2">
              <button
                onClick={() => setSelectedRows(new Set())}
                className="rounded px-3 py-1 text-xs text-slate-500 hover:text-slate-700"
              >
                Clear
              </button>
              <button
                onClick={downloadBulkExcel}
                disabled={bulkDownloading}
                className="rounded-lg bg-emerald-600 px-4 py-1.5 text-xs font-semibold text-white hover:bg-emerald-700 disabled:opacity-60"
              >
                {bulkDownloading ? "Exporting…" : `⬇ Download ${selectedRows.size} file${selectedRows.size !== 1 ? "s" : ""} as Excel`}
              </button>
            </div>
          </div>
        )}
      </div>

      {/* Fetch Coverage */}
      {fetchLogs.length > 0 && (
        <div className="gh-section">
          <h2 className="mb-1 font-semibold text-slate-900">Fetch Coverage</h2>
          <p className="mb-3 text-xs text-slate-500">
            Each row = one fetch run. Green = success, amber = partial, red = failed.
          </p>
          <div className="overflow-x-auto">
            <table className="min-w-full divide-y divide-slate-200 text-sm">
              <thead className="bg-slate-50">
                <tr>
                  <th className="px-3 py-2 text-left font-semibold text-slate-600">#</th>
                  <th className="px-3 py-2 text-left font-semibold text-slate-600">Date Range Fetched</th>
                  <th className="px-3 py-2 text-left font-semibold text-slate-600">Result</th>
                  <th className="px-3 py-2 text-right font-semibold text-slate-600">Files</th>
                  <th className="px-3 py-2 text-right font-semibold text-slate-600">Records</th>
                  <th className="px-3 py-2 text-right font-semibold text-slate-600">Failed</th>
                  <th className="px-3 py-2 text-left font-semibold text-slate-600">Completed</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {fetchLogs.map(f => {
                  const rowBg =
                    f.status === "success" ? "bg-emerald-50" :
                    f.status === "partial" ? "bg-amber-50" :
                    f.status === "failed"  ? "bg-red-50" : "";
                  const icon =
                    f.status === "success" ? "✅" : f.status === "partial" ? "⚠️" :
                    f.status === "failed"  ? "❌" : f.status === "running" ? "⏳" : "—";
                  const duration = f.completed_at
                    ? Math.round((new Date(f.completed_at) - new Date(f.started_at)) / 1000)
                    : null;
                  return (
                    <tr key={f.id} className={`${rowBg} hover:brightness-95`}>
                      <td className="px-3 py-2 text-slate-500 text-xs">#{f.id}</td>
                      <td className="px-3 py-2 font-mono text-slate-800">{f.date_from} → {f.date_to}</td>
                      <td className="px-3 py-2">
                        <span className="flex items-center gap-1">{icon} <StatusBadge status={f.status} /></span>
                      </td>
                      <td className="px-3 py-2 text-right text-slate-700">{f.records_fetched ?? "—"}</td>
                      <td className="px-3 py-2 text-right text-slate-700">{f.records_parsed ?? "—"}</td>
                      <td className="px-3 py-2 text-right text-slate-700">
                        {f.records_failed > 0
                          ? <span className="text-red-600 font-medium">{f.records_failed}</span>
                          : <span className="text-slate-600">0</span>}
                      </td>
                      <td className="px-3 py-2 text-slate-500 text-xs">
                        {f.completed_at ? `${new Date(f.completed_at).toLocaleString()} (${duration}s)` : "—"}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* Sync Run History */}
      <div className="gh-section">
        <h2 className="mb-3 font-semibold text-slate-900">Sync Run History</h2>
        <div className="overflow-x-auto">
          <table className="min-w-full divide-y divide-slate-200 text-sm">
            <thead className="bg-slate-50">
              <tr>
                <th className="px-3 py-2 text-left font-semibold text-slate-600">#</th>
                <th className="px-3 py-2 text-left font-semibold text-slate-600">Date From</th>
                <th className="px-3 py-2 text-left font-semibold text-slate-600">Date To</th>
                <th className="px-3 py-2 text-left font-semibold text-slate-600">Started</th>
                <th className="px-3 py-2 text-left font-semibold text-slate-600">Status</th>
                <th className="px-3 py-2 text-left font-semibold text-slate-600">Files</th>
                <th className="px-3 py-2 text-left font-semibold text-slate-600">Records</th>
                <th className="px-3 py-2 text-left font-semibold text-slate-600">Failed</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {fetchLogs.length === 0 && (
                <tr>
                  <td colSpan={8} className="px-3 py-4 text-center text-slate-600">No sync runs yet for this facility. Fetches started from Data sync &amp; jobs (or the nightly fetch) appear here.</td>
                </tr>
              )}
              {fetchLogs.map(f => (
                <tr key={f.id} className="hover:bg-slate-50">
                  <td className="px-3 py-2 text-slate-600 text-xs">#{f.id}</td>
                  <td className="px-3 py-2 font-mono text-slate-700">{f.date_from}</td>
                  <td className="px-3 py-2 font-mono text-slate-700">{f.date_to}</td>
                  <td className="px-3 py-2 text-slate-600">{new Date(f.started_at).toLocaleString()}</td>
                  <td className="px-3 py-2"><StatusBadge status={f.status} /></td>
                  <td className="px-3 py-2 text-slate-600">{f.records_fetched}</td>
                  <td className="px-3 py-2 text-slate-600">{f.records_parsed}</td>
                  <td className="px-3 py-2 text-slate-600">{f.records_failed}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
