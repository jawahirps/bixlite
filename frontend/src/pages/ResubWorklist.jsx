/**
 * ResubWorklist.jsx — Daily Resubmission Worklist
 *
 * Built from each night's RA denials. The Ghaf resubmission team works the
 * Action List; items that need facility input (doctor justification,
 * approvals, documents, eligibility) are routed to the facility, who reply here.
 *
 * Tabs: New Today · Action List · Needs Facility · Closed · Playbook
 */
import { useEffect, useMemo, useState } from "react";
import apiClient from "../api/client";
import { useAuth } from "../context/AuthContext";

const PRIORITY_STYLE = {
  P1: "bg-orange-100 text-orange-800 font-bold",
  P2: "bg-amber-100 text-amber-800 font-semibold",
  P3: "bg-slate-100 text-slate-600",
};
const OWNER_STYLE = {
  Ghaf: "bg-teal-100 text-teal-800",
  Facility: "bg-indigo-100 text-indigo-800",
};

function fmt(n) {
  if (n == null) return "—";
  return Number(n).toLocaleString("en-AE", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}
function fmtDate(s) {
  if (!s) return "—";
  const [y, m, d] = s.slice(0, 10).split("-");
  return `${d}/${m}/${y}`;
}

function DaysChip({ d, bucket }) {
  if (bucket === "expired" || d < 0) return <span className="rounded-full bg-slate-200 px-2 py-0.5 text-xs text-slate-500">Expired</span>;
  if (d <= 5) return <span className="rounded-full bg-orange-100 px-2 py-0.5 text-xs font-bold text-orange-700">{d}d left</span>;
  return <span className="rounded-full bg-emerald-100 px-2 py-0.5 text-xs text-emerald-700">{d}d left</span>;
}

function CopyButton({ text }) {
  const [done, setDone] = useState(false);
  return (
    <button
      onClick={() => { navigator.clipboard.writeText(text || ""); setDone(true); setTimeout(() => setDone(false), 1500); }}
      className="rounded border border-slate-300 px-2 py-0.5 text-xs text-slate-600 hover:bg-slate-50">
      {done ? "Copied" : "Copy"}
    </button>
  );
}

export default function ResubWorklist() {
  const { user } = useAuth();
  const [facilities, setFacilities] = useState([]);
  const [facilityId, setFacilityId] = useState(() => {
    const saved = localStorage.getItem("ghaf_dha_facility_id");
    return user?.facility_id || (saved ? Number(saved) : 13);
  });
  const [data, setData] = useState(null);
  const [playbook, setPlaybook] = useState([]);
  const [tab, setTab] = useState("action");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [expanded, setExpanded] = useState(null);
  const [saving, setSaving] = useState(null);
  const [filters, setFilters] = useState({ q: "", owner: "", priority: "", category: "" });
  const [limit, setLimit] = useState(200);

  const facilityView = data?.facility_view;
  const canEditPlaybook = ["admin", "billing_manager"].includes(user?.role);

  useEffect(() => {
    apiClient.get("/facilities").then(r => setFacilities(Array.isArray(r.data) ? r.data : [])).catch(() => {});
    load(facilityId);
    apiClient.get("/resub/playbook").then(r => setPlaybook(r.data)).catch(() => {});
  }, []);

  async function load(fid) {
    setLoading(true); setError(null); setExpanded(null);
    try {
      const r = await apiClient.get("/resub/worklist", { params: { facility_id: fid } });
      setData(r.data);
      if (r.data.facility_view) setTab("facility");
    } catch (e) {
      setError(e.response?.data?.detail || e.message);
    } finally { setLoading(false); }
  }

  function changeFacility(id) {
    setFacilityId(id);
    localStorage.setItem("ghaf_dha_facility_id", id);
    load(id);
  }

  async function saveItem(item, patch) {
    setSaving(item.key);
    try {
      await apiClient.post("/resub/tracking", {
        facility_id: facilityId,
        updates: [{ claim_id: item.claim_id, activity_id: item.activity_id, ra_date: item.ra_date, ...patch }],
      });
      setData(d => ({
        ...d,
        items: d.items.map(x => x.key === item.key ? {
          ...x, ...patch,
          owner: patch.owner_override || x.owner,
          bucket: ["Resubmitted", "Not resubmitting (write-off)", "Collect from patient"].includes(patch.status) ? "closed_team" : x.bucket,
          updated_by: user?.email,
        } : x),
      }));
    } catch (e) {
      setError(e.response?.data?.detail || e.message);
    } finally { setSaving(null); }
  }

  async function exportExcel(audience) {
    try {
      const r = await apiClient.get("/resub/export", { params: { facility_id: facilityId, audience }, responseType: "blob" });
      const url = URL.createObjectURL(r.data);
      const a = document.createElement("a"); a.href = url;
      a.download = `Resub_Worklist_${audience}_${facilityId}.xlsx`; a.click();
      setTimeout(() => URL.revokeObjectURL(url), 5000);
    } catch (e) {
      setError("Export failed: " + (e.response?.data?.detail || e.message));
    }
  }

  const items = data?.items || [];
  const open = items.filter(x => x.bucket === "open");
  const lists = {
    new: open.filter(x => x.is_new),
    action: open,
    facility: open.filter(x => x.owner === "Facility"),
    closed: items.filter(x => x.bucket !== "open"),
  };
  const categories = useMemo(() => [...new Set(items.map(x => x.category))].sort(), [items]);

  const shown = (lists[tab] || []).filter(x =>
    (!filters.owner || x.owner === filters.owner) &&
    (!filters.priority || x.priority === filters.priority) &&
    (!filters.category || x.category === filters.category) &&
    (!filters.q || `${x.claim_id} ${x.denial_code} ${x.receiver} ${x.payer} ${x.authorization_id} ${x.clinician} ${x.member_id}`.toLowerCase().includes(filters.q.toLowerCase()))
  );

  const s = data?.summary;
  const tabs = facilityView
    ? [{ key: "facility", label: `Needs Your Input (${lists.facility.length})` }]
    : [
        { key: "new", label: `New Today (${lists.new.length})` },
        { key: "action", label: `Action List (${lists.action.length})` },
        { key: "facility", label: `Needs Facility (${lists.facility.length})` },
        { key: "closed", label: `Closed (${lists.closed.length})` },
        { key: "playbook", label: `Playbook (${playbook.length})` },
      ];

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold text-slate-900">Resubmission Worklist</h1>
        <p className="text-sm text-slate-500">
          {facilityView
            ? "Claims where the Ghaf team needs your input to resubmit before the payer deadline."
            : `Every denied activity from the RAs, with what to do and who does it. Window ${data?.window_days ?? 21} days from RA date.`}
        </p>
      </div>

      {/* Facility + actions */}
      <div className="flex flex-wrap items-end gap-3 rounded-xl bg-white p-4 shadow-sm">
        <div className="min-w-48 flex-1">
          <label className="mb-1 block text-xs font-medium text-slate-600">Facility</label>
          <select value={facilityId} disabled={facilityView}
            onChange={e => changeFacility(Number(e.target.value))}
            className="w-full rounded-lg border border-slate-300 px-3 py-1.5 text-sm focus:border-ghaf-500 focus:outline-none focus:ring-1 focus:ring-ghaf-500">
            {facilities.filter(f => f.facility_code !== "GHAF-001").map(f => (
              <option key={f.id} value={f.id}>{f.facility_name}</option>
            ))}
          </select>
        </div>
        <button onClick={() => load(facilityId)} disabled={loading}
          className="rounded-lg bg-ghaf-600 px-4 py-1.5 text-sm font-semibold text-white hover:bg-ghaf-700 disabled:opacity-60">
          {loading ? "Loading…" : "Refresh"}
        </button>
        {!facilityView && (
          <button onClick={() => exportExcel("ghaf")}
            className="rounded-lg border border-slate-300 px-4 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-50">
            Excel — Ghaf team
          </button>
        )}
        <button onClick={() => exportExcel("facility")}
          className="rounded-lg border border-slate-300 px-4 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-50">
          Excel — Facility requests
        </button>
      </div>

      {error && <div className="rounded-lg bg-red-50 p-3 text-sm text-red-700">{error}</div>}

      {/* KPI cards */}
      {s && !facilityView && (
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
          {[
            { label: "New from last night", v: s.new, c: "bg-sky-50 text-sky-800" },
            { label: "Open (window running)", v: s.open, c: "bg-white text-slate-800" },
            { label: "Urgent (≤5 days)", v: s.urgent, c: s.urgent.count ? "bg-orange-50 text-orange-800" : "bg-white text-slate-400" },
            { label: "Ghaf team", v: s.ghaf, c: "bg-teal-50 text-teal-800" },
            { label: "Needs facility", v: s.facility, c: "bg-indigo-50 text-indigo-800" },
            { label: "Resubmitted (7 days)", v: s.resubmitted_7d, c: "bg-emerald-50 text-emerald-800" },
          ].map(k => (
            <div key={k.label} className={`rounded-xl p-4 shadow-sm ${k.c}`}>
              <p className="text-2xl font-bold">{k.v.count.toLocaleString()}</p>
              <p className="mt-0.5 text-xs font-medium opacity-80">{k.label}</p>
              <p className="mt-1 text-xs opacity-70">AED {fmt(k.v.denied)} denied · ~{fmt(k.v.expected)} expected</p>
            </div>
          ))}
        </div>
      )}
      {data && !facilityView && (
        <p className="text-xs text-slate-500">
          Expected recovery uses this facility's own history: of the amounts resubmitted to each payer for each
          denial code, how much was paid. Facility average: <strong>{data.overall_rate}%</strong>.
          {s?.expired_7d?.count > 0 && <> · {s.expired_7d.count} activities (AED {fmt(s.expired_7d.denied)}) expired in the last 7 days without resubmission.</>}
        </p>
      )}

      {/* Tabs */}
      <div className="flex flex-wrap gap-1 border-b border-slate-200">
        {tabs.map(t => (
          <button key={t.key} onClick={() => { setTab(t.key); setExpanded(null); setLimit(200); }}
            className={`border-b-2 px-4 py-2 text-sm font-medium transition-colors ${
              tab === t.key ? "border-ghaf-600 text-ghaf-700" : "border-transparent text-slate-500 hover:text-slate-700"}`}>
            {t.label}
          </button>
        ))}
      </div>

      {tab === "playbook" ? (
        <Playbook rows={playbook} setRows={setPlaybook} canEdit={canEditPlaybook} setError={setError} />
      ) : (
        <>
          {/* Filters */}
          <div className="flex flex-wrap gap-2">
            <input placeholder="Search claim, code, receiver, payer, auth ID, clinician, member…" value={filters.q}
              onChange={e => setFilters(f => ({ ...f, q: e.target.value }))}
              className="min-w-64 flex-1 rounded-lg border border-slate-300 px-3 py-1.5 text-sm" />
            {!facilityView && (
              <select value={filters.owner} onChange={e => setFilters(f => ({ ...f, owner: e.target.value }))}
                className="rounded-lg border border-slate-300 px-2 py-1.5 text-sm">
                <option value="">All owners</option><option>Ghaf</option><option>Facility</option>
              </select>
            )}
            <select value={filters.priority} onChange={e => setFilters(f => ({ ...f, priority: e.target.value }))}
              className="rounded-lg border border-slate-300 px-2 py-1.5 text-sm">
              <option value="">All priorities</option><option>P1</option><option>P2</option><option>P3</option>
            </select>
            <select value={filters.category} onChange={e => setFilters(f => ({ ...f, category: e.target.value }))}
              className="rounded-lg border border-slate-300 px-2 py-1.5 text-sm">
              <option value="">All categories</option>
              {categories.map(c => <option key={c}>{c}</option>)}
            </select>
          </div>

          <div className="overflow-hidden rounded-xl bg-white shadow-sm">
            <div className="overflow-x-auto">
              <table className="min-w-full divide-y divide-slate-200 text-sm">
                <thead className="bg-slate-50">
                  <tr>
                    {["Priority", "Deadline", "Claim / Activity", "Receiver / Payer", "Denial", "Denied (AED)",
                      ...(facilityView ? [] : ["Expected", "Owner"]), "Status"].map(h => (
                      <th key={h} className="whitespace-nowrap px-3 py-2 text-left font-semibold text-slate-600">{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {shown.slice(0, limit).map(x => (
                    <Row key={x.key} x={x} expanded={expanded === x.key}
                      onToggle={() => setExpanded(expanded === x.key ? null : x.key)}
                      onSave={patch => saveItem(x, patch)} saving={saving === x.key}
                      statuses={data.statuses} facilityView={facilityView} />
                  ))}
                  {shown.length === 0 && (
                    <tr><td colSpan={9} className="px-3 py-8 text-center text-slate-400">
                      {loading ? "Loading…" : "Nothing here."}
                    </td></tr>
                  )}
                </tbody>
              </table>
            </div>
            {shown.length > limit && (
              <div className="border-t border-slate-100 p-3 text-center">
                <button onClick={() => setLimit(l => l + 200)} className="text-sm font-medium text-ghaf-700 hover:underline">
                  Show more ({shown.length - limit} remaining)
                </button>
              </div>
            )}
          </div>
        </>
      )}
    </div>
  );
}

function Row({ x, expanded, onToggle, onSave, saving, statuses, facilityView }) {
  const [remarks, setRemarks] = useState(x.remarks || "");
  const [assigned, setAssigned] = useState(x.assigned_to || "");
  const [reply, setReply] = useState(x.facility_reply || "");
  const facilityStatuses = ["Facility replied", "Collect from patient"];

  return (
    <>
      <tr className={`cursor-pointer hover:bg-slate-50 ${x.is_new ? "bg-sky-50/50" : ""}`} onClick={onToggle}>
        <td className="px-3 py-2">
          {x.priority ? <span className={`rounded px-2 py-0.5 text-xs ${PRIORITY_STYLE[x.priority]}`}>{x.priority}</span> : "—"}
          {x.is_new && <span className="ml-1 rounded bg-sky-100 px-1.5 py-0.5 text-[10px] font-semibold text-sky-700">NEW</span>}
        </td>
        <td className="whitespace-nowrap px-3 py-2">
          <DaysChip d={x.days_left} bucket={x.bucket} />
          <div className="mt-0.5 text-xs text-slate-400">{fmtDate(x.deadline)}</div>
        </td>
        <td className="px-3 py-2">
          <div className="font-medium text-slate-800">{x.claim_id}</div>
          <div className="text-xs text-slate-400">{x.activity_code} · {x.activity_id}</div>
        </td>
        <td className="max-w-[220px] px-3 py-2 text-xs">
          <div className="truncate text-slate-700" title={x.receiver}>{x.receiver || "—"}</div>
          <div className="truncate text-slate-400" title={x.payer}>{x.payer || "—"}</div>
        </td>
        <td className="max-w-xs px-3 py-2">
          <div className="font-mono text-xs font-semibold text-slate-700">{x.denial_code}</div>
          <div className="truncate text-xs text-slate-500" title={x.denial_reason}>{x.category}{x.route === "Reconciliation" ? " · Reconciliation" : ""}</div>
        </td>
        <td className="px-3 py-2 text-right tabular-nums">{fmt(x.denied)}</td>
        {!facilityView && (
          <>
            <td className="px-3 py-2 text-right text-xs tabular-nums text-slate-500">
              {fmt(x.expected_recovery)}<div className="text-slate-400">{x.recovery_pct}%</div>
            </td>
            <td className="px-3 py-2">
              <span className={`rounded px-2 py-0.5 text-xs font-medium ${OWNER_STYLE[x.owner]}`}>{x.owner}</span>
              {x.owner === "Facility" && x.facility_role && <div className="mt-0.5 text-xs text-slate-400">{x.facility_role}</div>}
            </td>
          </>
        )}
        <td className="px-3 py-2 text-xs" onClick={e => e.stopPropagation()}>
          <select value={x.status} disabled={saving || x.bucket === "closed_resubmitted"}
            onChange={e => onSave({ status: e.target.value })}
            className="rounded border border-slate-300 px-1.5 py-1 text-xs">
            {(facilityView ? [x.status, ...facilityStatuses.filter(s => s !== x.status)] : statuses).map(s => <option key={s}>{s}</option>)}
          </select>
          {x.updated_by && <div className="mt-0.5 text-[10px] text-slate-400">{x.updated_by}</div>}
        </td>
      </tr>
      {expanded && (
        <tr className="bg-slate-50/70">
          <td colSpan={9} className="px-4 py-4">
            <div className="grid gap-4 text-sm lg:grid-cols-2">
              <div className="space-y-2">
                <Field label="Denial reason (from payer)">{x.denial_reason || "—"}</Field>
                <div className="grid grid-cols-2 gap-x-4 gap-y-2 rounded-lg border border-slate-200 bg-white p-3 sm:grid-cols-3">
                  <Field label="Receiver (TPA)">{x.receiver || "—"}</Field>
                  <Field label="Payer (insurer)">{x.payer || "—"}</Field>
                  <Field label="Member ID"><span className="font-mono">{x.member_id || "—"}</span></Field>
                  <Field label="Auth ID">
                    {x.authorization_id
                      ? <span className="flex items-center gap-2 font-mono">{x.authorization_id} <CopyButton text={x.authorization_id} /></span>
                      : <span className="text-slate-400">none on claim</span>}
                  </Field>
                  <Field label="Treatment / RA">{fmtDate(x.treatment_date)} · {fmtDate(x.ra_date)}</Field>
                  <Field label="Clinician">{x.clinician || "—"}</Field>
                </div>
                {!facilityView && <Field label="Action (Ghaf team)">{x.action}</Field>}
                {x.facility_request && <Field label={`Needed from facility${x.facility_role ? ` — ${x.facility_role}` : ""}`}>{x.facility_request}</Field>}
                <Field label="Documents to attach">{x.documents || "—"}</Field>
                {!facilityView && x.draft_comment && (
                  <Field label={<span className="flex items-center gap-2">Draft resubmission comment <CopyButton text={x.draft_comment} /></span>}>
                    <span className="italic">{x.draft_comment}</span>
                  </Field>
                )}
              </div>
              <div className="space-y-3">
                {!facilityView && (
                  <>
                    <div className="flex flex-wrap gap-2">
                      <label className="text-xs font-medium text-slate-600">Route to
                        <select value={x.owner} disabled={saving}
                          onChange={e => onSave({ owner_override: e.target.value })}
                          className="ml-2 rounded border border-slate-300 px-1.5 py-1 text-xs">
                          <option>Ghaf</option><option>Facility</option>
                        </select>
                      </label>
                    </div>
                    <TextBox label="Assigned to" value={assigned} onChange={setAssigned}
                      onSave={() => onSave({ assigned_to: assigned })} saving={saving} single />
                    <TextBox label="Ghaf remarks" value={remarks} onChange={setRemarks}
                      onSave={() => onSave({ remarks })} saving={saving} />
                  </>
                )}
                <TextBox label="Facility reply" value={reply} onChange={setReply} saving={saving}
                  onSave={() => onSave(facilityView ? { facility_reply: reply, status: "Facility replied" } : { facility_reply: reply })} />
              </div>
            </div>
          </td>
        </tr>
      )}
    </>
  );
}

function Field({ label, children }) {
  return (
    <div>
      <div className="text-xs font-semibold uppercase tracking-wide text-slate-500">{label}</div>
      <div className="mt-0.5 text-slate-700">{children}</div>
    </div>
  );
}

function TextBox({ label, value, onChange, onSave, saving, single }) {
  return (
    <div>
      <div className="mb-1 text-xs font-semibold uppercase tracking-wide text-slate-500">{label}</div>
      <div className="flex gap-2">
        {single
          ? <input value={value} onChange={e => onChange(e.target.value)} className="flex-1 rounded border border-slate-300 px-2 py-1 text-sm" />
          : <textarea value={value} onChange={e => onChange(e.target.value)} rows={2} className="flex-1 rounded border border-slate-300 px-2 py-1 text-sm" />}
        <button onClick={onSave} disabled={saving}
          className="self-start rounded bg-ghaf-600 px-3 py-1 text-xs font-semibold text-white hover:bg-ghaf-700 disabled:opacity-60">
          {saving ? "…" : "Save"}
        </button>
      </div>
    </div>
  );
}

function Playbook({ rows, setRows, canEdit, setError }) {
  const [editing, setEditing] = useState(null);
  const [draft, setDraft] = useState({});
  const FIELDS = [
    ["pattern", "Denial code pattern", true], ["activity_pattern", "Activity code pattern (optional)", true],
    ["reason_pattern", "Denial reason contains (optional)", true], ["category", "Category", true], ["owner", "Owner", true],
    ["facility_role", "Facility role", true], ["action", "Action (Ghaf team)"],
    ["facility_request", "What we ask the facility"], ["documents", "Documents to attach"],
    ["draft_comment", "Draft resubmission comment"],
  ];

  async function save(id) {
    try {
      const r = await apiClient.put(`/resub/playbook/${id}`, draft);
      setRows(rs => rs.map(x => x.id === id ? r.data : x));
      setEditing(null);
    } catch (e) { setError(e.response?.data?.detail || e.message); }
  }

  return (
    <div className="space-y-3">
      <div className="rounded-xl border border-teal-200 bg-teal-50 px-4 py-3 text-xs text-teal-900">
        Rules are checked top to bottom; the first rule whose denial code (and, if set, activity code and denial reason) all match is used.
        <strong> Owner = Facility</strong> sends the item to the facility with the request text below.
        {!canEdit && " Only admins and billing managers can edit."}
      </div>
      {rows.map(p => (
        <div key={p.id} className="rounded-xl bg-white p-4 shadow-sm">
          {editing === p.id ? (
            <div className="grid gap-3 md:grid-cols-2">
              {FIELDS.map(([k, label, short]) => (
                <label key={k} className={`text-xs font-medium text-slate-600 ${short ? "" : "md:col-span-2"}`}>
                  {label}
                  {k === "owner" ? (
                    <select value={draft.owner ?? p.owner} onChange={e => setDraft(d => ({ ...d, owner: e.target.value }))}
                      className="mt-1 block w-full rounded border border-slate-300 px-2 py-1 text-sm">
                      <option>Ghaf</option><option>Facility</option>
                    </select>
                  ) : short ? (
                    <input value={draft[k] ?? p[k] ?? ""} onChange={e => setDraft(d => ({ ...d, [k]: e.target.value }))}
                      className="mt-1 block w-full rounded border border-slate-300 px-2 py-1 text-sm" />
                  ) : (
                    <textarea rows={2} value={draft[k] ?? p[k] ?? ""} onChange={e => setDraft(d => ({ ...d, [k]: e.target.value }))}
                      className="mt-1 block w-full rounded border border-slate-300 px-2 py-1 text-sm" />
                  )}
                </label>
              ))}
              <div className="flex gap-2 md:col-span-2">
                <button onClick={() => save(p.id)} className="rounded bg-ghaf-600 px-4 py-1.5 text-sm font-semibold text-white hover:bg-ghaf-700">Save</button>
                <button onClick={() => setEditing(null)} className="rounded border border-slate-300 px-4 py-1.5 text-sm">Cancel</button>
              </div>
            </div>
          ) : (
            <div className="grid gap-2 text-sm md:grid-cols-[220px_1fr]">
              <div>
                <div className="font-mono text-xs font-semibold text-slate-700">{p.pattern === ".*" && (p.activity_pattern || p.reason_pattern) ? "any code" : p.pattern}</div>
                {p.activity_pattern && <div className="mt-0.5 text-[11px] text-slate-500">activity: <span className="font-mono">{p.activity_pattern}</span></div>}
                {p.reason_pattern && <div className="mt-0.5 text-[11px] text-slate-500">reason: <span className="font-mono">{p.reason_pattern}</span></div>}
                <div className="mt-1 font-medium text-slate-800">{p.category}</div>
                <span className={`mt-1 inline-block rounded px-2 py-0.5 text-xs font-medium ${OWNER_STYLE[p.owner]}`}>
                  {p.owner}{p.facility_role ? ` · ${p.facility_role}` : ""}
                </span>
                {canEdit && (
                  <button onClick={() => { setEditing(p.id); setDraft({}); }}
                    className="mt-2 block text-xs font-medium text-ghaf-700 hover:underline">Edit</button>
                )}
              </div>
              <div className="space-y-1.5">
                <Field label="Action (Ghaf team)">{p.action}</Field>
                {p.facility_request && <Field label="What we ask the facility">{p.facility_request}</Field>}
                <Field label="Documents">{p.documents || "—"}</Field>
                <Field label="Draft comment"><span className="italic">{p.draft_comment}</span></Field>
                {p.updated_by && <div className="text-[10px] text-slate-400">Last edited by {p.updated_by}</div>}
              </div>
            </div>
          )}
        </div>
      ))}
    </div>
  );
}
