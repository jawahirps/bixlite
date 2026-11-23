import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
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

// cycle colours (shared design brief)
const C = { paid: "#006884", denied: "#b3412e", awaiting: "#e9c98a", pending: "#8a5d0b", ink: "#0b2c3c", overdue: "#e7a99c" };
const SERIES = [{ key: "Paid", color: C.paid }, { key: "Denied", color: C.denied }, { key: "Awaiting RA", color: C.awaiting }];
const SHADOW = "shadow-[0_8px_30px_rgba(11,34,51,0.08)]";

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
function Sparkline({ values, color }) {
  if (!values || values.length < 2) return <div className="h-8" />;
  const max = Math.max(...values, 1), w = 100, h = 28;
  if (values.length < 6) {   // a line through 2–5 points reads as a meaningless slope: show month bars instead
    const bw = w / values.length;
    return (
      <svg viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none" className="h-8 w-full" aria-hidden="true">
        {values.map((v, i) => {
          const bh = Math.max(1.5, (v / max) * (h - 2));
          return <rect key={i} x={i * bw + bw * 0.18} y={h - bh} width={bw * 0.64} height={bh} rx="1.5"
                       fill={color} opacity={i === values.length - 1 ? 0.9 : 0.35} />;
        })}
      </svg>
    );
  }
  const pts = values.map((v, i) => [(i / (values.length - 1)) * w, h - 2 - (v / max) * (h - 4)]);
  const line = pts.map((p, i) => `${i ? "L" : "M"}${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(" ");
  return (
    <svg viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none" className="h-8 w-full" aria-hidden="true">
      <path d={`${line} L${w},${h} L0,${h} Z`} fill={color} opacity="0.12" />
      <path d={line} fill="none" stroke={color} strokeWidth="1.75" vectorEffect="non-scaling-stroke" strokeLinejoin="round" />
    </svg>
  );
}

function KpiTile({ label, amount, count, share, color, spark }) {
  return (
    <div className={`rounded-2xl bg-white p-5 ${SHADOW} ring-1 ring-black/[0.03]`}>
      <div className="flex items-center gap-2">
        <span className="grid h-6 w-6 place-items-center rounded-lg" style={{ background: color + "1f" }}>
          <span className="h-2 w-2 rounded-full" style={{ background: color }} />
        </span>
        <p className="text-[11px] font-semibold uppercase tracking-[0.08em] text-slate-500">{label}</p>
      </div>
      <p className="mt-3 font-display text-3xl font-extrabold tracking-[-0.03em] tabular-nums text-ink"><span className="mr-1 align-[0.35em] text-sm font-semibold tracking-normal text-slate-400">AED</span>{compact(amount)}</p>
      <p className="mt-1 text-[13px] text-slate-600 tabular-nums">{AED(amount)}{share ? ` · ${share}` : ""}</p>
      <p className="text-[12px] text-slate-500 tabular-nums">{count != null ? `${count.toLocaleString()} claims` : " "}</p>
      <div className="mt-3"><Sparkline values={spark} color={color} /></div>
    </div>
  );
}

function StatusCard({ label, count, total, amount, hint, color, alert }) {
  return (
    <div className={`rounded-2xl border p-4 ${alert ? "border-denied/25 bg-denied/[0.05]" : "border-hairline bg-white"}`}>
      <div className="flex items-center gap-2">
        <span className="h-2 w-2 shrink-0 rounded-full" style={{ background: color }} />
        <p className="text-[11px] font-semibold uppercase tracking-[0.08em] text-slate-500">{label}</p>
      </div>
      <p className="mt-2 font-display text-2xl font-extrabold tabular-nums text-ink">{count.toLocaleString()}</p>
      <p className="text-xs text-slate-600 tabular-nums">
        {pct(count, total)} of claims{amount != null ? ` · ${AED(amount)}` : ""}
      </p>
      {hint && <p className="mt-1 text-[11px] text-slate-500">{hint}</p>}
    </div>
  );
}

function Legend({ items }) {
  return (
    <div className="flex flex-wrap gap-2">
      {items.map(([k, c, extra]) => (
        <span key={k} className="inline-flex items-center gap-1.5 rounded-full bg-sage px-2.5 py-1 text-[11px] font-medium text-slate-700 tabular-nums">
          <span className="h-2 w-2 rounded-full" style={{ background: c }} />{k}{extra ? ` ${extra}` : ""}
        </span>
      ))}
    </div>
  );
}

// One strip showing where the submitted amount is in the cycle
function CycleStrip({ t }) {
  const parts = [["Paid", t.paid, C.paid], ["Denied", t.denied, C.denied], ["Awaiting RA", t.awaiting, C.awaiting]];
  return (
    <div className={`rounded-2xl bg-white p-5 sm:p-6 ${SHADOW}`}>
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 className="font-display text-base font-bold text-ink">Where the submitted amount is</h2>
        <p className="text-xs text-slate-500 tabular-nums">Net submitted {AED(t.net)}</p>
      </div>
      <div className="mt-4 flex h-4 w-full gap-0.5 overflow-hidden rounded-full bg-slate-100">
        {parts.map(([k, v, c]) =>
          v > 0 ? <div key={k} title={`${k}: ${AED(v)}`} style={{ width: `${(v / t.net) * 100}%`, background: c }} /> : null
        )}
      </div>
      <div className="mt-3"><Legend items={parts.map(([k, v, c]) => [k, c, pct(v, t.net)])} /></div>
    </div>
  );
}

// Monthly stacked bars (own SVG; the shared StackedBars component stays untouched)
function CycleChart({ data }) {
  const [hover, setHover] = useState(null);
  const W = 720, H = 260, padL = 44, padB = 26, padT = 10;
  const totals = data.map((d) => SERIES.reduce((s, x) => s + Number(d[x.key] || 0), 0));
  const max = Math.max(...totals, 1);
  const ticks = [0, 0.25, 0.5, 0.75, 1].map((f) => f * max);
  const plotH = H - padB - padT, slot = (W - padL) / Math.max(data.length, 1);
  const bw = Math.min(42, slot * 0.6);
  const y = (v) => padT + plotH - (v / max) * plotH;
  const hd = hover != null ? data[hover] : null;
  return (
    <div className="relative">
      <svg viewBox={`0 0 ${W} ${H}`} className="h-auto w-full" role="img" aria-label="Monthly submitted amount by cycle status"
           onMouseLeave={() => setHover(null)}>
        {ticks.map((v, i) => (
          <g key={i}>
            <line x1={padL} x2={W} y1={y(v)} y2={y(v)} stroke="#0b2c3c" strokeOpacity={i ? 0.07 : 0.18} />
            <text x={padL - 6} y={y(v) + 3} textAnchor="end" fontSize="10" fill="#64748b">{compact(v)}</text>
          </g>
        ))}
        {data.map((d, i) => {
          const x = padL + slot * i + (slot - bw) / 2;
          let acc = 0;
          const segs = SERIES.map((s) => { const v = Number(d[s.key] || 0); const r = { s, y0: acc, y1: acc + v }; acc += v; return r; })
            .filter((r) => r.y1 > r.y0);
          const top = y(acc);
          return (
            <g key={i} onMouseEnter={() => setHover(i)}>
              <rect x={padL + slot * i} y={padT} width={slot} height={plotH} fill={hover === i ? "#006884" : "transparent"} fillOpacity="0.05" />
              <clipPath id={`dash-bar-${i}`}><rect x={x} y={top} width={bw} height={Math.max(0, padT + plotH - top) + 6} rx="6" /></clipPath>
              <g clipPath={`url(#dash-bar-${i})`}>
                {segs.map((r) => <rect key={r.s.key} x={x} y={y(r.y1)} width={bw} height={y(r.y0) - y(r.y1)} fill={r.s.color} />)}
              </g>
              <text x={x + bw / 2} y={H - 8} textAnchor="middle" fontSize="10" fill="#64748b">{d.month}</text>
            </g>
          );
        })}
      </svg>
      {hd && (
        <div className="pointer-events-none absolute top-2 z-10 whitespace-nowrap rounded-xl bg-ink-deep px-3 py-2 text-xs text-white shadow-xl"
             style={{ left: `${Math.min(55, Math.max(2, ((padL + slot * hover) / W) * 100))}%` }}>
          <p className="mb-1 font-semibold">{hd.month}</p>
          {SERIES.map((s) => (
            <p key={s.key} className="flex items-center gap-1.5 tabular-nums">
              <span className="h-2 w-2 rounded-full" style={{ background: s.key === "Paid" ? "#3f97ad" : s.color }} />
              {s.key}: {AED(hd[s.key])} ({pct(hd[s.key], totals[hover])})
            </p>
          ))}
        </div>
      )}
    </div>
  );
}

