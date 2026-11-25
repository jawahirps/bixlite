import { useEffect, useState } from "react";
import apiClient from "../api/client";
import { useAuth } from "../context/AuthContext";
import { confirmDialog } from "../utils/notify";

export const ROLES = {
  admin: { label: "Super Admin", help: "Everything, all facilities: users, facility settings, PRODUCTION upload", scope: "all" },
  group_admin: { label: "Group Admin", help: "Approve resubmissions and reports for every facility in their group(s)", scope: "groups" },
  facility_admin: { label: "Facility Admin", help: "Same as Group Admin, for one facility", scope: "one" },
  staff: { label: "Staff (coder / RCM)", help: "Worklists, prepare resubmission, TEST upload — assigned facilities", scope: "any" },
  viewer: { label: "Viewer", help: "Reports and dashboards only (e.g. clinic owner) — assigned facilities", scope: "any" },
  billing_manager: { label: "Billing manager (old)", help: "Old role — change to one of the roles above", scope: "any", legacy: true },
};

function accessText(u, meta) {
  if (u.role === "admin") return "All facilities";
  if (!meta) return "";
  const g = (u.group_ids || []).map((id) => meta.groups.find((x) => x.id === id)?.name).filter(Boolean);
  const f = (u.facility_ids || []).map((id) => meta.facilities.find((x) => x.id === id)?.name).filter(Boolean);
  const parts = [...g.map((n) => `${n} (group)`), ...f];
  return parts.length ? parts.join(", ") : "None — can't see any facility";
}

function AccessPicker({ role, meta, value, onChange }) {
  const scope = ROLES[role]?.scope;
  if (!meta || scope === "all") return <p className="text-sm text-slate-500">Super Admin sees all facilities.</p>;
  const toggle = (k, id) => {
    const cur = value[k] || [];
    const next = scope === "one" ? [id] : cur.includes(id) ? cur.filter((x) => x !== id) : [...cur, id];
    onChange({ ...value, [k]: next });
  };
  const box = (k, id, label) => (
    <label key={`${k}${id}`} className="flex items-center gap-2 text-sm">
      <input type={scope === "one" ? "radio" : "checkbox"} name={`acc-${k}`} checked={(value[k] || []).includes(id)} onChange={() => toggle(k, id)} />
      {label}
    </label>
  );
  return (
    <div className="grid gap-4 sm:grid-cols-2">
      {scope !== "one" && (
        <div><p className="gh-label">Facility groups{scope === "groups" ? "" : " (optional)"}</p>
          <div className="mt-1 space-y-1">{meta.groups.map((g) => box("group_ids", g.id, `${g.name} (${g.facility_ids.length})`))}</div></div>
      )}
      {scope !== "groups" && (
        <div><p className="gh-label">{scope === "one" ? "Facility" : "Single facilities"}</p>
          <div className="mt-1 max-h-56 space-y-1 overflow-y-auto">{meta.facilities.map((f) => box("facility_ids", f.id, f.name))}</div></div>
      )}
    </div>
  );
}

const plusYear = (from) => { const d = from ? new Date(from) : new Date(); d.setFullYear(d.getFullYear() + 1); return d.toISOString().slice(0, 10); };
const todayISO = () => new Date(Date.now() + 4 * 3600e3).toISOString().slice(0, 10);
function expiryInfo(u) {
  if (!u.expires_at) return { text: "No limit", cls: "text-slate-500" };
  const days = Math.round((new Date(u.expires_at) - new Date(todayISO())) / 864e5);
  const d = new Date(u.expires_at).toLocaleDateString("en-GB", { dateStyle: "medium" });
  if (days < 0) return { text: `Expired ${d}`, cls: "text-denied font-medium", expired: true };
  if (days <= 30) return { text: `${d} (${days} days left)`, cls: "text-amber-700 font-medium" };
  return { text: d, cls: "text-slate-600" };
}

const fmt = (d) => (d ? new Date(d).toLocaleString("en-GB", { dateStyle: "medium", timeStyle: "short" }) : "Never");

function genPassword() {
  const chars = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789";
  const a = new Uint32Array(14);
  crypto.getRandomValues(a);
  return Array.from(a, (n) => chars[n % chars.length]).join("");
}

function PasswordField({ value, onChange }) {
  return (
    <div className="flex gap-2">
      <input className="gh-input w-full font-mono" value={value} onChange={(e) => onChange(e.target.value)}
        minLength={8} required placeholder="At least 8 characters" />
      <button type="button" className="gh-btn-secondary gh-btn-sm whitespace-nowrap" onClick={() => onChange(genPassword())}>
        Generate
      </button>
    </div>
  );
}

