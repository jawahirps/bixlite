import { useEffect, useMemo, useState } from "react";
import { BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, Legend, ResponsiveContainer } from "recharts";
import apiClient from "../api/client";
import DashboardTables, { TABLE_TABS } from "./DashboardTables";

// ─── Helpers ──────────────────────────────────────────────────────────────────
const AED = (v) => "AED " + Math.round(Number(v || 0)).toLocaleString("en-AE");
const compact = (v) => {
  const n = Number(v || 0);
  if (Math.abs(n) >= 1_000_000) return `${(n / 1_000_000).toFixed(2)}M`;
  if (Math.abs(n) >= 1_000) return `${(n / 1_000).toFixed(0)}K`;
  return Math.round(n).toLocaleString();
};
const pct = (a, b) => (b ? `${((a / b) * 100).toFixed(1)}%` : "—");
const iso = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
const monthLabel = (m) => new Date(`${m}-01`).toLocaleDateString("en-GB", { month: "short", year: "2-digit" });

// cycle colours: paid / denied / awaiting, from the Ghaf palette
const C = { paid: "#0f7c97", denied: "#c2410c", awaiting: "#cad5ca", overdue: "#b45309" };

function presets() {
  const t = new Date();
  const first = (y, m) => new Date(y, m, 1);
  return [
    ["This month", first(t.getFullYear(), t.getMonth())],
    ["Last 3 months", first(t.getFullYear(), t.getMonth() - 2)],
    ["Last 12 months", first(t.getFullYear(), t.getMonth() - 11)],
    ["This year", first(t.getFullYear(), 0)],
  ].map(([label, from]) => ({ label, from: iso(from), to: iso(t) }));
}

// ─── Cards ────────────────────────────────────────────────────────────────────
function MoneyCard({ label, amount, count, share, color }) {
  return (
    <div className="gh-card p-5 border-t-4" style={{ borderTopColor: color }}>
      <p className="gh-eyebrow">{label}</p>
      <p className="mt-2 font-display text-[28px] font-bold leading-9 tracking-[-0.03em] tabular-nums text-ink">
        {compact(amount)}
      </p>
      <p className="mt-1 text-[13px] text-slate-600 tabular-nums">
        {AED(amount)}{share ? ` · ${share}` : ""}
      </p>
      {count != null && <p className="text-[13px] text-slate-500 tabular-nums">{count.toLocaleString()} claims</p>}
    </div>
  );
}

function StatusCard({ label, count, total, amount, hint, tone }) {
  return (
    <div className={`gh-card p-4 ${tone || ""}`}>
      <p className="gh-label">{label}</p>
      <p className="mt-1 text-2xl font-bold tabular-nums text-ink">{count.toLocaleString()}</p>
      <p className="text-xs text-slate-600 tabular-nums">
        {pct(count, total)} of claims{amount != null ? ` · ${AED(amount)}` : ""}
      </p>
      {hint && <p className="mt-1 text-[11px] text-slate-500">{hint}</p>}
    </div>
  );
}

// One strip showing where the submitted amount is in the cycle
function CycleStrip({ t }) {
  const parts = [
    ["Paid", t.paid, C.paid],
    ["Denied", t.denied, C.denied],
    ["Awaiting RA", t.awaiting, C.awaiting],
  ];
  return (
    <div className="gh-card p-5">
      <div className="flex items-baseline justify-between">
        <p className="gh-label">Where the submitted amount is</p>
        <p className="text-xs text-slate-500 tabular-nums">Net submitted {AED(t.net)}</p>
      </div>
      <div className="mt-3 flex h-6 w-full overflow-hidden rounded-md bg-slate-100">
        {parts.map(([k, v, c]) =>
          v > 0 ? <div key={k} title={`${k}: ${AED(v)}`} style={{ width: `${(v / t.net) * 100}%`, background: c }} /> : null
        )}
      </div>
      <div className="mt-2 flex flex-wrap gap-x-6 gap-y-1 text-xs text-slate-600">
        {parts.map(([k, v, c]) => (
          <span key={k} className="flex items-center gap-1.5 tabular-nums">
            <span className="inline-block h-2.5 w-2.5 rounded-sm" style={{ background: c }} />
            {k} {pct(v, t.net)}
          </span>
        ))}
      </div>
    </div>
  );
}

