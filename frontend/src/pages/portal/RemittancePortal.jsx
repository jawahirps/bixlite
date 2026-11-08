/**
 * Public Remittance Activity Portal
 * Accessible at /portal/remittance?token=<PORTAL_TOKEN>
 * No login required — token is validated server-side.
 */
import { useState, useEffect, useCallback } from "react";
import axios from "axios";
import { toast } from "../../utils/notify";

// ── Standalone API client (no auth header, no 401 redirect) ─────────────────
const portalApi = axios.create({ baseURL: "/api/portal" });

// ── Helpers ──────────────────────────────────────────────────────────────────
const currency = (v) =>
  new Intl.NumberFormat("en-AE", {
    style: "currency",
    currency: "AED",
    maximumFractionDigits: 2,
  }).format(Number(v || 0));

const fmtNum = (v) => new Intl.NumberFormat("en-US").format(Number(v || 0));

function getToken() {
  return new URLSearchParams(window.location.search).get("token") || "";
}

// ── Sub-components ────────────────────────────────────────────────────────────
function KpiCard({ label, value, color = "text-slate-900" }) {
  return (
    <div className="rounded-xl bg-white p-4 shadow-sm border border-slate-100">
      <p className="text-xs font-medium text-slate-500 uppercase tracking-wide">{label}</p>
      <p className={`mt-1 text-xl font-bold ${color}`}>{value}</p>
    </div>
  );
}

function Badge({ status }) {
  const map = {
    "Paid":           "bg-emerald-100 text-emerald-800",
    "Partially Paid": "bg-amber-100  text-amber-800",
    "Rejected":       "bg-red-100    text-red-800",
  };
  const cls = map[status] || "bg-slate-100 text-slate-700";
  return (
    <span className={`inline-block rounded-full px-2 py-0.5 text-xs font-semibold ${cls}`}>
      {status || "—"}
    </span>
  );
}

function Spinner() {
  return (
    <div className="flex items-center gap-2 text-sm text-slate-500 py-8 justify-center">
      <svg className="animate-spin h-5 w-5 text-cyan-600" viewBox="0 0 24 24" fill="none">
        <circle cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" className="opacity-25" />
        <path fill="currentColor" d="M4 12a8 8 0 018-8v8z" className="opacity-75" />
      </svg>
      Loading…
    </div>
  );
}