// ─── Main Dashboard ───────────────────────────────────────────────────────────
// facilities whose final denial rate (after resubmissions, latest settled encounter month) is above 10%
function DenialFlagStrip({ facilityId }) {
  const [flags, setFlags] = useState(null);
  useEffect(() => {
    apiClient.get("/alerts", { timeout: 180000 })
      .then((r) => setFlags(r.data.items.filter((a) => a.type === "denial_rate_10")))
      .catch(() => setFlags([]));
  }, []);
  const list = (flags || []).filter((a) => !facilityId || String(a.facility_id) === String(facilityId))
    .sort((a, b) => b.rate_pct - a.rate_pct);
  if (!list.length) return null;
  const mon = (m) => (m ? new Date(m + "-01T00:00:00").toLocaleDateString("en-GB", { month: "short" }) : "");
  return (
    <div className="relative overflow-hidden rounded-2xl border border-denied/20 bg-gradient-to-r from-denied/[0.09] via-denied/[0.04] to-white p-4 pl-5 sm:p-5 sm:pl-6">
      <div className="absolute inset-y-0 left-0 w-1 bg-denied" />
      <div className="flex flex-wrap items-center gap-3">
        <span className="grid h-9 w-9 shrink-0 place-items-center rounded-xl bg-denied text-white">
          <svg viewBox="0 0 24 24" className="h-5 w-5" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
            <path d="M12 9v4m0 4h.01M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        </span>
        <div className="min-w-0 flex-1">
          <p className="font-display text-sm font-bold text-ink">{list.length} facilit{list.length === 1 ? "y" : "ies"} above 10% denial</p>
          <p className="text-xs text-slate-600">Final denial rate, latest settled encounter month</p>
        </div>
        <Link to="/alerts" className="rounded-full bg-white px-3 py-1.5 text-xs font-semibold text-denied ring-1 ring-denied/25 hover:bg-denied hover:text-white">
          Facility alerts →
        </Link>
      </div>
      <div className="mt-3 flex flex-wrap gap-2">
        {list.map((a) => (
          <Link key={a.facility_id} to={`/alerts`} title={a.detail}
            className="inline-flex max-w-full items-center gap-2 rounded-full bg-white px-3 py-1 text-xs text-slate-700 ring-1 ring-hairline hover:ring-denied/40">
            <span className="truncate">{a.facility_name}</span>
            <b className={`tabular-nums ${a.rate_pct > 15 ? "text-denied" : "text-pending"}`}>{a.rate_pct}%</b>
            <span className="text-slate-400">{mon(a.month)}</span>
          </Link>
        ))}
      </div>
    </div>
  );
}

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
  const months = data?.months || [];
  const chart = months.map((m) => ({
    month: monthLabel(m.month), Paid: m.paid, Denied: m.denied, "Awaiting RA": m.awaiting,
  }));
  const remitted = t ? t.full_n + t.part_n + t.den_n : 0;
  const sp = (k) => months.map((m) => Number(m[k] || 0));
  const field = "rounded-full border border-white/15 bg-white/10 px-3 py-1.5 text-xs text-white outline-none focus:border-white/40 [color-scheme:dark]";

  return (
    <div className="space-y-6">
      {/* ── Hero ── */}
      <div className="relative overflow-hidden rounded-2xl bg-gradient-to-br from-ink-deep via-ink to-ghaf-800 px-5 pb-24 pt-7 text-white sm:px-8">
        <div className="pointer-events-none absolute -right-24 -top-24 h-72 w-72 rounded-full bg-ghaf-400/30 blur-3xl" />
        <div className="pointer-events-none absolute inset-0 opacity-[0.07]"
             style={{ backgroundImage: "linear-gradient(#fff 1px,transparent 1px),linear-gradient(90deg,#fff 1px,transparent 1px)", backgroundSize: "32px 32px" }} />
        <div className="relative flex flex-wrap items-end justify-between gap-4">
          <div className="max-w-xl">
            <p className="text-[11px] font-semibold uppercase tracking-[0.16em] text-ghaf-200">Overview</p>
            <h1 className="mt-1 font-display text-3xl font-extrabold tracking-[-0.03em] sm:text-4xl">Claim cycle</h1>
            <p className="mt-2 text-sm text-white/70">
              Claims by encounter date and where each one is now: paid, denied or still awaiting the RA.
            </p>
          </div>
          {data?.as_of && <p className="rounded-full bg-white/10 px-3 py-1 text-xs text-white/80">Figures as of {data.as_of}</p>}
        </div>
        <div className="relative mt-6 flex flex-wrap items-center gap-2">
          {P.map((p) => {
            const on = p.from === range.from && p.to === range.to;
            return (
              <button key={p.label} type="button" onClick={() => setRange({ from: p.from, to: p.to })}
                      className={`rounded-full px-3 py-1.5 text-xs font-medium transition ${on
                        ? "bg-white text-ink shadow" : "bg-white/10 text-white/85 hover:bg-white/20"}`}>
                {p.label}
              </button>
            );
          })}
        </div>
        <div className="relative mt-3 flex flex-wrap items-center gap-2 text-xs text-white/70">
          <input type="date" className={field} value={range.from} max={range.to} aria-label="Encounter from"
                 onChange={(e) => setRange((r) => ({ ...r, from: e.target.value }))} />
          <span>to</span>
          <input type="date" className={field} value={range.to} min={range.from} aria-label="Encounter to"
                 onChange={(e) => setRange((r) => ({ ...r, to: e.target.value }))} />
          <select className={`${field} w-full min-w-0 sm:w-auto sm:min-w-[240px]`} value={facilityId} aria-label="Facility"
                  onChange={(e) => setFacilityId(e.target.value)}>
            <option value="" className="text-ink">All facilities</option>
            {facilities.map((f) => <option key={f.id} value={f.id} className="text-ink">{f.facility_name}</option>)}
          </select>
        </div>
      </div>

      {/* ── KPI tiles overlapping the hero ── */}
      <div className={`relative z-10 -mt-24 grid grid-cols-1 gap-4 px-3 transition-opacity sm:grid-cols-2 sm:px-5 xl:grid-cols-4 ${loading && t ? "opacity-60" : ""}`}>
        {t ? (<>
          <KpiTile label="Submitted (net)" amount={t.net} count={t.n} color={C.ink} spark={sp("net")} />
          <KpiTile label="Paid" amount={t.paid} share={pct(t.paid, t.net) + " of submitted"} color={C.paid} spark={sp("paid")} />
          <KpiTile label="Denied" amount={t.denied} share={pct(t.denied, t.paid + t.denied) + " of remitted"} color={C.denied} spark={sp("denied")} />
          <KpiTile label="Awaiting RA" amount={t.awaiting} count={t.await_n} share={pct(t.awaiting, t.net) + " of submitted"} color={C.pending} spark={sp("awaiting")} />
        </>) : [0, 1, 2, 3].map((i) => (
          <div key={i} className={`h-[178px] animate-pulse rounded-2xl bg-white ${SHADOW}`} />
        ))}
      </div>

      <DenialFlagStrip facilityId={facilityId} />

      {/* ── Tabs (segmented control) ── */}
      <div className="max-w-full overflow-x-auto">
        <div className="inline-flex gap-1 rounded-2xl bg-sage p-1">
          {[{ key: "cycle", label: "Claim cycle" }, ...TABLE_TABS].map((x) => (
            <button key={x.key} type="button" onClick={() => setTab(x.key)}
                    className={`shrink-0 whitespace-nowrap rounded-xl px-4 py-2 text-sm font-semibold transition ${tab === x.key
                      ? "bg-white text-ink shadow-sm" : "text-slate-600 hover:text-ink"}`}>
              {x.label}
            </button>
          ))}
        </div>
      </div>

      {tab !== "cycle" ? <DashboardTables tab={tab} facilityId={facilityId} range={range} /> : (<>
      {error && (
        <div className="flex items-center justify-between gap-3 rounded-2xl border border-denied/25 bg-denied/[0.06] p-4 text-sm text-denied">
          {error}
          <button className="gh-btn-secondary gh-btn-sm" onClick={() => setRange((r) => ({ ...r }))}>Retry</button>
        </div>
      )}

      {loading && !t && <div className="rounded-2xl bg-white p-10 text-center text-sm text-slate-500">Loading the claim cycle…</div>}

      {t && (
        <div className={`space-y-6 transition-opacity ${loading ? "opacity-50" : ""}`}>
          {t.n === 0 ? (
            <div className="rounded-2xl bg-white p-10 text-center text-sm text-slate-500">
              No claims with an encounter date in this period. Try a wider date range or another facility.
            </div>
          ) : (
            <>
              <CycleStrip t={t} />

              <div>
                <h2 className="mb-3 font-display text-base font-bold text-ink">
                  Claim status <span className="font-normal text-slate-500">— {t.n.toLocaleString()} claims</span>
                </h2>
                <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
                  <StatusCard label="Fully paid" count={t.full_n} total={t.n} color={C.paid} />
                  <StatusCard label="Partially paid" count={t.part_n} total={t.n} color="#3f97ad" />
                  <StatusCard label="Fully denied" count={t.den_n} total={t.n} color={C.denied} />
                  <StatusCard label="Awaiting RA" count={t.await_n} total={t.n} amount={t.awaiting} color={C.pending} />
                  <StatusCard label="Overdue RA" count={t.overdue_n} total={t.n} amount={t.overdue} color={C.overdue}
                              hint="No RA 45+ days after submission" alert={!!t.overdue_n} />
                  <StatusCard label="Resubmitted" count={t.resub} total={t.n} color={C.ink} hint={`${remitted.toLocaleString()} claims remitted`} />
                </div>
              </div>

              <div className={`rounded-2xl bg-white p-5 sm:p-6 ${SHADOW}`}>
                <div className="mb-4 flex flex-wrap items-start justify-between gap-3">
                  <div>
                    <h2 className="font-display text-base font-bold text-ink">Monthly submitted amount by cycle status</h2>
                    <p className="text-xs text-slate-500">Each bar is one encounter month; the amber part has no RA yet.</p>
                  </div>
                  <Legend items={SERIES.map((s) => [s.key, s.color])} />
                </div>
                <CycleChart data={chart} />
              </div>
            </>
          )}
        </div>
      )}
      </>)}
    </div>
  );
}
