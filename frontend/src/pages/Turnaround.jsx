import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import apiClient from "../api/client";
import { useAuth } from "../context/AuthContext";
import { readFacility, saveFacility } from "../utils/facility";

const fmtMoney = (n) => (n == null ? "—" : Number(n).toLocaleString("en-GB", { minimumFractionDigits: 2, maximumFractionDigits: 2 }));
const fmtNum = (n) => (n == null ? "—" : Number(n).toLocaleString("en-GB"));
const fmtPct = (n) => (n == null ? "—" : `${n}%`);
const fmtDay = (d) => (d ? String(d).slice(0, 10).split("-").reverse().join("/") : "");

function DaysChip({ days, limit }) {
  if (days == null) return <span className="gh-chip gh-chip-neutral">—</span>;
  const cls = days > limit * 2 ? "gh-chip-denied" : days > limit ? "gh-chip-pending" : "gh-chip-paid";
  return <span className={`gh-chip whitespace-nowrap ${cls}`}>{days} d</span>;
}

async function downloadXlsx(params, setBusy) {
  const q = new URLSearchParams(Object.entries(params).filter(([, v]) => v !== "" && v != null));
  setBusy(true);
  try {
    const r = await apiClient.get(`/turnaround/overdue.xlsx?${q}`, { responseType: "blob", timeout: 300000 });
    const cd = r.headers["content-disposition"] || "";
    const name = (cd.match(/filename="([^"]+)"/) || [])[1] || "overdue_claims.xlsx";
    const url = URL.createObjectURL(r.data);
    const a = document.createElement("a");
    a.href = url; a.download = name; a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  } finally {
    setBusy(false);
  }
}

