/**
 * DashboardPortal.jsx — facility-facing claim dashboard
 *
 * Accessible at /portal/dashboard?token=<PORTAL_TOKEN>. No login: the token is checked server-side and
 * scopes the page to that client's facilities. Summaries only — no claim, patient or member IDs.
 */
import { useEffect, useState } from "react";
import axios from "axios";

const api = axios.create({ baseURL: "/api/portal" });
const token = () => new URLSearchParams(window.location.search).get("token") || "";
const fmt = (v) => Number(v || 0).toLocaleString("en-GB", { maximumFractionDigits: 0 });
const compact = (v) => (v >= 1e6 ? `${(v / 1e6).toFixed(2)}M` : v >= 1e3 ? `${(v / 1e3).toFixed(0)}K` : fmt(v));
const monthLabel = (m) => new Date(m + "-01T00:00:00").toLocaleDateString("en-GB", { month: "short", year: "2-digit" });

const C = { paid: "#0f7c97", denied: "#b3412e", pending: "#8a5d0b", pendingFill: "#e9c98a", ink: "#0b2c3c" };
const SERIES = [
  { key: "paid", label: "Paid", color: C.paid },
  { key: "denied", label: "Denied", color: C.denied },
  { key: "awaiting", label: "Awaiting RA", color: C.pendingFill },
];

/* ---------- Sparkline ---------- */
function Sparkline({ values, color }) {
  if (!values || values.length < 2) return <div className="h-10" />;
  const w = 120, h = 40, max = Math.max(...values, 1);
  if (values.length < 6) {   // few months: bars read better than a 2–5 point line
    const bw = w / values.length;
    return (
      <svg viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none" className="h-10 w-full" aria-hidden="true">
        {values.map((v, i) => {
          const bh = Math.max(2, (v / max) * (h - 3));
          return <rect key={i} x={i * bw + bw * 0.18} y={h - bh} width={bw * 0.64} height={bh} rx="2"
                       fill={color} opacity={i === values.length - 1 ? 0.9 : 0.35} />;
        })}
      </svg>
    );
  }
  const pts = values.map((v, i) => [(i / (values.length - 1)) * w, h - 3 - (v / max) * (h - 6)]);
  const line = pts.map((p) => p.join(",")).join(" ");
  const id = `sp${color.replace("#", "")}`;
  return (
    <svg viewBox={`0 0 ${w} ${h}`} className="h-10 w-full" preserveAspectRatio="none" aria-hidden="true">
      <defs>
        <linearGradient id={id} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor={color} stopOpacity="0.25" />
          <stop offset="100%" stopColor={color} stopOpacity="0" />
        </linearGradient>
      </defs>
      <polygon points={`0,${h} ${line} ${w},${h}`} fill={`url(#${id})`} />
      <polyline points={line} fill="none" stroke={color} strokeWidth="2" strokeLinejoin="round" strokeLinecap="round" vectorEffect="non-scaling-stroke" />
    </svg>
  );
}

/* ---------- KPI tile ---------- */
function Kpi({ label, value, sub, color, spark }) {
  return (
    <div className="rounded-2xl bg-white p-5 shadow-[0_8px_30px_rgba(11,34,51,0.08)] ring-1 ring-slate-900/5">
      <div className="flex items-center justify-between">
        <p className="text-[11px] font-semibold uppercase tracking-wider text-slate-500">{label}</p>
        <span className="flex h-7 w-7 items-center justify-center rounded-full" style={{ background: `${color}1f` }}>
          <span className="h-2.5 w-2.5 rounded-full" style={{ background: color }} />
        </span>
      </div>
      <p className="mt-2 font-display text-3xl font-bold tabular-nums text-ink lg:text-[2rem]">{value}</p>
      {sub && <p className="mt-0.5 text-xs text-slate-500">{sub}</p>}
      <div className="mt-3"><Sparkline values={spark} color={color} /></div>
    </div>
  );
}

