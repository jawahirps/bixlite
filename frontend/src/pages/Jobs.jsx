import { useEffect, useRef, useState } from "react";
import apiClient from "../api/client";
import { localISODate } from "../utils/format";
import { useAuth } from "../context/AuthContext";
import { readFacility, saveFacility } from "../utils/facility";

const statusColors = {
  success: "gh-chip-paid",
  partial:  "gh-chip-pending",
  failed:   "gh-chip-denied",
  running:  "gh-chip-brand",
  pending:  "gh-chip-neutral",
};

function StatusBadge({ status }) {
  return (
    <span className={`gh-chip ${statusColors[status] || "gh-chip-neutral"}`}>
      {status}
    </span>
  );
}

function JobCard({ title, description, buttonLabel, onRun, loading, result, error, children }) {
  return (
    <div className="gh-section">
      <h3 className="mb-1 text-base font-semibold text-slate-800">{title}</h3>
      <p className="mb-4 text-sm text-slate-500">{description}</p>
      {children}
      <button
        onClick={onRun}
        disabled={loading}
        className="gh-btn disabled:cursor-not-allowed"
      >
        {loading ? "Running…" : buttonLabel}
      </button>
      {result && (
        <div className="mt-3 rounded-lg bg-emerald-50 p-3 text-sm text-emerald-800">
          ✅ {result}
        </div>
      )}
      {error && (
        <div className="mt-3 rounded-lg bg-red-50 p-3 text-sm text-red-800">
          ❌ {error}
        </div>
      )}
    </div>
  );
}

