import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import apiClient from "../api/client";

const TYPES = {
  denial_code_spike: "TPA denial spike (weekly)",
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

export default function Alerts() {
  const [data, setData] = useState(null);
  const [err, setErr] = useState(null);
  const [loading, setLoading] = useState(false);
  const [type, setType] = useState("");

  const load = (refresh = false) => {
    setLoading(true);
    apiClient.get(`/alerts${refresh ? "?refresh=true" : ""}`)
      .then(r => { setData(r.data); setErr(null); })
      .catch(e => setErr(e.response?.data?.detail || "Could not load alerts"))
      .finally(() => setLoading(false));
  };
  useEffect(() => { load(); }, []);

  const items = (data?.items || []).filter(a => !type || a.type === type);
  const counts = (data?.items || []).reduce((m, a) => ({ ...m, [a.type]: (m[a.type] || 0) + 1 }), {});

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <p className="gh-eyebrow">Overview</p>
          <h1 className="gh-h1">Facility alerts</h1>
          <p className="mt-1 max-w-3xl text-sm text-slate-500">
            Problems worth acting on, per facility. Each facility is compared with its own usual
            pattern; claim timelines follow DHA eClaimLink Standards Notice SN 03/2019
            (submit by the 15th, RA within 45 days, resubmit within 21 days).
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
          <div className="flex flex-wrap gap-2">
            <button onClick={() => setType("")}
              className={`gh-chip ${type === "" ? "gh-chip-brand" : "gh-chip-neutral"}`}>
              All ({data.count})
            </button>
            {Object.entries(TYPES).map(([k, label]) => (
              <button key={k} onClick={() => setType(k)} disabled={!counts[k]}
                className={`gh-chip ${type === k ? "gh-chip-brand" : "gh-chip-neutral"} ${counts[k] ? "" : "opacity-40"}`}>
                {label} ({counts[k] || 0})
              </button>
            ))}
          </div>

          <section className="gh-section">
            {items.length === 0 ? (
              <p className="text-sm text-slate-600">No alerts. Every facility is within its usual range.</p>
            ) : (
              <ul className="divide-y divide-slate-100">
                {items.map((a, i) => (
                  <li key={i} className="flex flex-wrap items-start gap-3 py-3">
                    <span className={`gh-chip mt-0.5 ${a.severity === "high" ? "gh-chip-denied" : "gh-chip-pending"}`}>
                      {a.severity === "high" ? "High" : "Watch"}
                    </span>
                    <div className="min-w-0 flex-1">
                      <p className="font-medium text-ink">
                        {a.facility_name} <span className="font-normal text-slate-400">· {TYPES[a.type]}</span>
                      </p>
                      <p className="text-sm text-slate-800">{a.title}</p>
                      <p className="text-xs text-slate-500">{a.detail}</p>
                    </div>
                    <Link to={a.link} className="text-sm font-medium text-ghaf-600 hover:underline">Open →</Link>
                  </li>
                ))}
              </ul>
            )}
            <p className="mt-3 text-xs text-slate-400">
              Checked {new Date(data.generated_at).toLocaleString("en-GB")} · refreshed hourly
            </p>
          </section>

          <section className="gh-section">
            <h2 className="gh-section-title">Learned defaults per facility</h2>
            <p className="mb-3 mt-0.5 text-sm text-slate-500">
              What counts as normal for each facility, learned from its last 3–6 months. An alert fires when
              the latest value crosses the threshold.
            </p>
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead className="text-left text-xs uppercase tracking-wide text-slate-500">
                  <tr>
                    <th className="py-2 pr-3">Facility</th>
                    <th className="py-2 pr-3">Submits</th>
                    <th className="py-2 pr-3">Alert if no claims for</th>
                    <th className="py-2 pr-3">Last month's services by now</th>
                    <th className="py-2 pr-3">Denial rate (usual → alert)</th>
                    <th className="py-2 pr-3">RA ≤ 45 days (usual → alert)</th>
                    <th className="py-2 pr-3">Paid at 1st RA (usual → alert)</th>
                    <th className="py-2">No RA &gt; 45 d</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {data.baselines.map(b => (
                    <tr key={b.facility_id}>
                      <td className="py-2 pr-3 font-medium">{b.facility_name}</td>
                      <td className="py-2 pr-3">{b.submission_pattern || "—"}</td>
                      <td className="py-2 pr-3">{b.submission_gap_allowed ? `${b.submission_gap_allowed} days (now ${b.days_since_submission})` : "—"}</td>
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
          </section>
        </>
      )}
    </div>
  );
}
