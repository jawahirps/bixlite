import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import apiClient from "../api/client";
import DenialDrill from "./DenialDrill";

const TYPES = {
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

function AlertRow({ a, onWhy }) {
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
      <div className="mt-2 flex flex-wrap gap-1.5">
        {a.code && <button className="gh-btn-secondary gh-btn-sm" onClick={() => onWhy(a)}>Why?</button>}
        {a.code && <button className="gh-btn-secondary gh-btn-sm" disabled={busy} onClick={() => downloadLines(a, setBusy)}>
          {busy ? "Preparing…" : "Excel"}</button>}
        <Link to={a.link} className="gh-btn-secondary gh-btn-sm">Open →</Link>
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

  const groups = useMemo(() => {
    const g = {};
    for (const a of items) (g[a.facility_name] ||= []).push(a);
    const rank = arr => (arr.some(a => a.severity === "high") ? 0 : arr.some(a => a.severity === "medium") ? 1 : 2);
    return Object.entries(g).sort((x, y) => rank(x[1]) - rank(y[1]) || y[1].length - x[1].length);
  }, [items]);
  const facilities = [...new Set(all.map(a => a.facility_name))].sort();

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <p className="gh-eyebrow">Overview</p>
          <h1 className="gh-h1">Facility alerts</h1>
          <p className="mt-1 max-w-3xl text-sm text-slate-500">
            Problems worth acting on, per facility. Denials are split by stage, following DHA SN 03/2019.
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
              {groups.map(([name, arr]) => {
                const high = arr.some(a => a.severity === "high");
                const watch = arr.some(a => a.severity === "medium");
                return (
                  <section key={name} className={`gh-card border-l-4 p-4 ${high ? "border-l-denied" : watch ? "border-l-pending" : "border-l-ghaf-600"}`}>
                    <div className="flex items-center justify-between gap-2">
                      <h2 className="gh-section-title">{name}</h2>
                      <span className="text-xs text-slate-500">{arr.length} alert{arr.length > 1 ? "s" : ""}</span>
                    </div>
                    <ul className="divide-y divide-slate-100">
                      {arr.map((a, i) => <AlertRow key={i} a={a} onWhy={x => setDrill(x)} />)}
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
                        <th className="py-2 pr-3">Denial rate (usual → alert)</th><th className="py-2 pr-3">RA ≤ 45 days (usual → alert)</th>
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
      {drill && <DenialDrill code={drill.code} payer={drill.payer} facilityId={drill.facility_id || ""}
        month={new Date().toISOString().slice(0, 7)} onClose={() => setDrill(null)} />}
    </div>
  );
}