function UserForm({ initial, meta, onCancel, onSaved }) {
  const isNew = !initial;
  const [f, setF] = useState(initial ? { full_name: initial.full_name, role: initial.role, expires_at: initial.expires_at || "" }
    : { full_name: "", email: "", role: "staff", password: genPassword(), expires_at: "" });
  const setRole = (e) => { const role = e.target.value;
    setF((x) => ({ ...x, role, expires_at: role === "viewer" && !x.expires_at ? plusYear() : x.expires_at })); };
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  const set = (k) => (v) => setF((x) => ({ ...x, [k]: v?.target ? v.target.value : v }));
  const [acc, setAcc] = useState({ facility_ids: initial?.facility_ids || [], group_ids: initial?.group_ids || [] });
  const scope = ROLES[f.role]?.scope;
  const accFor = () => scope === "groups" ? { facility_ids: [], group_ids: acc.group_ids }
    : scope === "one" ? { facility_ids: acc.facility_ids.slice(0, 1), group_ids: [] } : acc;

  const submit = async (e) => {
    e.preventDefault();
    setBusy(true); setErr(null);
    try {
      const exp = f.expires_at || null;
      const r = isNew ? await apiClient.post("/auth/users", { ...f, expires_at: exp })
        : await apiClient.patch(`/auth/users/${initial.id}`, { full_name: f.full_name, role: f.role, expires_at: exp });
      if (scope !== "all") {
        try { await apiClient.put(`/auth/users/${r.data.id}/access`, accFor()); }
        catch (e3) {
          const d3 = e3.response?.data?.detail;
          setErr(`User saved, but access not set: ${d3 || "error"}. Open Edit to fix it.`);
          setBusy(false); return;
        }
      } else if (!isNew) { await apiClient.put(`/auth/users/${r.data.id}/access`, { facility_ids: [], group_ids: [] }); }
      onSaved(r.data, isNew ? f.password : null);
    } catch (e2) {
      const d = e2.response?.data?.detail;
      setErr(Array.isArray(d) ? d.map((x) => x.msg).join("; ") : d || "Could not save");
    } finally { setBusy(false); }
  };

  return (
    <form onSubmit={submit} className="gh-section space-y-4">
      <h2 className="gh-section-title">{isNew ? "Add a user" : `Edit ${initial.email}`}</h2>
      <div className="grid gap-4 sm:grid-cols-2">
        <label className="space-y-1"><span className="gh-label">Full name</span>
          <input className="gh-input w-full" value={f.full_name} onChange={set("full_name")} required /></label>
        {isNew && (
          <label className="space-y-1"><span className="gh-label">{f.role === "viewer" ? "Email or username (used to sign in)" : "Email (used to sign in)"}</span>
            <input className="gh-input w-full" type={f.role === "viewer" ? "text" : "email"} autoCapitalize="none" value={f.email} onChange={set("email")} required
              placeholder={f.role === "viewer" ? "e.g. sanlucas.viewer" : "name@company.com"} />
            {f.role === "viewer" && <span className="block text-xs text-slate-500">A plain username is fine for Viewers; no mailbox needed.</span>}</label>
        )}
        <label className="space-y-1"><span className="gh-label">Role</span>
          <select className="gh-input w-full" value={f.role} onChange={setRole}>
            {Object.entries(ROLES).filter(([k, r]) => !r.legacy || k === f.role).map(([k, r]) => <option key={k} value={k}>{r.label}</option>)}
          </select>
          <span className="block text-xs text-slate-500">{ROLES[f.role]?.help}</span></label>
        <label className="space-y-1"><span className="gh-label">Access expires on</span>
          <div className="flex gap-2">
            <input className="gh-input w-full" type="date" value={f.expires_at || ""} onChange={set("expires_at")} />
            <button type="button" className="gh-btn-secondary gh-btn-sm whitespace-nowrap" onClick={() => set("expires_at")(plusYear(f.expires_at && f.expires_at > todayISO() ? f.expires_at : null))}>+1 year</button>
          </div>
          <span className="block text-xs text-slate-500">{f.expires_at ? "Sign-in stops after this day." : "Empty = no limit."}{f.role === "viewer" ? " Viewers default to 1 year." : ""}</span></label>
        {isNew && (
          <label className="space-y-1"><span className="gh-label">First password</span>
            <PasswordField value={f.password} onChange={set("password")} />
            <span className="block text-xs text-slate-500">Give it to the user privately.</span></label>
        )}
      </div>
      <div className="space-y-2 border-t border-slate-100 pt-4">
        <p className="gh-label">Which facilities this user can see</p>
        <AccessPicker role={f.role} meta={meta} value={acc} onChange={setAcc} />
      </div>
      {err && <p className="text-sm text-denied">{err}</p>}
      <div className="flex gap-2">
        <button className="gh-btn" disabled={busy}>{busy ? "Saving…" : isNew ? "Create user" : "Save"}</button>
        <button type="button" className="gh-btn-secondary" onClick={onCancel}>Cancel</button>
      </div>
    </form>
  );
}

