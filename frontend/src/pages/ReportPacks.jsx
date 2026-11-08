/**
 * ReportPacks.jsx — Client Report Packs
 *
 * Runs the PDF / Excel generators (monthly pack, quarterly, half-yearly,
 * receiver workbooks, resubmission worklist, recall audit, clinician ledger)
 * for one facility as background jobs, and lists every generated file.
 *
 * Sections: Performance reports · Work lists · Audits · Generated files
 */
import { useEffect, useMemo, useState } from "react";
import apiClient from "../api/client";
import { readFacility, saveFacility } from "../utils/facility";

const SECTIONS = [
  { key: "performance", title: "1. Performance reports (PDF)", note: "Client-ready reports for a period." },
  { key: "worklists", title: "2. Work lists for facilities (Excel)", note: "Follow-up, reconciliation and resubmission lists." },
  { key: "audits", title: "3. Audits", note: "Recall audit and clinician denial ledger." },
];

const STATUS_STYLE = {
  queued: "gh-chip gh-chip-neutral",
  running: "gh-chip gh-chip-pending",
  completed: "gh-chip gh-chip-paid",
  error: "gh-chip gh-chip-denied",
};

const now = new Date();
const thisYear = now.getFullYear();
const lastMonth = new Date(now.getFullYear(), now.getMonth() - 1, 1);
const ym = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
const iso = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;

function PeriodFields({ fields, value, onChange }) {
  const set = (k) => (e) => onChange({ ...value, [k]: e.target.value });
  if (fields.includes("month")) {
    return <input type="month" className="gh-input" value={value.month || ""} onChange={set("month")} />;
  }
  if (fields.includes("quarter")) {
    const opts = [];
    for (const y of [thisYear, thisYear - 1]) {
      ["Q1", "Q2", "Q3", "Q4"].forEach((q) => opts.push(`${q}-${y}`));
      opts.push(`annual-${y}`);
    }
    return (
      <select className="gh-input" value={value.period || ""} onChange={set("period")}>
        <option value="">Choose period…</option>
        {opts.map((o) => <option key={o} value={o}>{o}</option>)}
      </select>
    );
  }
  if (fields.includes("half")) {
    const custom = value.period && value.period.includes(":");
    const opts = [];
    for (const y of [thisYear, thisYear - 1]) ["H1", "H2", "FY"].forEach((h) => opts.push(`${h}-${y}`));
    return (
      <div className="flex flex-wrap items-center gap-2">
        <select className="gh-input" value={custom ? "custom" : value.period || ""}
          onChange={(e) => onChange({ ...value, period: e.target.value === "custom" ? `${ym(lastMonth)}:${ym(lastMonth)}` : e.target.value })}>
          <option value="">Choose period…</option>
          {opts.map((o) => <option key={o} value={o}>{o}</option>)}
          <option value="custom">Custom month range…</option>
        </select>
        {custom && (
          <>
            <input type="month" className="gh-input" value={value.period.split(":")[0]}
              onChange={(e) => onChange({ ...value, period: `${e.target.value}:${value.period.split(":")[1]}` })} />
            <span className="text-slate-400">→</span>
            <input type="month" className="gh-input" value={value.period.split(":")[1]}
              onChange={(e) => onChange({ ...value, period: `${value.period.split(":")[0]}:${e.target.value}` })} />
          </>
        )}
      </div>
    );
  }
  if (fields.includes("date_range")) {
    return (
      <div className="flex flex-wrap items-center gap-2">
        <input type="date" className="gh-input" value={value.date_from || ""} onChange={set("date_from")} />
        <span className="text-slate-400">→</span>
        <input type="date" className="gh-input" value={value.date_to || ""} onChange={set("date_to")} />
      </div>
    );
  }
  return <span className="text-xs text-slate-500">No period needed</span>;
}

