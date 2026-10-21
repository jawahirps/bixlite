/**
 * Scrubber.jsx — Pre-submission claim scrub module UI
 *
 * Panels:
 *   1. Upload XML → creates a ScrubSession + auto-runs rules
 *   2. Sessions list — click to view results
 *   3. Results table — per-claim ERROR / WARN / PASS badges + export
 *   4. Exclusion Codes tab — view / add / remove DHA exclusion codes
 */
import { useState, useEffect, useCallback } from "react";
import api from "../api/client";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
const SEVERITY_STYLES = {
  BLOCK: "bg-red-200 text-red-900 border border-red-400",
  ERROR: "bg-red-100 text-red-800 border border-red-300",
  WARN:  "bg-yellow-100 text-yellow-800 border border-yellow-300",
  PASS:  "bg-green-100 text-green-800 border border-green-300",
};

const RISK_STYLES = {
  HIGH:   "bg-red-100 text-red-700 border border-red-200",
  MEDIUM: "bg-yellow-100 text-yellow-700 border border-yellow-200",
  LOW:    "bg-green-100 text-green-700 border border-green-200",
};

const STATUS_STYLES = {
  uploaded:  "text-slate-500",
  parsed:    "text-blue-600",
  running:   "text-yellow-600 animate-pulse",
  completed: "text-green-600",
  failed:    "text-red-600",
};

function Badge({ label, style }) {
  return (
    <span className={`inline-block rounded-full px-2 py-0.5 text-xs font-semibold ${style}`}>
      {label}
    </span>
  );
}

function fmtDate(iso) {
  if (!iso) return "—";
  return new Date(iso).toLocaleString("en-AE", { dateStyle: "medium", timeStyle: "short" });
}

function fmtDay(iso) {
  if (!iso) return "—";
  return new Date(iso.slice(0, 10) + "T00:00:00").toLocaleDateString("en-AE", { dateStyle: "medium" });
}

// Download options shared by the single-batch and combined query Excel
function ExportControls({ value, onChange }) {
  const set = (k) => (e) => onChange({ ...value, [k]: e.target.value });
  const cls = "rounded-lg border border-slate-300 px-2 py-1.5 text-xs";
  return (
    <div className="flex flex-wrap items-center gap-2">
      <label className="text-xs text-slate-500">Visit date</label>
      <input type="date" value={value.from} onChange={set("from")} className={cls} aria-label="Visit date from" />
      <span className="text-xs text-slate-400">to</span>
      <input type="date" value={value.to} onChange={set("to")} className={cls} aria-label="Visit date to" />
      <select value={value.batch} onChange={set("batch")} className={cls} aria-label="Batch">
        <option value="">Batch: auto (1–10 / 11–20 / 21–end)</option>
        <option value="1ST">1st batch</option>
        <option value="2ND">2nd batch</option>
        <option value="FINAL">Final batch</option>
      </select>
      <select value={value.audience} onChange={set("audience")} className={cls} aria-label="Sheet">
        <option value="ghaf">Ghaf review sheet</option>
        <option value="facility">Facility sheet</option>
      </select>
    </div>
  );
}

const EXPORT_DEFAULT = { from: "", to: "", batch: "", audience: "ghaf" };

function exportQuery(o) {
  const p = new URLSearchParams();
  if (o.from) p.set("date_from", o.from);
  if (o.to) p.set("date_to", o.to);
  if (o.batch) p.set("batch", o.batch);
  p.set("audience", o.audience);
  return p.toString();
}

async function downloadBlob(url, fallbackName) {
  const response = await api.get(url, { responseType: "blob" });
  const cd = response.headers["content-disposition"] || "";
  const m = cd.match(/filename="([^"]+)"/);
  const href = URL.createObjectURL(response.data);
  const a = document.createElement("a");
  a.href = href;
  a.download = m ? m[1] : fallbackName;
  a.click();
  URL.revokeObjectURL(href);
}

