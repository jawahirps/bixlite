import { useEffect, useMemo, useState } from "react";
import apiClient from "../api/client";
import { useAuth } from "../context/AuthContext";

// Admin reference: every scrub rule BIX Lite applies and the journal / standard / evidence it comes from.
const SEV = {
  BLOCK: "bg-red-100 text-red-800", ERROR: "bg-red-100 text-red-800", "ERROR / WARN": "bg-red-50 text-red-700",
  WARN: "bg-amber-100 text-amber-800", QUERY: "bg-sky-100 text-sky-800", ALERT: "bg-violet-100 text-violet-800", RULE: "bg-teal-100 text-teal-800", INFO: "bg-slate-100 text-slate-700",
};

function Ref({ r }) {
  if (!r) return null;
  return r.url
    ? <a href={r.url} target="_blank" rel="noreferrer" className="text-teal-700 underline decoration-dotted hover:text-teal-900">{r.title}</a>
    : <span>{r.title}</span>;
}

export default function ScrubRules() {
  const { user } = useAuth();
  const [data, setData] = useState(null);
  const [error, setError] = useState("");
  const [q, setQ] = useState("");
  const [engine, setEngine] = useState("");
  const [ref, setRef] = useState("");

  useEffect(() => {
    apiClient.get("/rule-catalog").then((r) => setData(r.data))
      .catch((e) => setError(e?.response?.data?.detail || "Could not load the rule catalogue."));
  }, []);

  const rows = useMemo(() => {
    if (!data) return [];
    const s = q.trim().toLowerCase();
    return data.rules.filter((r) =>
      (!engine || r.engine === engine) && (!ref || r.refs.includes(ref)) &&
      (!s || [r.code, r.area, r.check, r.denial_code].join(" ").toLowerCase().includes(s)));
  }, [data, q, engine, ref]);

  if (user && user.role !== "admin") return <div className="gh-card p-6 text-sm text-slate-600">This page is for admins.</div>;
  if (error) return <div className="gh-card border-red-200 bg-red-50 p-4 text-sm text-red-700">{error}</div>;
  if (!data) return <div className="gh-card p-10 text-center text-sm text-slate-500">Loading rules…</div>;

  const refs = data.references;
  const engines = [...new Set(data.rules.map((r) => r.engine))];
  const used = (k) => data.rules.filter((r) => r.refs.includes(k)).length;
  const [uae, nas, exc] = data.packs;

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold text-slate-900">Scrub rules &amp; references</h1>
        <p className="mt-1 max-w-3xl text-sm text-slate-600">
          Every check the claim scrubber and pre-submission queries apply, and the regulator standard, journal or
          payer evidence each one is derived from. {data.rules.length} rules from {Object.keys(refs).length} references.
        </p>
      </div>

      {/* references */}
      <div className="gh-card p-5">
        <p className="gh-label">References (journals &amp; standards)</p>
        <div className="mt-3 grid grid-cols-1 gap-3 md:grid-cols-2">
          {Object.entries(refs).map(([k, r]) => (
            <button key={k} type="button" onClick={() => setRef(ref === k ? "" : k)}
              className={`rounded-lg border p-3 text-left text-sm transition ${ref === k ? "border-teal-600 bg-teal-50" : "border-slate-200 hover:border-teal-400"}`}>
              <div className="flex items-start justify-between gap-2">
                <span className="font-medium text-slate-900"><Ref r={r} /></span>
                <span className="shrink-0 rounded bg-slate-100 px-1.5 py-0.5 text-xs text-slate-600">{used(k)} rules</span>
              </div>
              <div className="mt-0.5 text-xs text-slate-500">{r.publisher} · {r.kind}</div>
              <div className="mt-1 text-xs text-slate-600">{r.note}</div>
            </button>
          ))}
        </div>
        <p className="mt-2 text-xs text-slate-500">Click a reference to list only the rules derived from it.</p>
      </div>

      {/* rules */}
      <div className="gh-card p-5">
        <div className="flex flex-wrap items-end gap-3">
          <div className="min-w-0 flex-1">
            <label htmlFor="rule-search" className="gh-label">Rules</label>
            <input id="rule-search" value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search code, area, denial code…"
              className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-sm" />
          </div>
          <select id="rule-engine" value={engine} onChange={(e) => setEngine(e.target.value)} className="rounded-md border border-slate-300 px-3 py-2 text-sm">
            <option value="">All engines</option>
            {engines.map((e) => <option key={e} value={e}>{e}</option>)}
          </select>
          {ref && <button type="button" onClick={() => setRef("")} className="gh-btn-secondary gh-btn-sm">Clear: {refs[ref].title.slice(0, 32)}… ✕</button>}
        </div>
        <div className="mt-4 overflow-x-auto">
          <table className="w-full min-w-[820px] text-sm">
            <thead><tr className="border-b text-left text-xs uppercase tracking-wide text-slate-500">
              <th className="py-2 pr-3">Rule</th><th className="py-2 pr-3">Severity</th><th className="py-2 pr-3">What it checks</th>
              <th className="py-2 pr-3">Derived from</th><th className="py-2">Denial code</th>
            </tr></thead>
            <tbody>
              {rows.map((r, i) => (
                <tr key={r.code + r.engine + i} className="border-b border-slate-100 align-top">
                  <td className="py-2 pr-3"><div className="font-mono text-xs font-medium text-slate-900">{r.code}</div>
                    <div className="text-xs text-slate-500">{r.area} · {r.engine}</div></td>
                  <td className="py-2 pr-3"><span className={`rounded px-1.5 py-0.5 text-xs font-medium ${SEV[r.severity] || SEV.INFO}`}>{r.severity}</span></td>
                  <td className="py-2 pr-3 text-slate-700">{r.check}</td>
                  <td className="py-2 pr-3 text-xs">{r.refs.map((k) => <div key={k}><Ref r={refs[k]} /></div>)}</td>
                  <td className="py-2 font-mono text-xs">{r.denial_code || "—"}</td>
                </tr>
              ))}
              {!rows.length && <tr><td colSpan={5} className="py-6 text-center text-slate-500">No rules match.</td></tr>}
            </tbody>
          </table>
        </div>
      </div>

      {/* rule packs */}
      <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
        <div className="gh-card p-5 text-sm">
          <p className="gh-label">{uae.title} · {uae.id} v{uae.version}</p>
          <p className="mt-1 text-xs text-amber-700">Authority: {uae.authority}. Only the tables below are loaded; the pack is a starter until licensed tables are added.</p>
          <p className="mt-3 font-medium text-slate-800">Frequency windows</p>
          <ul className="mt-1 space-y-1 text-slate-700">
            {uae.frequency.map((f) => <li key={f.rule_id}><span className="font-mono text-xs">{f.rule_id}</span>: {f.codes.join(", ")} — days {f.min_days}–{f.window_days}, {f.action}{f.required_modifier ? `, modifier ${f.required_modifier}` : ""} <span className="text-xs text-slate-500">({f.source_ids.join(", ")})</span></li>)}
          </ul>
          <p className="mt-3 font-medium text-slate-800">Medically unlikely edits</p>
          <ul className="mt-1 space-y-1 text-slate-700">
            {uae.mue.map((m) => <li key={m.rule_id}><span className="font-mono text-xs">{m.rule_id}</span>: {m.code} max {m.max_units} per {m.per.replace("_", " ")} <span className="text-xs text-slate-500">({m.source_ids.join(", ")})</span></li>)}
          </ul>
          <p className="mt-3 text-xs text-slate-500">Mutually exclusive pairs loaded: {uae.mutually_exclusive.length}</p>
        </div>

        <div className="gh-card p-5 text-sm">
          <p className="gh-label">{nas.title} · {nas.id} v{nas.version}</p>
          <p className="mt-1 text-xs text-slate-500">{nas.scope}. Captured {nas.captured} from <a className="text-teal-700 underline" href={nas.sources[0]} target="_blank" rel="noreferrer">nas.ae</a>.</p>
          <ul className="mt-3 space-y-2 text-slate-700">
            {nas.rules.map((r) => <li key={r.rule_id}><span className="font-mono text-xs">{r.rule_id}</span> <span className={`ml-1 rounded px-1 text-xs ${SEV[r.severity] || SEV.INFO}`}>{r.severity}</span><div className="text-xs text-slate-600">{r.message}</div></li>)}
          </ul>
        </div>

        <div className="gh-card p-5 text-sm">
          <p className="gh-label">ICD-10-CM Excludes-1 and symptom pairs</p>
          <p className="mt-1 text-slate-700"><b>{Number(exc.count).toLocaleString()}</b> official Excludes-1 rules — {exc.source_note}.</p>
          <p className="mt-3 font-medium text-slate-800">Symptom-integral pairs (ICD guideline I.B.5 + payer practice)</p>
          <ul className="mt-1 space-y-1 text-xs text-slate-700">
            {data.symptom_pairs.map((p, i) => <li key={i}>Drop <span className="font-mono">{p.drop}</span> with {p.with.join(", ")} — {p.why}</li>)}
          </ul>
        </div>

        <div className="gh-card p-5 text-sm">
          <p className="gh-label">DHA dental rules — frequency limits</p>
          <ul className="mt-2 space-y-1 text-slate-700">
            {data.dental_frequency.map((d, i) => <li key={i}><span className="font-mono text-xs">{d.codes.join(", ")}</span>: {d.rule}</li>)}
            <li>Fillings: max {data.dental_max_fillings_per_day} per claim per day (except general anaesthesia).</li>
          </ul>
          <p className="mt-2 text-xs text-slate-500">Not checked (not in the claim file): age limits, once-per-tooth limits, x-ray attachments.</p>
        </div>
      </div>
    </div>
  );
}
