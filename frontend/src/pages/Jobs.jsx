import { Fragment, useEffect, useRef, useState } from "react";
import apiClient from "../api/client";
import { localISODate } from "../utils/format";
import { useAuth } from "../context/AuthContext";
import { readFacility, saveFacility } from "../utils/facility";
import { confirmDialog } from "../utils/notify";

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

const DEMO_CODES = ["GHAF-001"];   // never fetched from real DHPO

const fmtDay = d => (d ? String(d).slice(0, 10).split("-").reverse().join("/") : "");
const fmtWhen = d => d ? new Date(d).toLocaleString("en-GB", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }) : "—";
function took(a, b) {
  if (!a || !b) return "—";
  const s = Math.max(0, Math.round((new Date(b) - new Date(a)) / 1000));
  return s < 60 ? `${s}s` : s < 3600 ? `${Math.floor(s / 60)}m ${s % 60}s` : `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
}
function ago(d) {
  if (!d) return "never";
  const m = Math.round((Date.now() - new Date(d)) / 60000);
  return m < 1 ? "just now" : m < 60 ? `${m} min ago` : m < 1440 ? `${Math.round(m / 60)} h ago` : `${Math.round(m / 1440)} d ago`;
}

function quickRange(kind) {
  const t = new Date();
  if (kind === "7d") return [localISODate(new Date(t.getFullYear(), t.getMonth(), t.getDate() - 6)), localISODate(t)];
  if (kind === "month") return [localISODate(new Date(t.getFullYear(), t.getMonth(), 1)), localISODate(t)];
  return [localISODate(new Date(t.getFullYear(), t.getMonth() - 1, 1)), localISODate(new Date(t.getFullYear(), t.getMonth(), 0))];
}

function DateRange({ from, to, onFrom, onTo }) {
  const set = k => { const [a, b] = quickRange(k); onFrom(a); onTo(b); };
  return (
    <div className="mb-4">
      <div className="flex flex-wrap gap-3">
        <label className="min-w-[130px] flex-1">
          <span className="gh-label mb-1 block">From</span>
          <input type="date" value={from} max={to || undefined} onChange={e => onFrom(e.target.value)} className="gh-input w-full" />
        </label>
        <label className="min-w-[130px] flex-1">
          <span className="gh-label mb-1 block">To</span>
          <input type="date" value={to} min={from || undefined} onChange={e => onTo(e.target.value)} className="gh-input w-full" />
        </label>
      </div>
      <div className="mt-2 flex flex-wrap gap-2">
        {[["7d", "Last 7 days"], ["month", "This month"], ["last", "Last month"]].map(([k, l]) => (
          <button key={k} type="button" onClick={() => set(k)}
            className="rounded-full border border-sage-line px-2.5 py-0.5 text-xs text-slate-600 hover:bg-sage">{l}</button>
        ))}
      </div>
    </div>
  );
}

function Outcome({ result, error }) {
  if (!result && !error) return null;
  return (
    <div role={error ? "alert" : "status"}
      className={`mt-3 rounded-lg p-3 text-sm ${error ? "bg-red-50 text-red-800" : "bg-emerald-50 text-emerald-800"}`}>
      {error || result}
    </div>
  );
}

function Stat({ label, value, sub, tone }) {
  return (
    <div className="gh-card p-4">
      <p className="gh-label">{label}</p>
      <p className={`mt-1 text-2xl font-semibold ${tone || "text-ink"}`}>{value}</p>
      {sub && <p className="mt-0.5 text-xs text-slate-500">{sub}</p>}
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

  const liveFacilities = facilities.filter(f => f.is_active !== false && !DEMO_CODES.includes(f.facility_code));
  const facName = selectedFac ? facMap[selectedFac] : "";

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
      return `No ${label} files found for that date range. Nothing to re-parse.`;
    return `Job #${data.fetch_log_id} queued: ${data.reset_count} ${label} file(s) processing in the background.`;
  }

  async function runReparseRA() {
    if (!(reparseFrom && reparseTo) && !(await confirmDialog(
      `No date range is set, so this re-parses every stored file for ${selectedFac ? facMap[selectedFac] || "this facility" : "ALL facilities"}. It can take a long time.`,
      { okLabel: "Re-parse everything" }))) return;
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
    if (!(reparseFrom && reparseTo) && !(await confirmDialog(
      `No date range is set, so this re-parses every stored file for ${selectedFac ? facMap[selectedFac] || "this facility" : "ALL facilities"}. It can take a long time.`,
      { okLabel: "Re-parse everything" }))) return;
    setAllLoading(true); setAllResult(null); setAllError(null);
    try {
      const r1 = await apiClient.post(`/dha-sync/reparse-and-trigger?${buildReparseParams("ClaimSubmission")}`);
      const r2 = await apiClient.post(`/dha-sync/reparse-and-trigger?${buildReparseParams("RemittanceAdvice")}`);
      const msg1 = reparseResultMsg(r1.data, "CS");
      const msg2 = reparseResultMsg(r2.data, "RA");
      setAllResult(`${msg1} ${msg2}`);
      loadLogs();
    } catch (e) {
      setAllError(e.response?.data?.detail || "Re-parse failed");
    } finally { setAllLoading(false); }
  }

  async function runFetch(facilityId) {
    if (!dateFrom || !dateTo) { setFetchError("Select both dates"); return; }
    if (!(facilityId || selectedFac || user?.facility_id)) { setFetchError("Pick a facility at the top, or use the all-facilities button."); return; }
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
    if (!liveFacilities.length) { setFetchError("No facilities loaded"); return; }
    if (!(await confirmDialog(
      `Fetch ${fmtDay(dateFrom)} to ${fmtDay(dateTo)} from DHA eClaimLink for all ${liveFacilities.length} active facilities?`,
      { okLabel: "Fetch all", danger: false }))) return;
    setFetchAllLoading(true); setFetchResult(null); setFetchError(null);

    // Fire all facilities in parallel — each is an independent DHPO call
    const results = await Promise.allSettled(
      liveFacilities.map(fac =>
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
      setFetchError(`All ${liveFacilities.length} facilities failed. See the job history below.`);
    } else if (failed.length > 0) {
      setFetchResult(`${succeeded.length} of ${liveFacilities.length} facilities fetched, ${total} files. ${failed.length} failed: see the job history below.`);
    } else {
      setFetchResult(`All ${liveFacilities.length} facilities fetched, ${total} files.`);
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
      setRiayatiResult(`Job #${d.id} ${d.status}: fetched ${d.records_fetched}, parsed ${d.records_parsed}, failed ${d.records_failed}.`);
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

  const running = logs.filter(l => l.status === "running");
  const failedJobs = logs.filter(l => l.status === "failed");
  const lastOk = logs.find(l => l.status === "success" && l.completed_at);
  const isAdmin = user?.role === "admin";
  const problems = l => (l.records_failed || 0) + (l.http_errors || 0) + (l.validation_errors || 0);

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <p className="gh-eyebrow">Data sync</p>
          <h1 className="gh-h1">Data sync &amp; jobs</h1>
          <p className="mt-1 text-sm text-slate-500">
            Fetch new claim and remittance files, or re-process the ones already stored.
            The last 7 days are fetched automatically every night at 2:00 AM.
          </p>
        </div>
        {isAdmin && facilities.length > 0 && (
          <label className="w-full sm:w-72">
            <span className="gh-label mb-1 block">Facility</span>
            <select value={selectedFac} onChange={e => setSelectedFac(Number(e.target.value))} className="gh-input w-full">
              <option value="">All facilities</option>
              {facilities.map(f => (
                <option key={f.id} value={f.id}>{f.facility_name}</option>
              ))}
            </select>
          </label>
        )}
      </div>

      {/* At a glance */}
      <div className="grid gap-3 sm:grid-cols-3">
        <Stat label="Running now" value={running.length}
          sub={running.length ? "History refreshes every 5 seconds" : "Nothing in progress"}
          tone={running.length ? "text-ghaf-600" : ""} />
        <Stat label="Failed (last 50 jobs)" value={failedJobs.length}
          sub={failedJobs.length ? "Filter the history by status to see them" : "No failures"}
          tone={failedJobs.length ? "text-denied" : ""} />
        <Stat label="Last successful job" value={ago(lastOk?.completed_at)}
          sub={lastOk ? `${lastOk.fetch_type} · ${facMap[lastOk.facility_id] || `Facility ${lastOk.facility_id}`}` : "No successful job in the list"} />
      </div>

      {/* Fetch */}
      <section>
        <h2 className="gh-section-title mb-3">Fetch new files</h2>
        <div className="grid gap-4 lg:grid-cols-2">
          <div className="gh-section">
            <h3 className="text-base font-semibold text-slate-800">DHA eClaimLink</h3>
            <p className="mb-4 mt-1 text-sm text-slate-500">
              Downloads claim submissions and remittance advices for the dates below, then parses them.
            </p>
            <DateRange from={dateFrom} to={dateTo} onFrom={setDateFrom} onTo={setDateTo} />
            <div className="flex flex-wrap gap-2">
              <button onClick={() => runFetch()} disabled={fetchLoading || fetchAllLoading || (isAdmin && !selectedFac)}
                title={isAdmin && !selectedFac ? "Pick a facility at the top first" : undefined}
                className="gh-btn disabled:cursor-not-allowed">
                {fetchLoading ? "Fetching…" : facName ? `Fetch ${facName}` : "Fetch"}
              </button>
              {isAdmin && liveFacilities.length > 0 && (
                <button onClick={runFetchAll} disabled={fetchLoading || fetchAllLoading}
                  className="gh-btn-secondary disabled:cursor-not-allowed disabled:opacity-50">
                  {fetchAllLoading ? "Fetching all…" : `Fetch all ${liveFacilities.length} active facilities`}
                </button>
              )}
            </div>
            {isAdmin && !selectedFac && (
              <p className="mt-2 text-xs text-slate-500">Pick a facility at the top to fetch just one.</p>
            )}
            <Outcome result={fetchResult} error={fetchError} />
          </div>

          <div className="gh-section">
            <h3 className="text-base font-semibold text-slate-800">Riayati (MOH)</h3>
            <p className="mb-4 mt-1 text-sm text-slate-500">
              Downloads claim submissions and remittance advices from Riayati for one facility.
              Goes through the Oracle UAE server, which starts automatically if it is down.
            </p>
            <DateRange from={riayatiFrom} to={riayatiTo} onFrom={setRiayatiFrom} onTo={setRiayatiTo} />
            <button onClick={runRiayatiFetch} disabled={riayatiLoading || (isAdmin && !selectedFac)}
              title={isAdmin && !selectedFac ? "Pick a facility at the top first" : undefined}
              className="gh-btn disabled:cursor-not-allowed">
              {riayatiLoading ? "Fetching…" : facName ? `Fetch ${facName}` : "Fetch from Riayati"}
            </button>
            {isAdmin && !selectedFac && (
              <p className="mt-2 text-xs text-slate-500">Riayati fetches one facility at a time. Pick one at the top.</p>
            )}
            <Outcome result={riayatiResult} error={riayatiError} />
          </div>
        </div>
      </section>

      {/* Re-parse */}
      <section>
        <h2 className="gh-section-title mb-3">Re-process stored files</h2>
        <div className="gh-section">
          <p className="mb-4 text-sm text-slate-500">
            Reads the files already in the database again. Use it when amounts, denial codes or file names look
            wrong in the reports. Nothing is downloaded and it is safe to run again.
          </p>
          <div className="flex flex-wrap items-end gap-3">
            <label>
              <span className="gh-label mb-1 block">Limit by</span>
              <select value={reparseDateField} onChange={e => setReparseDateField(e.target.value)} className="gh-input">
                <option value="treatment_date">Treatment date</option>
                <option value="remittance_date">Remittance transaction date</option>
              </select>
            </label>
            <label>
              <span className="gh-label mb-1 block">From</span>
              <input type="date" value={reparseFrom} max={reparseTo || undefined}
                onChange={e => setReparseFrom(e.target.value)} className="gh-input" />
            </label>
            <label>
              <span className="gh-label mb-1 block">To</span>
              <input type="date" value={reparseTo} min={reparseFrom || undefined}
                onChange={e => setReparseTo(e.target.value)} className="gh-input" />
            </label>
            {(reparseFrom || reparseTo) && (
              <button onClick={() => { setReparseFrom(""); setReparseTo(""); }}
                className="pb-2 text-xs text-slate-600 hover:text-denied">Clear dates</button>
            )}
          </div>
          <p className="mt-2 text-xs text-slate-500">
            {reparseFrom && reparseTo
              ? `Only files linked to ${reparseDateField === "treatment_date" ? "treatment" : "remittance"} dates ${fmtDay(reparseFrom)} to ${fmtDay(reparseTo)}${facName ? `, ${facName}` : ", all facilities"}.`
              : `No dates set: every stored file${facName ? ` for ${facName}` : " for all facilities"} will be re-processed.`}
          </p>
          <div className="mt-4 flex flex-wrap gap-2">
            <button onClick={runReparseRA} disabled={raLoading || allLoading} className="gh-btn disabled:cursor-not-allowed">
              {raLoading ? "Starting…" : "Re-process remittances"}
            </button>
            <button onClick={runReparseAll} disabled={raLoading || allLoading}
              className="gh-btn-secondary disabled:cursor-not-allowed disabled:opacity-50">
              {allLoading ? "Starting…" : "Re-process claims and remittances"}
            </button>
          </div>
          <Outcome result={raResult} error={raError} />
          <Outcome result={allResult} error={allError} />
        </div>
      </section>

      {/* History */}
      <section>
        <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
          <h2 className="gh-section-title">
            Job history
            {running.length > 0 && <span className="ml-2 animate-pulse text-xs font-normal text-ghaf-600">● refreshing</span>}
          </h2>
          <div className="flex flex-wrap items-center gap-2">
            <select aria-label="Job type" value={filterType} onChange={e => setFilterType(e.target.value)}
              className="gh-input px-3 py-1.5 text-xs">
              <option value="">All job types</option>
              {uniqueTypes.map(t => <option key={t} value={t}>{t}</option>)}
            </select>
            <select aria-label="Status" value={filterStatus} onChange={e => setFilterStatus(e.target.value)}
              className="gh-input px-3 py-1.5 text-xs">
              <option value="">All statuses</option>
              {uniqueStatuses.map(s => <option key={s} value={s}>{s}</option>)}
            </select>
            <button onClick={loadLogs} className="gh-btn-secondary gh-btn-sm">
              {logsLoading ? "Loading…" : "Refresh"}
            </button>
          </div>
        </div>

        <div className="gh-card overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="bg-sage-mint gh-label">
              <tr>
                {[["Job", ""], ["Type", ""], ["Facility", ""], ["Period", ""], ["Status", ""], ["Fetched", "r"], ["Parsed", "r"], ["Problems", "r"], ["Started", ""], ["Took", "r"]].map(([h, a]) => (
                  <th key={h} className={`whitespace-nowrap px-4 py-3 font-medium ${a ? "text-right" : "text-left"}`}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {filteredLogs.length === 0 ? (
                <tr><td colSpan={10} className="px-4 py-6 text-center text-slate-600">
                  {logsLoading ? "Loading…" : logs.length ? "No jobs match these filters." : "No jobs yet."}
                </td></tr>
              ) : filteredLogs.map(log => {
                const open = expandedRow === log.id, bad = problems(log);
                return (
                  <Fragment key={log.id}>
                    <tr className={`cursor-pointer hover:bg-slate-50 ${open ? "bg-slate-50" : ""}`}
                      onClick={() => setExpandedRow(open ? null : log.id)}>
                      <td className="whitespace-nowrap px-4 py-3 text-slate-500">
                        <span className="mr-1 inline-block w-3 text-slate-400">{open ? "▾" : "▸"}</span>#{log.id}
                      </td>
                      <td className="px-4 py-3 font-medium">{log.fetch_type}</td>
                      <td className="px-4 py-3">{facMap[log.facility_id] || `Facility ${log.facility_id}`}</td>
                      <td className="whitespace-nowrap px-4 py-3 text-slate-600">
                        {log.date_from ? `${fmtDay(log.date_from)} – ${fmtDay(log.date_to)}` : "All dates"}
                      </td>
                      <td className="px-4 py-3"><StatusBadge status={log.status} /></td>
                      <td className="px-4 py-3 text-right tabular-nums">{log.records_fetched ?? "—"}</td>
                      <td className="px-4 py-3 text-right tabular-nums">{log.records_parsed ?? "—"}</td>
                      <td className={`px-4 py-3 text-right tabular-nums ${bad ? "font-medium text-denied" : "text-slate-400"}`}>{bad || "—"}</td>
                      <td className="whitespace-nowrap px-4 py-3 text-xs text-slate-600">{fmtWhen(log.started_at)}</td>
                      <td className="whitespace-nowrap px-4 py-3 text-right text-xs text-slate-600">
                        {log.status === "running" ? "running" : took(log.started_at, log.completed_at)}
                      </td>
                    </tr>
                    {open && (
                      <tr className="bg-slate-50">
                        <td colSpan={10} className="px-6 pb-4 pt-1">
                          <dl className="flex flex-wrap gap-x-8 gap-y-1 text-xs text-slate-600">
                            <div><dt className="inline text-slate-500">Failed records: </dt><dd className="inline font-medium">{log.records_failed ?? 0}</dd></div>
                            <div><dt className="inline text-slate-500">Connection errors: </dt><dd className="inline font-medium">{log.http_errors ?? 0}</dd></div>
                            <div><dt className="inline text-slate-500">Validation errors: </dt><dd className="inline font-medium">{log.validation_errors ?? 0}</dd></div>
                            <div><dt className="inline text-slate-500">Finished: </dt><dd className="inline font-medium">{fmtWhen(log.completed_at)}</dd></div>
                          </dl>
                          {log.error_detail ? (
                            <pre className="mt-2 max-h-40 overflow-auto whitespace-pre-wrap rounded bg-red-50 p-2 font-mono text-xs text-red-800">
                              {typeof log.error_detail === "string" ? log.error_detail : JSON.stringify(log.error_detail, null, 2)}
                            </pre>
                          ) : (log.status === "failed" || log.status === "partial") && (
                            <p className="mt-2 text-xs text-slate-500">No error message was recorded for this job.</p>
                          )}
                        </td>
                      </tr>
                    )}
                  </Fragment>
                );
              })}
            </tbody>
          </table>
        </div>
        <p className="mt-2 text-xs text-slate-600">
          Showing {filteredLogs.length} of the last {logs.length} jobs. Click a row for details.
        </p>
      </section>
    </div>
  );
}
