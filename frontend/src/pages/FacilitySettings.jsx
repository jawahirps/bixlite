import { useEffect, useState } from "react";
import apiClient from "../api/client";

const PLACEHOLDER_PREFIXES = ["user-", "cred-", "LIC-"];
// "N/A" is intentional (Riayati-only facility, DHA skipped) — not a placeholder
const isPlaceholder = (val) =>
  !val || PLACEHOLDER_PREFIXES.some((p) => val.startsWith(p));

const isRiayatiOnly = (facility) =>
  facility.riayati_username && facility.eclaimlink_username === "N/A";

function Badge({ live }) {
  return live ? (
    <span className="gh-chip gh-chip-paid">
      <span className="h-1.5 w-1.5 rounded-full bg-emerald-500" /> Live
    </span>
  ) : (
    <span className="gh-chip gh-chip-pending">
      <span className="h-1.5 w-1.5 rounded-full bg-amber-500" /> Needs setup
    </span>
  );
}

function RiayatiBadge() {
  return (
    <span className="gh-chip gh-chip-brand">
      MOH / Riayati
    </span>
  );
}

function FacilityRow({ facility, onSaved }) {
  const [editing, setEditing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);
  const [form, setForm] = useState({
    facility_name: facility.facility_name,
    license_id: facility.license_id,
    eclaimlink_username: facility.eclaimlink_username,
    credential_key: facility.credential_key,
    dhpo_provider_id: facility.dhpo_provider_id || "",
    riayati_username: facility.riayati_username || "",
    riayati_password: "",
    is_active: facility.is_active,
  });

  // Riayati-only facility: eclaimlink_username is "N/A" and riayati_username is set
  const riayatiOnly = isRiayatiOnly(facility);

  const isLive =
    facility.has_credential &&
    (riayatiOnly || (!isPlaceholder(facility.eclaimlink_username) && !isPlaceholder(facility.license_id))) &&
    facility.is_active;

  const handleChange = (e) => {
    const { name, value, type, checked } = e.target;
    setForm((f) => ({ ...f, [name]: type === "checkbox" ? checked : value }));
  };

  const handleSave = async () => {
    setSaving(true);
    setError(null);
    try {
      const res = await apiClient.put(`/facilities/${facility.id}`, form);
      onSaved(res.data);
      setEditing(false);
    } catch (err) {
      setError(err.response?.data?.detail || "Save failed.");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="gh-section">
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <h3 className="truncate font-semibold text-slate-900">{facility.facility_name}</h3>
            <Badge live={isLive} />
            {(facility.riayati_username || facility.riayati_accounts?.length > 0) && <RiayatiBadge />}
            {!facility.is_active && (
              <span className="gh-chip gh-chip-neutral">
                Inactive
              </span>
            )}
          </div>
          <p className="mt-0.5 text-xs text-slate-600">Code: {facility.facility_code}</p>
        </div>
        <button
          onClick={() => setEditing((e) => !e)}
          className="gh-btn-secondary gh-btn-sm shrink-0"
        >
          {editing ? "Cancel" : "Edit"}
        </button>
      </div>

      {!editing && (
        <div className="mt-3 grid grid-cols-1 gap-x-6 gap-y-1 text-sm sm:grid-cols-2">
          <Field label="License ID" value={facility.license_id} placeholder={isPlaceholder(facility.license_id)} />
          <Field label="eClaimLink Username" value={facility.eclaimlink_username} placeholder={isPlaceholder(facility.eclaimlink_username)} />
          <Field label="Credential Key" value={facility.credential_key} secret placeholder={isPlaceholder(facility.credential_key)} />
          <Field label="DHPO Provider ID" value={facility.dhpo_provider_id || "—"} />
          {facility.riayati_username && (
            <Field label="Riayati Username (MOH)" value={facility.riayati_username} />
          )}
        </div>
      )}

      {!editing && (facility.riayati_username || facility.riayati_accounts?.length > 0) && (
        <RiayatiAccounts facility={facility} onChanged={onSaved} />
      )}

      {editing && (
        <div className="mt-4 space-y-3">
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <Input label="Facility Name" name="facility_name" value={form.facility_name} onChange={handleChange} />
            <Input label="License ID" name="license_id" value={form.license_id} onChange={handleChange} />
            <Input label="eClaimLink Username (use N/A for Riayati-only)" name="eclaimlink_username" value={form.eclaimlink_username} onChange={handleChange} />
            <Input label="Credential Key (DHA)" name="credential_key" value={form.credential_key} onChange={handleChange} type="password" showToggle />
            <Input label="DHPO Provider ID (optional)" name="dhpo_provider_id" value={form.dhpo_provider_id} onChange={handleChange} />
            <Input label="Riayati Username (MOH license no.)" name="riayati_username" value={form.riayati_username} onChange={handleChange} />
            <Input label="Riayati Password (API key — leave blank to keep current)" name="riayati_password" value={form.riayati_password} onChange={handleChange} type="password" showToggle />
            <label className="flex items-center gap-2 text-sm text-slate-700 self-end pb-1">
              <input
                type="checkbox"
                name="is_active"
                checked={form.is_active}
                onChange={handleChange}
                className="h-4 w-4 rounded border-slate-300 text-emerald-600"
              />
              Active facility
            </label>
          </div>
          {error && <p className="text-sm text-red-600">{error}</p>}
          <div className="flex justify-end gap-2">
            <button
              onClick={() => setEditing(false)}
              className="gh-btn-secondary"
            >
              Cancel
            </button>
            <button
              onClick={handleSave}
              disabled={saving}
              className="rounded-lg bg-emerald-600 px-4 py-2 text-sm font-medium text-white hover:bg-emerald-700 disabled:opacity-50"
            >
              {saving ? "Saving…" : "Save"}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

// Extra Riayati logins for one facility — e.g. the SHA licence after a move from RHA. The nightly fetch pulls
// every active account into this facility, so RAs match their claims by claim ID whichever account sends them.
const EMPTY_ACCOUNT = { label: "", username: "", password: "", license_id: "", active_from: "" };

function RiayatiAccounts({ facility, onChanged }) {
  const [form, setForm] = useState(null);          // null = closed; {id?, ...} = adding / editing
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);
  const accounts = facility.riayati_accounts || [];

  const change = (e) => setForm((f) => ({ ...f, [e.target.name]: e.target.value }));
  const refresh = async () => onChanged((await apiClient.get(`/facilities/${facility.id}`)).data);

  const save = async () => {
    setSaving(true);
    setError(null);
    const body = { ...form, active_from: form.active_from || null };
    delete body.id;
    try {
      if (form.id) await apiClient.put(`/facilities/${facility.id}/riayati-accounts/${form.id}`, body);
      else await apiClient.post(`/facilities/${facility.id}/riayati-accounts`, body);
      await refresh();
      setForm(null);
    } catch (err) {
      setError(err.response?.data?.detail || "Save failed.");
    } finally {
      setSaving(false);
    }
  };

  const toggle = async (a) => {
    await apiClient.put(`/facilities/${facility.id}/riayati-accounts/${a.id}`, { is_active: !a.is_active });
    await refresh();
  };

  const remove = async (a) => {
    if (!window.confirm(`Remove Riayati account "${a.label}" (${a.username})? Data already fetched is kept.`)) return;
    await apiClient.delete(`/facilities/${facility.id}/riayati-accounts/${a.id}`);
    await refresh();
  };

  return (
    <div className="mt-4 border-t border-slate-100 pt-3">
      <div className="flex items-center justify-between">
        <span className="text-xs font-medium text-slate-600">Additional Riayati accounts (same facility, fetched together)</span>
        {!form && (
          <button onClick={() => setForm(EMPTY_ACCOUNT)}
            className="gh-btn-secondary gh-btn-sm">
            + Add account
          </button>
        )}
      </div>
      {accounts.length === 0 && !form && <p className="mt-1 text-xs text-slate-600">None</p>}
      {accounts.map((a) => (
        <div key={a.id} className="mt-2 flex flex-wrap items-center justify-between gap-2 rounded-lg bg-slate-50 px-3 py-2 text-sm">
          <div>
            <span className="font-medium text-slate-800">{a.label}</span>
            <span className="ml-2 text-slate-600">{a.username}</span>
            {a.license_id && <span className="ml-2 text-xs text-slate-600">Licence {a.license_id}</span>}
            {a.active_from && <span className="ml-2 text-xs text-slate-600">from {a.active_from}</span>}
            {!a.has_password && <span className="ml-2 text-xs text-amber-600">no password</span>}
            {!a.is_active && <span className="ml-2 gh-chip gh-chip-neutral">Inactive</span>}
          </div>
          <div className="flex gap-1">
            <button onClick={() => setForm({ ...EMPTY_ACCOUNT, ...a, password: "", license_id: a.license_id || "", active_from: a.active_from || "" })}
              className="rounded px-2 py-1 text-xs text-slate-600 hover:bg-white">Edit</button>
            <button onClick={() => toggle(a)} className="rounded px-2 py-1 text-xs text-slate-600 hover:bg-white">
              {a.is_active ? "Deactivate" : "Activate"}
            </button>
            <button onClick={() => remove(a)} className="rounded px-2 py-1 text-xs text-red-600 hover:bg-white">Remove</button>
          </div>
        </div>
      ))}
      {form && (
        <div className="mt-3 space-y-3 rounded-lg border border-slate-200 p-3">
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <Input label="Label (e.g. SHA)" name="label" value={form.label} onChange={change} />
            <Input label="Riayati Username" name="username" value={form.username} onChange={change} />
            <Input label={form.id ? "Password (API key — leave blank to keep current)" : "Password (API key)"}
              name="password" value={form.password} onChange={change} type="password" showToggle />
            <Input label="Licence (e.g. HF-YYYY-NNNNNN)" name="license_id" value={form.license_id} onChange={change} />
            <div>
              <label className="mb-1 block text-xs font-medium text-slate-500">Licence used from (first visit date)</label>
              <input type="date" name="active_from" value={form.active_from} onChange={change}
                className="gh-input w-full" />
            </div>
          </div>
          {error && <p className="text-sm text-red-600">{error}</p>}
          <div className="flex justify-end gap-2">
            <button onClick={() => { setForm(null); setError(null); }}
              className="gh-btn-secondary">Cancel</button>
            <button onClick={save} disabled={saving}
              className="rounded-lg bg-emerald-600 px-4 py-2 text-sm font-medium text-white hover:bg-emerald-700 disabled:opacity-50">
              {saving ? "Saving…" : "Save account"}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

function Field({ label, value, placeholder, secret }) {
  const display = secret && !placeholder ? "••••••••••••" : value;
  return (
    <div>
      <span className="text-xs font-medium text-slate-600">{label}</span>
      <p className={`mt-0.5 truncate text-sm ${placeholder ? "italic text-amber-600" : "text-slate-800"}`}>
        {display}
      </p>
    </div>
  );
}

function Input({ label, name, value, onChange, type = "text", showToggle }) {
  const [show, setShow] = useState(false);
  return (
    <div>
      <label className="mb-1 block text-xs font-medium text-slate-500">{label}</label>
      <div className="relative">
        <input
          type={showToggle && !show ? "password" : "text"}
          name={name}
          value={value}
          onChange={onChange}
          className="gh-input w-full"
        />
        {showToggle && (
          <button
            type="button"
            onClick={() => setShow((s) => !s)}
            className="absolute right-2 top-1/2 -translate-y-1/2 text-xs text-slate-600 hover:text-slate-600"
          >
            {show ? "Hide" : "Show"}
          </button>
        )}
      </div>
    </div>
  );
}

export default function FacilitySettings() {
  const [facilities, setFacilities] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  useEffect(() => {
    apiClient
      .get("/facilities")
      .then((res) => setFacilities(res.data))
      .catch((err) => setError(err.response?.data?.detail || "Failed to load facilities."))
      .finally(() => setLoading(false));
  }, []);

  const handleSaved = (updated) =>
    setFacilities((prev) => prev.map((f) => (f.id === updated.id ? updated : f)));

  const liveCount = facilities.filter((f) => {
    const riOnly = isRiayatiOnly(f);
    return (
      f.has_credential &&
      (riOnly || (!isPlaceholder(f.eclaimlink_username) && !isPlaceholder(f.license_id))) &&
      f.is_active
    );
  }).length;

  return (
    <div className="space-y-6">
      <div className="flex items-start justify-between">
        <div>
          <p className="gh-eyebrow">05 — Setup</p>
          <h1 className="gh-h1">Facility Settings</h1>
          <p className="text-sm text-slate-500">
            Manage eClaimLink credentials for live data sync. Fields shown in amber need real values.
          </p>
        </div>
        {!loading && (
          <div className="gh-card px-4 py-3 text-center">
            <p className="text-2xl font-bold text-slate-900">{liveCount}/{facilities.length}</p>
            <p className="text-xs text-slate-500">Facilities live</p>
          </div>
        )}
      </div>

      {loading && <p className="text-sm text-slate-500">Loading facilities…</p>}
      {error && <p className="text-sm text-red-600">{error}</p>}

      <div className="space-y-4">
        {facilities.map((f) => (
          <FacilityRow key={f.id} facility={f} onSaved={handleSaved} />
        ))}
      </div>
    </div>
  );
}