export default function Turnaround() {
  const { user } = useAuth() || {};
  const isAdmin = user?.role === "admin" || !user?.facility_id;
  const [facilities, setFacilities] = useState([]);
  const [params] = useSearchParams();
  // a link (e.g. from Facility alerts) can name the facility: ?facility_id=7; otherwise the last one used
  const [facilityId, setFacilityId] = useState(() => (isAdmin ? (params.get("facility_id") ?? readFacility("")) : String(user?.facility_id || "")));
  const [months, setMonths] = useState(6);
  const [days, setDays] = useState(45);
  const [daysInput, setDaysInput] = useState("45");
  const [payer, setPayer] = useState("");
  const [receiver, setReceiver] = useState("");
  const [open, setOpen] = useState({});
  const [busy, setBusy] = useState(false);
  const [summary, setSummary] = useState(null);
  const [overdue, setOverdue] = useState(null);
  const [loadingS, setLoadingS] = useState(true);
  const [loadingO, setLoadingO] = useState(true);
  const [error, setError] = useState(null);
  const [sort, setSort] = useState("overdue_amount");

  useEffect(() => {
    if (!isAdmin) return;
    apiClient.get("/facilities")
      .then((r) => setFacilities(r.data.filter((f) => f.facility_code !== "GHAF-001" && f.is_active !== false)))
      .catch(() => {});
  }, [isAdmin]);

  useEffect(() => {
    if (isAdmin && facilityId) saveFacility(facilityId);
    setLoadingS(true); setError(null);
    const q = new URLSearchParams({ months: String(months), days: String(days) });
    if (facilityId) q.set("facility_id", facilityId);
    apiClient.get(`/turnaround/summary?${q}`, { timeout: 180000 })
      .then((r) => setSummary(r.data))
      .catch((e) => setError(e.response?.data?.detail || "Couldn't load payer turnaround."))
      .finally(() => setLoadingS(false));
  }, [facilityId, months, days, isAdmin]);

  useEffect(() => {
    setLoadingO(true);
    const q = new URLSearchParams({ days: String(days), months: "12", limit: "2000" });
    if (facilityId) q.set("facility_id", facilityId);
    if (payer) q.set("payer", payer);
    if (receiver) q.set("receiver", receiver);
    apiClient.get(`/turnaround/overdue?${q}`, { timeout: 180000 })
      .then((r) => setOverdue(r.data))
      .catch((e) => setError(e.response?.data?.detail || "Couldn't load overdue claims."))
      .finally(() => setLoadingO(false));
  }, [facilityId, days, payer, receiver]);

  const tpas = useMemo(() => {
    const rows = [...(summary?.tpas || [])];
    rows.sort((a, b) => (b[sort] ?? -1) - (a[sort] ?? -1));
    return rows;
  }, [summary, sort]);

  const selectFor = (rec, pay) => {
    setReceiver(rec || ""); setPayer(pay || "");
    setTimeout(() => document.getElementById("overdue-list")?.scrollIntoView({ behavior: "smooth" }), 50);
  };
  const xlsx = (rec, pay) => downloadXlsx({ facility_id: facilityId, receiver: rec, payer: pay, days, months: 12 }, setBusy);
  const selectedLabel = (() => {
    const t = (summary?.tpas || []).find((x) => x.receiver_id === receiver);
    const p = t?.payers.find((x) => x.payer_id === payer);
    return [t && (t.receiver_name || t.receiver_id), p && (p.payer_name || p.payer_id)].filter(Boolean).join(" › ");
  })();

  const t = summary?.total;
  const showFacility = !facilityId;
  const applyDays = () => {
    const n = parseInt(daysInput, 10);
    if (n >= 1 && n <= 730) setDays(n); else setDaysInput(String(days));
  };
  const SortTh = ({ k, children }) => (
    <th className="cursor-pointer select-none px-3 py-2 text-right" onClick={() => setSort(k)}>
      {children}{sort === k ? " ↓" : ""}
    </th>
  );

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <p className="gh-eyebrow">Receivables</p>
          <h1 className="gh-h1">Payer turnaround &amp; overdue claims</h1>
          <p className="mt-0.5 text-sm text-slate-500">
            Days from first submission to the first remittance, per payer, and claims still waiting for an RA after {days} days.
          </p>
        </div>
        {summary?.as_of && <p className="text-xs text-slate-500">Figures as of {summary.as_of}</p>}
      </div>

      <div className="gh-card flex flex-wrap items-end gap-3 p-4">
        {isAdmin && (
          <label className="flex flex-col gap-1 text-xs text-slate-600">
            Facility
            <select className="gh-input min-w-[240px]" value={facilityId} onChange={(e) => { setFacilityId(e.target.value); setPayer(""); }}>
              <option value="">All facilities</option>
              {facilities.map((f) => <option key={f.id} value={f.id}>{f.facility_name}</option>)}
            </select>
          </label>
        )}
        <label className="flex flex-col gap-1 text-xs text-slate-600">
          Submitted in the last
          <select className="gh-input" value={months} onChange={(e) => setMonths(Number(e.target.value))}>
            {[3, 6, 12, 24].map((m) => <option key={m} value={m}>{m} months</option>)}
          </select>
        </label>
        <label className="flex flex-col gap-1 text-xs text-slate-600">
          Overdue after (days)
          <input type="number" min="1" max="730" className="gh-input w-28" value={daysInput}
                 onChange={(e) => setDaysInput(e.target.value)} onBlur={applyDays}
                 onKeyDown={(e) => e.key === "Enter" && applyDays()} />
        </label>
      </div>

      {error && <div className="gh-card border-red-200 bg-red-50 p-3 text-sm text-red-700">{error}</div>}

      {t && (
        <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
          {[
            ["Claims submitted", fmtNum(t.claims)],
            ["Remitted", `${fmtNum(t.remitted)} (${t.claims ? Math.round((100 * t.remitted) / t.claims) : 0}%)`],
            ["Awaiting RA", `${fmtNum(t.outstanding_count)} · ${fmtMoney(t.outstanding_amount)}`],
            [`Overdue > ${days} d`, `${fmtNum(t.overdue_count)} · ${fmtMoney(t.overdue_amount)}`],
          ].map(([k, v]) => (
            <div key={k} className="gh-card p-4">
              <p className="gh-eyebrow">{k}</p>
              <p className="mt-1 text-lg font-semibold text-slate-800">{v}</p>
            </div>
          ))}
        </div>
      )}

      <section id="overdue-list">
        <div className="gh-card overflow-hidden">
          <div className="flex flex-wrap items-center justify-between gap-2 border-b border-slate-100 px-4 py-3">
            <div>
              <h2 className="font-semibold text-slate-800">Turnaround by receiver (TPA) › payer</h2>
              <p className="text-xs text-slate-500">Claims go to the receiver (TPA or insurer). Click a receiver to see the payers under it.</p>
            </div>
            <div className="flex items-center gap-2">
              {loadingS && <span className="text-xs text-slate-500">Loading…</span>}
              <button className="gh-btn-secondary text-sm" disabled={busy || !t?.overdue_count} onClick={() => xlsx("", "")}>
                {busy ? "Preparing…" : "Excel: all overdue"}
              </button>
            </div>
          </div>
          <div className="overflow-x-auto">
            <table className="min-w-full text-[13px]">
              <thead className="bg-slate-50 text-xs uppercase text-slate-500">
                <tr>
                  <th className="px-3 py-2 text-left">Receiver › payer</th>
                  <SortTh k="claims">Claims</SortTh>
                  <SortTh k="remitted_pct">Remitted</SortTh>
                  <SortTh k="avg_days">Avg days</SortTh>
                  <th className="hidden px-3 py-2 text-right 2xl:table-cell" title="Share remitted within 30 / 45 / 60 days">RA within<br/>30·45·60 d</th>
                  <SortTh k="outstanding_amount">Awaiting RA</SortTh>
                  <SortTh k="overdue_amount">Overdue</SortTh>
                  <th className="px-3 py-2" />
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {tpas.map((tp) => {
                  const isOpen = open[tp.receiver_id];
                  const rows = [{ ...tp, _tpa: true }].concat(isOpen && !tp.is_direct ? tp.payers : []);
                  return rows.map((p) => (
                    <tr key={p._tpa ? `t-${tp.receiver_id}` : `p-${tp.receiver_id}-${p.facility_id}-${p.payer_id}`}
                        className={p._tpa ? "cursor-pointer bg-white hover:bg-slate-50" : "bg-slate-50/60"}
                        onClick={p._tpa && !tp.is_direct ? () => setOpen((o) => ({ ...o, [tp.receiver_id]: !o[tp.receiver_id] })) : undefined}>
                      <td className={`min-w-[220px] max-w-[300px] px-3 py-2 leading-snug ${p._tpa ? "font-medium text-slate-800" : "pl-9 text-slate-700"}`}>
                        {p._tpa ? (
                          <>
                            <span className="mr-1 inline-block w-3 text-slate-400">{tp.is_direct ? "" : isOpen ? "▾" : "▸"}</span>
                            {tp.receiver_name || tp.receiver_id}
                            <span className="ml-1 text-xs font-normal text-slate-400">
                              {tp.receiver_id}{tp.is_direct ? " · direct" : ` · ${tp.payers.length} payer${tp.payers.length > 1 ? "s" : ""}`}
                            </span>
                          </>
                        ) : (
                          <>
                            {p.payer_name || p.payer_id}
                            <span className="ml-1 text-xs text-slate-400">{p.payer_id}{showFacility ? ` · ${p.facility_name}` : ""}</span>
                          </>
                        )}
                      </td>
                      <td className="px-3 py-2 text-right">{fmtNum(p.claims)}</td>
                      <td className="whitespace-nowrap px-3 py-2 text-right">{fmtNum(p.remitted)} <span className="block text-xs text-slate-400">{fmtPct(p.remitted_pct)}</span></td>
                      <td className="px-3 py-2 text-right" title={`RA within 30 / 45 / 60 days: ${fmtPct(p.within_30_pct)} / ${fmtPct(p.within_45_pct)} / ${fmtPct(p.within_60_pct)}`}><DaysChip days={p.avg_days} limit={days} /></td>
                      <td className="hidden whitespace-nowrap px-3 py-2 text-right text-xs text-slate-600 2xl:table-cell">
                        {fmtPct(p.within_30_pct)} · {fmtPct(p.within_45_pct)} · {fmtPct(p.within_60_pct)}
                      </td>
                      <td className="whitespace-nowrap px-3 py-2 text-right">{fmtNum(p.outstanding_count)} <span className="block text-xs text-slate-400">{fmtMoney(p.outstanding_amount)}</span></td>
                      <td className="whitespace-nowrap px-3 py-2 text-right">
                        {p.overdue_count ? <span className="font-medium text-red-700">{fmtNum(p.overdue_count)}</span> : "0"}
                        <span className="block text-xs text-slate-400">{fmtMoney(p.overdue_amount)}</span>
                      </td>
                      <td className="whitespace-nowrap px-3 py-2 text-right" onClick={(e) => e.stopPropagation()}>
                        {p.overdue_count > 0 && (
                          <span className="inline-flex flex-col gap-1">
                            <button className="gh-chip gh-chip-neutral" onClick={() => selectFor(tp.receiver_id, p._tpa ? "" : p.payer_id)}>List</button>
                            <button className="gh-chip gh-chip-brand" disabled={busy} onClick={() => xlsx(tp.receiver_id, p._tpa ? "" : p.payer_id)}>Excel</button>
                          </span>
                        )}
                      </td>
                    </tr>
                  ));
                })}
                {!loadingS && tpas.length === 0 && (
                  <tr><td colSpan={8} className="px-3 py-6 text-center text-slate-500">No claims submitted in this period.</td></tr>
                )}
              </tbody>
            </table>
          </div>
        </div>
      </section>

      <section id="overdue-list">
        <div className="gh-card overflow-hidden">
          <div className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-100 px-4 py-3">
            <div>
              <h2 className="font-semibold text-slate-800">Overdue claims — no RA after {days} days{selectedLabel ? ` · ${selectedLabel}` : ""}</h2>
              <p className="text-xs text-slate-500">
                Submitted in the last 12 months, oldest first.
                {overdue?.truncated && ` Showing the first ${overdue.limit}.`}
              </p>
            </div>
            <div className="flex flex-wrap items-end gap-2">
              {(receiver || payer) && (
                <button className="gh-chip gh-chip-neutral" onClick={() => { setReceiver(""); setPayer(""); }}>
                  {selectedLabel || receiver} ✕
                </button>
              )}
              <button className="gh-chip gh-chip-brand" disabled={busy || !overdue?.claims?.length} onClick={() => xlsx(receiver, payer)}>
                {busy ? "Preparing…" : "Download Excel"}
              </button>
              {loadingO && <span className="text-xs text-slate-500">Loading…</span>}
            </div>
          </div>
          <div className="max-h-[600px] overflow-auto">
            <table className="min-w-full text-sm">
              <thead className="sticky top-0 bg-slate-50 text-xs uppercase text-slate-500">
                <tr>
                  {showFacility && <th className="px-3 py-2 text-left">Facility</th>}
                  <th className="px-3 py-2 text-left">Claim ID</th>
                  <th className="px-3 py-2 text-left">Receiver › payer</th>
                  <th className="px-3 py-2 text-left">Encounter</th>
                  <th className="px-3 py-2 text-left">Submitted</th>
                  <th className="px-3 py-2 text-right">Net</th>
                  <th className="px-3 py-2 text-right">Outstanding</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {(overdue?.claims || []).map((c) => (
                  <tr key={`${c.facility_id}-${c.claim_id}`} className="hover:bg-slate-50">
                    {showFacility && <td className="px-3 py-2 text-slate-600">{c.facility_name}</td>}
                    <td className="px-3 py-2 font-mono text-xs">{c.claim_id}</td>
                    <td className="px-3 py-2">{c.receiver_name || c.receiver_id}{c.payer_id !== c.receiver_id && <span className="block text-xs text-slate-500">{c.payer_name || c.payer_id}</span>}</td>
                    <td className="px-3 py-2">{fmtDay(c.encounter_date)}</td>
                    <td className="px-3 py-2">
                      {fmtDay(c.submission_date)}
                      {c.first_submission !== c.submission_date && <span className="ml-1 text-xs text-slate-400">(first {fmtDay(c.first_submission)})</span>}
                    </td>
                    <td className="px-3 py-2 text-right">{fmtMoney(c.net_amount)}</td>
                    <td className="px-3 py-2 text-right"><DaysChip days={c.days_outstanding} limit={days} /></td>
                  </tr>
                ))}
                {!loadingO && !overdue?.claims?.length && (
                  <tr><td colSpan={7} className="px-3 py-6 text-center text-slate-500">Nothing overdue.</td></tr>
                )}
              </tbody>
            </table>
          </div>
        </div>
      </section>
    </div>
  );
}