export default function Jobs() {
  const { user } = useAuth();

  const [facilities, setFacilities]   = useState([]);
  const [facMap, setFacMap]           = useState({});   // { id: name }
  const [selectedFac, setSelectedFac] = useState(() => {
    const s = readFacility();
    return s ? Number(s) : (user?.facility_id || "");
  });

  // Shared reparse date filter state
  const [reparseFrom,      setReparseFrom]      = useState("");
  const [reparseTo,        setReparseTo]        = useState("");
  const [reparseDateField, setReparseDateField] = useState("treatment_date");

  // Per-job state
  const [raLoading,   setRaLoading]   = useState(false);
  const [raResult,    setRaResult]    = useState(null);
  const [raError,     setRaError]     = useState(null);

  const [allLoading,  setAllLoading]  = useState(false);
  const [allResult,   setAllResult]   = useState(null);
  const [allError,    setAllError]    = useState(null);

  const [fetchLoading,    setFetchLoading]    = useState(false);
  const [fetchAllLoading, setFetchAllLoading] = useState(false);
  const [fetchResult,     setFetchResult]     = useState(null);
  const [fetchError,      setFetchError]      = useState(null);
  const [dateFrom,        setDateFrom]        = useState("");
  const [dateTo,          setDateTo]          = useState(() => localISODate());

  // Riayati (MOH / TMB API) fetch state
  const [riayatiFrom,     setRiayatiFrom]     = useState("");
  const [riayatiTo,       setRiayatiTo]       = useState(() => localISODate());
  const [riayatiLoading,  setRiayatiLoading]  = useState(false);
  const [riayatiResult,   setRiayatiResult]   = useState(null);
  const [riayatiError,    setRiayatiError]    = useState(null);

  const [logs,         setLogs]         = useState([]);
  const [logsLoading,  setLogsLoading]  = useState(false);
  const [expandedRow,  setExpandedRow]  = useState(null);   // log id with expanded error

  // Filters for log table
  const [filterType,   setFilterType]   = useState("");
  const [filterStatus, setFilterStatus] = useState("");

  // Load facilities
  useEffect(() => {
    if (user?.role === "admin") {
      apiClient.get("/facilities").then(r => {
        setFacilities(r.data);
        const map = {};
        r.data.forEach(f => { map[f.id] = f.facility_name; });
        setFacMap(map);
      }).catch(() => {});
    }
  }, [user]);

  useEffect(() => {
    saveFacility(selectedFac);
    // Clear stale results when switching facilities
    setRaResult(null); setRaError(null);
    setAllResult(null); setAllError(null);
    setFetchResult(null); setFetchError(null);
    loadLogs();
  }, [selectedFac]);

  // Auto-poll every 5 s while any job is still running
  useEffect(() => {
    const hasRunning = logs.some(l => l.status === "running");
    if (!hasRunning) return;
    const timer = setInterval(loadLogs, 5000);
    return () => clearInterval(timer);
  }, [logs]);

  const facParam = selectedFac ? `?facility_id=${selectedFac}` : "";

  // Tracks the latest logs request so a slow response for a previously selected
  // facility cannot overwrite the current one.
  const logsReqId = useRef(0);

  async function loadLogs() {
    const reqId = ++logsReqId.current;
    setLogsLoading(true);
    try {
      const r = await apiClient.get(`/dha-sync/fetch-logs${facParam ? facParam + "&" : "?"}limit=50`);
      if (reqId !== logsReqId.current) return;
      setLogs(r.data);
    } catch { /* silent */ }
    finally { setLogsLoading(false); }
  }

  function buildReparseParams(xml_type) {
    const params = new URLSearchParams({ xml_type });
    if (selectedFac)       params.set("facility_id",  selectedFac);
    if (reparseFrom)       params.set("date_from",    reparseFrom);
    if (reparseTo)         params.set("date_to",      reparseTo);
    if (reparseFrom && reparseTo) params.set("date_field", reparseDateField);
    return params;
  }

  function reparseResultMsg(data, label) {
    if (!data.fetch_log_id || data.reset_count === 0)
      return `ℹ️ No ${label} XMLs found for the given date range. Nothing to re-parse.`;
    return `🔄 Job #${data.fetch_log_id} queued — ${data.reset_count} ${label} XML(s) processing in background. Job History will auto-refresh.`;
  }

  async function runReparseRA() {
    setRaLoading(true); setRaResult(null); setRaError(null);
    try {
      const r = await apiClient.post(`/dha-sync/reparse-and-trigger?${buildReparseParams("RemittanceAdvice")}`);
      setRaResult(reparseResultMsg(r.data, "RA"));
      loadLogs();
    } catch (e) {
      setRaError(e.response?.data?.detail || "Re-parse failed");
    } finally { setRaLoading(false); }
  }

  async function runReparseAll() {
    setAllLoading(true); setAllResult(null); setAllError(null);
    try {
      const r1 = await apiClient.post(`/dha-sync/reparse-and-trigger?${buildReparseParams("ClaimSubmission")}`);
      const r2 = await apiClient.post(`/dha-sync/reparse-and-trigger?${buildReparseParams("RemittanceAdvice")}`);
      const msg1 = reparseResultMsg(r1.data, "CS");
      const msg2 = reparseResultMsg(r2.data, "RA");
      setAllResult(`${msg1} | ${msg2}`);
      loadLogs();
    } catch (e) {
      setAllError(e.response?.data?.detail || "Re-parse failed");
    } finally { setAllLoading(false); }
  }

  async function runFetch(facilityId) {
    if (!dateFrom || !dateTo) { setFetchError("Select both dates"); return; }
    setFetchLoading(true); setFetchResult(null); setFetchError(null);
    try {
      const payload = { facility_id: facilityId || selectedFac || user?.facility_id, date_from: dateFrom, date_to: dateTo };
      const r = await apiClient.post("/dha-sync/fetch", payload);
      setFetchResult(`Fetched ${r.data.records_fetched || 0} files, parsed ${r.data.records_parsed || 0} records. Status: ${r.data.status}`);
      loadLogs();
    } catch (e) {
      setFetchError(e.response?.data?.detail || "Fetch failed");
    } finally { setFetchLoading(false); }
  }

  async function runFetchAll() {
    if (!dateFrom || !dateTo) { setFetchError("Select both dates"); return; }
    if (!facilities.length) { setFetchError("No facilities loaded"); return; }
    setFetchAllLoading(true); setFetchResult(null); setFetchError(null);

    // Fire all facilities in parallel — each is an independent DHPO call
    const results = await Promise.allSettled(
      facilities.map(fac =>
        apiClient.post("/dha-sync/fetch", {
          facility_id: fac.id, date_from: dateFrom, date_to: dateTo,
        })
      )
    );

    setFetchAllLoading(false);

    const succeeded = results.filter(r => r.status === "fulfilled");
    const failed    = results.filter(r => r.status === "rejected");
    const total     = succeeded.reduce((s, r) => s + (r.value?.data?.records_fetched || 0), 0);

    if (failed.length > 0 && succeeded.length === 0) {
      setFetchError(`All ${facilities.length} facilities failed. Check Job History for details.`);
    } else if (failed.length > 0) {
      setFetchResult(`${succeeded.length}/${facilities.length} facilities fetched. Total files: ${total}. ${failed.length} failed — see Job History.`);
    } else {
      setFetchResult(`All ${facilities.length} facilities fetched successfully. Total files: ${total}`);
    }
    loadLogs();
  }

  async function runRiayatiFetch() {
    if (!riayatiFrom || !riayatiTo) { setRiayatiError("Select both dates"); return; }
    const fid = selectedFac || user?.facility_id;
    if (!fid) { setRiayatiError("Select a facility first"); return; }
    setRiayatiLoading(true); setRiayatiResult(null); setRiayatiError(null);
    try {
      const r = await apiClient.post("/dha-sync/riayati-fetch", {
        facility_id: fid, date_from: riayatiFrom, date_to: riayatiTo,
      });
      const d = r.data;
      setRiayatiResult(`Run #${d.id}: ${d.status} — fetched ${d.records_fetched}, parsed ${d.records_parsed}, failed ${d.records_failed}.`);
      loadLogs();
    } catch (e) {
      setRiayatiError(e.response?.data?.detail || "Riayati fetch failed");
    } finally { setRiayatiLoading(false); }
  }

  // Derived: filtered logs
  const filteredLogs = logs.filter(l => {
    if (filterType   && l.fetch_type !== filterType)   return false;
    if (filterStatus && l.status     !== filterStatus)  return false;
    return true;
  });

  const uniqueTypes    = [...new Set(logs.map(l => l.fetch_type))].sort();
  const uniqueStatuses = [...new Set(logs.map(l => l.status))].sort();

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <p className="gh-eyebrow">07 — Data sync</p>
          <h1 className="gh-h1">Background Jobs</h1>
          <p className="text-sm text-slate-500 mt-1">
            Trigger data sync and re-parse jobs manually. Nightly fetch runs automatically at 2 AM.
          </p>
        </div>
        {user?.role === "admin" && facilities.length > 0 && (
          <select
            value={selectedFac}
            onChange={e => setSelectedFac(Number(e.target.value))}
            className="gh-input w-full sm:w-auto"
          >
            <option value="">All facilities</option>
            {facilities.map(f => (
              <option key={f.id} value={f.id}>{f.facility_name}</option>
            ))}
          </select>
        )}
      </div>

      {/* Shared reparse date filter */}
      <div className="gh-card p-4">
        <p className="gh-label mb-3">
          Re-parse Date Filter <span className="font-normal text-slate-600">(optional — leave blank to reparse all dates)</span>
        </p>
        <div className="flex flex-wrap items-center gap-3">
          <select
            value={reparseDateField}
            onChange={e => setReparseDateField(e.target.value)}
            className="gh-input"
          >
            <option value="treatment_date">Treatment Date</option>
            <option value="remittance_date">Remittance Transaction Date</option>
          </select>
          <input
            type="date" value={reparseFrom}
            onChange={e => setReparseFrom(e.target.value)}
            className="gh-input"
          />
          <span className="text-slate-600 text-sm">→</span>
          <input
            type="date" value={reparseTo}
            onChange={e => setReparseTo(e.target.value)}
            className="gh-input"
          />
          {(reparseFrom || reparseTo) && (
            <button
              onClick={() => { setReparseFrom(""); setReparseTo(""); }}
              className="text-xs text-slate-600 hover:text-red-500"
            >✕ Clear</button>
          )}
          {reparseFrom && reparseTo && (
            <span className="text-xs text-emerald-600 font-medium">
              ✓ Targeted reparse — only XMLs linked to {reparseDateField === "treatment_date" ? "treatment" : "remittance"} dates {reparseFrom} → {reparseTo}
            </span>
          )}
        </div>
      </div>

      {/* Job cards */}
      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
        <JobCard
          title="Re-parse Remittances"
          description="Re-processes RemittanceAdvice XMLs already in the database. Fills payment amounts, denial codes, RA file names, and TPA comments. Safe to re-run."
          buttonLabel="Re-parse RAs"
          onRun={runReparseRA}
          loading={raLoading}
          result={raResult}
          error={raError}
        />

        {/* Fetch Latest Data */}
        <div className="gh-section">
          <h3 className="mb-1 text-base font-semibold text-slate-800">Fetch Latest Data</h3>
          <p className="mb-4 text-sm text-slate-500">
            Pull new ClaimSubmission and RemittanceAdvice files from DHA eClaimLink for a date range, then parse them.
          </p>
          <div className="mb-3 flex gap-2">
            <input
              type="date" value={dateFrom}
              onChange={e => setDateFrom(e.target.value)}
              className="gh-input flex-1"
            />
            <input
              type="date" value={dateTo}
              onChange={e => setDateTo(e.target.value)}
              className="gh-input flex-1"
            />
          </div>
          <div className="flex gap-2">
            <button
              onClick={() => runFetch()}
              disabled={fetchLoading || fetchAllLoading}
              className="gh-btn disabled:cursor-not-allowed"
            >
              {fetchLoading ? "Fetching…" : "Fetch & Parse"}
            </button>
            {user?.role === "admin" && facilities.length > 0 && (
              <button
                onClick={runFetchAll}
                disabled={fetchLoading || fetchAllLoading}
                className="gh-btn-secondary disabled:opacity-50 disabled:cursor-not-allowed"
              >
                {fetchAllLoading ? "Fetching all…" : `All ${facilities.length} Facilities`}
              </button>
            )}
          </div>
          {fetchResult && (
            <div className="mt-3 rounded-lg bg-emerald-50 p-3 text-sm text-emerald-800">✅ {fetchResult}</div>
          )}
          {fetchError && (
            <div className="mt-3 rounded-lg bg-red-50 p-3 text-sm text-red-800">❌ {fetchError}</div>
          )}
        </div>

        <JobCard
          title="Re-parse All XMLs"
          description="Re-processes both ClaimSubmission and RemittanceAdvice XMLs. Use when data looks inconsistent across reports. Set a date filter above to target a specific treatment month."
          buttonLabel="Re-parse All"
          onRun={runReparseAll}
          loading={allLoading}
          result={allResult}
          error={allError}
        />

        {/* Riayati (MOH) Fetch */}
        <div className="rounded-xl border border-teal-200 bg-white p-6 shadow-sm">
          <h3 className="mb-1 text-base font-semibold text-slate-800">Fetch from Riayati</h3>
          <p className="mb-1 text-sm text-slate-500">
            Fetches ClaimSubmission + RemittanceAdvice from the MOH Riayati TMB API
            (<code className="rounded bg-slate-100 px-1 text-xs">tmbapi.riayati.ae:8083</code>).
          </p>
          <p className="mb-3 text-xs text-amber-700 bg-amber-50 rounded px-2 py-1">
            ⓘ Routed through the Oracle UAE VM tunnel (whitelisted IP) — the tunnel is started automatically if it is down.
          </p>
          <div className="mb-3 flex gap-2">
            <input type="date" value={riayatiFrom} onChange={e => setRiayatiFrom(e.target.value)}
              className="gh-input flex-1" />
            <input type="date" value={riayatiTo} onChange={e => setRiayatiTo(e.target.value)}
              className="gh-input flex-1" />
          </div>
          <button
            onClick={runRiayatiFetch}
            disabled={riayatiLoading}
            className="rounded-lg bg-teal-600 px-4 py-2 text-sm font-medium text-white hover:bg-teal-700 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {riayatiLoading ? "Fetching…" : "Fetch from Riayati"}
          </button>
          {riayatiResult && (
            <div className="mt-3 rounded-lg bg-emerald-50 p-3 text-sm text-emerald-800">✅ {riayatiResult}</div>
          )}
          {riayatiError && (
            <div className="mt-3 rounded-lg bg-red-50 p-3 text-sm text-red-800">❌ {riayatiError}</div>
          )}
        </div>
      </div>

      {/* Nightly info */}
      <div className="rounded-lg border border-blue-200 bg-blue-50 p-4 text-sm text-blue-800">
        <span className="font-semibold">🕑 Nightly Fetch</span> — Automatically runs every night at 2:00 AM via launchd.
        Fetches the last 7 days of data from DHA eClaimLink for all active facilities.
        Log: <code className="rounded bg-blue-100 px-1">/tmp/ghaf_nightly_fetch.log</code>
      </div>

      {/* Log table */}
      <div>
        <div className="mb-3 flex items-center justify-between gap-3 flex-wrap">
          <h2 className="text-lg font-semibold text-slate-800">Recent Job History</h2>
          <div className="flex flex-wrap items-center gap-2">
            {/* Type filter */}
            <select
              value={filterType}
              onChange={e => setFilterType(e.target.value)}
              className="gh-input px-3 py-1.5 text-xs"
            >
              <option value="">All Types</option>
              {uniqueTypes.map(t => <option key={t} value={t}>{t}</option>)}
            </select>
            {/* Status filter */}
            <select
              value={filterStatus}
              onChange={e => setFilterStatus(e.target.value)}
              className="gh-input px-3 py-1.5 text-xs"
            >
              <option value="">All Statuses</option>
              {uniqueStatuses.map(s => <option key={s} value={s}>{s}</option>)}
            </select>
            {logs.some(l => l.status === "running") && (
              <span className="text-xs text-blue-500 animate-pulse">● Auto-refreshing…</span>
            )}
            <button onClick={loadLogs} className="text-sm text-ghaf-600 hover:underline">
              {logsLoading ? "Loading…" : "Refresh"}
            </button>
          </div>
        </div>

        <div className="gh-card overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="bg-sage-mint gh-label">
              <tr>
                {["ID","Type","Facility","Date Range","Status","Fetched","Parsed","Failed","HTTP Err","Val Err","Started","Completed"].map(h => (
                  <th key={h} className="px-4 py-3 text-left font-medium whitespace-nowrap">{h}</th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {filteredLogs.length === 0 ? (
                <tr><td colSpan={12} className="px-4 py-6 text-center text-slate-600">
                  {logsLoading ? "Loading…" : "No job history found"}
                </td></tr>
              ) : filteredLogs.map(log => (
                <>
                  <tr
                    key={log.id}
                    className={`hover:bg-slate-50 cursor-pointer ${expandedRow === log.id ? "bg-slate-50" : ""}`}
                    onClick={() => setExpandedRow(expandedRow === log.id ? null : log.id)}
                  >
                    <td className="px-4 py-3 text-slate-500">{log.id}</td>
                    <td className="px-4 py-3 font-medium">{log.fetch_type}</td>
                    <td className="px-4 py-3 whitespace-nowrap">
                      {facMap[log.facility_id] || `Facility ${log.facility_id}`}
                    </td>
                    <td className="px-4 py-3 whitespace-nowrap text-slate-600">
                      {log.date_from} → {log.date_to}
                    </td>
                    <td className="px-4 py-3"><StatusBadge status={log.status} /></td>
                    <td className="px-4 py-3 text-right">{log.records_fetched ?? "—"}</td>
                    <td className="px-4 py-3 text-right">{log.records_parsed ?? "—"}</td>
                    <td className={`px-4 py-3 text-right font-medium ${log.records_failed > 0 ? "text-red-600" : ""}`}>
                      {log.records_failed ?? "—"}
                    </td>
                    <td className={`px-4 py-3 text-right ${log.http_errors > 0 ? "text-amber-600 font-medium" : "text-slate-400"}`}>
                      {log.http_errors ?? "—"}
                    </td>
                    <td className={`px-4 py-3 text-right ${log.validation_errors > 0 ? "text-amber-600 font-medium" : "text-slate-400"}`}>
                      {log.validation_errors ?? "—"}
                    </td>
                    <td className="px-4 py-3 whitespace-nowrap text-slate-600 text-xs">
                      {log.started_at ? new Date(log.started_at).toLocaleString() : "—"}
                    </td>
                    <td className="px-4 py-3 whitespace-nowrap text-slate-600 text-xs">
                      {log.completed_at ? new Date(log.completed_at).toLocaleString() : "—"}
                    </td>
                  </tr>
                  {expandedRow === log.id && log.error_detail && (
                    <tr key={`${log.id}-detail`} className="bg-red-50">
                      <td colSpan={12} className="px-6 py-3">
                        <p className="text-xs font-semibold text-red-700 mb-1">Error Detail</p>
                        <pre className="text-xs text-red-800 whitespace-pre-wrap font-mono bg-red-100 rounded p-2 max-h-40 overflow-auto">
                          {JSON.stringify(log.error_detail, null, 2)}
                        </pre>
                      </td>
                    </tr>
                  )}
                  {expandedRow === log.id && !log.error_detail && (
                    <tr key={`${log.id}-nodetail`} className="bg-slate-50">
                      <td colSpan={12} className="px-6 py-2 text-xs text-slate-600 italic">
                        No error detail recorded for this job.
                      </td>
                    </tr>
                  )}
                </>
              ))}
            </tbody>
          </table>
        </div>
        <p className="mt-2 text-xs text-slate-600">
          Showing {filteredLogs.length} of {logs.length} jobs. Click a row to see error detail.
        </p>
      </div>
    </div>
  );
}