function PackCard({ kind, spec, facilityId, onQueued }) {
  const [value, setValue] = useState({
    month: ym(lastMonth),
    date_from: `${thisYear}-01-01`,
    date_to: iso(now),
  });
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");

  const run = async () => {
    setBusy(true); setMsg("");
    try {
      await apiClient.post("/packs/run", { kind, facility_id: Number(facilityId), ...value });
      setMsg("Queued — see Generated files below.");
      onQueued();
    } catch (e) {
      setMsg(e.response?.data?.detail || "Could not start.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="gh-card flex flex-col gap-3 p-4">
      <div>
        <p className="font-semibold text-slate-900">{spec.label}</p>
        <p className="text-xs text-slate-500">{spec.help}</p>
      </div>
      <PeriodFields fields={spec.fields} value={value} onChange={setValue} />
      <div className="flex items-center gap-3">
        <button className="gh-btn" disabled={!facilityId || busy} onClick={run}>
          {busy ? "Starting…" : "Generate"}
        </button>
        {msg && <span className="text-xs text-slate-600">{msg}</span>}
      </div>
    </div>
  );
}

async function download(job, f) {
  const res = await apiClient.get(`/packs/jobs/${job.id}/file`, { params: { path: f.path }, responseType: "blob" });
  const url = URL.createObjectURL(res.data);
  const a = document.createElement("a");
  a.href = url; a.download = f.name; document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 30000);
}

export default function ReportPacks() {
  const [catalog, setCatalog] = useState({});
  const [facilities, setFacilities] = useState([]);
  const [facilityId, setFacilityIdState] = useState(() => readFacility());
  const setFacilityId = (id) => { setFacilityIdState(id); saveFacility(id); };
  const [jobs, setJobs] = useState([]);
  const [filterFac, setFilterFac] = useState("");
  const [filterKind, setFilterKind] = useState("");
  const [openLog, setOpenLog] = useState(null);

  const loadJobs = () =>
    apiClient.get("/packs/jobs", { params: { limit: 100 } }).then((r) => setJobs(r.data)).catch(() => {});

  useEffect(() => {
    apiClient.get("/packs/catalog").then((r) => setCatalog(r.data)).catch(() => {});
    apiClient.get("/facilities").then((r) => setFacilities(r.data.filter((f) => f.facility_code !== "GHAF-001"))).catch(() => {});
    loadJobs();
  }, []);

  const active = jobs.some((j) => j.status === "queued" || j.status === "running");
  useEffect(() => {
    if (!active) return undefined;
    const t = setInterval(loadJobs, 5000);
    return () => clearInterval(t);
  }, [active]);

  const facName = useMemo(() => Object.fromEntries(facilities.map((f) => [f.id, f.facility_name])), [facilities]);
  const shown = jobs.filter((j) => (!filterFac || String(j.facility_id) === filterFac) && (!filterKind || j.kind === filterKind));

  return (
    <div className="space-y-6">
      <div>
        <p className="gh-eyebrow">Reports &amp; analytics</p>
        <h1 className="gh-h1">Client Report Packs</h1>
        <p className="text-sm text-slate-500">
          Generate client packs for one facility. Jobs run one at a time in the background; files appear under
          Generated files. Nothing is emailed from here.
        </p>
      </div>

      <div className="gh-card flex flex-wrap items-center gap-3 p-4">
        <label className="text-sm font-medium text-slate-700">Facility</label>
        <select className="gh-input min-w-[320px]" value={facilityId} onChange={(e) => setFacilityId(e.target.value)}>
          <option value="">Choose a facility…</option>
          {facilities.map((f) => <option key={f.id} value={f.id}>{f.facility_name} (#{f.id})</option>)}
        </select>
      </div>

      {SECTIONS.map((s) => (
        <section key={s.key} className="space-y-3">
          <div>
            <h2 className="text-lg font-semibold text-slate-900">{s.title}</h2>
            <p className="text-xs text-slate-500">{s.note}</p>
          </div>
          <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
            {Object.entries(catalog).filter(([, v]) => v.section === s.key).map(([k, v]) => (
              <PackCard key={k} kind={k} spec={v} facilityId={facilityId} onQueued={loadJobs} />
            ))}
          </div>
        </section>
      ))}

      <section className="space-y-3">
        <div className="flex flex-wrap items-end justify-between gap-3">
          <div>
            <h2 className="text-lg font-semibold text-slate-900">4. Generated files</h2>
            <p className="text-xs text-slate-500">Newest first. Files are kept in ~/ghaf_reports on the server.</p>
          </div>
          <div className="flex gap-2">
            <select className="gh-input" value={filterFac} onChange={(e) => setFilterFac(e.target.value)}>
              <option value="">All facilities</option>
              {facilities.map((f) => <option key={f.id} value={f.id}>{f.facility_name}</option>)}
            </select>
            <select className="gh-input" value={filterKind} onChange={(e) => setFilterKind(e.target.value)}>
              <option value="">All types</option>
              {Object.entries(catalog).map(([k, v]) => <option key={k} value={k}>{v.label}</option>)}
            </select>
            <button className="gh-btn-secondary" onClick={loadJobs}>Refresh</button>
          </div>
        </div>

        <div className="gh-card divide-y divide-slate-100">
          {shown.length === 0 && <p className="p-4 text-sm text-slate-500">No packs generated yet.</p>}
          {shown.map((j) => (
            <div key={j.id} className="space-y-2 p-4">
              <div className="flex flex-wrap items-center gap-2">
                <span className={STATUS_STYLE[j.status] || "gh-chip"}>{j.status}</span>
                <span className="font-medium text-slate-900">{j.label}</span>
                <span className="text-xs text-slate-500">
                  #{j.id} · {facName[j.facility_id] ? "" : `facility #${j.facility_id} · `}
                  {new Date(j.created_at).toLocaleString()}
                </span>
                {j.log_tail && (
                  <button className="text-xs text-slate-500 underline" onClick={() => setOpenLog(openLog === j.id ? null : j.id)}>
                    {openLog === j.id ? "Hide log" : "Log"}
                  </button>
                )}
              </div>
              {j.files.length > 0 && (
                <div className="flex flex-wrap gap-2">
                  {j.files.map((f) => (
                    <button key={f.path} className="gh-btn-secondary gh-btn-sm" title={f.folder} onClick={() => download(j, f)}>
                      ⬇ {f.name}
                    </button>
                  ))}
                </div>
              )}
              {openLog === j.id && (
                <pre className="max-h-64 overflow-auto rounded bg-slate-50 p-2 text-[11px] text-slate-700">{j.log_tail}</pre>
              )}
            </div>
          ))}
        </div>
      </section>
    </div>
  );
}
