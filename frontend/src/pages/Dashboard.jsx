import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import {
  AreaChart, Area,
  BarChart, Bar, Cell,
  XAxis, YAxis, CartesianGrid, Tooltip, Legend, ResponsiveContainer,
} from "recharts";
import apiClient from "../api/client";

// ─── Helpers ──────────────────────────────────────────────────────────────────
const AED = (v) =>
  new Intl.NumberFormat("en-AE", {
    style: "currency", currency: "AED", maximumFractionDigits: 0,
  }).format(Number(v || 0));

const compact = (v) => {
  const n = Number(v || 0);
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(0)}K`;
  return n.toLocaleString();
};

// ─── KPI Stat Card ────────────────────────────────────────────────────────────
function StatCard({ label, value, sub, accent, icon }) {
  return (
    <div className="gh-section flex flex-col gap-1">
      <div className="flex items-center justify-between">
        <p className="gh-label">{label}</p>
        {icon && <span className="text-lg">{icon}</span>}
      </div>
      <p className={`text-2xl font-bold ${accent || "text-slate-900"}`}>{value}</p>
      {sub && <p className="text-xs text-slate-600">{sub}</p>}
    </div>
  );
}

// ─── Section Header ───────────────────────────────────────────────────────────
function SectionTitle({ children }) {
  return (
    <h2 className="gh-label mb-3">
      {children}
    </h2>
  );
}

// ─── Custom Tooltip ───────────────────────────────────────────────────────────
function MoneyTooltip({ active, payload, label }) {
  if (!active || !payload?.length) return null;
  return (
    <div className="rounded-lg border border-slate-200 bg-white px-3 py-2 shadow-lg text-xs">
      <p className="font-semibold text-slate-700 mb-1">{label}</p>
      {payload.map((p) => (
        <p key={p.dataKey} style={{ color: p.color }}>
          {p.name}: {AED(p.value)}
        </p>
      ))}
    </div>
  );
}

// ─── Main Dashboard ───────────────────────────────────────────────────────────
export default function Dashboard() {
  const [summary, setSummary]   = useState(null);
  const [trend, setTrend]       = useState([]);
  const [loading, setLoading]   = useState(true);
  const [error, setError]       = useState(null);

  useEffect(() => {
    // Limit submissions trend to last 13 months — avoids full-table scan on large DBs
    const dateFrom = new Date();
    dateFrom.setMonth(dateFrom.getMonth() - 13);
    const dateFromStr = dateFrom.toISOString().split("T")[0];

    Promise.all([
      apiClient.get("/analytics/summary"),
      apiClient.get(`/analytics/submissions?date_from=${dateFromStr}`),
    ])
      .then(([sumRes, subRes]) => {
        setSummary(sumRes.data);
        // Keep last 12 months of trend data
        const raw = subRes.data?.monthly_trend || [];
        setTrend(raw.slice(-12));
      })
      .catch((err) =>
        setError(err.response?.data?.detail || "Failed to load dashboard data")
      )
      .finally(() => setLoading(false));
  }, []);

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64 text-sm text-slate-400">
        Loading dashboard…
      </div>
    );
  }

  if (error) {
    return <p className="text-sm text-red-600">{error}</p>;
  }

  // Derive collection rate
  const collectionRate =
    summary && summary.total_net_amount > 0
      ? Math.round((summary.total_paid_amount / summary.total_net_amount) * 100)
      : 0;

  // Financial bar data (single grouped bar)
  const finBar = summary
    ? [
        { name: "Net Submitted", amount: summary.total_net_amount },
        { name: "Collected",     amount: summary.total_paid_amount },
        { name: "Denied",        amount: summary.total_denial_amount },
      ]
    : [];

  return (
    <div className="space-y-6">
      {/* ── Page Header ── */}
      <div>
        <p className="gh-eyebrow">01 — Overview</p>
        <h1 className="gh-h1">Overview</h1>
        <p className="text-sm text-slate-500 mt-0.5">
          Live snapshot of claims, collections, and pipeline health.
        </p>
      </div>

      {/* ── KPI Cards ── */}
      {summary && (
        <>
          <div>
            <SectionTitle>Claims Pipeline</SectionTitle>
            <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
              <StatCard
                label="Total Claims"
                value={compact(summary.total_claims)}
                icon="📋"
              />
              <StatCard
                label="Net Submitted"
                value={AED(summary.total_net_amount)}
                icon="📤"
              />
              <StatCard
                label="Total Collected"
                value={AED(summary.total_paid_amount)}
                sub={`${collectionRate}% collection rate`}
                accent="text-emerald-600"
                icon="✅"
              />
              <StatCard
                label="Total Denied"
                value={AED(summary.total_denial_amount)}
                accent="text-red-600"
                icon="🚫"
              />
            </div>
          </div>

          <div>
            <SectionTitle>System Health</SectionTitle>
            <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
              <StatCard
                label="Clinical Profiles"
                value={compact(summary.active_clinical_profiles)}
                sub="Active"
                icon="👤"
              />
              <StatCard
                label="Pending DHA XML"
                value={compact(summary.pending_raw_xml)}
                accent={summary.pending_raw_xml > 0 ? "text-amber-600" : undefined}
                icon="⏳"
              />
              <StatCard
                label="Quarantined XML"
                value={compact(summary.quarantined_raw_xml)}
                accent={summary.quarantined_raw_xml > 0 ? "text-red-600" : undefined}
                icon="⚠️"
              />
              <StatCard
                label="Logins (24h)"
                value={compact(summary.recent_logins_24h)}
                icon="🔑"
              />
            </div>
          </div>
        </>
      )}

      {/* ── Charts Row ── */}
      <div className="grid grid-cols-1 gap-6 xl:grid-cols-3">

        {/* Monthly Submission Trend — takes 2/3 width */}
        <div className="xl:col-span-2 gh-section">
          <h3 className="text-sm font-semibold text-slate-900">Monthly Submission Trend</h3>
          <p className="text-xs text-slate-600 mt-0.5 mb-4">
            Claimed vs collected vs denied (last 12 months, AED)
          </p>
          {trend.length > 0 ? (
            <ResponsiveContainer width="100%" height={260}>
              <AreaChart data={trend} margin={{ top: 4, right: 8, left: 0, bottom: 0 }}>
                <defs>
                  <linearGradient id="gClaimed" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="5%"  stopColor="#0ea5e9" stopOpacity={0.18} />
                    <stop offset="95%" stopColor="#0ea5e9" stopOpacity={0} />
                  </linearGradient>
                  <linearGradient id="gPaid" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="5%"  stopColor="#10b981" stopOpacity={0.18} />
                    <stop offset="95%" stopColor="#10b981" stopOpacity={0} />
                  </linearGradient>
                  <linearGradient id="gRejected" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="5%"  stopColor="#ef4444" stopOpacity={0.12} />
                    <stop offset="95%" stopColor="#ef4444" stopOpacity={0} />
                  </linearGradient>
                </defs>
                <CartesianGrid strokeDasharray="3 3" stroke="#f1f5f9" />
                <XAxis
                  dataKey="month"
                  tick={{ fontSize: 10, fill: "#94a3b8" }}
                  tickLine={false}
                  axisLine={false}
                />
                <YAxis
                  tick={{ fontSize: 10, fill: "#94a3b8" }}
                  tickLine={false}
                  axisLine={false}
                  tickFormatter={(v) => `${compact(v)}`}
                  width={52}
                />
                <Tooltip content={<MoneyTooltip />} />
                <Legend
                  wrapperStyle={{ fontSize: 11, paddingTop: 8 }}
                  iconType="circle"
                  iconSize={8}
                />
                <Area
                  type="monotone"
                  dataKey="claimed_amount"
                  name="Claimed"
                  stroke="#0ea5e9"
                  strokeWidth={2}
                  fill="url(#gClaimed)"
                  dot={false}
                />
                <Area
                  type="monotone"
                  dataKey="paid_amount"
                  name="Collected"
                  stroke="#10b981"
                  strokeWidth={2}
                  fill="url(#gPaid)"
                  dot={false}
                />
                <Area
                  type="monotone"
                  dataKey="rejected_amount"
                  name="Rejected"
                  stroke="#ef4444"
                  strokeWidth={2}
                  fill="url(#gRejected)"
                  dot={false}
                />
              </AreaChart>
            </ResponsiveContainer>
          ) : (
            <div className="flex items-center justify-center h-48 text-sm text-slate-400">
              No submission data yet
            </div>
          )}
        </div>

        {/* Financial Breakdown — takes 1/3 width */}
        <div className="gh-section">
          <h3 className="text-sm font-semibold text-slate-900">Financial Summary</h3>
          <p className="text-xs text-slate-600 mt-0.5 mb-4">All-time totals (AED)</p>
          {finBar.length > 0 ? (
            <ResponsiveContainer width="100%" height={260}>
              <BarChart
                data={finBar}
                layout="vertical"
                margin={{ top: 0, right: 8, left: 0, bottom: 0 }}
                barSize={20}
              >
                <CartesianGrid strokeDasharray="3 3" horizontal={false} stroke="#f1f5f9" />
                <XAxis
                  type="number"
                  tick={{ fontSize: 10, fill: "#94a3b8" }}
                  tickLine={false}
                  axisLine={false}
                  tickFormatter={(v) => compact(v)}
                />
                <YAxis
                  type="category"
                  dataKey="name"
                  tick={{ fontSize: 11, fill: "#475569" }}
                  tickLine={false}
                  axisLine={false}
                  width={96}
                />
                <Tooltip
                  formatter={(v) => AED(v)}
                  cursor={{ fill: "#f8fafc" }}
                />
                <Bar dataKey="amount" name="Amount" radius={[0, 6, 6, 0]}>
                  {finBar.map((entry, i) => (
                    <Cell
                      key={i}
                      fill={
                        entry.name === "Collected"
                          ? "#10b981"
                          : entry.name === "Denied"
                          ? "#ef4444"
                          : "#0ea5e9"
                      }
                    />
                  ))}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          ) : (
            <div className="flex items-center justify-center h-48 text-sm text-slate-400">
              No financial data yet
            </div>
          )}
        </div>
      </div>

      {/* ── Navigation Cards ── */}
      <div>
        <SectionTitle>Quick Access</SectionTitle>
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
          <Link
            to="/clinical-profiles"
            className="gh-section transition hover:border-ghaf-400 hover:shadow-md"
          >
            <div className="text-xl mb-1">👤</div>
            <h2 className="font-semibold text-slate-900">Clinical Profiles</h2>
            <p className="mt-1 text-sm text-slate-500">
              Set up and manage patient clinical profiles.
            </p>
          </Link>
          <Link
            to="/dha-sync"
            className="gh-section transition hover:border-ghaf-400 hover:shadow-md"
          >
            <div className="text-xl mb-1">🔄</div>
            <h2 className="font-semibold text-slate-900">DHA XML Sync</h2>
            <p className="mt-1 text-sm text-slate-500">
              Upload and trigger the DHA Shafafiya sync pipeline.
            </p>
          </Link>
          <Link
            to="/analytics"
            className="gh-section transition hover:border-ghaf-400 hover:shadow-md"
          >
            <div className="text-xl mb-1">📊</div>
            <h2 className="font-semibold text-slate-900">Analytics &amp; Reports</h2>
            <p className="mt-1 text-sm text-slate-500">
              Payer breakdowns, submissions, remittance, and denial analysis.
            </p>
          </Link>
        </div>
      </div>
    </div>
  );
}