function MoneyTooltip({ active, payload, label }) {
  if (!active || !payload?.length) return null;
  const sum = payload.reduce((s, p) => s + Number(p.value || 0), 0);
  return (
    <div className="rounded-lg border border-slate-200 bg-white px-3 py-2 text-xs shadow-lg">
      <p className="mb-1 font-semibold text-slate-700">{label}</p>
      {payload.map((p) => (
        <p key={p.dataKey} className="tabular-nums" style={{ color: p.color === C.awaiting ? "#596d66" : p.color }}>
          {p.name}: {AED(p.value)} ({pct(p.value, sum)})
        </p>
      ))}
    </div>
  );
}

// ─── Main Dashboard ───────────────────────────────────────────────────────────
export default function Dashboard() {
  const P = useMemo(presets, []);
  const [facilities, setFacilities] = useState([]);
  const [facilityId, setFacilityId] = useState(() => {
    try { return localStorage.getItem("ghaf_dash_facility_id") || ""; } catch { return ""; }
  });
  const [range, setRange] = useState({ from: P[2].from, to: P[2].to });
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [tab, setTab] = useState("cycle");

  useEffect(() => {
    apiClient.get("/facilities")
      .then((r) => setFacilities(r.data.filter((f) => f.facility_code !== "GHAF-001" && f.is_active !== false)))
      .catch(() => {});
  }, []);

  useEffect(() => {
    try { localStorage.setItem("ghaf_dash_facility_id", facilityId); } catch { /* private window */ }
    if (!range.from || !range.to || range.from > range.to) return;
    setLoading(true); setError(null);
    const q = new URLSearchParams({ date_from: range.from, date_to: range.to });
    if (facilityId) q.set("facility_id", facilityId);
    apiClient.get(`/dashboard/cycle?${q}`, { timeout: 120000 })
      .then((r) => setData(r.data))
      .catch((err) => setError(err.response?.data?.detail || "Couldn't load the claim cycle."))
      .finally(() => setLoading(false));
  }, [facilityId, range]);

  const t = data?.total;
  const chart = (data?.months || []).map((m) => ({
    month: monthLabel(m.month), Paid: m.paid, Denied: m.denied, "Awaiting RA": m.awaiting,
  }));
  const remitted = t ? t.full_n + t.part_n + t.den_n : 0;

  return (
    <div className="space-y-6">
      {/* ── Header + filters ── */}
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <p className="gh-eyebrow">Overview</p>
          <h1 className="gh-h1">Claim cycle</h1>
          <p className="mt-0.5 text-sm text-slate-500">
            Claims by encounter date and where each one is now: paid, denied or still awaiting the RA.
          </p>
        </div>
        {data?.as_of && <p className="text-xs text-slate-500">Figures as of {data.as_of}</p>}
      </div>

      <div className="gh-card flex flex-wrap items-end gap-3 p-4">
        <label className="flex flex-col gap-1 text-xs text-slate-600">
          Facility
          <select className="gh-input min-w-[240px]" value={facilityId} onChange={(e) => setFacilityId(e.target.value)}>
            <option value="">All facilities</option>
            {facilities.map((f) => <option key={f.id} value={f.id}>{f.facility_name}</option>)}
          </select>
        </label>
        <label className="flex flex-col gap-1 text-xs text-slate-600">
          Encounter from
          <input type="date" className="gh-input" value={range.from} max={range.to}
                 onChange={(e) => setRange((r) => ({ ...r, from: e.target.value }))} />
        </label>
        <label className="flex flex-col gap-1 text-xs text-slate-600">
          to
          <input type="date" className="gh-input" value={range.to} min={range.from}
                 onChange={(e) => setRange((r) => ({ ...r, to: e.target.value }))} />
        </label>
        <div className="flex flex-wrap gap-1.5">
          {P.map((p) => {
            const on = p.from === range.from && p.to === range.to;
            return (
              <button key={p.label} type="button" onClick={() => setRange({ from: p.from, to: p.to })}
                      className={`rounded-full border px-3 py-1.5 text-xs ${on
                        ? "border-ghaf-600 bg-ghaf-600 text-white" : "border-slate-300 text-slate-700 hover:bg-slate-100"}`}>
                {p.label}
              </button>
            );
          })}
        </div>
      </div>

      {/* ── Tabs ── */}
      <div className="flex gap-1 overflow-x-auto border-b border-slate-200">
        {[{ key: "cycle", label: "Claim cycle" }, ...TABLE_TABS].map((x) => (
          <button key={x.key} type="button" onClick={() => setTab(x.key)}
                  className={`shrink-0 whitespace-nowrap border-b-2 px-4 py-2 text-sm font-medium ${tab === x.key
                    ? "border-ghaf-600 text-ghaf-700" : "border-transparent text-slate-500 hover:text-slate-700"}`}>
            {x.label}
          </button>
        ))}
      </div>

      {tab !== "cycle" ? <DashboardTables tab={tab} facilityId={facilityId} range={range} /> : (<>
      {error && (
        <div className="gh-card flex items-center justify-between border-red-200 bg-red-50 p-4 text-sm text-red-700">
          {error}
          <button className="gh-btn-secondary gh-btn-sm" onClick={() => setRange((r) => ({ ...r }))}>Retry</button>
        </div>
      )}

      {loading && !t && <div className="gh-card p-10 text-center text-sm text-slate-500">Loading the claim cycle…</div>}

      {t && (
        <div className={`space-y-6 transition-opacity ${loading ? "opacity-50" : ""}`}>
          {/* ── Money cards ── */}
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
            <MoneyCard label="Submitted (net)" amount={t.net} count={t.n} color="#0b2c3c" />
            <MoneyCard label="Paid" amount={t.paid} share={pct(t.paid, t.net)} color={C.paid} />
            <MoneyCard label="Denied" amount={t.denied} share={pct(t.denied, t.paid + t.denied) + " of remitted"} color={C.denied} />
            <MoneyCard label="Awaiting RA" amount={t.awaiting} count={t.await_n} share={pct(t.awaiting, t.net)} color={C.overdue} />
          </div>

          {t.n === 0 ? (
            <div className="gh-card p-10 text-center text-sm text-slate-500">
              No claims with an encounter date in this period. Try a wider date range or another facility.
            </div>
          ) : (
            <>
              <CycleStrip t={t} />

              {/* ── Cycle status cards ── */}
              <div>
                <p className="gh-label mb-3">Claim status — {t.n.toLocaleString()} claims</p>
                <div className="grid grid-cols-2 gap-4 md:grid-cols-3 xl:grid-cols-6">
                  <StatusCard label="Fully paid" count={t.full_n} total={t.n} />
                  <StatusCard label="Partially paid" count={t.part_n} total={t.n} />
                  <StatusCard label="Fully denied" count={t.den_n} total={t.n} />
                  <StatusCard label="Awaiting RA" count={t.await_n} total={t.n} amount={t.awaiting} />
                  <StatusCard label="Overdue RA" count={t.overdue_n} total={t.n} amount={t.overdue}
                              hint="No RA 45+ days after submission" tone={t.overdue_n ? "bg-amber-50" : ""} />
                  <StatusCard label="Resubmitted" count={t.resub} total={t.n} hint={`${remitted.toLocaleString()} claims remitted`} />
                </div>
              </div>

              {/* ── Monthly cycle chart ── */}
              <div className="gh-card p-5">
                <p className="gh-label">Monthly submitted amount by cycle status</p>
                <p className="mb-3 text-xs text-slate-500">Each bar is one encounter month; the grey part has no RA yet.</p>
                <ResponsiveContainer width="100%" height={320}>
                  <BarChart data={chart} margin={{ top: 8, right: 8, left: 8, bottom: 0 }} barCategoryGap="20%">
                    <CartesianGrid strokeDasharray="3 3" vertical={false} stroke="#e5eee6" />
                    <XAxis dataKey="month" tick={{ fontSize: 12, fill: "#596d66" }} axisLine={false} tickLine={false} />
                    <YAxis tickFormatter={compact} tick={{ fontSize: 12, fill: "#596d66" }} axisLine={false} tickLine={false} width={56} />
                    <Tooltip content={<MoneyTooltip />} cursor={{ fill: "#f1f7f9" }} />
                    <Legend wrapperStyle={{ fontSize: 12 }} iconType="square"
                            formatter={(v) => <span className="text-slate-700">{v}</span>} />
                    <Bar dataKey="Paid" stackId="a" fill={C.paid} />
                    <Bar dataKey="Denied" stackId="a" fill={C.denied} />
                    <Bar dataKey="Awaiting RA" stackId="a" fill={C.awaiting} radius={[4, 4, 0, 0]} />
                  </BarChart>
                </ResponsiveContainer>
              </div>
            </>
          )}
        </div>
      )}
      </>)}
    </div>
  );
}