// ---------------------------------------------------------------------------
// Upload panel
// ---------------------------------------------------------------------------
function UploadPanel({ onUploaded }) {
  const [facilityId, setFacilityId] = useState("");
  const [facilities, setFacilities] = useState([]);
  const [files, setFiles] = useState([]);
  const [progress, setProgress] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    api.get("/facilities").then((r) => setFacilities(r.data)).catch(() => {});
  }, []);

  async function handleUpload(e) {
    e.preventDefault();
    if (!files.length || !facilityId) return;
    setBusy(true);
    setError("");
    const ids = [];
    const failed = [];
    for (const [i, f] of files.entries()) {
      setProgress(`${i + 1} / ${files.length}`);
      const form = new FormData();
      form.append("file", f);
      form.append("facility_id", facilityId);
      try {
        const { data: session } = await api.post("/scrub/sessions", form, {
          headers: { "Content-Type": "multipart/form-data" },
        });
        // Auto-run rules immediately after parse
        await api.post(`/scrub/sessions/${session.id}/run`);
        ids.push(session.id);
      } catch (err) {
        failed.push(`${f.name}: ${err?.response?.data?.detail || "upload failed"}`);
      }
    }
    if (failed.length) setError(failed.join(" · "));
    setProgress("");
    setBusy(false);
    if (ids.length) onUploaded(ids);
  }

  return (
    <form onSubmit={handleUpload} className="rounded-xl border border-slate-200 bg-white p-6 shadow-sm">
      <h2 className="mb-1 text-base font-semibold text-slate-700">Upload batch — ERP test XML or Excel</h2>
      <p className="mb-4 text-xs text-slate-500">
        Excel: one row per activity with columns Claim ID (or Invoice), MRN, Member ID, Payer ID, Receiver ID, Date,
        Clinician, Activity code, Quantity, Net, Approval no, Diagnosis (comma-separated, principal first).
      </p>
      <div className="flex flex-col gap-3 sm:flex-row sm:items-end">
        <div className="flex-1">
          <label className="mb-1 block text-xs font-medium text-slate-500">Facility</label>
          <select
            required
            value={facilityId}
            onChange={(e) => setFacilityId(e.target.value)}
            className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ghaf-500"
          >
            <option value="">Select facility…</option>
            {facilities.map((f) => (
              <option key={f.id} value={f.id}>{f.facility_name}</option>
            ))}
          </select>
        </div>
        <div className="flex-1">
          <label className="mb-1 block text-xs font-medium text-slate-500">XML / Excel files (select one or more)</label>
          <input
            type="file"
            accept=".xml,.xlsx"
            multiple
            required
            onChange={(e) => setFiles(Array.from(e.target.files || []))}
            className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm file:mr-3 file:rounded file:border-0 file:bg-ghaf-50 file:px-3 file:py-1 file:text-xs file:font-medium file:text-ghaf-700 focus:outline-none"
          />
        </div>
        <button
          type="submit"
          disabled={busy || !files.length || !facilityId}
          className="rounded-lg bg-ghaf-600 px-5 py-2 text-sm font-semibold text-white hover:bg-ghaf-700 disabled:opacity-40"
        >
          {busy ? `Processing ${progress}…` : files.length > 1 ? `Upload & Scrub ${files.length} files` : "Upload & Scrub"}
        </button>
      </div>
      {error && <p className="mt-2 text-sm text-red-600">{error}</p>}
    </form>
  );
}