function GroupsPanel({ meta, reload }) {
  const [edit, setEdit] = useState(null);   // {id?, name, facility_ids}
  const [err, setErr] = useState(null);
  if (!meta) return null;
  const save = async (e) => {
    e.preventDefault(); setErr(null);
    try {
      edit.id ? await apiClient.put(`/auth/groups/${edit.id}`, edit) : await apiClient.post("/auth/groups", edit);
      setEdit(null); reload();
    } catch (e2) { setErr(e2.response?.data?.detail || "Could not save group"); }
  };
  const tog = (id) => setEdit((x) => ({ ...x, facility_ids: x.facility_ids.includes(id) ? x.facility_ids.filter((y) => y !== id) : [...x.facility_ids, id] }));
  return (
    <section className="gh-section space-y-3">
      <div className="flex items-center justify-between">
        <h2 className="gh-section-title">Facility groups</h2>
        {!edit && <button className="gh-btn-secondary gh-btn-sm" onClick={() => setEdit({ name: "", facility_ids: [] })}>Add group</button>}
      </div>
      <p className="text-xs text-slate-500">A Group Admin sees every facility in their group; a facility added to the group later is included automatically.</p>
      <ul className="space-y-1 text-sm">
        {meta.groups.map((g) => (
          <li key={g.id} className="flex items-start justify-between gap-3">
            <span><b>{g.name}</b>: {g.facility_ids.map((id) => meta.facilities.find((f) => f.id === id)?.name).join(", ") || "no facilities"}</span>
            {!edit && <button className="gh-btn-secondary gh-btn-sm" onClick={() => setEdit({ id: g.id, name: g.name, facility_ids: g.facility_ids })}>Edit</button>}
          </li>
        ))}
      </ul>
      {edit && (
        <form onSubmit={save} className="space-y-3 border-t border-slate-100 pt-3">
          <input className="gh-input w-full sm:w-80" value={edit.name} onChange={(e) => setEdit({ ...edit, name: e.target.value })} placeholder="Group name" required />
          <div className="grid gap-1 sm:grid-cols-2">
            {meta.facilities.map((f) => (
              <label key={f.id} className="flex items-center gap-2 text-sm">
                <input type="checkbox" checked={edit.facility_ids.includes(f.id)} onChange={() => tog(f.id)} />
                {f.name}{f.group_id && f.group_id !== edit.id && <span className="text-xs text-amber-700">(moves from {meta.groups.find((g) => g.id === f.group_id)?.name})</span>}
              </label>
            ))}
          </div>
          {err && <p className="text-sm text-denied">{err}</p>}
          <div className="flex gap-2"><button className="gh-btn">Save group</button>
            <button type="button" className="gh-btn-secondary" onClick={() => setEdit(null)}>Cancel</button></div>
        </form>
      )}
    </section>
  );
}