/* ---------- Monthly stacked bar chart ---------- */
function MonthlyChart({ months }) {
  const [hover, setHover] = useState(null);
  const W = 720, H = 280, padL = 48, padB = 28, padT = 12;
  const totals = months.map((m) => SERIES.reduce((s, x) => s + Number(m[x.key] || 0), 0));
  const max = Math.max(...totals, 1);
  const ticks = [0, 0.25, 0.5, 0.75, 1].map((f) => f * max);
  const plotW = W - padL - 8, plotH = H - padB - padT;
  const slot = plotW / Math.max(months.length, 1);
  const bw = Math.min(44, slot * 0.62);
  const y = (v) => padT + plotH - (v / max) * plotH;
  const hm = hover != null ? months[hover] : null;

  return (
    <div className="relative">
      <svg viewBox={`0 0 ${W} ${H}`} className="h-auto w-full" role="img" aria-label="Monthly submitted amount by status">
        {ticks.map((t, i) => (
          <g key={i}>
            <line x1={padL} x2={W - 8} y1={y(t)} y2={y(t)} stroke="#e2e8f0" strokeDasharray={i ? "3 4" : ""} />
            <text x={padL - 8} y={y(t) + 4} textAnchor="end" fontSize="11" fill="#64748b">{compact(t)}</text>
          </g>
        ))}
        {months.map((m, i) => {
          const x = padL + i * slot + (slot - bw) / 2;
          let acc = 0;
          const segs = SERIES.map((s) => { const v = Number(m[s.key] || 0); const seg = { s, y0: acc, v }; acc += v; return seg; }).filter((g) => g.v > 0);
          const top = segs[segs.length - 1];
          return (
            <g key={m.month} onMouseEnter={() => setHover(i)} onMouseLeave={() => setHover(null)} onClick={() => setHover(i)} className="cursor-pointer">
              <rect x={padL + i * slot} y={padT} width={slot} height={plotH} fill={hover === i ? "#f1f5f9" : "transparent"} rx="6" />
              {segs.map((g) => {
                const yt = y(g.y0 + g.v), hh = y(g.y0) - yt, r = g === top ? Math.min(6, bw / 2, hh) : 0;
                return r > 0 ? (
                  <path key={g.s.key} fill={g.s.color}
                    d={`M${x},${yt + hh} V${yt + r} Q${x},${yt} ${x + r},${yt} H${x + bw - r} Q${x + bw},${yt} ${x + bw},${yt + r} V${yt + hh} Z`} />
                ) : <rect key={g.s.key} x={x} y={yt} width={bw} height={hh} fill={g.s.color} />;
              })}
              <text x={x + bw / 2} y={H - 8} textAnchor="middle" fontSize="11" fill="#64748b">{monthLabel(m.month)}</text>
            </g>
          );
        })}
      </svg>
      {hm && (
        <div className="pointer-events-none absolute top-2 rounded-xl bg-ink-deep/95 px-3 py-2 text-xs text-white shadow-lg"
          style={{ left: `${Math.min(62, Math.max(0, ((padL + hover * slot) / W) * 100))}%` }}>
          <p className="mb-1 font-display font-semibold">{monthLabel(hm.month)}</p>
          {SERIES.map((s) => (
            <p key={s.key} className="flex items-center gap-2 tabular-nums">
              <span className="h-2 w-2 rounded-full" style={{ background: s.color }} />{s.label}
              <span className="ml-auto pl-3">AED {fmt(hm[s.key])}</span>
            </p>
          ))}
        </div>
      )}
    </div>
  );
}

/* ---------- Ranked share list ---------- */
function Share({ title, rows, name, desc }) {
  return (
    <section className="rounded-2xl bg-white p-5 shadow-[0_8px_30px_rgba(11,34,51,0.06)] ring-1 ring-slate-900/5 sm:p-6">
      <h2 className="font-display text-base font-bold text-ink">{title}</h2>
      {!rows?.length && <p className="mt-3 text-sm text-slate-500">Nothing to show for this period.</p>}
      <ol className="mt-4 space-y-4">
        {rows?.map((r, i) => (
          <li key={i} className="flex gap-3">
            <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-lg bg-sage font-display text-xs font-bold text-ink">{i + 1}</span>
            <div className="min-w-0 flex-1">
              <div className="flex items-baseline justify-between gap-3">
                <p className="min-w-0 truncate text-sm font-semibold text-ink">{name(r)}</p>
                <p className="shrink-0 text-sm tabular-nums text-slate-700">AED {fmt(r.amount)}</p>
              </div>
              {desc?.(r) && <p className="text-xs text-slate-500">{desc(r)}</p>}
              <div className="mt-1.5 flex items-center gap-2">
                <div className="h-2 flex-1 overflow-hidden rounded-full bg-slate-100">
                  <div className="h-2 rounded-full bg-gradient-to-r from-denied to-denied/40" style={{ width: `${Math.min(100, r.share_pct)}%` }} />
                </div>
                <span className="w-11 text-right text-xs font-semibold tabular-nums text-slate-600">{r.share_pct}%</span>
              </div>
            </div>
          </li>
        ))}
      </ol>
    </section>
  );
}

