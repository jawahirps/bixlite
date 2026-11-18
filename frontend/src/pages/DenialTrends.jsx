/**
 * DenialTrends.jsx — monthly denial trends per payer + scrubber rule coverage ("rule gaps").
 *
 * Complements Denial Intelligence (Calibration.jsx: all-time top codes, rule precision, recall queue).
 * API: GET /api/denial-trends/monthly, GET /api/denial-trends/rule-coverage
 */
import { Suspense, lazy, useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import apiClient from "../api/client";
import DenialDrill from "./DenialDrill";
import { useAuth } from "../context/AuthContext";
import { readFacility, saveFacility } from "../utils/facility";

const TrendChart = lazy(() =>
  import("recharts").then(({ BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer }) => ({
    default: ({ data }) => (
      <ResponsiveContainer width="100%" height={180}>
        <BarChart data={data} margin={{ top: 8, right: 8, left: 0, bottom: 0 }}>
          <XAxis dataKey="month" tick={{ fontSize: 11 }} />
          <YAxis tick={{ fontSize: 11 }} width={48} />
          <Tooltip formatter={(v, n) => [n === "amount" ? `AED ${fmt(v)}` : Number(v).toLocaleString(), n === "amount" ? "Denied amount" : "Denied lines"]} />
          <Bar dataKey="lines" fill="#0f766e" radius={[3, 3, 0, 0]} />
        </BarChart>
      </ResponsiveContainer>
    ),
  }))
);

const fmt = n => (n == null ? "—" : Number(n).toLocaleString("en-AE", { maximumFractionDigits: 0 }));
const monthLabel = m => {
  if (!m) return "";
  const [y, mo] = m.split("-");
  return new Date(Number(y), Number(mo) - 1, 1).toLocaleString("en-GB", { month: "short", year: "numeric" });
};

function Trend({ pct, diff }) {
  if (pct == null) return <span className="gh-chip gh-chip-neutral">new</span>;
  if (Math.abs(pct) < 5) return <span className="text-xs text-slate-500">→ {pct}%</span>;
  const up = pct > 0;   // more denials = bad
  return (
    <span className={`text-xs font-semibold ${up ? "text-red-600" : "text-emerald-600"}`}>
      {up ? "▲" : "▼"} {Math.abs(pct)}%{diff != null ? ` (${diff > 0 ? "+" : ""}${fmt(diff)})` : ""}
    </span>
  );
}

function WhyBtn({ onClick }) {
  return (
    <button onClick={onClick} title="See the TPA reasons, services, clinicians and diagnoses behind this code"
      className="ml-1 rounded-full border border-ghaf-600/40 px-2 py-0.5 text-[11px] font-semibold text-ghaf-600 hover:bg-ghaf-600 hover:text-white">
      Why?
    </button>
  );
}

function Coverage({ c }) {
  if (c === "exact") return <span className="gh-chip gh-chip-paid">rule</span>;
  if (c === "family") return <span className="gh-chip gh-chip-pending">family only</span>;
  return <span className="gh-chip gh-chip-denied">no rule</span>;
}

export default function DenialTrends() {
  const { user } = useAuth();
  const isAdmin = user?.role === "admin";
  const [facilities, setFacilities] = useState([]);
  const [params] = useSearchParams();
  // a link (e.g. from Facility alerts) can name the facility: ?facility_id=7; otherwise the last one used
  const [facilityId, setFacilityId] = useState(() => (isAdmin ? (params.get("facility_id") ?? readFacility("")) : String(user?.facility_id || "")));
  const [payer, setPayer] = useState(() => params.get("payer") || "");
  const [months, setMonths] = useState(6);
  const [month, setMonth] = useState(() => params.get("month") || "");
  const [exporting, setExporting] = useState(false);
  const [drill, setDrill] = useState(null);
  const exportXlsx = async () => {
    const q = new URLSearchParams({ months: String(months) });
    if (facilityId) q.set("facility_id", facilityId);
    if (payer) q.set("payer", payer);
    setExporting(true);
    try {
      const r = await apiClient.get(`/denial-trends/export.xlsx?${q}`, { responseType: "blob", timeout: 300000 });
      const name = ((r.headers["content-disposition"] || "").match(/filename="([^"]+)"/) || [])[1] || "denial_trends.xlsx";
      const url = URL.createObjectURL(r.data);
      const a = document.createElement("a");
      a.href = url; a.download = name; a.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } finally { setExporting(false); }
  };
  const [monthly, setMonthly] = useState(null);
  const [coverage, setCoverage] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [gapsOnly, setGapsOnly] = useState(true);

  useEffect(() => {
    if (!isAdmin) return;
    apiClient.get("/facilities")
      .then(r => setFacilities((r.data || []).filter(f => f.facility_code !== "GHAF-001")))
      .catch(() => {});
  }, [isAdmin]);

  useEffect(() => {
    const params = { months };
    if (facilityId) params.facility_id = facilityId;
    if (payer) params.payer = payer;
    setLoading(true); setError("");
    Promise.all([
      apiClient.get("/denial-trends/monthly", { params: { ...params, top: 20 } }),
      apiClient.get("/denial-trends/rule-coverage", { params }),
    ])
      .then(([m, c]) => {
        setMonthly(m.data); setCoverage(c.data);
        const ms = m.data.months || [];
        // default to the last complete month
        const complete = ms.filter(x => x !== m.data.partial_month);
        setMonth(prev => (ms.includes(prev) ? prev : complete[complete.length - 1] || ms[ms.length - 1] || ""));
      })
      .catch(e => setError(e?.response?.data?.detail || "Could not load denial trends"))
      .finally(() => setLoading(false));
  }, [facilityId, payer, months]);

  const payerOptions = monthly?.payers || [];
  const [attnFirst, setAttnFirst] = useState(true);
  const rows = useMemo(() => {
    const rs = (monthly?.rows || []).filter(r => r.month === month);
    if (attnFirst) rs.sort((a, b) => (b.attention - a.attention) || (b.lines - a.lines));
    return rs.slice(0, 40);
  }, [monthly, month, attnFirst]);
  const attnCount = (monthly?.rows || []).filter(r => r.month === month && r.attention).length;
  const isPartial = monthly?.partial_month === month;
  const codes = useMemo(
    () => (coverage?.codes || []).filter(c => !gapsOnly || c.coverage === "none"),
    [coverage, gapsOnly]
  );
  const s = coverage?.summary;

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <p className="gh-eyebrow">Denial intelligence</p>
          <h1 className="gh-h1">Denial trends</h1>
          <p className="mt-1 text-sm text-slate-600">
            Top denial codes per payer by <b>RA month</b> (the month the remittance advice arrived, not the
            treatment month), and which ones the Claim Scrubber already catches before submission.
          </p>
        </div>
        <button className="gh-btn-secondary text-sm" disabled={exporting} onClick={exportXlsx}>
          {exporting ? "Preparing…" : "Download Excel"}
        </button>
      </div>

      <section className="gh-section">
        <div className="flex flex-wrap items-end gap-3">
          {isAdmin && (
            <label className="min-w-[200px] flex-1">
              <span className="gh-label mb-1 block">Facility</span>
              <select className="gh-input w-full" value={facilityId}
                onChange={e => { setFacilityId(e.target.value); saveFacility(e.target.value); setPayer(""); }}>
                <option value="">All facilities</option>
                {facilities.map(f => <option key={f.id} value={f.id}>{f.facility_name}</option>)}
              </select>
            </label>
          )}
          <label className="min-w-[200px] flex-1">
            <span className="gh-label mb-1 block">Payer / TPA</span>
            <select className="gh-input w-full" value={payer} onChange={e => setPayer(e.target.value)}>
              <option value="">All payers</option>
              {payer && !payerOptions.some(p => p.payer === payer) && <option value={payer}>{payer}</option>}
              {payerOptions.map(p => <option key={p.payer} value={p.payer}>{p.payer}{p.name ? ` — ${p.name}` : ""}</option>)}
            </select>
          </label>
          <label className="w-32">
            <span className="gh-label mb-1 block">Window</span>
            <select className="gh-input w-full" value={months} onChange={e => setMonths(Number(e.target.value))}>
              {[3, 6, 12].map(n => <option key={n} value={n}>{n} months</option>)}
            </select>
          </label>
          <label className="w-40">
            <span className="gh-label mb-1 block">RA month</span>
            <select className="gh-input w-full" value={month} onChange={e => setMonth(e.target.value)}>
              {(monthly?.months || []).map(m => (
                <option key={m} value={m}>{monthLabel(m)}{m === monthly?.partial_month ? " (so far)" : ""}</option>
              ))}
            </select>
          </label>
        </div>
        {loading && <p className="mt-3 text-sm text-slate-500">Loading… (first load of all facilities can take ~10 s)</p>}
        {error && <p className="mt-3 text-sm text-red-600">{error}</p>}
      </section>

      {monthly && (
        <div className="grid gap-4 md:grid-cols-4">
          <div className="gh-card p-4">
            <p className="gh-eyebrow">Denied lines · RA {monthLabel(month)}</p>
            <p className="text-2xl font-semibold">{fmt(monthly.totals.find(t => t.month === month)?.lines)}</p>
          </div>
          <div className="gh-card p-4">
            <p className="gh-eyebrow">Denied amount · RA {monthLabel(month)}</p>
            <p className="text-2xl font-semibold">AED {fmt(monthly.totals.find(t => t.month === month)?.amount)}</p>
          </div>
          <div className="gh-card p-4">
            <p className="gh-eyebrow">Lines with a scrubber rule</p>
            <p className="text-2xl font-semibold">{s ? `${s.covered_lines_pct}%` : "—"}</p>
          </div>
          <div className="gh-card p-4">
            <p className="gh-eyebrow">Codes with no rule</p>
            <p className="text-2xl font-semibold">{s ? `${s.gap_codes} / ${s.codes}` : "—"}</p>
          </div>
        </div>
      )}

      {monthly?.totals?.length > 1 && (
        <section className="gh-card p-4">
          <p className="gh-eyebrow mb-2">Denied lines per RA month</p>
          <Suspense fallback={<div className="h-[180px]" />}>
            <TrendChart data={monthly.totals} />
          </Suspense>
        </section>
      )}

      <section className="gh-card overflow-x-auto p-4">
        <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
          <p className="gh-eyebrow">
            Top denial codes by payer · RA {monthLabel(month)}
            {isPartial ? " · compared with the same days of last month" : " · compared with the previous month"}
          </p>
          <label className="flex items-center gap-2 text-xs text-slate-600">
            <input type="checkbox" checked={attnFirst} onChange={e => setAttnFirst(e.target.checked)} />
            Above-usual first {attnCount > 0 && <span className="gh-chip gh-chip-denied">{attnCount} above usual</span>}
          </label>
        </div>
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left text-xs uppercase text-slate-500">
              <th className="py-2 pr-3">Payer</th><th className="pr-3">Code</th><th className="pr-3">Description</th>
              <th className="pr-3 text-right">Lines</th><th className="pr-3 text-right">Claims</th>
              <th className="pr-3 text-right">Denied AED</th><th className="pr-3">{isPartial ? "vs same days last month" : "vs prev month"}</th><th className="pr-3" title="Against the average of the 3 full months before (pro-rated for a part month)">vs usual</th><th>Scrubber</th>
            </tr>
          </thead>
          <tbody>
            {rows.map(r => (
              <tr key={`${r.payer}-${r.denial_code}`} className={`border-t border-slate-100 ${r.attention ? "bg-denied/5" : ""}`}>
                <td className="max-w-[170px] truncate py-1.5 pr-3" title={`${r.payer_name || ""} ${r.payer}`}>{r.payer}{r.payer_name && <span className="block truncate text-xs text-slate-400">{r.payer_name}</span>}</td>
                <td className="whitespace-nowrap pr-3"><span className="font-mono">{r.denial_code}</span> <WhyBtn onClick={() => setDrill({ code: r.denial_code, payer: r.payer, month })} /></td>
                <td className="min-w-[220px] max-w-[340px] py-1.5 pr-3 leading-snug text-slate-600">{r.description || "—"}</td>
                <td className="pr-3 text-right">{fmt(r.lines)}</td>
                <td className="pr-3 text-right">{fmt(r.claims)}</td>
                <td className="pr-3 text-right">{fmt(r.amount)}</td>
                <td className="whitespace-nowrap pr-3"><Trend pct={r.change_pct} diff={r.change_lines} /></td>
                <td className="whitespace-nowrap pr-3">{r.attention
                  ? <span className="gh-chip gh-chip-denied" title={`Usual ≈ ${fmt(r.usual_lines)} lines`}>▲ {r.vs_usual_pct == null ? "new" : `${r.vs_usual_pct > 0 ? "+" : ""}${r.vs_usual_pct}%`}</span>
                  : <span className="text-xs text-slate-500" title={r.usual_lines ? `Usual ≈ ${fmt(r.usual_lines)} lines` : ""}>{r.vs_usual_pct == null ? "—" : `${r.vs_usual_pct > 0 ? "+" : ""}${r.vs_usual_pct}%`}</span>}</td>
                <td>{r.has_rule ? <span className="gh-chip gh-chip-paid">rule</span> : <span className="gh-chip gh-chip-denied">gap</span>}</td>
              </tr>
            ))}
            {!rows.length && !loading && <tr><td colSpan={9} className="py-4 text-center text-slate-500">No denials in this month.</td></tr>}
          </tbody>
        </table>
      </section>

      <section className="gh-card overflow-x-auto p-4">
        <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
          <p className="gh-eyebrow">
            Rule coverage · {months} months · trend {coverage?.trend_months?.filter(Boolean).map(monthLabel).join(" → ")}
          </p>
          <label className="flex items-center gap-2 text-sm">
            <input type="checkbox" checked={gapsOnly} onChange={e => setGapsOnly(e.target.checked)} /> Rule gaps only
          </label>
        </div>
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left text-xs uppercase text-slate-500">
              <th className="py-2 pr-3">Code</th><th className="pr-3">Description</th>
              <th className="pr-3 text-right">Lines</th><th className="pr-3 text-right">Share</th>
              <th className="pr-3 text-right">Denied AED</th><th className="pr-3">Trend</th>
              <th className="pr-3">Top payers</th><th className="pr-3">Coverage</th><th>Rules / playbook</th>
            </tr>
          </thead>
          <tbody>
            {codes.map(c => (
              <tr key={c.denial_code} className="border-t border-slate-100 align-top">
                <td className="whitespace-nowrap py-1.5 pr-3"><span className="font-mono">{c.denial_code}</span> <WhyBtn onClick={() => setDrill({ code: c.denial_code, payer, month: "" })} /></td>
                <td className="min-w-[200px] max-w-[320px] py-1.5 pr-3 leading-snug text-slate-600">{c.description || "—"}</td>
                <td className="pr-3 text-right">{fmt(c.lines)}</td>
                <td className="pr-3 text-right">{c.share_pct}%</td>
                <td className="pr-3 text-right">{fmt(c.amount)}</td>
                <td className="pr-3"><Trend pct={c.change_pct} /></td>
                <td className="pr-3 text-xs">{c.top_payers.map(p => `${p.name || p.payer} (${fmt(p.lines)})`).join(", ")}</td>
                <td className="pr-3"><Coverage c={c.coverage} /></td>
                <td className="text-xs">
                  {c.rules.map((r, i) => <div key={i}><b>{r.source}:</b> {r.rule}</div>)}
                  {c.playbook && <div className="text-slate-500">Playbook: {c.playbook.category} ({c.playbook.owner})</div>}
                  {!c.rules.length && !c.playbook && "—"}
                </td>
              </tr>
            ))}
            {!codes.length && !loading && <tr><td colSpan={9} className="py-4 text-center text-slate-500">Nothing to show.</td></tr>}
          </tbody>
        </table>
      </section>
      {drill && <DenialDrill code={drill.code} payer={drill.payer} facilityId={facilityId} month={drill.month} onClose={() => setDrill(null)} />}
    </div>
  );
}
