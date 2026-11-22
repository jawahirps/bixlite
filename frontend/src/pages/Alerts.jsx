import { Fragment, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import apiClient from "../api/client";
import DenialDrill from "./DenialDrill";

const TYPES = {
  denial_rate_10: "Denial rate above 10%",
  denial_code_spike: "TPA denial spike (weekly)",
  clinician_pattern: "Clinician: service × diagnosis",
  clinician_rate: "Clinician: denial rate vs peers",
  clinician_consult: "Clinician: consultation rules",
  clinician_tests: "Clinician: tests per visit",
  clinician_unspec: "Clinician: unspecified diagnosis",
  denial_above_usual: "Denial code above usual (month)",
  denial_spike: "Denial rate (monthly)",
  payment_rate: "Payment rate",
  overdue: "Overdue (SN 45 days)",
  submission_gap: "Submissions stopped",
  sn_deadline: "SN 15th deadline",
  credentials: "Login / credentials",
};

const CODE_TYPES = new Set(["denial_code_spike", "denial_above_usual"]);
const PAGE_LABEL = { "/denial-trends": "Denial trends", "/turnaround": "Payer turnaround", "/jobs": "Data sync",
  "/facility-settings": "Facility settings", "/resub-worklist": "Resubmission worklist", "/doc-review": "Documentation review" };
const linkLabel = (link) => `${PAGE_LABEL[(link || "").split("?")[0]] || "Open"} →`;

const fmt = (v, d = 0) => (v == null ? "—" : Number(v).toLocaleString("en-GB", { maximumFractionDigits: d }));
const pct = v => (v == null ? "—" : `${fmt(v, 1)}%`);
const day = iso => (iso ? new Date(iso + "T00:00:00").toLocaleDateString("en-GB", { day: "2-digit", month: "2-digit" }) : "—");
const daysLeft = iso => (iso ? Math.round((new Date(iso + "T00:00:00") - new Date(new Date().toDateString())) / 86400000) : null);

function DueChip({ iso }) {
  const d = daysLeft(iso);
  if (d == null) return null;
  const cls = d < 0 ? "gh-chip-neutral" : d <= 3 ? "gh-chip-denied" : d <= 7 ? "gh-chip-pending" : "gh-chip-brand";
  return <span className={`gh-chip ${cls}`}>{d < 0 ? "window closed" : d === 0 ? "due today" : `due ${day(iso)} · ${d}d`}</span>;
}

async function downloadLines(a, setBusy) {
  const q = new URLSearchParams({ code: a.code, ...(a.payer ? { payer: a.payer } : {}),
    ...(a.facility_id ? { facility_id: a.facility_id } : {}), days: a.type === "reconciliation_due" ? 21 : 31 });
  setBusy(true);
  try {
    const r = await apiClient.get(`/alerts/lines.xlsx?${q}`, { responseType: "blob", timeout: 300000 });
    const name = ((r.headers["content-disposition"] || "").match(/filename="([^"]+)"/) || [])[1] || "alert_lines.xlsx";
    const url = URL.createObjectURL(r.data);
    const el = document.createElement("a"); el.href = url; el.download = name; el.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  } finally { setBusy(false); }
}

function Stat({ label, value, sub, tone }) {
  return (
    <div className={`gh-card p-4 ${tone || ""}`}>
      <p className="gh-label">{label}</p>
      <p className="mt-1 font-display text-[26px] font-semibold leading-8 text-ink tabular-nums">{value}</p>
      {sub && <p className="mt-0.5 text-xs text-slate-500">{sub}</p>}
    </div>
  );
}

const CAUSE = {   // subtle accents in the app's own tones
  repeat: "bg-pending", habit: "bg-pending", payer: "bg-ghaf-600", doc: "bg-ink", unclear: "bg-slate-400",
};

function InsightStrip({ i }) {
  return (
    <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 rounded-md border border-hairline bg-sage/60 px-3 py-1.5"
      title="Likely cause: a rule-based reading of the facts below, not a fact itself">
      <span className="flex items-center gap-1.5 text-xs">
        <span className={`h-1.5 w-1.5 rounded-full ${CAUSE[i.cause] || CAUSE.unclear}`} />
        <span className="text-slate-500">Likely</span>
        <span className="font-semibold text-ink">{i.label}</span>
      </span>
      {i.evidence?.map((e, k) => (
        <span key={k} className="border-l border-hairline pl-4 text-xs text-slate-500">
          <b className="font-semibold tabular-nums text-ink">{e.v}</b> {e.l}
        </span>
      ))}
      <span className="w-full text-xs text-slate-600 sm:ml-auto sm:w-auto">
        <span className="text-ghaf-600">→</span> {i.act}
      </span>
    </div>
  );
}

function ClinicianPanel({ a, onClose }) {
  const [d, setD] = useState(null);
  const [err, setErr] = useState(null);
  const [busy, setBusy] = useState(false);
  const q = new URLSearchParams({ facility_id: a.facility_id, clinician: a.clinician, kind: a.type });
  useEffect(() => {
    apiClient.get(`/alerts/clinician-lines?${q}`, { timeout: 120000 }).then(r => setD(r.data))
      .catch(e => setErr(e.response?.data?.detail || "Could not load the lines"));
  }, []);
  const excel = async () => {
    setBusy(true);
    try {
      const r = await apiClient.get(`/alerts/clinician-lines.xlsx?${q}`, { responseType: "blob", timeout: 300000 });
      const name = ((r.headers["content-disposition"] || "").match(/filename="([^"]+)"/) || [])[1] || "clinician_lines.xlsx";
      const url = URL.createObjectURL(r.data);
      const el = document.createElement("a"); el.href = url; el.download = name; el.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } finally { setBusy(false); }
  };
  const gapH = { clinician_tests: "Days since same test", clinician_consult: "Days since last visit" }[a.type];
  return (
    <div className="fixed inset-0 z-50 flex justify-end bg-ink/30" onClick={onClose}>
      <div className="h-full w-full max-w-4xl overflow-y-auto bg-white p-5 shadow-xl" onClick={e => e.stopPropagation()}>
        <div className="flex items-start justify-between gap-3">
          <div>
            <p className="gh-eyebrow">{TYPES[a.type]} · {a.facility_name}</p>
            <h2 className="font-display text-lg font-semibold text-ink">{a.clinician_name || a.clinician} <span className="text-sm font-normal text-slate-500">{a.clinician}</span></h2>
            <p className="text-xs text-slate-500">Denied lines behind this alert · new denials, last 90 days</p>
          </div>
          <div className="flex gap-1.5">
            <button className="gh-btn-secondary gh-btn-sm" disabled={busy || !d} onClick={excel}>{busy ? "Preparing…" : "Excel"}</button>
            <button className="gh-btn-secondary gh-btn-sm" onClick={onClose}>Close</button>
          </div>
        </div>
        {a.insight && <InsightStrip i={a.insight} />}
        {err && <p className="mt-3 text-sm text-denied">{err}</p>}
        {!d && !err && <p className="mt-4 text-sm text-slate-500">Loading…</p>}
        {d && (
          <>
            <div className="mt-3 flex flex-wrap gap-x-6 gap-y-1 text-sm">
              <span><b className="tabular-nums text-ink">{fmt(d.total)}</b> <span className="text-slate-500">lines</span></span>
              <span><b className="tabular-nums text-ink">AED {fmt(d.amount)}</b> <span className="text-slate-500">denied</span></span>
              {d.gap_bands && Object.entries(d.gap_bands).map(([k, v]) => (
                <span key={k}><b className="tabular-nums text-ink">{fmt(v)}</b> <span className="text-slate-500">{k}</span></span>
              ))}
            </div>
            <table className="mt-3 min-w-full text-xs">
              <thead className="text-left uppercase tracking-wide text-slate-500">
                <tr><th className="py-1 pr-2">Service</th>{a.type !== "clinician_tests" && <th className="py-1 pr-2">Principal Dx</th>}
                  <th className="py-1 pr-2 text-right">Lines</th><th className="py-1 pr-2 text-right">AED</th><th className="py-1">Main code</th></tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {d.groups.map((g, i) => (
                  <tr key={i}><td className="py-1 pr-2 font-mono">{g.activity_code}{g.activity && <span className="ml-1 font-sans text-slate-500">{g.activity}</span>}</td>
                    {a.type !== "clinician_tests" && <td className="py-1 pr-2 font-mono">{g.diagnosis_code}</td>}
                    <td className="py-1 pr-2 text-right tabular-nums">{g.lines}</td><td className="py-1 pr-2 text-right tabular-nums">{fmt(g.amount)}</td>
                    <td className="py-1">{g.top_code}</td></tr>
                ))}
              </tbody>
            </table>
            <h3 className="mt-5 text-xs font-semibold uppercase tracking-wide text-slate-500">Lines {d.total > d.rows.length ? `(largest ${d.rows.length} of ${d.total}; all in Excel)` : ""}</h3>
            <div className="mt-1 overflow-x-auto">
              <table className="min-w-full text-xs">
                <thead className="text-left text-slate-500">
                  <tr><th className="py-1 pr-2">Claim</th><th className="py-1 pr-2">Enc.</th><th className="py-1 pr-2">Service</th>
                    <th className="py-1 pr-2">Dx</th><th className="py-1 pr-2">Payer</th><th className="py-1 pr-2">Code</th>
                    <th className="py-1 pr-2 text-right">AED</th>{gapH && <th className="py-1 pr-2 text-right">{gapH}</th>}<th className="py-1">TPA remark</th></tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {d.rows.map((x, i) => (
                    <tr key={i}><td className="py-1 pr-2 font-mono">{x.claim_id}</td><td className="py-1 pr-2">{day(x.enc)}</td>
                      <td className="py-1 pr-2 font-mono">{x.activity_code}</td><td className="py-1 pr-2 font-mono">{x.dx}</td>
                      <td className="py-1 pr-2">{x.payer}</td><td className="py-1 pr-2">{x.code}</td>
                      <td className="py-1 pr-2 text-right tabular-nums">{fmt(x.amount)}</td>
                      {gapH && <td className="py-1 pr-2 text-right tabular-nums">{x.gap ?? "—"}</td>}
                      <td className="max-w-[260px] truncate py-1" title={x.remark}>{x.remark}</td></tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        )}
      </div>
    </div>
  );
}

function MiniTable({ title, rows, label, extra }) {
  if (!rows?.length) return null;
  return (
    <div>
      <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-500">{title}</h3>
      <table className="mt-1 w-full text-xs">
        <tbody className="divide-y divide-slate-100">
          {rows.map((r, i) => (
            <tr key={i}>
              <td className="py-1 pr-2"><span className="font-mono">{r.key || "—"}</span>
                {label(r) && <span className="block max-w-[220px] truncate text-slate-500" title={label(r)}>{label(r)}</span>}</td>
              {extra && <td className="py-1 pr-2 text-slate-500">{extra(r)}</td>}
              <td className="py-1 text-right font-semibold tabular-nums text-ink">{r.share_pct}%</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function FacilityDenialPanel({ a, onClose }) {
  const [d, setD] = useState(null);
  const [err, setErr] = useState(null);
  const [busy, setBusy] = useState(false);
  const q = new URLSearchParams({ facility_id: a.facility_id, month: a.month });
  useEffect(() => {
    apiClient.get(`/alerts/facility-denials?${q}`, { timeout: 120000 }).then(r => setD(r.data))
      .catch(e => setErr(e.response?.data?.detail || "Could not load the lines"));
  }, []);
  const excel = async () => {
    setBusy(true);
    try {
      const r = await apiClient.get(`/alerts/facility-denials.xlsx?${q}`, { responseType: "blob", timeout: 300000 });
      const name = ((r.headers["content-disposition"] || "").match(/filename="([^"]+)"/) || [])[1] || "facility_unpaid.xlsx";
      const url = URL.createObjectURL(r.data);
      const el = document.createElement("a"); el.href = url; el.download = name; el.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } finally { setBusy(false); }
  };
  const mon = new Date(a.month + "-01T00:00:00").toLocaleDateString("en-GB", { month: "short", year: "numeric" });
  const stageCls = { "Final": "text-denied", "Resubmission awaiting RA": "text-pending", "Still in window": "text-ghaf-600" };
  return (
    <div className="fixed inset-0 z-50 flex justify-end bg-ink/30" onClick={onClose}>
      <div className="h-full w-full max-w-4xl overflow-y-auto bg-white p-5 shadow-xl" onClick={e => e.stopPropagation()}>
        <div className="flex items-start justify-between gap-3">
          <div>
            <p className="gh-eyebrow">Denial rate above 10% · {a.facility_name}</p>
            <h2 className="font-display text-lg font-semibold text-ink">{a.rate_pct}% <span className="text-sm font-normal text-slate-500">{mon} encounters, settled</span></h2>
          </div>
          <div className="flex gap-1.5">
            <button className="gh-btn-secondary gh-btn-sm" disabled={busy || !d} onClick={excel}>{busy ? "Preparing…" : "Excel"}</button>
            <button className="gh-btn-secondary gh-btn-sm" onClick={onClose}>Close</button>
          </div>
        </div>
        {a.insight && <InsightStrip i={a.insight} />}
        {err && <p className="mt-3 text-sm text-denied">{err}</p>}
        {!d && !err && <p className="mt-4 text-sm text-slate-500">Loading…</p>}
        {d && (
          <>
            {d.stages?.length > 0 && (
              <p className="mt-3 flex flex-wrap gap-x-5 text-xs text-slate-500">
                {d.stages.map(s => <span key={s.stage}><b className={`tabular-nums ${stageCls[s.stage] || "text-ink"}`}>{s.share_pct}%</b> {s.stage.toLowerCase()}</span>)}
              </p>
            )}
            <div className="mt-4 grid gap-5 sm:grid-cols-2">
              <MiniTable title="Where: denial code" rows={d.codes} label={r => r.description} extra={r => r.who} />
              <MiniTable title="Payer" rows={d.payers} label={r => r.name} />
              <MiniTable title="Clinician" rows={d.clinicians} label={r => r.name} />
              <MiniTable title="Service" rows={d.services} label={() => ""} />
            </div>
            <h3 className="mt-5 text-xs font-semibold uppercase tracking-wide text-slate-500">Lines {d.total > d.rows.length ? `(largest ${d.rows.length} of ${d.total}; all in Excel)` : ""}</h3>
            <div className="mt-1 overflow-x-auto">
              <table className="min-w-full text-xs">
                <thead className="text-left text-slate-500">
                  <tr><th className="py-1 pr-2">Claim</th><th className="py-1 pr-2">Enc.</th><th className="py-1 pr-2">Service</th>
                    <th className="py-1 pr-2">Dx</th><th className="py-1 pr-2">Payer</th><th className="py-1 pr-2">Code</th>
                    <th className="py-1 pr-2 text-right">AED</th><th className="py-1 pr-2">Stage</th><th className="py-1">TPA remark</th></tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {d.rows.map((x, i) => (
                    <tr key={i}><td className="py-1 pr-2 font-mono">{x.claim_id}</td><td className="py-1 pr-2">{day(x.enc)}</td>
                      <td className="py-1 pr-2 font-mono">{x.activity_code}</td><td className="py-1 pr-2 font-mono">{x.dx}</td>
                      <td className="py-1 pr-2">{x.payer}</td><td className="py-1 pr-2">{x.code}</td>
                      <td className="py-1 pr-2 text-right tabular-nums">{fmt(x.amount)}</td>
                      <td className={`py-1 pr-2 ${stageCls[x.stage] || ""}`}>{x.stage === "Still in window" && x.due ? `Resubmit by ${day(x.due)}` : x.stage}</td>
                      <td className="max-w-[240px] truncate py-1" title={x.remark}>{x.remark}</td></tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        )}
      </div>
    </div>
  );
}

// several clinicians flagged by the same check at one facility -> one compact card, a row per clinician
function keyFigure(a) {
  const st = a.stats || {};
  if (a.type === "clinician_pattern" && a.patterns?.[0]) {
    const p = a.patterns[0];
    return `${p.activity_code} + ${p.diagnosis_code}: ${p.rate_pct}%${p.others_rate_pct != null ? ` vs ${p.others_rate_pct}%` : ""}`
      + (a.patterns.length > 1 ? ` (+${a.patterns.length - 1})` : "");
  }
  if (a.type === "clinician_tests") return `${st.tests_per_visit} vs ${st.peers_tests_per_visit} tests / visit`;
  if (a.type === "clinician_unspec") return `${st.share_pct}% vs ${st.peers_share_pct}% unspecified`;
  if (st.rate_pct != null) return `${st.rate_pct}% vs ${st.peers_rate_pct}% denied`;
  return "";
}

function ClinicianGroup({ items, onWhy, onClin }) {
  const [open, setOpen] = useState(null);
  const a0 = items[0];
  const high = items.some(a => a.severity === "high");
  return (
    <li className="py-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className={`gh-chip ${high ? "gh-chip-denied" : "gh-chip-pending"}`}>{high ? "High" : "Watch"}</span>
        <span className="text-xs text-slate-500">Clinician denial patterns</span>
      </div>
      <p className="mt-1 text-sm font-medium text-ink">{new Set(items.map(a => a.clinician)).size} clinician{new Set(items.map(a => a.clinician)).size > 1 ? "s" : ""} flagged · {items.length} finding{items.length > 1 ? "s" : ""}</p>
      <div className="mt-2 overflow-x-auto">
        <table className="min-w-full text-xs">
          <thead className="text-left uppercase tracking-wide text-slate-500">
            <tr><th className="py-1 pr-2">Check</th><th className="py-1 pr-2">Clinician</th><th className="py-1 pr-2">Key figure</th><th className="py-1 pr-2">Likely</th>
              <th className="py-1 pr-2 text-right">AED</th><th className="py-1" /></tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {items.map((a, i) => (
              <Fragment key={i}>
                <tr className="align-top">
                  <td className="py-1.5 pr-2 text-slate-500">{i === 0 || items[i - 1].type !== a.type ? (TYPES[a.type] || a.type).replace("Clinician: ", "") : ""}</td>
                  <td className="py-1.5 pr-2"><span className="font-medium text-ink">{a.clinician_name ? titleCase(a.clinician_name) : a.clinician}</span>
                    <span className="block text-slate-400">{a.clinician}</span></td>
                  <td className="py-1.5 pr-2 tabular-nums text-ink">{keyFigure(a)}</td>
                  <td className="py-1.5 pr-2">
                    {a.insight && <span className="flex items-center gap-1.5">
                      <span className={`h-1.5 w-1.5 rounded-full ${CAUSE[a.insight.cause] || CAUSE.unclear}`} />{a.insight.label}</span>}
                  </td>
                  <td className="py-1.5 pr-2 text-right tabular-nums">{fmt(a.value)}</td>
                  <td className="py-1.5 text-right whitespace-nowrap">
                    <button className="gh-btn-secondary gh-btn-sm" onClick={() => onClin(a)}>Clinician's denials</button>
                    <button className="ml-1 px-1 text-ghaf-600 hover:underline" onClick={() => setOpen(open === i ? null : i)}>
                      {open === i ? "Hide" : "Details"}</button>
                  </td>
                </tr>
                {open === i && (
                  <tr><td colSpan={6} className="bg-sage/40 px-2"><ul><AlertRow a={a} onWhy={onWhy} onClin={onClin} /></ul></td></tr>
                )}
              </Fragment>
            ))}
          </tbody>
        </table>
      </div>
    </li>
  );
}

const titleCase = (t) => t.toLowerCase().replace(/\b\w/g, c => c.toUpperCase());

function groupRows(arr) {
  const out = [], seen = {};
  for (const a of arr) {
    if (a.type?.startsWith("clinician_")) {
      if (seen.clin == null) { seen.clin = out.length; out.push({ type: "clinicians", items: [a] }); }
      else out[seen.clin].items.push(a);
    } else out.push({ type: a.type, items: [a] });
  }
  const order = ["clinician_pattern", "clinician_rate", "clinician_tests", "clinician_consult", "clinician_unspec"];
  for (const g of out) if (g.type === "clinicians") g.items.sort((x, y) => order.indexOf(x.type) - order.indexOf(y.type) || (y.value || 0) - (x.value || 0));
  // facility-level alerts first (high before watch), the clinicians card last
  out.sort((x, y) => (x.type === "clinicians") - (y.type === "clinicians")
    || (x.items[0].severity === "high" ? 0 : 1) - (y.items[0].severity === "high" ? 0 : 1));
  return out;
}

// the same payer + code spiking at several facilities this week -> one card above the facilities
function SpikeGroup({ items, onWhy }) {
  const [busy, setBusy] = useState(null);
  const a0 = items[0];
  const tot = items.reduce((t, a) => t + (a.value || 0), 0);
  return (
    <section className="gh-card border-l-4 border-l-denied p-4">
      <div className="flex flex-wrap items-center gap-2">
        <span className="gh-chip gh-chip-denied">High</span>
        <span className="text-xs text-slate-500">TPA denial spike (weekly) · {items.length} facilities</span>
      </div>
      <p className="mt-1 text-sm font-medium text-ink">{(a0.title || "").split(":")[0]}: {a0.code} denials, {fmt(tot)} lines in 7 days across {items.length} facilities</p>
      <table className="mt-2 min-w-full text-xs">
        <thead className="text-left uppercase tracking-wide text-slate-500">
          <tr><th className="py-1 pr-2">Facility</th><th className="py-1 pr-2">This week</th><th className="py-1 pr-2">Stage / due</th><th className="py-1" /></tr>
        </thead>
        <tbody className="divide-y divide-slate-100">
          {items.map((a, i) => (
            <tr key={i}>
              <td className="py-1.5 pr-2 font-medium text-ink">{a.facility_name}</td>
              <td className="py-1.5 pr-2 tabular-nums">{(a.title.match(/\d[\d,]* lines in 7 days \([^)]*\)/) || [""])[0]}</td>
              <td className="py-1.5 pr-2">{a.split?.new_due ? <DueChip iso={a.split.new_due} /> : ""}</td>
              <td className="py-1.5 text-right whitespace-nowrap">
                <button className="gh-btn-secondary gh-btn-sm" onClick={() => onWhy(a)}>Code breakdown</button>
                <button className="ml-1 gh-btn-secondary gh-btn-sm" disabled={busy === i}
                  onClick={async () => { setBusy(i); try { await downloadLines(a, () => {}); } finally { setBusy(null); } }}>
                  {busy === i ? "Preparing…" : "Code lines (Excel)"}</button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="mt-2 text-xs text-slate-500">The same payer and code at several facilities points to a payer-side change: check the payer's circular / rule before correcting claims one by one.</p>
    </section>
  );
}

function AlertRow({ a, onWhy, onClin }) {
  const [busy, setBusy] = useState(false);
  const s = a.split;
  return (
    <li className="py-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className={`gh-chip ${a.severity === "high" ? "gh-chip-denied" : a.severity === "info" ? "gh-chip-brand" : "gh-chip-pending"}`}>
          {a.severity === "high" ? "High" : a.severity === "info" ? "Notify facility" : "Watch"}
        </span>
        <span className="text-xs text-slate-500">{TYPES[a.type] || a.type}</span>
      </div>
      <p className="mt-1 text-sm font-medium text-ink">{a.title}</p>
      {a.insight && <InsightStrip i={a.insight} />}
      {s && s.new_lines > 0 && (
        <div className="mt-2 grid gap-1.5 text-sm">
          {s.new_lines > 0 && (
            <div className="flex flex-wrap items-center gap-2">
              <span className="w-44 text-slate-600">New denials → <b>resubmit</b></span>
              <span className="tabular-nums">{fmt(s.new_lines)} lines · AED {fmt(s.new_amt)}</span>
              <DueChip iso={s.new_due} />
            </div>
          )}
        </div>
      )}
      {a.patterns?.length > 0 && (
        <div className="mt-2 overflow-x-auto">
          <table className="min-w-full text-xs">
            <thead className="text-left uppercase tracking-wide text-slate-500">
              <tr><th className="py-1 pr-2">Service</th><th className="py-1 pr-2">Principal Dx</th>
                <th className="py-1 pr-2 text-right">Denied</th><th className="py-1 pr-2 text-right">Rate</th>
                <th className="py-1 pr-2 text-right">Others</th><th className="py-1 pr-2 text-right">AED</th>
                <th className="py-1">Payer · code</th></tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {a.patterns.map((p, i) => (
                <tr key={i}>
                  <td className="py-1 pr-2 font-mono">{p.activity_code}{p.activity && <span className="block max-w-[160px] truncate font-sans text-slate-500">{p.activity}</span>}</td>
                  <td className="py-1 pr-2 font-mono">{p.diagnosis_code}</td>
                  <td className="py-1 pr-2 text-right tabular-nums">{p.denied} / {p.lines}</td>
                  <td className="py-1 pr-2 text-right font-semibold tabular-nums text-denied">{p.rate_pct}%</td>
                  <td className="py-1 pr-2 text-right tabular-nums text-slate-500" title={`${p.others_lines} lines by other clinicians`}>
                    {p.others_rate_pct == null ? "rarely billed" : `${p.others_rate_pct}%`}</td>
                  <td className="py-1 pr-2 text-right tabular-nums">{fmt(p.amount)}</td>
                  <td className="py-1">{p.payer}{p.denial_code ? ` · ${p.denial_code}` : ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <p className="mt-1 text-xs text-slate-500">{a.detail}</p>
      {a.stats?.top_cpt?.length > 0 && (
        <p className="mt-1 text-xs text-slate-700"><b>Most denied tests:</b> {a.stats.top_cpt.map(t =>
          `${t.code}${t.description ? " " + t.description : ""} (${t.denied} lines, AED ${fmt(t.amount)})`).join(" · ")}</p>
      )}
      {a.facts?.length > 0 && (
        <ul className="mt-1 space-y-0.5 text-xs text-slate-600">
          {a.facts.map((f, i) => <li key={i}>• {f}</li>)}
        </ul>
      )}
      <div className="mt-2 flex flex-wrap items-center gap-1.5">
        {/* code-level alerts: drill into the code, its lines, then the trend */}
        {CODE_TYPES.has(a.type) && a.code && <>
          <button className="gh-btn-secondary gh-btn-sm" onClick={() => onWhy(a)} title="Patterns, TPA remarks and clinicians for this denial code">Code breakdown</button>
          <button className="gh-btn-secondary gh-btn-sm" disabled={busy} onClick={() => downloadLines(a, setBusy)}>
            {busy ? "Preparing…" : "Code lines (Excel)"}</button>
        </>}
        {a.clinician ? (
          a.insight?.cause === "doc" && a.insight?.doc_month ? (
            <>
              <button className="gh-btn-secondary gh-btn-sm" onClick={() => onClin(a)} title="This clinician's denied lines behind the alert">Clinician's denials</button>
              <Link className="gh-btn-secondary gh-btn-sm"
                to={`/doc-review?facility_id=${a.facility_id}&month=${a.insight.doc_month}&clinician=${encodeURIComponent(a.clinician)}`}>
                Documentation review →</Link>
            </>
          ) : <button className="gh-btn-secondary gh-btn-sm" onClick={() => onClin(a)} title="This clinician's denied lines behind the alert">Clinician's denials</button>
        ) : a.type === "denial_rate_10" ? (
          <>
            <button className="gh-btn-secondary gh-btn-sm" onClick={() => onClin(a)} title="Where this facility's unpaid amount sits: code, payer, clinician, service">Facility breakdown</button>
            <Link to={`/denial-trends?facility_id=${a.facility_id}`} className="px-1 py-1 text-xs text-ghaf-600 hover:underline">Denial trends →</Link>
          </>
        ) : <Link to={a.link} className="gh-btn-secondary gh-btn-sm">{linkLabel(a.link)}</Link>}
      </div>
    </li>
  );
}

export default function Alerts() {
  const [data, setData] = useState(null);
  const [err, setErr] = useState(null);
  const [loading, setLoading] = useState(false);
  const [type, setType] = useState("");
  const [fac, setFac] = useState("");
  const [week, setWeek] = useState(false);
  const [drill, setDrill] = useState(null);
  const [clin, setClin] = useState(null);
  const [showBase, setShowBase] = useState(false);

  const load = (refresh = false) => {
    setLoading(true);
    apiClient.get(`/alerts${refresh ? "?refresh=true" : ""}`, { timeout: 120000 })
      .then(r => { setData(r.data); setErr(null); })
      .catch(e => setErr(e.response?.data?.detail || "Could not load alerts"))
      .finally(() => setLoading(false));
  };
  useEffect(() => { load(); }, []);

  const all = data?.items || [];
  const dueSoon = a => { const x = daysLeft(a.split?.new_due); return x != null && x >= 0 && x <= 7; };
  const items = all.filter(a => (!type || a.type === type) && (!fac || a.facility_name === fac) && (!week || dueSoon(a)));
  const counts = all.reduce((m, a) => ({ ...m, [a.type]: (m[a.type] || 0) + 1 }), {});

  const summary = useMemo(() => {
    let wkLines = 0, wkAmt = 0, reLines = 0, reAmt = 0;
    for (const a of all) {
      const s = a.split; if (!s) continue;
      const nd = daysLeft(s.new_due);
      if (nd != null && nd >= 0 && nd <= 7) { wkLines += s.new_lines; wkAmt += s.new_amt; }
      reLines += s.re_lines; reAmt += s.re_amt;
    }
    return { wkLines, wkAmt, reLines, reAmt };
  }, [all]);

  const spikeGroups = useMemo(() => {
    const m = {};
    for (const a of items) if (a.type === "denial_code_spike" && a.facility_id) (m[`${a.payer}|${a.code}`] ||= []).push(a);
    return Object.values(m).filter(v => v.length > 1);
  }, [items]);
  const groups = useMemo(() => {
    const shared = new Set(spikeGroups.flat());
    const g = {};
    for (const a of items) if (!shared.has(a)) (g[a.facility_name] ||= []).push(a);
    const rank = arr => (arr.some(a => a.severity === "high") ? 0 : arr.some(a => a.severity === "medium") ? 1 : 2);
    return Object.entries(g).sort((x, y) => rank(x[1]) - rank(y[1]) || y[1].length - x[1].length);
  }, [items, spikeGroups]);
  const facilities = [...new Set(all.map(a => a.facility_name))].sort();

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <p className="gh-eyebrow">Overview</p>
          <h1 className="gh-h1">Facility alerts</h1>
          <p className="mt-1 max-w-3xl text-sm text-slate-500">
            What needs action at each facility: denial spikes to resubmit within 21 days, final denial above 10%,
            clinician denial patterns, overdue RAs and missed submissions, with who should act. Checked hourly
            against each facility's own usual levels and DHA SN 03/2019.
          </p>
        </div>
        <button className="gh-btn-secondary text-sm" onClick={() => load(true)} disabled={loading}>
          {loading ? "Checking…" : "Re-check now"}
        </button>
      </div>

      {err && <p className="text-sm text-denied">{err}</p>}
      {!data && !err && <p className="text-sm text-slate-500">Checking all facilities…</p>}

      {data && (
        <>
          <div className="grid gap-3 sm:grid-cols-2">
            <Stat label="Needs action" value={fmt(data.count)} sub={`${fmt(data.high)} high priority`}
              tone={data.high ? "border-l-4 border-l-denied" : ""} />
            <Stat label="Resubmission deadlines in 7 days" value={`${fmt(summary.wkLines)} lines`} sub={`AED ${fmt(summary.wkAmt)} new denials to resubmit (RA + 21 days)`}
              tone={summary.wkLines ? "border-l-4 border-l-pending" : ""} />
          </div>

          <details className="gh-card p-4 text-sm text-slate-700">
            <summary className="cursor-pointer font-medium text-ink">How denials are handled (DHA SN 03/2019)</summary>
            <ul className="mt-2 list-disc space-y-1 pl-5">
              <li><b>New denial</b> (first remittance): resubmit with the correction or supporting document within
                <b> 21 calendar days</b> of the RA (3.8.1). A late resubmission can attract a 0.03% per day delay fee (3.8.4).</li>
              <li>A claim can be resubmitted <b>at most twice</b>: the resubmission and the reconciliation, which is the 2nd
                and last one (3.8.2).</li>
              <li><b>Re-denied after resubmission</b>: the next step is <b>reconciliation</b>. It is allowed only after the
                facility and the insurer / TPA have discussed the claims and agreed the outcome (3.10.1), so
                <b> only the facility can take it up with the insurance company — notify them</b>.</li>
              <li>Reconciliation has <b>no 21-day limit</b> (3.10.4). Insurers and providers must hold reconciliation at least
                twice a year (3.10.3), and a claim stays valid for 24 months from submission (3.10.5).</li>
              <li>Agreed claims are then resubmitted as type <b>Reconciliation</b> (3.10.2).</li>
            </ul>
          </details>

          <div className="flex flex-wrap items-center gap-2">
            <select className="gh-input py-1.5 text-sm" value={fac} onChange={e => setFac(e.target.value)}>
              <option value="">All facilities</option>
              {facilities.map(f => <option key={f}>{f}</option>)}
            </select>
            <button onClick={() => setType("")} className={`gh-chip ${type === "" ? "gh-chip-brand" : "gh-chip-neutral"}`}>
              All ({data.count})
            </button>
            {Object.entries(TYPES).filter(([k]) => counts[k]).map(([k, label]) => (
              <button key={k} onClick={() => setType(k)} className={`gh-chip ${type === k ? "gh-chip-brand" : "gh-chip-neutral"}`}>
                {label} ({counts[k]})
              </button>
            ))}
            <label className="ml-1 flex items-center gap-1.5 text-sm text-slate-600">
              <input type="checkbox" checked={week} onChange={e => setWeek(e.target.checked)} /> Due in 7 days
            </label>
          </div>

          {groups.length === 0 ? (
            <section className="gh-section"><p className="text-sm text-slate-600">No alerts for this selection.</p></section>
          ) : (
            <div className="grid gap-4 xl:grid-cols-2">
              {spikeGroups.map((sg, i) => <div key={`sg${i}`} className="xl:col-span-2"><SpikeGroup items={sg} onWhy={x => setDrill(x)} /></div>)}
              {groups.map(([name, arr]) => {
                const high = arr.some(a => a.severity === "high");
                const watch = arr.some(a => a.severity === "medium");
                return (
                  <section key={name} className={`gh-card border-l-4 p-4 ${high ? "border-l-denied" : watch ? "border-l-pending" : "border-l-ghaf-600"}`}>
                    <div className="flex items-center justify-between gap-2">
                      <h2 className="gh-section-title">{name}</h2>
                      <span className="text-xs text-slate-500">{groupRows(arr).length} card{groupRows(arr).length > 1 ? "s" : ""}</span>
                    </div>
                    <ul className="divide-y divide-slate-100">
                      {groupRows(arr).map((g, i) => g.items.length > 1
                        ? <ClinicianGroup key={i} items={g.items} onWhy={x => setDrill(x)} onClin={x => setClin(x)} />
                        : <AlertRow key={i} a={g.items[0]} onWhy={x => setDrill(x)} onClin={x => setClin(x)} />)}
                    </ul>
                  </section>
                );
              })}
            </div>
          )}
          <p className="text-xs text-slate-400">
            Checked {new Date(data.generated_at).toLocaleString("en-GB")} · refreshed hourly
          </p>

          <section className="gh-section">
            <button className="flex w-full items-center justify-between text-left" onClick={() => setShowBase(v => !v)}>
              <h2 className="gh-section-title">Learned defaults per facility</h2>
              <span className="text-sm text-slate-500">{showBase ? "Hide" : "Show"}</span>
            </button>
            {showBase && (
              <>
                <p className="mb-3 mt-1 text-sm text-slate-500">
                  What counts as normal for each facility, learned from its last 3–6 months. An alert fires when the
                  latest value crosses the threshold.
                </p>
                <div className="overflow-x-auto">
                  <table className="w-full text-sm">
                    <thead className="text-left text-xs uppercase tracking-wide text-slate-500">
                      <tr>
                        <th className="py-2 pr-3">Facility</th><th className="py-2 pr-3">Submits (first submissions)</th>
                        <th className="py-2 pr-3">Alert if no new claims</th><th className="py-2 pr-3">Last month's services by now</th>
                        <th className="py-2 pr-3">First-RA denial, last 30 days (usual → alert)</th><th className="py-2 pr-3" title="Unpaid after resubmissions, latest settled encounter month; limit 10%">Final denial (settled month)</th><th className="py-2 pr-3">RA ≤ 45 days (usual → alert)</th>
                        <th className="py-2 pr-3">Paid at 1st RA (usual → alert)</th><th className="py-2">No RA &gt; 45 d</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-slate-100">
                      {data.baselines.map(b => (
                        <tr key={b.facility_id}>
                          <td className="py-2 pr-3 font-medium">{b.facility_name}</td>
                          <td className="py-2 pr-3">{b.submission_pattern || "—"}</td>
                          <td className="py-2 pr-3">{b.next_batch
                            ? `next batch ~${day(b.next_batch)}, alert after ${day(b.alert_after)} (last ${b.days_since_submission}d ago)`
                            : b.submission_gap_allowed ? `${b.submission_gap_allowed} days (now ${b.days_since_submission})` : "—"}</td>
                          <td className="py-2 pr-3">{b.sn_expected ? `${fmt(b.sn_submitted)} of ~${fmt(b.sn_expected)}` : "—"}</td>
                          <td className="py-2 pr-3">{b.denial_rate_baseline != null ? `${pct(b.denial_rate_baseline)} → ${pct(b.denial_rate_threshold)} (now ${pct(b.denial_rate_last30)})` : "—"}</td>
                          <td className="py-2 pr-3">{b.final_denial_pct != null ? <span>
                            <b className={`tabular-nums ${b.final_denial_pct > 10 ? "text-denied" : "text-ink"}`}>{pct(b.final_denial_pct)}</b>
                            <span className="text-slate-500"> {new Date(b.final_denial_month + "-01T00:00:00").toLocaleDateString("en-GB", { month: "short" })}</span></span> : "—"}</td>
                          <td className="py-2 pr-3">{b.ra45_baseline != null ? `${pct(b.ra45_baseline)} → ${pct(b.ra45_threshold)} (${b.ra45_latest_month}: ${pct(b.ra45_latest)})` : "—"}</td>
                          <td className="py-2 pr-3">{b.paid_pct_baseline != null ? `${pct(b.paid_pct_baseline)} → ${pct(b.paid_pct_threshold)} (now ${pct(b.paid_pct_latest)})` : "—"}</td>
                          <td className="py-2">{b.overdue_claims != null ? `${fmt(b.overdue_claims)} · AED ${fmt(b.overdue_amount)}` : "—"}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </>
            )}
          </section>
        </>
      )}
      {clin && (clin.type === "denial_rate_10" ? <FacilityDenialPanel a={clin} onClose={() => setClin(null)} /> : <ClinicianPanel a={clin} onClose={() => setClin(null)} />)}
      {drill && <DenialDrill code={drill.code} payer={drill.payer} facilityId={drill.facility_id || ""}
        month={new Date().toISOString().slice(0, 7)} onClose={() => setDrill(null)} />}
    </div>
  );
}