function Skeleton() {
  const b = "animate-pulse rounded-2xl bg-white/80 ring-1 ring-slate-900/5";
  return (
    <div className="space-y-6" aria-busy="true" aria-label="Loading">
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">{[0, 1, 2, 3].map((i) => <div key={i} className={`${b} h-40`} />)}</div>
      <div className={`${b} h-80`} />
      <div className="grid gap-6 lg:grid-cols-2"><div className={`${b} h-64`} /><div className={`${b} h-64`} /></div>
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
  const ms = d?.months || [];
  const series = (k) => ms.map((m) => Number(m[k] || 0));
  const submitted = ms.map((m) => Number(m.paid || 0) + Number(m.denied || 0) + Number(m.awaiting || 0));

  return (
    <div className="min-h-screen overflow-x-hidden bg-[#f5f7f2]">
      {/* Hero */}
      <header className="relative overflow-hidden bg-gradient-to-br from-ink-deep via-ink to-ghaf-800 pb-24 text-white">
        <div className="pointer-events-none absolute inset-0 opacity-[0.07]"
          style={{ backgroundImage: "linear-gradient(#fff 1px, transparent 1px), linear-gradient(90deg, #fff 1px, transparent 1px)", backgroundSize: "36px 36px" }} />
        <div className="pointer-events-none absolute -right-24 -top-24 h-96 w-96 rounded-full bg-ghaf-400/30 blur-3xl" />
        <div className="relative mx-auto max-w-6xl px-4 pt-8 sm:px-6 sm:pt-12">
          <p className="text-xs font-semibold uppercase tracking-[0.2em] text-ghaf-200">Claim dashboard</p>
          <h1 className="mt-2 break-words font-display text-3xl font-extrabold leading-tight sm:text-4xl lg:text-5xl">{d?.facility || " "}</h1>
          <p className="mt-2 max-w-2xl text-sm text-white/70">Claims by encounter month and where each one is now. Prepared by Ghaf Medical Billing Services.</p>
          <div className="mt-6 flex flex-wrap items-center gap-2">
            {[3, 6, 12].map((m) => (
              <button key={m} onClick={() => setMonths(m)} aria-pressed={months === m}
                className={`rounded-full px-4 py-1.5 text-xs font-semibold transition ${months === m ? "bg-white text-ink shadow" : "bg-white/10 text-white ring-1 ring-white/20 hover:bg-white/20"}`}>
                Last {m} months</button>
            ))}
            {d?.facilities?.length > 1 && (
              <select aria-label="Facility" value={fid || d.facility_id} onChange={(e) => setFid(e.target.value)}
                className="max-w-full rounded-full border-0 bg-white/10 px-4 py-1.5 text-xs font-semibold text-white ring-1 ring-white/20 focus:outline-none focus:ring-2 focus:ring-white/60">
                {d.facilities.map((f) => <option key={f.id} value={f.id} className="text-ink">{f.name}</option>)}
              </select>
            )}
            {loading && d && <span className="text-xs text-white/60">Updating…</span>}
          </div>
        </div>
      </header>

      <main className="relative mx-auto -mt-16 max-w-6xl space-y-6 px-4 pb-12 sm:px-6">
        {err && (
          <div className="rounded-2xl bg-white p-6 shadow-[0_8px_30px_rgba(11,34,51,0.08)] ring-1 ring-denied/20">
            <p className="font-display text-lg font-bold text-denied">Dashboard unavailable</p>
            <p className="mt-1 text-sm text-slate-600">{err}</p>
          </div>
        )}
        {loading && !d && !err && <Skeleton />}

        {d && t && (
          <>
            <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
              <Kpi label="Submitted (net)" value={`AED ${compact(t.net)}`} sub={`${fmt(t.n)} claims`} color={C.ink} spark={submitted} />
              <Kpi label="Paid" value={`AED ${compact(t.paid)}`} sub={`${t.net ? Math.round(100 * t.paid / t.net) : 0}% of submitted`} color={C.paid} spark={series("paid")} />
              <Kpi label="Denied" value={`AED ${compact(t.denied)}`} sub={`${remitted ? (100 * t.denied / remitted).toFixed(1) : 0}% of remitted`} color={C.denied} spark={series("denied")} />
              <Kpi label="Awaiting RA" value={`AED ${compact(t.awaiting)}`} sub={`${fmt(t.await_n)} claims`} color={C.pending} spark={series("awaiting")} />
            </div>

            <section className="rounded-2xl bg-white p-5 shadow-[0_8px_30px_rgba(11,34,51,0.06)] ring-1 ring-slate-900/5 sm:p-6">
              <div className="mb-4 flex flex-wrap items-start justify-between gap-3">
                <div>
                  <h2 className="font-display text-base font-bold text-ink">Monthly submitted amount by status</h2>
                  <p className="text-xs text-slate-500">Each bar is one encounter month; the amber part has no RA yet.</p>
                </div>
                <div className="flex flex-wrap gap-2">
                  {SERIES.map((s) => (
                    <span key={s.key} className="inline-flex items-center gap-1.5 rounded-full bg-slate-50 px-2.5 py-1 text-xs font-medium text-slate-600 ring-1 ring-slate-200">
                      <span className="h-2 w-2 rounded-full" style={{ background: s.color }} />{s.label}
                    </span>
                  ))}
                </div>
              </div>
              <MonthlyChart months={ms} />
            </section>

            <div className="grid gap-6 lg:grid-cols-2">
              <Share title="Why claims are denied (unpaid amount)" rows={d.denial_reasons} name={(r) => r.code} desc={(r) => r.description} />
              <Share title="Unpaid amount by payer" rows={d.payers} name={(r) => r.payer} />
            </div>

            <p className="text-center text-xs text-slate-500">Figures as of {d.as_of}. Denied = net not paid after all remittance advices, including resubmissions.</p>
          </>
        )}
      </main>
    </div>
  );
}
