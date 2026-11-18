import { useEffect, useState } from "react";
import apiClient from "../api/client";

const fmt = v => (v == null ? "—" : Number(v).toLocaleString("en-GB", { maximumFractionDigits: 0 }));

function Bars({ title, rows, label, sub }) {
  if (!rows?.length) return null;
  const max = Math.max(...rows.map(r => r.lines));
  return (
    <div>
      <p className="gh-eyebrow mb-2">{title}</p>
      <ul className="space-y-1.5">
        {rows.map((r, i) => (
          <li key={i} className="text-sm">
            <div className="flex items-baseline justify-between gap-3">
              <span className="min-w-0 text-slate-800">{label(r)}{sub && sub(r) && <span className="ml-1 text-xs text-slate-400">{sub(r)}</span>}</span>
              <span className="whitespace-nowrap text-xs text-slate-500">{fmt(r.lines)} lines · AED {fmt(r.amount)} · {r.share_pct}%</span>
            </div>
            <div className="mt-0.5 h-1.5 rounded bg-slate-100">
              <div className="h-1.5 rounded bg-ghaf-600" style={{ width: `${(100 * r.lines) / max}%` }} />
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}

export default function DenialDrill({ code, payer, facilityId, month, onClose }) {
  const [d, setD] = useState(null);
  const [err, setErr] = useState(null);
  const [busy, setBusy] = useState(false);
  const q = new URLSearchParams({ code });
  if (payer) q.set("payer", payer);
  if (facilityId) q.set("facility_id", facilityId);
  if (month) q.set("month", month);

  useEffect(() => {
    setD(null); setErr(null);
    apiClient.get(`/denial-trends/drilldown?${q}`, { timeout: 180000 })
      .then(r => setD(r.data)).catch(e => setErr(e.response?.data?.detail || "Could not load the breakdown"));
    const esc = e => e.key === "Escape" && onClose();
    window.addEventListener("keydown", esc);
    return () => window.removeEventListener("keydown", esc);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [code, payer, facilityId, month]);

  const excel = async () => {
    setBusy(true);
    try {
      const r = await apiClient.get(`/denial-trends/drilldown.xlsx?${q}`, { responseType: "blob", timeout: 300000 });
      const name = ((r.headers["content-disposition"] || "").match(/filename="([^"]+)"/) || [])[1] || `denial_${code}.xlsx`;
      const url = URL.createObjectURL(r.data);
      const a = document.createElement("a"); a.href = url; a.download = name; a.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } finally { setBusy(false); }
  };

  return (
    <div className="fixed inset-0 z-50 flex justify-end bg-black/30" onClick={onClose} role="dialog" aria-modal="true">
      <aside className="h-full w-full max-w-3xl overflow-y-auto bg-white p-6 shadow-xl" onClick={e => e.stopPropagation()}>
        <div className="flex items-start justify-between gap-3">
          <div>
            <p className="gh-eyebrow">Why {code}? · {month ? `RA ${month}` : "window"}{payer ? ` · ${payer}` : ""}</p>
            <h2 className="text-xl font-semibold text-ink">{d?.description || code}</h2>
          </div>
          <div className="flex gap-2">
            <button className="gh-btn-secondary whitespace-nowrap text-sm" disabled={busy || !d?.lines} onClick={excel}>
              {busy ? "Preparing…" : "Excel: all lines"}
            </button>
            <button className="gh-btn-secondary text-sm" onClick={onClose} aria-label="Close">✕</button>
          </div>
        </div>

        {err && <p className="mt-4 text-sm text-denied">{err}</p>}
        {!d && !err && <p className="mt-6 text-sm text-slate-500">Reading the remittances…</p>}

        {d && (
          <div className="mt-5 space-y-6">
            <div className="grid grid-cols-3 gap-3">
              <div className="gh-card p-3"><p className="gh-label">Denied lines</p><p className="text-lg font-semibold">{fmt(d.lines)}</p></div>
              <div className="gh-card p-3"><p className="gh-label">Denied AED</p><p className="text-lg font-semibold">{fmt(d.amount)}</p></div>
              <div className="gh-card p-3"><p className="gh-label">With a TPA reason</p>
                <p className="text-lg font-semibold">{d.lines ? Math.round(d.comment_coverage_pct - d.reference_only_pct) : 0}%</p></div>
            </div>
            {d.reference_only_pct >= 25 && (
              <p className="rounded-md bg-pending/10 p-3 text-sm text-pending">
                {d.reference_only_pct}% of these denials carry only a reference number (e.g. “tag A4…”) instead of a reason.
                SN 03/2019 6.1.3 makes a denial comment mandatory — worth raising with the TPA.
              </p>
            )}
            <Bars title="TPA reasons (comment themes)" rows={d.themes} label={r => r.theme} />
            <Bars title="Services denied" rows={d.activities} label={r => r.activity_code} sub={r => r.description} />
            <div className="grid gap-6 md:grid-cols-2">
              <Bars title="Clinicians" rows={d.clinicians} label={r => r.clinician_id} sub={r => r.name} />
              <Bars title="Principal diagnosis" rows={d.diagnoses} label={r => r.diagnosis} />
            </div>
            <div className="grid gap-6 md:grid-cols-2">
              {!payer && <Bars title="Receivers / TPAs" rows={d.payers} label={r => r.payer} sub={r => r.payer_name} />}
              {!facilityId && <Bars title="Facilities" rows={d.facilities} label={r => r.facility} />}
            </div>
            <div>
              <p className="gh-eyebrow mb-2">Largest denied lines</p>
              <div className="overflow-x-auto">
                <table className="w-full text-xs">
                  <thead className="text-left text-slate-500"><tr>
                    <th className="py-1 pr-2">Claim</th><th className="pr-2">Service</th><th className="pr-2">Diagnosis</th>
                    <th className="pr-2 text-right">AED</th><th>TPA comment</th></tr></thead>
                  <tbody className="divide-y divide-slate-100">
                    {d.samples.map((s, i) => (
                      <tr key={i} className="align-top">
                        <td className="py-1 pr-2 font-mono">{s.claim_id}<span className="block font-sans text-slate-400">{s.facility_name} · {s.ra_date}</span></td>
                        <td className="pr-2">{s.activity_code}</td>
                        <td className="pr-2">{s.diagnosis || "—"}</td>
                        <td className="pr-2 text-right">{fmt(s.amount)}</td>
                        <td className="max-w-xs text-slate-600">{s.comment || "—"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          </div>
        )}
      </aside>
    </div>
  );
}