function MenuByRole() {
  const [d, setD] = useState(null);
  const [m, setM] = useState(null);
  const [msg, setMsg] = useState(null);
  const load = () => apiClient.get("/auth/role-pages").then((r) => { setD(r.data); setM(r.data.matrix); }).catch(() => {});
  useEffect(() => { load(); }, []);
  if (!d || !m) return null;
  const tog = (role, path) => setM((x) => ({ ...x, [role]: x[role].includes(path) ? x[role].filter((p) => p !== path) : [...x[role], path] }));
  const save = async () => {
    setMsg(null);
    try { const r = await apiClient.put("/auth/role-pages", { matrix: m }); setM(r.data.matrix); setMsg("Saved. Users see the change on their next page load."); }
    catch (e) { setMsg(e.response?.data?.detail || "Could not save"); }
  };
  const dirty = JSON.stringify(m) !== JSON.stringify(d.matrix);
  return (
    <section className="gh-section space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="gh-section-title">Menu by role</h2>
        <div className="flex gap-2">
          {dirty && <button className="gh-btn-secondary gh-btn-sm" onClick={() => { setM(d.matrix); setMsg(null); }}>Undo</button>}
          <button className="gh-btn gh-btn-sm" disabled={!dirty} onClick={async () => { await save(); load(); }}>Save menu</button>
        </div>
      </div>
      <p className="text-xs text-slate-500">
        Tick the pages each role sees. Super Admin always sees everything; Users, Facility settings, Data sync, Login
        activity and Scrub rules stay Super Admin only. Dashboard is always on. Hiding a page hides the menu entry and
        blocks its address; Viewers stay read-only and limited to their facilities whatever is ticked.
      </p>
      <div className="overflow-x-auto">
        <table className="min-w-full text-sm">
          <thead className="text-left text-xs uppercase tracking-wide text-slate-500">
            <tr><th className="py-2 pr-4">Page</th>{d.roles.map((r) => <th key={r} className="px-3 py-2 text-center">{ROLES[r]?.label || r}</th>)}</tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {d.pages.map((p) => (
              <tr key={p.path}>
                <td className="py-1.5 pr-4">{p.label}</td>
                {d.roles.map((r) => (
                  <td key={r} className="px-3 py-1.5 text-center">
                    <input type="checkbox" aria-label={`${p.label} for ${r}`} disabled={p.path === "/"}
                      checked={m[r]?.includes(p.path) || false} onChange={() => tog(r, p.path)} />
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {msg && <p className="text-sm text-slate-600">{msg}</p>}
    </section>
  );
}

export default function Users() {
  const { user: me } = useAuth();
  const [users, setUsers] = useState(null);
  const [err, setErr] = useState(null);
  const [mode, setMode] = useState(null);        // null | "new" | user object
  const [notice, setNotice] = useState(null);    // { text, password? }
  const [pwFor, setPwFor] = useState(null);      // user whose password is being reset
  const [newPw, setNewPw] = useState("");

  const [meta, setMeta] = useState(null);
  const load = () => apiClient.get("/auth/users").then((r) => setUsers(r.data))
    .catch((e) => setErr(e.response?.data?.detail || "Could not load users"));
  const loadMeta = () => apiClient.get("/auth/groups").then((r) => setMeta(r.data)).catch(() => {});
  useEffect(() => { load(); loadMeta(); }, []);

  const act = async (fn, okText) => {
    setErr(null);
    try { await fn(); setNotice({ text: okText }); load(); }
    catch (e) { setErr(e.response?.data?.detail || "Action failed"); }
  };

  const toggleActive = async (u) => {
    if (u.is_active && !(await confirmDialog(`${u.role === "viewer" ? "Stop access for" : "Deactivate"} ${u.full_name}? They are signed out on their next click and can't sign in until you restore access.`,
      { okLabel: "Deactivate" }))) return;
    act(() => apiClient.patch(`/auth/users/${u.id}`, { is_active: !u.is_active }),
      `${u.full_name} ${u.is_active ? "deactivated" : "reactivated"}.`);
  };
  const reset2fa = async (u) => {
    if (!(await confirmDialog(`Reset the authenticator app for ${u.full_name}? They will set it up again at next sign-in.`,
      { okLabel: "Reset 2FA" }))) return;
    act(() => apiClient.post(`/auth/users/${u.id}/totp/reset`), `Authenticator reset for ${u.full_name}.`);
  };
  const savePw = async (e) => {
    e.preventDefault();
    const u = pwFor, pw = newPw;
    setErr(null);
    try {
      await apiClient.post(`/auth/users/${u.id}/password`, { password: pw });
      setPwFor(null);
      setNotice({ text: `New password set for ${u.full_name}. Give it to them privately:`, password: pw });
    } catch (e2) { setErr(e2.response?.data?.detail?.[0]?.msg || e2.response?.data?.detail || "Could not set password"); }
  };

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <p className="gh-eyebrow">Administration</p>
          <h1 className="gh-h1">Users</h1>
          <p className="mt-1 max-w-3xl text-sm text-slate-500">
            Everyone signs in at reports.ghafservices.com with their own email (or username, for Viewers) and password. Only a Super Admin can change passwords. The role decides what they
            can do; the facilities column decides which facilities they see (enforced from step 2 of the access spec).
          </p>
        </div>
        {!mode && <button className="gh-btn" onClick={() => { setMode("new"); setNotice(null); }}>Add user</button>}
      </div>

      {notice && (
        <div className="gh-card border-paid/30 bg-paid/5 p-4 text-sm">
          <p>{notice.text}</p>
          {notice.password && (
            <p className="mt-2 flex items-center gap-3">
              <code className="rounded bg-white px-2 py-1 font-mono text-base">{notice.password}</code>
              <button className="gh-btn-secondary gh-btn-sm" onClick={() => navigator.clipboard?.writeText(notice.password)}>Copy</button>
              <span className="text-xs text-slate-500">Shown once only.</span>
            </p>
          )}
        </div>
      )}
      {err && <p className="text-sm text-denied">{err}</p>}

      {mode && (
        <UserForm initial={mode === "new" ? null : mode} meta={meta} onCancel={() => setMode(null)}
          onSaved={(u, pw) => {
            setMode(null); load();
            setNotice(pw ? { text: `${u.full_name} can now sign in as ${u.email}. First password:`, password: pw }
              : { text: `${u.full_name} updated.` });
          }} />
      )}

      {pwFor && (
        <form onSubmit={savePw} className="gh-section space-y-3">
          <h2 className="gh-section-title">New password for {pwFor.full_name}</h2>
          <PasswordField value={newPw} onChange={setNewPw} />
          <div className="flex gap-2">
            <button className="gh-btn">Set password</button>
            <button type="button" className="gh-btn-secondary" onClick={() => setPwFor(null)}>Cancel</button>
          </div>
        </form>
      )}

      <GroupsPanel meta={meta} reload={() => { loadMeta(); load(); }} />

      <MenuByRole />

      <section className="gh-card overflow-x-auto">
        <table className="min-w-full text-sm">
          <thead className="bg-slate-50 text-left text-xs uppercase tracking-wide text-slate-500">
            <tr>
              <th className="px-4 py-3">User</th><th className="px-4 py-3">Role</th><th className="px-4 py-3">Facilities</th>
              <th className="px-4 py-3">Expires</th><th className="px-4 py-3">Last sign-in</th><th className="px-4 py-3">Status</th><th className="px-4 py-3" />
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {!users && !err && <tr><td className="px-4 py-4 text-slate-500" colSpan={7}>Loading…</td></tr>}
            {(users || []).map((u) => (
              <tr key={u.id} className={u.is_active ? "" : "opacity-60"}>
                <td className="px-4 py-3">
                  <p className="font-medium text-ink">{u.full_name}{u.id === me?.id && <span className="ml-2 text-xs text-slate-400">(you)</span>}</p>
                  <p className="text-xs text-slate-500">{u.email}</p>
                </td>
                <td className="px-4 py-3">{ROLES[u.role]?.label || u.role}</td>
                <td className="max-w-xs px-4 py-3 text-xs text-slate-600">{accessText(u, meta)}</td>
                <td className={`px-4 py-3 text-xs ${expiryInfo(u).cls}`}>{expiryInfo(u).text}</td>
                <td className="px-4 py-3 text-slate-600">{fmt(u.last_login_at)}</td>
                <td className="px-4 py-3">
                  <span className={`gh-chip ${u.is_active ? "gh-chip-brand" : "gh-chip-neutral"}`}>{u.is_active ? "Active" : "Deactivated"}</span>
                </td>
                <td className="px-4 py-3">
                  <div className="flex flex-wrap justify-end gap-1.5">
                    <button className="gh-btn-secondary gh-btn-sm" onClick={() => { setMode(u); setNotice(null); }}>Edit</button>
                    {u.expires_at && <button className="gh-btn-secondary gh-btn-sm" onClick={() => act(
                      () => apiClient.patch(`/auth/users/${u.id}`, { expires_at: plusYear(u.expires_at > todayISO() ? u.expires_at : null) }),
                      `${u.full_name}: access extended by 1 year.`)}>Extend 1 year</button>}
                    <button className="gh-btn-secondary gh-btn-sm" onClick={() => { setPwFor(u); setNewPw(genPassword()); setNotice(null); }}>Reset password</button>
                    {u.totp_enabled && <button className="gh-btn-secondary gh-btn-sm" onClick={() => reset2fa(u)}>Reset 2FA</button>}
                    {u.id !== me?.id && (
                      <button className={`gh-btn-sm ${u.is_active ? "gh-btn-danger" : "gh-btn-secondary"}`} onClick={() => toggleActive(u)}>
                        {u.is_active ? (u.role === "viewer" ? "Stop access" : "Deactivate") : (u.role === "viewer" ? "Restore access" : "Reactivate")}
                      </button>
                    )}
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
    </div>
  );
}