// ---------------------------------------------------------------------------
// Sessions list
// ---------------------------------------------------------------------------
function SessionsList({ refreshKey, onSelect, selectedId, checked, onCheck }) {
  const [sessions, setSessions] = useState([]);
  const [loading, setLoading] = useState(true);

  const load = useCallback(() => {
    setLoading(true);
    api.get("/scrub/sessions?limit=30")
      .then((r) => setSessions(r.data))
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => { load(); }, [load, refreshKey]);

  if (loading) return <p className="py-4 text-sm text-slate-400">Loading sessions…</p>;
  if (!sessions.length) return <p className="py-4 text-sm text-slate-400">No scrub sessions yet.</p>;

  return (
    <div className="overflow-x-auto rounded-xl border border-slate-200 bg-white shadow-sm">
      <table className="min-w-full text-sm">
        <thead className="bg-slate-50 text-xs font-semibold uppercase text-slate-500">
          <tr>
            <th className="w-10 px-4 py-3">
              <input type="checkbox" aria-label="Select all sessions"
                checked={sessions.length > 0 && sessions.every((s) => checked.includes(s.id))}
                onChange={(e) => onCheck(e.target.checked ? sessions.map((s) => s.id) : [])} />
            </th>
            <th className="px-4 py-3 text-left">File</th>
            <th className="px-4 py-3 text-left">Uploaded</th>
            <th className="px-4 py-3 text-center">Claims</th>
            <th className="px-4 py-3 text-center">Status</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100">
          {sessions.map((s) => (
            <tr
              key={s.id}
              onClick={() => onSelect(s.id)}
              className={`cursor-pointer transition-colors hover:bg-ghaf-50 ${
                s.id === selectedId ? "bg-ghaf-50" : ""
              }`}
            >
              <td className="px-4 py-3" onClick={(e) => e.stopPropagation()}>
                <input type="checkbox" aria-label={`Select ${s.uploaded_filename}`}
                  checked={checked.includes(s.id)}
                  onChange={(e) => onCheck(e.target.checked ? [...checked, s.id] : checked.filter((x) => x !== s.id))} />
              </td>
              <td className="max-w-xs truncate px-4 py-3 font-medium text-slate-700">
                {s.uploaded_filename}
              </td>
              <td className="px-4 py-3 text-slate-500">{fmtDate(s.uploaded_at)}</td>
              <td className="px-4 py-3 text-center text-slate-600">{s.claims_count ?? "—"}</td>
              <td className="px-4 py-3 text-center">
                <span className={`font-medium ${STATUS_STYLES[s.status] || "text-slate-500"}`}>
                  {s.status}
                </span>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Rule Coverage panel
// ---------------------------------------------------------------------------
const RULE_LABELS = {
  FREE_FOLLOWUP: "Free Follow-up (7-day)",
  DUPLICATE:     "Duplicate Claim",
  MISSING_AUTH:  "Missing Prior Auth",
  EXCLUDE_1:     "DHA Exclude 1 (Absolute)",
  EXCLUDE_2:     "DHA Exclude 2 (Conditional)",
};

function RuleCoveragePanel({ sessionId }) {
  const [summary, setSummary] = useState(null);

  useEffect(() => {
    if (!sessionId) return;
    api.get(`/scrub/sessions/${sessionId}/rule-summary`)
      .then((r) => setSummary(r.data))
      .catch(() => {});
  }, [sessionId]);

  if (!summary) return null;

  return (
    <div className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
      <h3 className="mb-3 text-sm font-semibold text-slate-700">
        Rule Coverage — {summary.total_claims} claims checked against all 5 rules
      </h3>
      <table className="w-full text-sm">
        <thead>
          <tr className="text-xs font-semibold uppercase text-slate-500">
            <th className="pb-2 text-left">Rule</th>
            <th className="pb-2 text-center">Errors</th>
            <th className="pb-2 text-center">Warnings</th>
            <th className="pb-2 text-center">Passed</th>
            <th className="pb-2 text-center">Status</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100">
          {summary.rules.map((r) => {
            const clean = r.errors === 0 && r.warnings === 0;
            return (
              <tr key={r.rule_code} className="hover:bg-slate-50">
                <td className="py-2 font-mono text-xs text-slate-700">
                  {r.rule_code}
                  <span className="ml-2 text-slate-400 font-sans normal-case">
                    {RULE_LABELS[r.rule_code] || ""}
                  </span>
                </td>
                <td className="py-2 text-center">
                  {r.errors > 0
                    ? <span className="font-bold text-red-600">{r.errors}</span>
                    : <span className="text-slate-300">0</span>}
                </td>
                <td className="py-2 text-center">
                  {r.warnings > 0
                    ? <span className="font-bold text-yellow-600">{r.warnings}</span>
                    : <span className="text-slate-300">0</span>}
                </td>
                <td className="py-2 text-center text-slate-500">{r.passed}</td>
                <td className="py-2 text-center">
                  {r.errors > 0
                    ? <span className="rounded-full bg-red-100 px-2 py-0.5 text-xs font-semibold text-red-700">Issues found</span>
                    : r.warnings > 0
                    ? <span className="rounded-full bg-yellow-100 px-2 py-0.5 text-xs font-semibold text-yellow-700">Warnings</span>
                    : <span className="rounded-full bg-green-100 px-2 py-0.5 text-xs font-semibold text-green-700">✓ All clear</span>}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Results table
// ---------------------------------------------------------------------------
function ResultsPanel({ sessionId }) {
  const [results, setResults] = useState([]);
  const [filter, setFilter] = useState("FLAGGED");
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!sessionId) return;
    setLoading(true);
    api.get(`/scrub/sessions/${sessionId}/results`)
      .then((r) => setResults(r.data))
      .catch(() => {})
      .finally(() => setLoading(false));
  }, [sessionId]);

  async function handleExport() {
    try {
      const response = await api.get(`/scrub/sessions/${sessionId}/export`, {
        responseType: "blob",
      });
      const url = URL.createObjectURL(response.data);
      const a = document.createElement("a");
      const ts = new Date().toISOString().slice(0, 16).replace(/[-T:]/g, "").replace("T", "_");
      a.href = url;
      a.download = `ScrubReport_${sessionId}_${ts}.xlsx`;
      a.click();
      URL.revokeObjectURL(url);
    } catch {
      alert("Export failed.");
    }
  }

  if (!sessionId) {
    return (
      <div className="flex h-40 items-center justify-center rounded-xl border border-dashed border-slate-200 text-sm text-slate-400">
        Select a session above to view results
      </div>
    );
  }

  // Unique claims (deduplicate for counts — each claim has 5 rule rows now)
  const claimSet = new Set(results.map((r) => r.scrub_claim_id));
  const errorClaims = new Set(results.filter((r) => r.severity === "ERROR").map((r) => r.scrub_claim_id));
  const warnClaims  = new Set(results.filter((r) => r.severity === "WARN").map((r) => r.scrub_claim_id));

  const filtered =
    filter === "ALL"     ? results :
    filter === "FLAGGED" ? results.filter((r) => r.severity !== "PASS") :
                           results.filter((r) => r.severity === filter);

  return (
    <div className="space-y-3">
      {/* Rule coverage summary */}
      <RuleCoveragePanel sessionId={sessionId} />

      {/* Filter chips + export */}
      <div className="flex flex-wrap items-center gap-2">
        {[
          { key: "FLAGGED", label: `Flagged (${errorClaims.size + warnClaims.size} claims)` },
          { key: "ERROR",   label: `ERROR (${errorClaims.size} claims)` },
          { key: "WARN",    label: `WARN (${warnClaims.size} claims)` },
          { key: "ALL",     label: `All rows (${results.length})` },
        ].map(({ key, label }) => (
          <button
            key={key}
            onClick={() => setFilter(key)}
            className={`rounded-full px-3 py-1 text-xs font-semibold transition-colors ${
              filter === key
                ? "bg-ghaf-600 text-white"
                : "bg-slate-100 text-slate-600 hover:bg-slate-200"
            }`}
          >
            {label}
          </button>
        ))}
        <span className="flex-1" />
        <button
          onClick={handleExport}
          className="rounded-lg border border-ghaf-300 bg-white px-4 py-1.5 text-xs font-semibold text-ghaf-700 hover:bg-ghaf-50"
        >
          Export Excel
        </button>
      </div>

      {loading ? (
        <p className="py-4 text-sm text-slate-400">Loading results…</p>
      ) : (
        <div className="overflow-x-auto rounded-xl border border-slate-200 bg-white shadow-sm">
          <table className="min-w-full text-sm">
            <thead className="bg-slate-50 text-xs font-semibold uppercase text-slate-500">
              <tr>
                <th className="px-4 py-3 text-left">Claim ID</th>
                <th className="px-4 py-3 text-left">Emirates ID</th>
                <th className="px-4 py-3 text-left">Enc. Start</th>
                <th className="px-4 py-3 text-center">Severity</th>
                <th className="px-4 py-3 text-left">Rule</th>
                <th className="px-4 py-3 text-left">Message</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {filtered.length === 0 ? (
                <tr>
                  <td colSpan={6} className="py-6 text-center text-slate-400">
                    {filter === "FLAGGED" ? "No flagged claims — all rules passed for every claim ✓" : "No results for selected filter."}
                  </td>
                </tr>
              ) : (
                filtered.map((r, i) => (
                  <tr key={i} className="hover:bg-slate-50">
                    <td className="px-4 py-3 font-mono text-xs text-slate-700">{r.claim_id}</td>
                    <td className="px-4 py-3 text-slate-600">{r.emirates_id || "—"}</td>
                    <td className="px-4 py-3 text-slate-500">{r.encounter_start || "—"}</td>
                    <td className="px-4 py-3 text-center">
                      <Badge label={r.severity} style={SEVERITY_STYLES[r.severity] || ""} />
                    </td>
                    <td className="px-4 py-3 font-mono text-xs text-slate-600">{r.rule_code}</td>
                    <td className="px-4 py-3 text-slate-600">{r.message}</td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Exclusion codes panel
// ---------------------------------------------------------------------------
function ExclusionsPanel() {
  const [codes, setCodes] = useState([]);
  const [loading, setLoading] = useState(true);
  const [form, setForm] = useState({ exclusion_type: "1", code_type: "diagnosis", code: "", description: "" });
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  const load = useCallback(() => {
    setLoading(true);
    api.get("/scrub/exclusions?active_only=false")
      .then((r) => setCodes(r.data))
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => { load(); }, [load]);

  async function handleAdd(e) {
    e.preventDefault();
    setSaving(true);
    setError("");
    try {
      await api.post("/scrub/exclusions", {
        exclusion_type: parseInt(form.exclusion_type),
        code_type: form.code_type,
        code: form.code.trim(),
        description: form.description || null,
      });
      setForm({ exclusion_type: "1", code_type: "diagnosis", code: "", description: "" });
      load();
    } catch (err) {
      setError(err?.response?.data?.detail || "Failed to add code.");
    } finally {
      setSaving(false);
    }
  }

  async function handleDelete(id) {
    if (!window.confirm("Deactivate this exclusion code?")) return;
    await api.delete(`/scrub/exclusions/${id}`);
    load();
  }

  const inp = "rounded-lg border border-slate-300 px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ghaf-500";

  return (
    <div className="space-y-6">
      {/* Add form */}
      <form onSubmit={handleAdd} className="rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
        <h3 className="mb-3 text-sm font-semibold text-slate-700">Add Exclusion Code</h3>
        <div className="flex flex-wrap gap-3">
          <select
            value={form.exclusion_type}
            onChange={(e) => setForm((f) => ({ ...f, exclusion_type: e.target.value }))}
            className={inp}
          >
            <option value="1">Exclude 1 (ERROR)</option>
            <option value="2">Exclude 2 (WARN)</option>
          </select>
          <select
            value={form.code_type}
            onChange={(e) => setForm((f) => ({ ...f, code_type: e.target.value }))}
            className={inp}
          >
            <option value="diagnosis">Diagnosis (ICD)</option>
            <option value="activity">Activity (CPT/HCPCS)</option>
          </select>
          <input
            required
            placeholder="Code (e.g. Z41.1, 27447)"
            value={form.code}
            onChange={(e) => setForm((f) => ({ ...f, code: e.target.value }))}
            className={`${inp} w-40`}
          />
          <input
            placeholder="Description (optional)"
            value={form.description}
            onChange={(e) => setForm((f) => ({ ...f, description: e.target.value }))}
            className={`${inp} flex-1`}
          />
          <button
            type="submit"
            disabled={saving}
            className="rounded-lg bg-ghaf-600 px-4 py-2 text-sm font-semibold text-white hover:bg-ghaf-700 disabled:opacity-40"
          >
            {saving ? "Saving…" : "Add"}
          </button>
        </div>
        {error && <p className="mt-2 text-sm text-red-600">{error}</p>}
      </form>

      {/* Table */}
      {loading ? (
        <p className="text-sm text-slate-400">Loading…</p>
      ) : (
        <div className="overflow-x-auto rounded-xl border border-slate-200 bg-white shadow-sm">
          <table className="min-w-full text-sm">
            <thead className="bg-slate-50 text-xs font-semibold uppercase text-slate-500">
              <tr>
                <th className="px-4 py-3 text-center">Type</th>
                <th className="px-4 py-3 text-left">Code Type</th>
                <th className="px-4 py-3 text-left">Code</th>
                <th className="px-4 py-3 text-left">Description</th>
                <th className="px-4 py-3 text-center">Active</th>
                <th className="px-4 py-3" />
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {codes.map((c) => (
                <tr key={c.id} className={`hover:bg-slate-50 ${!c.is_active ? "opacity-40" : ""}`}>
                  <td className="px-4 py-3 text-center">
                    <Badge
                      label={`Exclude ${c.exclusion_type}`}
                      style={c.exclusion_type === 1 ? SEVERITY_STYLES.ERROR : SEVERITY_STYLES.WARN}
                    />
                  </td>
                  <td className="px-4 py-3 capitalize text-slate-600">{c.code_type}</td>
                  <td className="px-4 py-3 font-mono text-xs font-semibold text-slate-800">{c.code}</td>
                  <td className="px-4 py-3 text-slate-500">{c.description || "—"}</td>
                  <td className="px-4 py-3 text-center">
                    {c.is_active ? "✓" : "—"}
                  </td>
                  <td className="px-4 py-3 text-right">
                    {c.is_active && (
                      <button
                        onClick={() => handleDelete(c.id)}
                        className="text-xs text-red-500 hover:underline"
                      >
                        Remove
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// XML & Coding Check panel (Strawberry scrubber layer)
// ---------------------------------------------------------------------------
const XML_RULE_LABELS = {
  // Structural
  ROOT_NAME:           "Root element name",
  HEADER_MISSING:      "Missing submission header",
  HEADER_REQUIRED:     "Required header field missing",
  RECORD_COUNT:        "RecordCount mismatch",
  DATE_FORMAT:         "Unrecognised date format",
  // Claim / encounter
  CLAIM_REQUIRED:      "Required claim field missing",
  ENCOUNTER_REQUIRED:  "Required encounter field missing",
  ENCOUNTER_DATE:      "Encounter date format",
  ENCOUNTER_ORDER:     "Encounter start after end",
  MONEY_FORMAT:        "Monetary field not numeric",
  CLAIM_TOTALS:        "Gross − PatientShare ≠ Net",
  NEGATIVE_AMOUNT:     "Negative amount",
  LINE_TOTALS:         "Activity sum ≠ Claim Net",
  // Diagnosis
  DIAGNOSIS_MISSING:   "No diagnosis on claim",
  PRINCIPAL_DX_MISSING:"No principal diagnosis",
  DIAGNOSIS_CODE_MISSING:"Diagnosis code missing",
  ICD10_FORMAT:        "ICD-10 format warning",
  // Activity
  ACTIVITY_MISSING:    "No activities on claim",
  ACTIVITY_REQUIRED:   "Required activity field missing",
  ACTIVITY_DATE:       "Activity date format",
  ACTIVITY_ID_DUPLICATE:"Duplicate activity ID",
  ACTIVITY_OUTSIDE_ENCOUNTER:"Activity outside encounter window",
  OBSERVATION_INCOMPLETE:"Incomplete observation",
  CPT_CODE_FORMAT:     "Non-standard CPT code format",
  CPT_ICD_LINK_MISSING:"CPT–ICD linkage missing",
  MODIFIER_REVIEW:     "Modifier review (Pure RCM comparison)",
  // Coding rules
  DUPLICATE_CLAIM:     "Duplicate claim signature",
  OVERLAPPING_ACTIVITY:"Overlapping activity (same member)",
  MUE_EXCEEDED:        "MUE limit exceeded",
  MUTUALLY_EXCLUSIVE_CODES:"Mutually exclusive codes",
  ICD_EXCLUDES1:       "ICD Excludes-1 pair",
  SERVICE_FREQUENCY_LIMIT:"Service frequency limit",
  FREQUENCY_MODIFIER_REQUIRED:"Frequency modifier required",
  CONSULT_SAME_DAY_DUPLICATE:"Same-day consultation duplicate",
  CONSULT_FOLLOWUP_7D: "7-day consultation follow-up",
  CONSULT_REVIEW_8_28D:"8–28 day consultation review",
  BUNDLING_SEQUENCE_REVIEW:"Bundling / sequencing review",
  // NAS
  NAS_VITD_REPEAT_90D: "NAS: Vit D repeat <90 days",
  NAS_VITD_82652_COMPLEX_INDICATION:"NAS: 82652 complex indication",
  NAS_VITD_COMBINATION_REVIEW:"NAS: 82306+82652 combination",
  NAS_ECHO_DOPPLER_BUNDLE_REVIEW:"NAS: Echo Doppler bundling",
  NAS_INFUSION_96376_30MIN_REVIEW:"NAS: 96376 timing review",
  ALL_CLEAR:           "All clear",
};

function XmlCheckPanel({ sessionId }) {
  const [findings, setFindings] = useState([]);
  const [summary, setSummary] = useState(null);
  const [filter, setFilter] = useState("FLAGGED");
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!sessionId) return;
    setLoading(true);
    // Fetch all findings (BLOCK + WARN) and separately the first row for summary
    Promise.all([
      api.get(`/scrub/sessions/${sessionId}/xml-findings`),
      api.get(`/scrub/sessions/${sessionId}/xml-findings?severity=BLOCK`),
      api.get(`/scrub/sessions/${sessionId}/xml-findings?severity=WARN`),
    ])
      .then(([all, blocks, warns]) => {
        setFindings(all.data);
        const first = all.data[0];
        if (first) {
          // Get session-level summary from the first stored row
          setSummary({
            status: first.session_status,
            risk_level: first.risk_level,
            risk_score: first.risk_score,
            blocks: blocks.data.length,
            warnings: warns.data.length,
          });
        }
      })
      .catch(() => {})
      .finally(() => setLoading(false));
  }, [sessionId]);

  const filtered =
    filter === "FLAGGED" ? findings.filter((f) => f.severity !== "PASS") :
    filter === "BLOCK"   ? findings.filter((f) => f.severity === "BLOCK") :
    filter === "WARN"    ? findings.filter((f) => f.severity === "WARN") :
                           findings;

  const blockCount = findings.filter((f) => f.severity === "BLOCK").length;
  const warnCount  = findings.filter((f) => f.severity === "WARN").length;

  return (
    <div className="space-y-4">
      {/* Summary bar */}
      {summary && (
        <div className="flex flex-wrap items-center gap-3 rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
          <div>
            <p className="text-xs font-semibold uppercase text-slate-400">XML Status</p>
            <Badge label={summary.status} style={SEVERITY_STYLES[summary.status] || SEVERITY_STYLES.PASS} />
          </div>
          {summary.risk_level && (
            <div>
              <p className="text-xs font-semibold uppercase text-slate-400">Denial Risk</p>
              <span className={`inline-block rounded-full border px-3 py-0.5 text-xs font-bold ${RISK_STYLES[summary.risk_level] || ""}`}>
                {summary.risk_level}
                {summary.risk_score > 0 && <span className="ml-1 opacity-70">(score {summary.risk_score})</span>}
              </span>
            </div>
          )}
          <div>
            <p className="text-xs font-semibold uppercase text-slate-400">Blocks</p>
            <p className={`text-lg font-bold ${blockCount > 0 ? "text-red-700" : "text-slate-300"}`}>{blockCount}</p>
          </div>
          <div>
            <p className="text-xs font-semibold uppercase text-slate-400">Warnings</p>
            <p className={`text-lg font-bold ${warnCount > 0 ? "text-yellow-600" : "text-slate-300"}`}>{warnCount}</p>
          </div>
          <div className="ml-auto text-right">
            <p className="text-xs text-slate-400">Profile</p>
            <p className="text-xs font-mono font-semibold text-slate-600">{findings[0]?.profile || "UAE_COMMON"}</p>
          </div>
        </div>
      )}

      {/* Filter chips */}
      <div className="flex flex-wrap gap-2">
        {[
          { key: "FLAGGED", label: `Flagged (${blockCount + warnCount})` },
          { key: "BLOCK",   label: `BLOCK (${blockCount})` },
          { key: "WARN",    label: `WARN (${warnCount})` },
          { key: "ALL",     label: `All (${findings.length})` },
        ].map(({ key, label }) => (
          <button
            key={key}
            onClick={() => setFilter(key)}
            className={`rounded-full px-3 py-1 text-xs font-semibold transition-colors ${
              filter === key
                ? "bg-ghaf-600 text-white"
                : "bg-slate-100 text-slate-600 hover:bg-slate-200"
            }`}
          >
            {label}
          </button>
        ))}
      </div>

      {loading ? (
        <p className="py-4 text-sm text-slate-400">Loading XML findings…</p>
      ) : (
        <div className="overflow-x-auto rounded-xl border border-slate-200 bg-white shadow-sm">
          <table className="min-w-full text-sm">
            <thead className="bg-slate-50 text-xs font-semibold uppercase text-slate-500">
              <tr>
                <th className="px-4 py-3 text-center">Severity</th>
                <th className="px-4 py-3 text-left">Rule</th>
                <th className="px-4 py-3 text-left">Claim</th>
                <th className="px-4 py-3 text-left">Location</th>
                <th className="px-4 py-3 text-left">Message</th>
                <th className="px-4 py-3 text-left">Source</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {filtered.length === 0 ? (
                <tr>
                  <td colSpan={6} className="py-6 text-center text-slate-400">
                    {filter === "FLAGGED"
                      ? "✓ No XML or coding issues found — all checks passed"
                      : "No results for selected filter."}
                  </td>
                </tr>
              ) : (
                filtered.map((f) => (
                  <tr key={f.id} className="hover:bg-slate-50">
                    <td className="px-4 py-3 text-center">
                      <Badge label={f.severity} style={SEVERITY_STYLES[f.severity] || ""} />
                    </td>
                    <td className="px-4 py-2">
                      <p className="font-mono text-xs font-semibold text-slate-700">{f.rule_id}</p>
                      <p className="text-xs text-slate-400">{XML_RULE_LABELS[f.rule_id] || ""}</p>
                    </td>
                    <td className="px-4 py-3 font-mono text-xs text-slate-600">{f.claim_id || "—"}</td>
                    <td className="px-4 py-3 font-mono text-xs text-slate-500 max-w-[160px] truncate" title={f.location}>{f.location || "—"}</td>
                    <td className="px-4 py-3 text-slate-600 max-w-xs">{f.message}</td>
                    <td className="px-4 py-3 text-xs text-slate-400 max-w-[120px] truncate" title={f.source}>{f.source || "—"}</td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main page
// ---------------------------------------------------------------------------
// ---------------------------------------------------------------------------
// Claim queries (Ghaf claim-query template wording)
// ---------------------------------------------------------------------------

const STATUS_STYLE = {
  "CORRECTION APPLIED": "bg-emerald-50 text-emerald-700",
  "CORRECTION REQUIRED": "bg-red-50 text-red-700",
};

function ClaimQueriesPanel({ sessionId }) {
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(false);
  const [opts, setOpts] = useState(EXPORT_DEFAULT);
  const [type, setType] = useState("ALL");

  useEffect(() => {
    if (!sessionId) return;
    setLoading(true);
    api.get(`/scrub/sessions/${sessionId}/queries`)
      .then((r) => setRows(r.data))
      .catch(() => setRows([]))
      .finally(() => setLoading(false));
  }, [sessionId]);

  async function handleDownload() {
    try {
      await downloadBlob(`/scrub/sessions/${sessionId}/query-excel?${exportQuery(opts)}`, "CLAIM QUERIES.xlsx");
    } catch {
      alert("Download failed.");
    }
  }

  const types = [...new Set(rows.map((r) => r.query_type))].sort();
  const shown = type === "ALL" ? rows : rows.filter((r) => r.query_type === type);
  const corrections = rows.filter((r) => r.status === "CORRECTION APPLIED").length;

  return (
    <div className="rounded-xl border border-slate-200 bg-white shadow-sm">
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-100 px-5 py-4">
        <div className="text-sm text-slate-600">
          <span className="font-semibold text-slate-800">{rows.length}</span> queries on{" "}
          <span className="font-semibold text-slate-800">{new Set(rows.map((r) => r.claim_id)).size}</span> claims
          {corrections > 0 && (
            <span className="ml-2 text-xs text-emerald-700">
              · {corrections} corrections to apply in the ERP before sending
            </span>
          )}
        </div>
        <div className="flex items-center gap-2">
          <select value={type} onChange={(e) => setType(e.target.value)}
            className="rounded-lg border border-slate-300 px-2 py-1.5 text-xs">
            <option value="ALL">All query types</option>
            {types.map((t) => <option key={t} value={t}>{t}</option>)}
          </select>
          <ExportControls value={opts} onChange={setOpts} />
          <button onClick={handleDownload} disabled={!rows.length}
            className="rounded-lg bg-ghaf-600 px-4 py-1.5 text-xs font-semibold text-white hover:bg-ghaf-700 disabled:opacity-40">
            Download query Excel
          </button>
        </div>
      </div>
      {loading ? (
        <p className="p-6 text-sm text-slate-400">Loading…</p>
      ) : !shown.length ? (
        <p className="p-6 text-sm text-slate-400">No queries for this batch.</p>
      ) : (
        <div className="max-h-[60vh] overflow-auto">
          <table className="min-w-full text-xs">
            <thead className="sticky top-0 bg-slate-50 text-left text-slate-500">
              <tr>
                <th className="px-3 py-2">Claim / MRN</th>
                <th className="px-3 py-2">Date</th>
                <th className="px-3 py-2">Payer</th>
                <th className="px-3 py-2">Code / Dx</th>
                <th className="px-3 py-2">Query type</th>
                <th className="px-3 py-2">Query</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {shown.map((r) => (
                <tr key={r.id} className="align-top">
                  <td className="px-3 py-2 font-mono text-[11px] text-slate-700">
                    {r.claim_id}<div className="text-slate-400">{r.patient_id}</div>
                  </td>
                  <td className="whitespace-nowrap px-3 py-2">{fmtDay(r.encounter_start)}</td>
                  <td className="px-3 py-2">{r.receiver}</td>
                  <td className="px-3 py-2 font-mono text-[11px]">{r.item}</td>
                  <td className="px-3 py-2">
                    {r.query_type}
                    <div className={`mt-1 inline-block rounded px-1.5 py-0.5 text-[10px] font-semibold ${STATUS_STYLE[r.status] || "bg-slate-100 text-slate-600"}`}>
                      {r.status}
                    </div>
                  </td>
                  <td className="px-3 py-2 text-slate-700">
                    {r.query_text}
                    {r.evidence && <div className="mt-1 text-[10px] text-slate-400">Why: {r.evidence}</div>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

export default function Scrubber() {
  const [tab, setTab] = useState("scrub");           // 'scrub' | 'exclusions'
  const [resultsTab, setResultsTab] = useState("queries"); // 'queries' | 'rules' | 'xml'
  const [refreshKey, setRefreshKey] = useState(0);
  const [selectedSessionId, setSelectedSessionId] = useState(null);
  const [checked, setChecked] = useState([]);
  const [combinedOpts, setCombinedOpts] = useState(EXPORT_DEFAULT);

  function handleUploaded(ids) {
    setRefreshKey((k) => k + 1);
    setSelectedSessionId(ids?.length ? ids[ids.length - 1] : null);
    setChecked(ids?.length > 1 ? ids : []);
    setResultsTab("queries");
  }

  async function handleCombinedDownload() {
    try {
      await downloadBlob(`/scrub/query-excel?session_ids=${checked.join(",")}&${exportQuery(combinedOpts)}`,
        "CLAIM QUERIES.xlsx");
    } catch {
      alert("Download failed.");
    }
  }

  function handleSelectSession(id) {
    // clicking the open batch again closes it
    setSelectedSessionId((cur) => (cur === id ? null : id));
    setResultsTab("queries");
  }

  // Results sit under the upload panel; bring them into view when a session is picked or uploaded
  useEffect(() => {
    if (selectedSessionId) {
      setTimeout(() => document.getElementById("scrub-results")?.scrollIntoView({ behavior: "smooth", block: "start" }), 50);
    }
  }, [selectedSessionId]);

  const tabClass = (t) =>
    `px-4 py-2 text-sm font-medium rounded-t-lg transition-colors ${
      tab === t
        ? "bg-white text-ghaf-700 border border-b-white border-slate-200 -mb-px"
        : "text-slate-500 hover:text-slate-700"
    }`;

  const subTabClass = (t) =>
    `px-4 py-1.5 text-xs font-semibold rounded-full transition-colors ${
      resultsTab === t
        ? "bg-ghaf-600 text-white"
        : "bg-slate-100 text-slate-600 hover:bg-slate-200"
    }`;

  return (
    <div className="mx-auto max-w-7xl space-y-6 p-6">
      {/* Page header */}
      <div>
        <p className="gh-eyebrow">08 — Pre-submission</p>
        <h1 className="gh-h1">Claim Scrubber</h1>
        <p className="mt-1 text-sm text-slate-500">
          Upload the batch (ERP test XML or Excel) — generates the claim query Excel in the Ghaf template, plus DHA
          business rules and XML / coding checks.
        </p>
      </div>

      {/* Main tabs */}
      <div className="border-b border-slate-200">
        <button className={tabClass("scrub")} onClick={() => setTab("scrub")}>
          Scrub Sessions
        </button>
        <button className={tabClass("exclusions")} onClick={() => setTab("exclusions")}>
          Exclusion Codes
        </button>
      </div>

      {tab === "scrub" && (
        <div className="space-y-6">
          <UploadPanel onUploaded={handleUploaded} />

          {selectedSessionId && (
            <div id="scrub-results" className="space-y-4">
              {/* Results header + sub-tabs */}
              <div className="flex items-center justify-between">
                <h2 className="text-sm font-semibold text-slate-600">
                  Results — Session #{selectedSessionId}
                </h2>
                <div className="flex gap-2">
                  <button className={subTabClass("queries")} onClick={() => setResultsTab("queries")}>
                    Claim Queries
                  </button>
                  <button className={subTabClass("rules")} onClick={() => setResultsTab("rules")}>
                    Business Rules
                  </button>
                  <button className={subTabClass("xml")} onClick={() => setResultsTab("xml")}>
                    XML &amp; Coding Check
                  </button>
                  <button onClick={() => setSelectedSessionId(null)} aria-label="Close results"
                    className="rounded-full px-3 py-1.5 text-xs font-semibold text-slate-500 hover:bg-slate-100 hover:text-slate-700">
                    ✕ Close
                  </button>
                </div>
              </div>

              {resultsTab === "queries" && <ClaimQueriesPanel sessionId={selectedSessionId} />}
              {resultsTab === "rules" && <ResultsPanel sessionId={selectedSessionId} />}
              {resultsTab === "xml"   && <XmlCheckPanel sessionId={selectedSessionId} />}
            </div>
          )}

          <div className="flex flex-wrap items-center justify-between gap-3">
            <h2 className="text-sm font-semibold text-slate-600">Recent Sessions</h2>
            <div className="flex items-center gap-2">
              <span className="text-xs text-slate-500">
                {checked.length ? `${checked.length} batches selected` : "Tick batches to combine their queries"}
              </span>
              <ExportControls value={combinedOpts} onChange={setCombinedOpts} />
              <button onClick={handleCombinedDownload} disabled={!checked.length}
                className="rounded-lg bg-ghaf-600 px-4 py-1.5 text-xs font-semibold text-white hover:bg-ghaf-700 disabled:opacity-40">
                Download combined query Excel
              </button>
            </div>
          </div>
          <SessionsList
            refreshKey={refreshKey}
            onSelect={handleSelectSession}
            selectedId={selectedSessionId}
            checked={checked}
            onCheck={setChecked}
          />

        </div>
      )}

      {tab === "exclusions" && <ExclusionsPanel />}
    </div>
  );
}
