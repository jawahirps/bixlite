/**
 * BulkResub.jsx — Bulk resubmission tab of the Resubmission worklist.
 *
 * 1. Denied claims with an outstanding balance, with a local readiness check.
 * 2. Reconciliation workbook import → Matched / Partial / Missing / Invalid per row,
 *    fetch of missing windows (DHA + Riayati), exceptions download.
 * 3. Select up to 100 claims, validate locally or save a remark to the worklist.
 * Submission to DHA / Riayati stays locked.
 */
import { useEffect, useMemo, useState } from "react";
import apiClient from "../api/client";

const MATCH_STYLE = {
  Matched: "gh-chip gh-chip-paid",
  Partial: "gh-chip gh-chip-pending",
  Missing: "gh-chip gh-chip-denied",
  Invalid: "gh-chip gh-chip-neutral",
  Pending: "gh-chip gh-chip-neutral",
};
const ACTIVE = ["Queued", "Fetching", "Matching"];
const aed = (v) => (v == null ? "" : Number(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 }));

async function saveBlob(res, name) {
  const url = URL.createObjectURL(res.data);
  const a = document.createElement("a");
  a.href = url; a.download = name; document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 30000);
}

function ImportDetail({ id, onClose, onUseBatch }) {
  const [data, setData] = useState(null);
  const [status, setStatus] = useState("");
  const [page, setPage] = useState(1);
  const [err, setErr] = useState("");
  const [preparing, setPreparing] = useState(false);

  const load = () =>
    apiClient.get(`/resub/bulk/imports/${id}`, { params: { status: status || undefined, page } })
      .then((r) => setData(r.data)).catch((e) => setErr(e.response?.data?.detail || "Could not load import."));
  useEffect(() => { load(); }, [id, status, page]);
  const running = data && ACTIVE.includes(data.batch.status);
  useEffect(() => {
    if (!running) return undefined;
    const t = setInterval(load, 5000);
    return () => clearInterval(t);
  }, [running, id, status, page]);

  if (!data) return <div className="gh-card p-4 text-sm text-slate-500">{err || "Loading import…"}</div>;
  const b = data.batch;
  const fetchNow = async () => {
    try { await apiClient.post(`/resub/bulk/imports/${id}/fetch`); load(); }
    catch (e) { setErr(e.response?.data?.detail || "Could not queue the fetch."); }
  };
  const exceptions = async () => {
    const res = await apiClient.get(`/resub/bulk/imports/${id}/exceptions`, { responseType: "blob" });
    saveBlob(res, `reconciliation-exceptions-${id}.xlsx`);
  };
  const reconFiles = async () => {
    setPreparing(true); setErr("");
    try {
      const res = await apiClient.post(`/resub/bulk/imports/${id}/reconciliation`, null, { responseType: "blob", timeout: 600000 });
      saveBlob(res, `reconciliation-import-${id}.zip`);
    } catch (e) { setErr("Could not prepare the reconciliation files."); }
    finally { setPreparing(false); }
  };

  return (
    <div className="gh-card space-y-3 p-4">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <p className="font-semibold text-slate-900">Import #{b.id} — {b.filename}</p>
          <p className="text-xs text-slate-500">
            {b.status} · {b.progress}{b.completed_at ? ` · finished ${new Date(b.completed_at).toLocaleString()}` : ""}
          </p>
          {b.error && <p className="mt-1 whitespace-pre-wrap text-xs text-red-600">{b.error}</p>}
        </div>
        <div className="flex flex-wrap gap-2">
          <button className="gh-btn-secondary gh-btn-sm" onClick={() => onUseBatch(b.id)}>Show these claims below</button>
          <button className="gh-btn-secondary gh-btn-sm" disabled={!data.can_fetch} onClick={fetchNow}
            title="Fetch DHA / Riayati data for the dated Missing and Partial rows">
            Fetch missing ({b.fetch_windows} window{b.fetch_windows === 1 ? "" : "s"})
          </button>
          <button className="gh-btn-secondary gh-btn-sm" onClick={exceptions}>Download exceptions</button>
          <button className="gh-btn-secondary gh-btn-sm" disabled={running || preparing} onClick={reconFiles}
            title="Exact-amount claims only: XML for DHA, JSON for Riayati, type reconciliation. Nothing is sent.">
            {preparing ? "Preparing…" : "Prepare reconciliation files"}
          </button>
          <button className="gh-btn-secondary gh-btn-sm" onClick={onClose}>Close</button>
        </div>
      </div>
      <div className="flex flex-wrap gap-2 text-xs">
        {[["", `All ${b.unique_rows}`], ["Matched", `Matched ${b.matched_rows}`], ["Partial", `Partial ${b.partial_rows}`],
          ["Missing", `Missing ${b.missing_rows}`], ["Invalid", `Invalid ${b.invalid_rows}`]].map(([k, l]) => (
          <button key={k} onClick={() => { setStatus(k); setPage(1); }}
            className={`rounded-full border px-3 py-1 ${status === k ? "border-ghaf-600 bg-ghaf-50 text-ghaf-700" : "border-slate-200 text-slate-600"}`}>
            {l}
          </button>
        ))}
        <span className="self-center text-slate-400">{b.duplicate_rows} duplicate rows ignored</span>
      </div>
      {err && <p className="text-xs text-red-600">{err}</p>}
      <div className="max-h-[480px] overflow-auto">
        <table className="w-full text-xs">
          <thead className="sticky top-0 bg-slate-50 text-left text-slate-500">
            <tr>{["Status", "Facility ID", "Facility", "Invoice No", "CPT", "Payer", "Rejected", "Denial", "Date", "Source", "Check"].map((h) => (
              <th key={h} className="px-2 py-1 font-medium">{h}</th>))}</tr>
          </thead>
          <tbody>
            {data.rows.map((r) => (
              <tr key={r.id} className="border-t border-slate-100 align-top">
                <td className="px-2 py-1"><span className={MATCH_STYLE[r.match_status]}>{r.match_status}</span></td>
                <td className="px-2 py-1">{r.facility_external_id}</td>
                <td className="px-2 py-1">{r.facility_name || "—"}</td>
                <td className="px-2 py-1 font-mono">{r.claim_id}</td>
                <td className="px-2 py-1">{r.cpt_code}</td>
                <td className="px-2 py-1">{r.payer_id}</td>
                <td className="px-2 py-1 text-right">{aed(r.rejected_amount)}</td>
                <td className="px-2 py-1">{r.denial_code}</td>
                <td className="px-2 py-1 whitespace-nowrap">{r.inferred_date || ""}</td>
                <td className="px-2 py-1">{r.date_source}</td>
                <td className="px-2 py-1 text-slate-600">{r.message}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {data.total_pages > 1 && (
        <div className="flex items-center gap-2 text-xs">
          <button className="gh-btn-secondary gh-btn-sm" disabled={page <= 1} onClick={() => setPage(page - 1)}>Previous</button>
          <span>Page {page} of {data.total_pages}</span>
          <button className="gh-btn-secondary gh-btn-sm" disabled={page >= data.total_pages} onClick={() => setPage(page + 1)}>Next</button>
        </div>
      )}
    </div>
  );
}

export default function BulkResub({ facilityId, facilities }) {
  const [fac, setFac] = useState(facilityId ? String(facilityId) : "");
  const [search, setSearch] = useState("");
  const [batchId, setBatchId] = useState(null);
  const [claims, setClaims] = useState([]);
  const [meta, setMeta] = useState({ max_claims: 100, production_message: "" });
  const [selected, setSelected] = useState(new Set());
  const [remarks, setRemarks] = useState("");
  const [result, setResult] = useState(null);
  const [msg, setMsg] = useState("");
  const [loading, setLoading] = useState(false);
  const [imports, setImports] = useState([]);
  const [openImport, setOpenImport] = useState(null);
  const [file, setFile] = useState(null);
  const [fetchMissing, setFetchMissing] = useState(true);
  const [uploading, setUploading] = useState(false);

  const loadClaims = () => {
    setLoading(true);
    apiClient.get("/resub/bulk/candidates", { params: { facility_id: fac || undefined, search: search || undefined, batch_id: batchId || undefined } })
      .then((r) => { setClaims(r.data.claims); setMeta(r.data); setSelected(new Set()); })
      .catch((e) => setMsg(e.response?.data?.detail || "Could not load claims."))
      .finally(() => setLoading(false));
  };
  const loadImports = () => apiClient.get("/resub/bulk/imports").then((r) => setImports(r.data)).catch(() => {});
  useEffect(() => { loadClaims(); }, [fac, batchId]);
  useEffect(() => { loadImports(); }, []);

  const toggle = (k) => setSelected((s) => {
    const n = new Set(s);
    if (n.has(k)) n.delete(k); else if (n.size < meta.max_claims) n.add(k);
    return n;
  });
  const eligibleKeys = useMemo(() => claims.filter((c) => c.eligible).map((c) => c.key), [claims]);
  const remarkOk = remarks.trim().length >= 10 && remarks.trim().length <= 2000;

  const post = async (path) => {
    setMsg(""); setResult(null);
    try {
      const r = await apiClient.post(`/resub/bulk/${path}`, { keys: [...selected], remarks });
      if (path === "validate") setResult(r.data);
      else { setMsg(r.data.message); loadClaims(); }
    } catch (e) {
      setMsg(e.response?.data?.detail || e.response?.data?.message || "Request failed.");
    }
  };

  const upload = async () => {
    if (!file) return;
    setUploading(true); setMsg("");
    const fd = new FormData();
    fd.append("workbook", file);
    fd.append("fetch_missing", fetchMissing ? "true" : "false");
    try {
      const r = await apiClient.post("/resub/bulk/imports", fd);
      setMsg(`Imported ${r.data.unique_rows} unique rows; ${r.data.duplicate_rows} duplicates ignored.`);
      setOpenImport(r.data.id); setFile(null); loadImports();
    } catch (e) {
      setMsg(e.response?.data?.detail || "The workbook could not be analysed. Nothing was submitted.");
    } finally {
      setUploading(false);
    }
  };

  return (
    <div className="space-y-5">
      <div className="rounded-md border border-amber-200 bg-amber-50 px-4 py-2 text-xs text-amber-800">
        {meta.production_message || "Submission to DHA / Riayati is locked."} Validating and saving remarks never sends anything.
      </div>

      {/* Workbook import */}
      <div className="gh-card space-y-3 p-4">
        <div>
          <p className="font-semibold text-slate-900">Import reconciliation workbook</p>
          <p className="text-xs text-slate-500">
            .xlsx up to 25 MB. Any sheet with FacilityID and InvoiceNo headers (first 25 rows) is read; CPT, Receiver, Payer,
            Rejected Amt, Denial Code, Comments and Remittance File Name are used when present.
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-3">
          <input type="file" accept=".xlsx" onChange={(e) => setFile(e.target.files?.[0] || null)} className="text-sm" />
          <label className="flex items-center gap-2 text-sm text-slate-700">
            <input type="checkbox" checked={fetchMissing} onChange={(e) => setFetchMissing(e.target.checked)} />
            Fetch missing data after import
          </label>
          <button className="gh-btn" disabled={!file || uploading} onClick={upload}>{uploading ? "Analysing…" : "Import"}</button>
        </div>
        {imports.length > 0 && (
          <div className="flex flex-wrap gap-2 text-xs">
            <span className="self-center text-slate-500">Recent imports:</span>
            {imports.map((b) => (
              <button key={b.id} onClick={() => setOpenImport(b.id)}
                className="rounded border border-slate-200 px-2 py-1 text-slate-700 hover:bg-slate-50">
                #{b.id} {b.filename} · {b.status} · ✓{b.matched_rows} ~{b.partial_rows} ✗{b.missing_rows}
              </button>
            ))}
          </div>
        )}
      </div>

      {openImport && (
        <ImportDetail id={openImport} onClose={() => { setOpenImport(null); loadImports(); }}
          onUseBatch={(id) => { setBatchId(id); }} />
      )}

      {/* Candidates */}
      <div className="gh-card space-y-3 p-4">
        <div className="flex flex-wrap items-center gap-2">
          <select className="gh-input" value={fac} onChange={(e) => setFac(e.target.value)}>
            <option value="">All facilities</option>
            {facilities.map((f) => <option key={f.id} value={f.id}>{f.facility_name}</option>)}
          </select>
          <input className="gh-input" placeholder="Claim ID, payer claim ID or RA file" value={search}
            onChange={(e) => setSearch(e.target.value)} onKeyDown={(e) => e.key === "Enter" && loadClaims()} />
          <button className="gh-btn-secondary" onClick={loadClaims}>Search</button>
          {batchId && (
            <span className="gh-chip gh-chip-brand">
              From import #{batchId} <button className="ml-1" onClick={() => setBatchId(null)}>✕</button>
            </span>
          )}
          <span className="text-xs text-slate-500">
            {loading ? "Loading…" : `${claims.length} denied claim(s) · ${eligibleKeys.length} ready · ${selected.size}/${meta.max_claims} selected`}
          </span>
        </div>

        <div className="max-h-[520px] overflow-auto">
          <table className="w-full text-xs">
            <thead className="sticky top-0 bg-slate-50 text-left text-slate-500">
              <tr>
                <th className="px-2 py-1">
                  <input type="checkbox" checked={selected.size > 0 && selected.size === Math.min(eligibleKeys.length, meta.max_claims)}
                    onChange={(e) => setSelected(e.target.checked ? new Set(eligibleKeys.slice(0, meta.max_claims)) : new Set())} />
                </th>
                {["Facility", "Claim ID", "Payer claim", "Payer", "RA date", "Net", "Paid", "Outstanding", "Denial", "Readiness", "Remarks"].map((h) => (
                  <th key={h} className="px-2 py-1 font-medium">{h}</th>))}
              </tr>
            </thead>
            <tbody>
              {claims.map((c) => (
                <tr key={c.key} className="border-t border-slate-100 align-top">
                  <td className="px-2 py-1">
                    <input type="checkbox" disabled={!c.eligible} checked={selected.has(c.key)} onChange={() => toggle(c.key)} />
                  </td>
                  <td className="px-2 py-1">{c.facility_name}</td>
                  <td className="px-2 py-1 font-mono">{c.claim_id}</td>
                  <td className="px-2 py-1">{c.payer_claim_id}</td>
                  <td className="px-2 py-1">{c.payer_id}</td>
                  <td className="px-2 py-1 whitespace-nowrap">{c.ra_date}</td>
                  <td className="px-2 py-1 text-right">{aed(c.net)}</td>
                  <td className="px-2 py-1 text-right">{aed(c.paid)}</td>
                  <td className="px-2 py-1 text-right font-medium">{aed(c.outstanding)}</td>
                  <td className="px-2 py-1">{c.denial_code}</td>
                  <td className={`px-2 py-1 ${c.eligible ? "text-emerald-700" : "text-red-600"}`}>{c.readiness}</td>
                  <td className="max-w-xs whitespace-pre-wrap px-2 py-1 text-slate-500">{c.existing_remarks}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        <div className="space-y-2 border-t border-slate-100 pt-3">
          <textarea className="gh-input min-h-[80px] w-full" maxLength={2000} value={remarks} onChange={(e) => setRemarks(e.target.value)}
            placeholder="Resubmission remark (10–2,000 characters), saved with date and your name to each selected claim" />
          <div className="flex flex-wrap items-center gap-2">
            <button className="gh-btn-secondary" disabled={!selected.size || !remarkOk} onClick={() => post("validate")}>Validate (local)</button>
            <button className="gh-btn" disabled={!selected.size || !remarkOk} onClick={() => post("remarks")}>Save remark to worklist</button>
            <button className="gh-btn-secondary" disabled title={meta.production_message}>Submit to DHA / Riayati (locked)</button>
            <span className="text-xs text-slate-500">{remarks.trim().length}/2000</span>
          </div>
          {msg && <p className="text-sm text-slate-700">{msg}</p>}
          {result && (
            <p className={`text-sm ${result.ok ? "text-emerald-700" : "text-amber-700"}`}>
              {result.reference}: {result.selected} selected · {result.eligible} ready · {result.blocked} blocked (local check only — nothing sent)
            </p>
          )}
        </div>
      </div>
    </div>
  );
}
