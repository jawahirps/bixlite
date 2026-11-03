/**
 * DocReview.jsx — Documentation review
 *
 * Does the case sheet (chief complaint / notes) support every coded diagnosis? Run per facility and
 * treatment month; findings are routed by tier:
 *   A  claim query before submission (also raised by the Claim scrubber on ERP uploads)
 *   B  monthly doctor feedback
 *   C  Ghaf internal review
 *
 * Tabs: Dashboard · Findings · Payer patterns · Downloads
 */
import { useEffect, useState } from "react";
import apiClient from "../api/client";

const TIERS = {
  A: { label: "Tier A · Claim query", chip: "gh-chip gh-chip-denied", note: "Fix before submission" },
  B: { label: "Tier B · Doctor feedback", chip: "gh-chip gh-chip-pending", note: "Monthly feedback, no per-claim query" },
  C: { label: "Tier C · Ghaf review", chip: "gh-chip gh-chip-neutral", note: "Internal check only" },
};
const RUN_STYLE = { Queued: "gh-chip gh-chip-neutral", Running: "gh-chip gh-chip-pending", Completed: "gh-chip gh-chip-paid", Failed: "gh-chip gh-chip-denied" };

const lastMonth = (() => { const d = new Date(); d.setDate(1); d.setMonth(d.getMonth() - 1); return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`; })();
const pct = (v) => (v == null ? "–" : `${(v * 100).toFixed(1)}%`);
const aed = (v) => (v ? Number(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : "–");

async function download(url, params, name) {
  const res = await apiClient.get(url, { params, responseType: "blob" });
  const href = URL.createObjectURL(res.data);
  const a = document.createElement("a");
  a.href = href; a.download = name; document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(href), 30000);
}

function Dashboard({ dash, onDoctor }) {
  if (!dash?.run) return <div className="gh-card p-4 text-sm text-slate-500">No completed check for this month yet. Run the check above.</div>;
  const r = dash.run;
  return (
    <div className="space-y-4">
      <div className="grid gap-3 md:grid-cols-4">
        <div className="gh-card p-4">
          <p className="gh-label">Consultations checked</p>
          <p className="text-2xl font-semibold text-slate-900">{r.checked.toLocaleString()}</p>
          <p className="text-xs text-slate-500">{r.flagged.toLocaleString()} with a finding ({pct(r.checked ? r.flagged / r.checked : null)}) · {r.no_note} without case sheet</p>
        </div>
        {Object.entries(TIERS).map(([t, s]) => {
          const v = dash.tiers?.[t] || { findings: 0, claims: 0, amount: 0 };
          return (
            <div key={t} className="gh-card p-4">
              <p className="gh-label">{s.label}</p>
              <p className="text-2xl font-semibold text-slate-900">{v.findings.toLocaleString()}</p>
              <p className="text-xs text-slate-500">{v.claims.toLocaleString()} claims · {s.note}{t === "A" && v.amount ? ` · AED ${aed(v.amount)} at risk` : ""}</p>
            </div>
          );
        })}
      </div>

      <div className="grid gap-4 xl:grid-cols-3">
        <div className="gh-card p-4">
          <p className="mb-2 font-semibold text-slate-900">By category</p>
          <table className="w-full text-xs">
            <tbody>
              {dash.categories.map((c) => (
                <tr key={c.category} className="border-t border-slate-100">
                  <td className="py-1">{c.category}</td>
                  <td className="py-1 text-right">{c.findings.toLocaleString()}</td>
                </tr>))}
            </tbody>
          </table>
        </div>
        <div className="gh-card p-4 xl:col-span-2">
          <p className="mb-2 font-semibold text-slate-900">Trend</p>
          <table className="w-full text-xs">
            <thead className="text-left text-slate-500"><tr>{["Month", "Checked", "With finding", "Rate", "Tier A"].map((h) => <th key={h} className="py-1 font-medium">{h}</th>)}</tr></thead>
            <tbody>
              {dash.trend.map((t) => (
                <tr key={t.month} className="border-t border-slate-100">
                  <td className="py-1">{t.month}</td><td>{t.checked}</td><td>{t.flagged}</td><td>{pct(t.rate)}</td><td>{t.tier_a}</td>
                </tr>))}
            </tbody>
          </table>
        </div>
      </div>

      <div className="gh-card p-4">
        <p className="mb-2 font-semibold text-slate-900">Doctors</p>
        <div className="max-h-[480px] overflow-auto">
          <table className="w-full text-xs">
            <thead className="sticky top-0 bg-slate-50 text-left text-slate-500">
              <tr>{["Doctor", "Consultations", "With finding", "Rate", "A", "B", "C", "Most-flagged Dx"].map((h) => <th key={h} className="px-2 py-1 font-medium">{h}</th>)}</tr>
            </thead>
            <tbody>
              {dash.doctors.map((d) => (
                <tr key={d.clinician_id} className="cursor-pointer border-t border-slate-100 hover:bg-slate-50" onClick={() => onDoctor(d.clinician_id)}>
                  <td className="px-2 py-1"><span className="font-medium text-slate-900">{d.name || "—"}</span><br /><span className="text-slate-400">{d.clinician_id}</span></td>
                  <td className="px-2 py-1">{d.checked}</td>
                  <td className="px-2 py-1">{d.claims_flagged}</td>
                  <td className={`px-2 py-1 font-medium ${d.rate > r.flagged / r.checked ? "text-red-600" : "text-slate-700"}`}>{pct(d.rate)}</td>
                  <td className="px-2 py-1">{d.a}</td><td className="px-2 py-1">{d.b}</td><td className="px-2 py-1">{d.c}</td>
                  <td className="px-2 py-1 text-slate-500">{d.top.join("; ")}</td>
                </tr>))}
            </tbody>
          </table>
        </div>
        <p className="mt-2 text-xs text-slate-400">Click a doctor to see their findings. Rates above the facility average are shown in red.</p>
      </div>
    </div>
  );
}

function FindingRow({ f, onSaved }) {
  const [open, setOpen] = useState(false);
  const [remark, setRemark] = useState(f.review_remark || "");
  const save = async (status) => {
    const r = await apiClient.patch(`/docreview/findings/${f.id}`, { review_status: status, review_remark: remark });
    onSaved({ ...f, ...r.data });
  };
  return (
    <>
      <tr className="cursor-pointer border-t border-slate-100 align-top hover:bg-slate-50" onClick={() => setOpen(!open)}>
        <td className="px-2 py-1"><span className={TIERS[f.tier].chip}>{f.tier}</span></td>
        <td className="px-2 py-1">{f.claim_id}<br /><span className="text-slate-400">{f.encounter_date} · {f.payer_id}</span></td>
        <td className="px-2 py-1">{f.clinician_name || f.clinician_id}</td>
        <td className="px-2 py-1"><span className="font-medium">{f.dx_code}</span><br /><span className="text-slate-500">{f.dx_description}</span></td>
        <td className="px-2 py-1">{f.category}<br /><span className="text-slate-500">{f.reason}</span></td>
        <td className="px-2 py-1 text-right">{f.amount_at_risk ? aed(f.amount_at_risk) : ""}</td>
        <td className="px-2 py-1">{f.review_status ? <span className={f.review_status === "Agree" ? "gh-chip gh-chip-paid" : "gh-chip gh-chip-neutral"}>{f.review_status}</span> : ""}</td>
      </tr>
      {open && (
        <tr className="bg-slate-50 text-xs">
          <td colSpan={7} className="px-3 py-3">
            <div className="grid gap-3 lg:grid-cols-2">
              <div className="space-y-2">
                <p><span className="gh-label">Ghaf query</span><br />{f.query_text}</p>
                <p><span className="gh-label">Suggested action</span><br />{f.suggestion}</p>
                {f.not_coded && <p><span className="gh-label">Supported in case sheet, not coded</span><br />{f.not_coded}</p>}
                {f.services_at_risk && <p><span className="gh-label">Services at risk</span><br />{f.services_at_risk} · AED {aed(f.amount_at_risk)}</p>}
                <p><span className="gh-label">All Dx on claim</span><br /><span className="whitespace-pre-line">{f.all_dx}</span></p>
              </div>
              <div className="space-y-2">
                <p className="gh-label">Case sheet</p>
                <pre className="max-h-64 overflow-auto whitespace-pre-wrap rounded border border-slate-200 bg-white p-2 font-sans">{f.case_sheet || "— no case sheet —"}</pre>
                <div className="flex flex-wrap items-center gap-2">
                  <input className="gh-input flex-1" placeholder="Reviewer remark" value={remark} onChange={(e) => setRemark(e.target.value)} />
                  <button className="gh-btn-secondary gh-btn-sm" onClick={() => save("Agree")}>Agree</button>
                  <button className="gh-btn-secondary gh-btn-sm" onClick={() => save("Not needed")}>Not needed</button>
                  {f.review_status && <button className="gh-btn-secondary gh-btn-sm" onClick={() => save("")}>Clear</button>}
                </div>
              </div>
            </div>
          </td>
        </tr>)}
    </>
  );
}

function Findings({ facilityId, month, dash, filters, setFilters }) {
  const [data, setData] = useState(null);
  const [page, setPage] = useState(1);
  useEffect(() => { setPage(1); }, [facilityId, month, filters]);
  useEffect(() => {
    if (!facilityId || !dash?.run) return;
    apiClient.get("/docreview/findings", { params: { facility_id: facilityId, month, page, ...Object.fromEntries(Object.entries(filters).filter(([, v]) => v)) } })
      .then((r) => setData(r.data)).catch(() => setData(null));
  }, [facilityId, month, page, filters, dash]);
  if (!dash?.run) return <div className="gh-card p-4 text-sm text-slate-500">No completed check for this month yet.</div>;
  const set = (k) => (e) => setFilters({ ...filters, [k]: e.target.value });
  const replace = (f) => setData({ ...data, rows: data.rows.map((x) => (x.id === f.id ? f : x)) });
  return (
    <div className="gh-card space-y-3 p-4">
      <div className="flex flex-wrap gap-2">
        <select className="gh-input" value={filters.tier} onChange={set("tier")}>
          <option value="">All tiers</option>{Object.entries(TIERS).map(([t, s]) => <option key={t} value={t}>{s.label}</option>)}
        </select>
        <select className="gh-input" value={filters.category} onChange={set("category")}>
          <option value="">All categories</option>{dash.categories.map((c) => <option key={c.category} value={c.category}>{c.category}</option>)}
        </select>
        <select className="gh-input" value={filters.clinician} onChange={set("clinician")}>
          <option value="">All doctors</option>{dash.doctors.map((d) => <option key={d.clinician_id} value={d.clinician_id}>{d.name || d.clinician_id}</option>)}
        </select>
        <select className="gh-input" value={filters.review} onChange={set("review")}>
          <option value="">Any review</option><option value="pending">Not reviewed</option><option value="Agree">Agree</option><option value="Not needed">Not needed</option>
        </select>
      </div>
      {!data ? <p className="text-sm text-slate-500">Loading…</p> : (
        <>
          <p className="text-xs text-slate-500">{data.total.toLocaleString()} findings · click a row to see the case sheet and review it</p>
          <div className="overflow-auto">
            <table className="w-full text-xs">
              <thead className="sticky top-0 bg-slate-50 text-left text-slate-500">
                <tr>{["Tier", "Claim", "Doctor", "Flagged Dx", "Category / detail", "AED at risk", "Review"].map((h) => <th key={h} className="px-2 py-1 font-medium">{h}</th>)}</tr>
              </thead>
              <tbody>{data.rows.map((f) => <FindingRow key={f.id} f={f} onSaved={replace} />)}</tbody>
            </table>
          </div>
          {data.pages > 1 && (
            <div className="flex items-center gap-2 text-xs">
              <button className="gh-btn-secondary gh-btn-sm" disabled={page <= 1} onClick={() => setPage(page - 1)}>Previous</button>
              <span>Page {page} of {data.pages}</span>
              <button className="gh-btn-secondary gh-btn-sm" disabled={page >= data.pages} onClick={() => setPage(page + 1)}>Next</button>
            </div>)}
        </>)}
    </div>
  );
}

function PayerPatterns({ facilityId, month }) {
  const [rows, setRows] = useState(null);
  useEffect(() => {
    setRows(null);
    apiClient.get("/docreview/payer-patterns", { params: { facility_id: facilityId, month } })
      .then((r) => setRows(r.data)).catch(() => setRows([]));
  }, [facilityId, month]);
  if (!rows) return <div className="gh-card p-4 text-sm text-slate-500">Loading…</div>;
  if (!rows.length) return <div className="gh-card p-4 text-sm text-slate-500">No payer patterns in the scrubbed claims of this month.</div>;
  const table = (list, first) => (
    <div className="overflow-auto">
      <table className="w-full text-xs">
        <thead className="bg-slate-50 text-left text-slate-500">
          <tr>{["Payer", first, "Pattern", "Claims", "Net AED"].map((h) => <th key={h} className="px-2 py-1 font-medium">{h}</th>)}</tr>
        </thead>
        <tbody>{list.map((r) => (
          <tr key={`${r.payer}-${r.code}`} className="border-t border-slate-100 align-top">
            <td className="px-2 py-1 whitespace-nowrap">{r.payer_name || r.payer}<span className="block text-slate-400">{r.payer}</span></td>
            <td className="px-2 py-1 font-mono">{r.code}</td>
            <td className="px-2 py-1">{r.pattern}</td>
            <td className="px-2 py-1 text-right">{r.claims}</td>
            <td className="px-2 py-1 text-right">{aed(r.net)}</td>
          </tr>))}
        </tbody>
      </table>
    </div>
  );
  const denial = rows.filter((r) => r.kind === "denial");
  const licence = rows.filter((r) => r.kind === "licence");
  return (
    <div className="space-y-4">
      <p className="text-xs text-slate-500">
        Patterns found by the Claim scrubber for clinic administration and payer / contract follow-up — not claim queries.
      </p>
      <div className="gh-card space-y-2 p-4">
        <p className="font-semibold text-slate-900">Payer denial patterns ({denial.length})</p>
        {denial.length ? table(denial, "Code") : <p className="text-sm text-slate-500">None this month.</p>}
      </div>
      <div className="gh-card space-y-2 p-4">
        <p className="font-semibold text-slate-900">Clinician licence watch-list ({licence.length})</p>
        <p className="text-xs text-slate-500">Payers that denied this clinician's lines for licence status — confirm the licence is active and registered with the payer.</p>
        {licence.length ? table(licence, "Clinician") : <p className="text-sm text-slate-500">None this month.</p>}
      </div>
    </div>
  );
}

function Downloads({ facilityId, month, fac, dash }) {
  const [err, setErr] = useState("");
  if (!dash?.run) return <div className="gh-card p-4 text-sm text-slate-500">No completed check for this month yet.</div>;
  const slug = (fac?.facility_name || "facility").replace(/[^A-Za-z0-9]+/g, "-");
  const get = (url, params, name) => download(url, { facility_id: facilityId, month, ...params }, name).catch(() => setErr("Download failed."));
  const items = [
    ["Tier A · claim queries", "Unsupported Dx that put billed services at risk, contradictions and missing case sheets.", "/docreview/findings.xlsx", { tier: "A" }, `${slug}_Documentation_TierA_${month}.xlsx`],
    ["Doctor feedback", "One sheet per doctor above the facility average, with examples, for the medical director. Add the pattern and suggested change before sending.", "/docreview/feedback.xlsx", {}, `${slug}_Doctor_feedback_${month}.xlsx`],
    ["Tier C · Ghaf review", "Unusual codes for internal review only.", "/docreview/findings.xlsx", { tier: "C" }, `${slug}_Documentation_TierC_${month}.xlsx`],
    ["All findings", "Every finding with case sheet, query, suggested action and review.", "/docreview/findings.xlsx", {}, `${slug}_Documentation_findings_${month}.xlsx`],
  ];
  return (
    <div className="grid gap-4 md:grid-cols-2">
      {items.map(([t, n, url, p, name]) => (
        <div key={t} className="gh-card flex flex-col justify-between gap-3 p-4">
          <div><p className="font-semibold text-slate-900">{t}</p><p className="text-xs text-slate-500">{n}</p></div>
          <button className="gh-btn self-start" onClick={() => get(url, p, name)}>Download Excel</button>
        </div>))}
      {err && <p className="text-sm text-red-600">{err}</p>}
      <p className="text-xs text-slate-400 md:col-span-2">Files contain case-sheet text (patient data). Share only through the usual secure channel.</p>
    </div>
  );
}

export default function DocReview() {
  const [facilities, setFacilities] = useState([]);
  const [facilityId, setFacilityId] = useState("");
  const [month, setMonth] = useState(lastMonth);
  const [runs, setRuns] = useState([]);
  const [dash, setDash] = useState(null);
  const [tab, setTab] = useState("dashboard");
  const [filters, setFilters] = useState({ tier: "", category: "", clinician: "", review: "" });
  const [msg, setMsg] = useState("");

  useEffect(() => {
    apiClient.get("/facilities").then((r) => setFacilities(r.data.filter((f) => f.facility_code !== "GHAF-001"))).catch(() => {});
  }, []);
  const load = () => {
    if (!facilityId) return;
    apiClient.get("/docreview/runs", { params: { facility_id: facilityId } }).then((r) => setRuns(r.data)).catch(() => {});
    apiClient.get("/docreview/dashboard", { params: { facility_id: facilityId, month } }).then((r) => setDash(r.data)).catch(() => setDash(null));
  };
  useEffect(load, [facilityId, month]);
  const active = runs.some((r) => r.status === "Queued" || r.status === "Running");
  useEffect(() => {
    if (!active) return undefined;
    const t = setInterval(load, 5000);
    return () => clearInterval(t);
  }, [active, facilityId, month]);

  const run = async () => {
    setMsg("");
    try { await apiClient.post("/docreview/runs", { facility_id: Number(facilityId), month }); load(); }
    catch (e) { setMsg(e.response?.data?.detail || "Could not start the check."); }
  };
  const fac = facilities.find((f) => String(f.id) === String(facilityId));
  const latest = runs.find((r) => r.month === month);

  return (
    <div className="space-y-6">
      <div>
        <p className="gh-eyebrow">Claim scrubbing &amp; analysis</p>
        <h1 className="gh-h1">Documentation Review</h1>
        <p className="text-sm text-slate-500">
          Checks that the case sheet supports every coded diagnosis. Tier A goes to claim queries (also raised by the Claim scrubber on
          ERP uploads); Tier B is monthly doctor feedback; Tier C is for Ghaf review only. Coded symptoms that belong to a confirmed
          diagnosis are kept and not queried.
        </p>
      </div>

      <div className="gh-card flex flex-wrap items-center gap-3 p-4">
        <label className="text-sm font-medium text-slate-700">Facility</label>
        <select className="gh-input min-w-[300px]" value={facilityId} onChange={(e) => setFacilityId(e.target.value)}>
          <option value="">Choose a facility…</option>
          {facilities.map((f) => <option key={f.id} value={f.id}>{f.facility_name} (#{f.id})</option>)}
        </select>
        <label className="text-sm font-medium text-slate-700">Treatment month</label>
        <input type="month" className="gh-input" value={month} onChange={(e) => setMonth(e.target.value)} />
        <button className="gh-btn" disabled={!facilityId || active} onClick={run}>{latest?.status === "Completed" ? "Run again" : "Run check"}</button>
        {latest && <span className={RUN_STYLE[latest.status]}>{latest.status}</span>}
        {latest && <span className="text-xs text-slate-500">{latest.error || latest.progress}</span>}
        {msg && <span className="text-sm text-red-600">{msg}</span>}
      </div>

      {facilityId && (
        <>
          <div className="flex gap-2">
            {[["dashboard", "Dashboard"], ["findings", "Findings"], ["patterns", "Payer patterns"], ["downloads", "Downloads"]].map(([k, l]) => (
              <button key={k} className={tab === k ? "gh-btn gh-btn-sm" : "gh-btn-secondary gh-btn-sm"} onClick={() => setTab(k)}>{l}</button>))}
          </div>
          {tab === "dashboard" && <Dashboard dash={dash} onDoctor={(c) => { setFilters({ ...filters, clinician: c }); setTab("findings"); }} />}
          {tab === "findings" && <Findings facilityId={facilityId} month={month} dash={dash} filters={filters} setFilters={setFilters} />}
          {tab === "patterns" && <PayerPatterns facilityId={facilityId} month={month} />}
          {tab === "downloads" && <Downloads facilityId={facilityId} month={month} fac={fac} dash={dash} />}
        </>)}
    </div>
  );
}
