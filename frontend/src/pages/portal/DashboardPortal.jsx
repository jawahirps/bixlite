/**
 * DashboardPortal.jsx — facility-facing claim dashboard
 *
 * Accessible at /portal/dashboard?token=<PORTAL_TOKEN>. No login: the token is checked server-side and
 * scopes the page to that client's facilities. Summaries only — no claim, patient or member IDs.
 */
import { useEffect, useState } from "react";
import axios from "axios";
import StackedBars from "../../components/StackedBars";

const api = axios.create({ baseURL: "/api/portal" });
const token = () => new URLSearchParams(window.location.search).get("token") || "";
const fmt = (v) => Number(v || 0).toLocaleString("en-GB", { maximumFractionDigits: 0 });
const compact = (v) => (v >= 1e6 ? `${(v / 1e6).toFixed(2)}M` : v >= 1e3 ? `${(v / 1e3).toFixed(0)}K` : fmt(v));
const monthLabel = (m) => new Date(m + "-01T00:00:00").toLocaleDateString("en-GB", { month: "short", year: "2-digit" });
const SERIES = [
  { key: "Paid", color: "#0f7c97" },
  { key: "Denied", color: "#b3412e" },
  { key: "Awaiting RA", color: "#cad5ca" },
];

function Card({ label, value, sub, tone }) {
  return (
    <div className={`rounded-xl border border-slate-200 border-t-4 bg-white p-4 ${tone}`}>
      <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">{label}</p>
      <p className="mt-1 font-display text-2xl font-semibold text-ink">{value}</p>
      {sub && <p className="mt-0.5 text-xs text-slate-500">{sub}</p>}
    </div>
  );
}

function Share({ title, rows, name, desc }) {
  return (
    <div className="rounded-xl border border-slate-200 bg-white p-4">
      <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">{title}</p>
      <table className="mt-2 w-full text-sm">
        <tbody className="divide-y divide-slate-100">
          {rows.map((r, i) => (
            <tr key={i}>
              <td className="py-1.5 pr-2">
                <span className="font-medium text-ink">{name(r)}</span>
                {desc?.(r) && <span className="block text-xs text-slate-500">{desc(r)}</span>}
              </td>
              <td className="py-1.5 pr-2 text-right tabular-nums text-slate-600">AED {fmt(r.amount)}</td>
              <td className="w-28 py-1.5">
                <div className="h-1.5 rounded bg-slate-100"><div className="h-1.5 rounded bg-denied/70" style={{ width: `${r.share_pct}%` }} /></div>
              </td>
              <td className="w-10 py-1.5 text-right text-xs tabular-nums text-slate-500">{r.share_pct}%</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default function DashboardPortal() {
  const [months, setMonths] = useState(12);
  const [fid, setFid] = useState("");
  const [d, setD] = useState(null);
  const [err, setErr] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (!token()) { setErr("This link is missing its access key."); setLoading(false); return; }
    setLoading(true);
    api.get("/dashboard", { params: { token: token(), months, ...(fid ? { facility_id: fid } : {}) }, timeout: 120000 })
      .then((r) => { setD(r.data); setErr(null); })
      .catch((e) => setErr(e.response?.status === 401 ? "This link has expired or been switched off. Please contact Ghaf." : "Could not load the dashboard."))
      .finally(() => setLoading(false));
  }, [months, fid]);

  const t = d?.total;
  const remitted = t ? t.net - t.awaiting : 0;
  return (
    <div className="min-h-screen bg-sage/40">
      <div className="mx-auto max-w-6xl space-y-5 px-4 py-6">
        <header className="flex flex-wrap items-end justify-between gap-3">
          <div>
            <p className="text-xs font-semibold uppercase tracking-widest text-ghaf-600">Claim dashboard</p>
            <h1 className="font-display text-2xl font-semibold text-ink">{d?.facility || "…"}</h1>
            <p className="text-sm text-slate-500">Claims by encounter month and where each one is now. Prepared by Ghaf Medical Billing Services.</p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            {d?.facilities?.length > 1 && (
              <select className="gh-input" value={fid || d.facility_id} onChange={(e) => setFid(e.target.value)}>
                {d.facilities.map((f) => <option key={f.id} value={f.id}>{f.name}</option>)}
              </select>
            )}
            {[3, 6, 12].map((m) => (
              <button key={m} onClick={() => setMonths(m)}
                className={`rounded-full border px-3 py-1.5 text-xs ${months === m ? "border-ghaf-600 bg-ghaf-600 text-white" : "border-slate-300 bg-white text-slate-700"}`}>
                Last {m} months</button>
            ))}
          </div>
        </header>

        {err && <p className="rounded-lg border border-denied/30 bg-white p-4 text-sm text-denied">{err}</p>}
        {loading && !d && !err && <p className="text-sm text-slate-500">Loading…</p>}

        {d && t && (
          <>
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
              <Card label="Submitted (net)" value={`AED ${compact(t.net)}`} sub={`${fmt(t.n)} claims`} tone="border-t-ink" />
              <Card label="Paid" value={`AED ${compact(t.paid)}`} sub={`${t.net ? Math.round(100 * t.paid / t.net) : 0}% of submitted`} tone="border-t-ghaf-600" />
              <Card label="Denied" value={`AED ${compact(t.denied)}`} sub={`${remitted ? (100 * t.denied / remitted).toFixed(1) : 0}% of remitted`} tone="border-t-denied" />
              <Card label="Awaiting RA" value={`AED ${compact(t.awaiting)}`} sub={`${fmt(t.await_n)} claims`} tone="border-t-pending" />
            </div>

            <div className="rounded-xl border border-slate-200 bg-white p-4">
              <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">Monthly submitted amount by status</p>
              <p className="mb-3 text-xs text-slate-500">Each bar is one encounter month; the grey part has no RA yet.</p>
              <StackedBars height={260} format={compact}
                data={d.months.map((m) => ({ label: monthLabel(m.month), values: { Paid: m.paid, Denied: m.denied, "Awaiting RA": m.awaiting } }))}
                series={SERIES} />
            </div>

            <div className="grid gap-4 lg:grid-cols-2">
              <Share title="Why claims are denied (unpaid amount)" rows={d.denial_reasons} name={(r) => r.code} desc={(r) => r.description} />
              <Share title="Unpaid amount by payer" rows={d.payers} name={(r) => r.payer} />
            </div>

            <p className="text-xs text-slate-400">Figures as of {d.as_of}. Denied = net not paid after all remittance advices, including resubmissions.</p>
          </>
        )}
      </div>
    </div>
  );
}