// ── Main component ────────────────────────────────────────────────────────────
export default function RemittancePortal() {
  const token = getToken();

  // Client info
  const [clientInfo, setClientInfo]   = useState(null);
  const [authError, setAuthError]     = useState("");

  // Filters
  const today = new Date().toISOString().slice(0, 10);
  const firstDay = new Date(new Date().getFullYear(), new Date().getMonth(), 1)
    .toISOString().slice(0, 10);

  const [dateFrom,    setDateFrom]    = useState(firstDay);
  const [dateTo,      setDateTo]      = useState(today);
  const [payerId,     setPayerId]     = useState("");
  const [claimStatus, setClaimStatus] = useState("");
  const [search,      setSearch]      = useState("");
  const [page,        setPage]        = useState(1);

  // Data
  const [payers,    setPayers]    = useState([]);
  const [result,    setResult]    = useState(null);
  const [loading,   setLoading]   = useState(false);
  const [error,     setError]     = useState("");
  const [exporting, setExporting] = useState(false);

  // ── Verify token on mount ────────────────────────────────────────────────
  useEffect(() => {
    if (!token) { setAuthError("No access token provided in the URL."); return; }
    portalApi.get("/verify", { params: { token } })
      .then(r => setClientInfo(r.data))
      .catch(e => setAuthError(e.response?.data?.detail || "Invalid or expired access token."));
  }, [token]);

  // ── Load payer list once ─────────────────────────────────────────────────
  useEffect(() => {
    if (!clientInfo) return;
    portalApi.get("/remittance/payers", { params: { token } })
      .then(r => setPayers(r.data))
      .catch(() => {});
  }, [clientInfo, token]);

  // ── Fetch data ───────────────────────────────────────────────────────────
  const fetchData = useCallback(() => {
    if (!clientInfo) return;
    setLoading(true);
    setError("");
    portalApi.get("/remittance", {
      params: {
        token,
        date_from: dateFrom || undefined,
        date_to:   dateTo   || undefined,
        payer_id:  payerId  || undefined,
        claim_status: claimStatus || undefined,
        search:    search   || undefined,
        page,
        page_size: 50,
      },
    })
      .then(r => setResult(r.data))
      .catch(e => setError(e.response?.data?.detail || "Failed to load data."))
      .finally(() => setLoading(false));
  }, [clientInfo, token, dateFrom, dateTo, payerId, claimStatus, search, page]);

  useEffect(() => { fetchData(); }, [fetchData]);

  // ── Export ───────────────────────────────────────────────────────────────
  const handleExport = async () => {
    setExporting(true);
    try {
      const resp = await portalApi.get("/remittance/export", {
        params: {
          token,
          date_from: dateFrom || undefined,
          date_to:   dateTo   || undefined,
          payer_id:  payerId  || undefined,
          claim_status: claimStatus || undefined,
          search:    search || undefined,
        },
        responseType: "blob",
      });
      const url = URL.createObjectURL(resp.data);
      const a   = document.createElement("a");
      a.href    = url;
      a.download = resp.headers["content-disposition"]
        ?.split("filename=")[1]?.replace(/"/g, "") || "RemittanceActivity.xlsx";
      a.click();
      URL.revokeObjectURL(url);
    } catch {
      toast("Export failed. Please try again.", "error");
    } finally {
      setExporting(false);
    }
  };

  // ── Render: auth error ───────────────────────────────────────────────────
  if (authError) {
    return (
      <div className="min-h-screen bg-slate-50 flex items-center justify-center">
        <div className="bg-white rounded-2xl shadow p-10 max-w-md text-center">
          <div className="text-5xl mb-4">🔒</div>
          <h2 className="text-xl font-semibold text-slate-800 mb-2">Access Denied</h2>
          <p className="text-slate-500 text-sm">{authError}</p>
        </div>
      </div>
    );
  }

  if (!clientInfo) return <Spinner />;

  const { summary, pagination, transactions } = result || {};

  // ── Render: portal ───────────────────────────────────────────────────────
  return (
    <div className="min-h-screen bg-slate-50">
      {/* Header */}
      <header className="bg-white border-b border-slate-200 shadow-sm">
        <div className="max-w-screen-xl mx-auto px-6 py-4 flex items-center justify-between">
          <div>
            <p className="text-xs text-slate-400 uppercase tracking-wider">Ghaf RCM Platform</p>
            <h1 className="text-lg font-bold text-slate-800">Remittance Activity Report</h1>
          </div>
          <div className="text-right">
            <p className="text-sm font-semibold text-slate-700">{clientInfo.client_name}</p>
            <p className="text-xs text-slate-400">
              {clientInfo.facilities.map(f => f.name).join(" · ")}
            </p>
          </div>
        </div>
      </header>

      <main className="max-w-screen-xl mx-auto px-6 py-6 space-y-6">

        {/* Filters */}
        <div className="bg-white rounded-2xl shadow-sm border border-slate-100 p-5">
          <h2 className="text-sm font-semibold text-slate-600 mb-4">Search &amp; Filter</h2>
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">

            <div className="flex flex-col gap-1">
              <label className="text-xs text-slate-500">Remittance Date From</label>
              <input type="date" value={dateFrom} onChange={e => { setDateFrom(e.target.value); setPage(1); }}
                className="rounded-lg border border-slate-200 px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-cyan-500" />
            </div>

            <div className="flex flex-col gap-1">
              <label className="text-xs text-slate-500">Remittance Date To</label>
              <input type="date" value={dateTo} onChange={e => { setDateTo(e.target.value); setPage(1); }}
                className="rounded-lg border border-slate-200 px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-cyan-500" />
            </div>

            <div className="flex flex-col gap-1">
              <label className="text-xs text-slate-500">Payer</label>
              <select value={payerId} onChange={e => { setPayerId(e.target.value); setPage(1); }}
                className="rounded-lg border border-slate-200 px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-cyan-500">
                <option value="">All Payers</option>
                {payers.map(p => (
                  <option key={p.payer_id} value={p.payer_id}>{p.payer_name}</option>
                ))}
              </select>
            </div>

            <div className="flex flex-col gap-1">
              <label className="text-xs text-slate-500">Status</label>
              <select value={claimStatus} onChange={e => { setClaimStatus(e.target.value); setPage(1); }}
                className="rounded-lg border border-slate-200 px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-cyan-500">
                <option value="">All Statuses</option>
                <option value="Paid">Paid</option>
                <option value="Partially Paid">Partially Paid</option>
                <option value="Rejected">Rejected</option>
              </select>
            </div>

            <div className="flex flex-col gap-1">
              <label className="text-xs text-slate-500">Search</label>
              <input type="text" placeholder="Claim ID / Member / RA File"
                value={search} onChange={e => { setSearch(e.target.value); setPage(1); }}
                className="rounded-lg border border-slate-200 px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-cyan-500" />
            </div>

            <div className="flex flex-col gap-1 justify-end">
              <button onClick={handleExport} disabled={exporting || loading}
                className="flex items-center justify-center gap-2 rounded-lg bg-cyan-600 px-4 py-2 text-sm font-semibold text-white hover:bg-cyan-700 disabled:opacity-50 transition-colors">
                {exporting ? "Exporting…" : "⬇ Export Excel"}
              </button>
            </div>

          </div>
        </div>

        {/* KPI Cards */}
        {summary && (
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5">
            <KpiCard label="Claims" value={fmtNum(summary.claim_count)} />
            <KpiCard label="Total Paid" value={currency(summary.total_paid)} color="text-emerald-600" />
            <KpiCard label="Total Denied" value={currency(summary.total_denied)} color="text-red-600" />
            <KpiCard label="Patient Share" value={currency(summary.total_patient_share)} color="text-amber-600" />
            <KpiCard label="Discount" value={currency(summary.total_discount)} color="text-slate-600" />
          </div>
        )}

        {/* Table */}
        <div className="bg-white rounded-2xl shadow-sm border border-slate-100 overflow-hidden">
          <div className="px-5 py-4 border-b border-slate-100 flex items-center justify-between">
            <h2 className="text-sm font-semibold text-slate-700">
              Transactions
              {pagination && (
                <span className="ml-2 text-xs font-normal text-slate-400">
                  ({fmtNum(pagination.total)} rows)
                </span>
              )}
            </h2>
          </div>

          {loading && <Spinner />}
          {error && <p className="text-sm text-red-600 px-5 py-4">{error}</p>}

          {!loading && !error && transactions && (
            <div className="overflow-x-auto">
              <table className="min-w-full divide-y divide-slate-100 text-xs">
                <thead className="bg-slate-50">
                  <tr>
                    {[
                      "Remittance Date", "Date of Settlement", "Payment Reference", "RA File",
                      "Claim ID", "Patient ID", "Member ID",
                      "Receiver Name", "Payer Name", "Status",
                      "Activity Code", "Type",
                      "Gross", "Net", "Paid", "Patient Share",
                      "Denial Code", "Denial Description",
                    ].map(h => (
                      <th key={h}
                        className="whitespace-nowrap px-3 py-2 text-left font-semibold text-slate-500 uppercase tracking-wide">
                        {h}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-50">
                  {transactions.length === 0 && (
                    <tr><td colSpan={18} className="text-center py-8 text-slate-400">No records found.</td></tr>
                  )}
                  {transactions.map((tx, i) => (
                    <tr key={i} className={i % 2 === 0 ? "bg-white" : "bg-slate-50/50"}>
                      <td className="px-3 py-2 whitespace-nowrap text-slate-700 font-medium">{tx.remittance_date || "—"}</td>
                      <td className="px-3 py-2 whitespace-nowrap text-slate-500">{tx.settlement_date || "—"}</td>
                      <td className="px-3 py-2 whitespace-nowrap text-slate-500 font-mono">{tx.payment_ref || "—"}</td>
                      <td className="px-3 py-2 whitespace-nowrap text-slate-400 font-mono text-xs" title={tx.ra_file_name || tx.ra_file_id || ""}>{tx.ra_file_name || tx.ra_file_id || "—"}</td>
                      <td className="px-3 py-2 whitespace-nowrap font-medium text-cyan-700">{tx.claim_id}</td>
                      <td className="px-3 py-2 whitespace-nowrap text-slate-600">{tx.patient_id || "—"}</td>
                      <td className="px-3 py-2 whitespace-nowrap text-slate-600">{tx.member_id || "—"}</td>
                      <td className="px-3 py-2 whitespace-nowrap text-slate-700 font-medium">{tx.receiver_name || "—"}</td>
                      <td className="px-3 py-2 whitespace-nowrap text-slate-700">{tx.payer_name || "—"}</td>
                      <td className="px-3 py-2 whitespace-nowrap"><Badge status={tx.claim_status} /></td>
                      <td className="px-3 py-2 whitespace-nowrap text-slate-600">{tx.activity_code || "—"}</td>
                      <td className="px-3 py-2 whitespace-nowrap text-slate-500">{tx.activity_type || "—"}</td>
                      <td className="px-3 py-2 whitespace-nowrap text-right text-slate-700">{tx.gross != null ? currency(tx.gross) : "—"}</td>
                      <td className="px-3 py-2 whitespace-nowrap text-right text-slate-700">{tx.net  != null ? currency(tx.net)  : "—"}</td>
                      <td className="px-3 py-2 whitespace-nowrap text-right font-semibold text-emerald-700">{tx.paid != null ? currency(tx.paid) : "—"}</td>
                      <td className="px-3 py-2 whitespace-nowrap text-right text-amber-700">{tx.patient_share != null ? currency(tx.patient_share) : "—"}</td>
                      <td className="px-3 py-2 whitespace-nowrap text-red-600 font-mono">{tx.denial_code || "—"}</td>
                      <td className="px-3 py-2 text-slate-500 max-w-xs truncate">{tx.denial_desc || "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {/* Pagination */}
          {pagination && pagination.pages > 1 && (
            <div className="px-5 py-3 border-t border-slate-100 flex items-center justify-between">
              <p className="text-xs text-slate-500">
                Page {pagination.page} of {pagination.pages}
              </p>
              <div className="flex gap-2">
                <button disabled={page <= 1} onClick={() => setPage(p => p - 1)}
                  className="rounded-lg border border-slate-200 px-3 py-1.5 text-xs font-medium text-slate-600 hover:bg-slate-50 disabled:opacity-40">
                  ← Previous
                </button>
                <button disabled={page >= pagination.pages} onClick={() => setPage(p => p + 1)}
                  className="rounded-lg border border-slate-200 px-3 py-1.5 text-xs font-medium text-slate-600 hover:bg-slate-50 disabled:opacity-40">
                  Next →
                </button>
              </div>
            </div>
          )}
        </div>

        {/* Footer */}
        <p className="text-center text-xs text-slate-400 pb-4">
          Ghaf RCM Platform · Data is refreshed nightly ·{" "}
          {clientInfo.expires_at
            ? `Access expires ${clientInfo.expires_at}`
            : "Access does not expire"}
        </p>
      </main>
    </div>
  );
}
