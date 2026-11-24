import { useEffect, useMemo, useState } from "react";
import apiClient from "../../api/client";
import { useAuth } from "../../context/AuthContext";

const TYPES = ["internal complaint", "correction", "reconciliation", "legacy"];
const TEST = "TEST";
const PROD = "PRODUCTION";
const aed = (v) => (v == null || v === "" ? "—" : Number(v).toLocaleString("en-AE", { minimumFractionDigits: 2, maximumFractionDigits: 2 }));
const errMsg = (e) => e?.response?.data?.detail || e?.message || "Request failed";

function Section({ title, children, right }) {
  return (
    <section className="rounded-xl border border-hairline bg-white p-4">
      <div className="mb-2 flex items-center justify-between">
        <h3 className="font-display text-[13px] font-semibold uppercase tracking-wide text-slate-500">{title}</h3>
        {right}
      </div>
      {children}
    </section>
  );
}

function Fact({ label, value }) {
  return (
    <div>
      <div className="text-[11px] text-slate-400">{label}</div>
      <div className="truncate text-sm text-ink">{value || "—"}</div>
    </div>
  );
}

export default function PrepareResubmission({ claim, onClose }) {
  const { user } = useAuth();
  const isAdmin = user?.role === "admin";
  const [draft, setDraft] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [rtype, setRtype] = useState("internal complaint");
  const [just, setJust] = useState({});
  const [comment, setComment] = useState("");
  const [commentTouched, setCommentTouched] = useState(false);
  const [actEdits, setActEdits] = useState({});
  const [ticks, setTicks] = useState({});
  const [attachment, setAttachment] = useState(null);
  const [gen, setGen] = useState(null);
  const [tab, setTab] = useState("xml");
  const [busy, setBusy] = useState("");
  const [result, setResult] = useState(null);
  const [history, setHistory] = useState([]);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [typed, setTyped] = useState("");

  const params = { facility_id: claim.facility_id, claim_id: claim.claim_id };
  const loadHistory = () => apiClient.get("/resub-prepare/history", { params }).then((r) => setHistory(r.data)).catch(() => {});

  useEffect(() => {
    setLoading(true);
    apiClient
      .get("/resub-prepare/draft", { params })
      .then(({ data }) => {
        setDraft(data);
        setRtype(data.suggestion.resubmission_type);
        setJust(Object.fromEntries(data.denied.map((d) => [d.activity_id, d.suggestion.justification])));
        setComment(data.suggestion.comment);
      })
      .catch((e) => setError(errMsg(e)))
      .finally(() => setLoading(false));
    loadHistory();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [claim.facility_id, claim.claim_id]);

  // combined comment follows the per-line justifications until edited by hand
  const autoComment = useMemo(
    () => (draft ? draft.denied.map((d) => `Act ${d.activity_id} ${d.code} (${d.facts.denial_code}): ${just[d.activity_id] || ""}`).join(" | ") : ""),
    [draft, just]
  );
  useEffect(() => {
    if (!commentTouched) setComment(autoComment);
  }, [autoComment, commentTouched]);

  const isRiayati = draft?.format === "riayati_json";
  const reqs = draft ? draft.denied.flatMap((d) => d.requirements.map((r) => ({ ...r, id: `${d.activity_id}:${r.key}`, line: d }))) : [];

  const onFile = (f) => {
    if (!f) return setAttachment(null);
    if (f.type !== "application/pdf") return setError("Attachment must be a PDF");
    if (f.size > 5 * 1024 * 1024) return setError("Attachment must be 5 MB or less");
    const rd = new FileReader();
    rd.onload = () => setAttachment({ name: f.name, b64: String(rd.result).split(",")[1] });
    rd.readAsDataURL(f);
  };

  const generate = async (disposition = TEST) => {
    setBusy("xml"); setError(""); setResult(null);
    try {
      const edits = { resubmission_type: rtype, comment, activities: actEdits };
      if (attachment) edits.attachment_b64 = attachment.b64;
      const { data } = await apiClient.post("/resub-prepare/xml", { ...params, disposition, edits });
      setGen({ ...data, disposition });
      loadHistory();
      return data;
    } catch (e) {
      setError(errMsg(e));
      return null;
    } finally {
      setBusy("");
    }
  };

  const download = async () => {
    if (!gen?.id) return;
    const r = await apiClient.get("/resub-prepare/xml/download", { params: { facility_id: claim.facility_id, id: gen.id }, responseType: "blob" });
    const url = URL.createObjectURL(r.data);
    const a = document.createElement("a");
    a.href = url; a.download = gen.filename || `RESUB_${claim.claim_id}.xml`; a.click();
    URL.revokeObjectURL(url);
  };

  const upload = async (disposition) => {
    // always regenerate so the file's DispositionFlag matches the upload
    const g = await generate(disposition);
    if (!g || g.errors?.length) return;
    setBusy("upload");
    try {
      const body = { facility_id: claim.facility_id, id: g.id, disposition };
      if (disposition === PROD) Object.assign(body, { confirm: true, typed_claim_id: typed });
      const { data } = await apiClient.post("/resub-prepare/upload", body);
      setResult(data);
      loadHistory();
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setBusy(""); setConfirmOpen(false); setTyped("");
    }
  };

  const f = draft?.facts;
  return (
    <div className="fixed inset-0 z-50 flex justify-end bg-ink/30" onClick={onClose}>
      <aside className="flex h-full w-full max-w-3xl flex-col bg-sage shadow-2xl" onClick={(e) => e.stopPropagation()}>
        <header className="flex items-center justify-between border-b border-hairline bg-white px-5 py-3">
          <div>
            <div className="font-display text-base font-semibold text-ink">Prepare resubmission</div>
            <div className="text-xs text-slate-500">{claim.claim_id}</div>
          </div>
          <button onClick={onClose} className="rounded-lg px-2 py-1 text-slate-500 hover:bg-sage" aria-label="Close">✕</button>
        </header>

        <div className="flex-1 space-y-3 overflow-y-auto p-4">
          {loading && <div className="text-sm text-slate-500">Loading claim…</div>}
          {error && <div className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-denied">{error}</div>}
          {draft?.sn && (
            <div className={`rounded-lg border px-3 py-2 text-xs ${draft.sn.limit_reached || draft.sn.expired || draft.sn.window_closed ? "border-red-200 bg-red-50 text-denied" : "border-slate-200 bg-slate-50 text-slate-700"}`}>
              <b>SN 03/2019:</b> resubmissions used {draft.sn.resubmissions_used} of {draft.sn.max_resubmissions}
              {draft.sn.resubmit_by && <> · resubmit by {draft.sn.resubmit_by} (21 days from RA)</>}
              {draft.sn.valid_until && <> · claim valid until {draft.sn.valid_until}</>}
              {draft.sn.resubmissions_used >= 1 && <> · reconciliation agreement: {draft.sn.recon_agreed ? "confirmed in worklist" : "not marked in worklist"}</>}
            </div>
          )}
          {draft?.warnings?.map((w) => (
            <div key={w} className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-pending">{w}</div>
          ))}

          {f && (
            <>
              <Section title="Facts" right={<span className="text-[11px] text-slate-400">source: {f.source === "raw_xml" ? "original file" : "stored tables"}</span>}>
                <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
                  <Fact label="Facility" value={f.facility_name} />
                  <Fact label="Payer / Receiver" value={`${f.payer_name || f.payer_id} · ${f.receiver_id}`} />
                  <Fact label="Member" value={f.member_id_masked} />
                  <Fact label="Encounter" value={f.encounter_start} />
                  <Fact label="Gross / PS / Net" value={`${aed(f.gross)} / ${aed(f.patient_share)} / ${aed(f.net)}`} />
                  <Fact label="Latest RA" value={f.ra ? `${f.ra.remittance_date} · paid ${aed(f.ra.paid)}` : "none"} />
                  <Fact label="Denied" value={f.ra ? aed(f.ra.denied) : "—"} />
                  <Fact label="Resubmissions so far" value={String(f.resubmission_count ?? 0)} />
                </div>
                <div className="mt-3 text-xs text-slate-500">
                  Dx: {f.diagnoses.map((d) => `${d.code}${d.type === "Principal" ? " (P)" : ""}`).join(", ")}
                </div>
              </Section>

              <Section title={`Denied lines (${draft.denied.length})`}>
                <div className="space-y-3">
                  {draft.denied.map((d) => (
                    <div key={d.activity_id} className="rounded-lg border border-sage-line p-3">
                      <div className="flex flex-wrap items-baseline justify-between gap-2">
                        <div className="text-sm font-semibold text-ink">
                          {d.code} <span className="font-normal text-slate-500">{d.description}</span>
                        </div>
                        <div className="text-xs text-slate-500">Net {aed(d.facts.net)} · paid {aed(d.facts.paid)}</div>
                      </div>
                      <div className="mt-1 text-xs">
                        <span className="rounded bg-red-50 px-1.5 py-0.5 font-semibold text-denied">{d.facts.denial_code}</span>{" "}
                        <span className="text-slate-600">{d.facts.denial_description}</span>
                      </div>
                      <div className="mt-2 grid grid-cols-3 gap-2">
                        {[["code", "Code", d.code], ["quantity", "Qty", ""], ["prior_authorization_id", "Approval no.", d.facts.prior_auth]].map(([k, l, ph]) => (
                          <label key={k} className="text-[11px] text-slate-400">
                            {l}
                            <input
                              className="mt-0.5 w-full rounded-md border border-sage-line px-2 py-1 text-sm text-ink"
                              placeholder={ph || "unchanged"}
                              value={actEdits[d.activity_id]?.[k] ?? ""}
                              onChange={(e) => setActEdits((s) => ({ ...s, [d.activity_id]: { ...s[d.activity_id], [k]: e.target.value || undefined } }))}
                            />
                          </label>
                        ))}
                      </div>
                      <div className="mt-2 text-[11px] text-slate-400">Suggested justification · {d.suggestion.family} → {d.suggestion.resubmission_type}</div>
                      <textarea
                        rows={3}
                        className="mt-0.5 w-full rounded-md border border-sage-line px-2 py-1 text-sm text-ink"
                        value={just[d.activity_id] || ""}
                        onChange={(e) => setJust((s) => ({ ...s, [d.activity_id]: e.target.value }))}
                      />
                    </div>
                  ))}
                  {!draft.denied.length && <div className="text-sm text-slate-500">No denied lines on the latest RA.</div>}
                </div>
              </Section>

              <Section title="Requirements">
                <ul className="space-y-2">
                  {reqs.map((r) => (
                    <li key={r.id} className="flex gap-2 text-sm">
                      <input type="checkbox" className="mt-1 accent-ghaf-600" checked={!!ticks[r.id]} onChange={(e) => setTicks((s) => ({ ...s, [r.id]: e.target.checked }))} />
                      <div>
                        <div className={ticks[r.id] ? "text-slate-400 line-through" : "text-ink"}>
                          {r.label} <span className="text-xs text-slate-400">· {r.line.code}</span>
                        </div>
                        <div className="text-xs text-slate-500">{r.reason}</div>
                        {r.evidence.map((e) => (
                          <div key={e} className="text-xs text-pending">{e}</div>
                        ))}
                      </div>
                    </li>
                  ))}
                  {!reqs.length && <li className="text-sm text-slate-500">None.</li>}
                </ul>
              </Section>

              <Section title="Resubmission">
                <div className="grid gap-3 sm:grid-cols-3">
                  <label className="text-[11px] text-slate-400">
                    Type
                    <select className="mt-0.5 w-full rounded-md border border-sage-line px-2 py-1.5 text-sm text-ink" value={rtype} onChange={(e) => setRtype(e.target.value)}>
                      {TYPES.map((t) => <option key={t}>{t}</option>)}
                    </select>
                  </label>
                  <label className="text-[11px] text-slate-400 sm:col-span-2">
                    Attachment (PDF, optional)
                    <input type="file" accept="application/pdf" className="mt-0.5 block w-full text-sm" onChange={(e) => onFile(e.target.files?.[0])} />
                    {attachment && <span className="text-xs text-slate-500">{attachment.name}</span>}
                  </label>
                </div>
                <label className="mt-3 block text-[11px] text-slate-400">
                  Comment ({comment.length}/2000){commentTouched && (
                    <button className="ml-2 text-ghaf-600 underline" onClick={() => setCommentTouched(false)}>reset from lines</button>
                  )}
                  <textarea rows={4} className="mt-0.5 w-full rounded-md border border-sage-line px-2 py-1 text-sm text-ink" value={comment}
                    onChange={(e) => { setComment(e.target.value); setCommentTouched(true); }} />
                </label>
              </Section>

              {gen && (
                <Section title={`Generated · ${gen.disposition}`} right={
                  <div className="flex gap-1 text-xs">
                    {["xml", "json"].map((t) => (
                      <button key={t} onClick={() => setTab(t)} className={`rounded px-2 py-0.5 ${tab === t ? "bg-ghaf-600 text-white" : "text-slate-500 hover:bg-sage"}`}>{t.toUpperCase()}</button>
                    ))}
                    <button onClick={download} className="rounded px-2 py-0.5 text-ghaf-700 hover:bg-sage">Download</button>
                  </div>
                }>
                  {gen.errors?.length ? (
                    <ul className="mb-2 list-disc pl-5 text-xs text-denied">{gen.errors.map((e) => <li key={e}>{e}</li>)}</ul>
                  ) : (
                    <div className="mb-2 text-xs text-paid">Structure checks passed.</div>
                  )}
                  {gen.warnings?.map((w) => <div key={w} className="mb-1 text-xs text-pending">{w}</div>)}
                  <pre className="max-h-72 overflow-auto rounded-lg bg-ink p-3 text-[11px] leading-relaxed text-sage">
                    {tab === "xml" ? gen.xml : JSON.stringify(gen.json, null, 2)}
                  </pre>
                </Section>
              )}

              {result && (
                <div className={`rounded-lg border px-3 py-2 text-sm ${result.status.startsWith("uploaded") ? "border-emerald-200 bg-emerald-50 text-paid" : "border-red-200 bg-red-50 text-denied"}`}>
                  DHPO {result.disposition}: {result.status} (code {result.dhpo_result ?? "—"}) {result.message}
                </div>
              )}

              <Section title="History">
                <ul className="divide-y divide-sage-line text-xs">
                  {history.map((h) => (
                    <li key={h.id} className="flex justify-between py-1.5">
                      <span className="text-ink">#{h.id} · {h.disposition} · <b>{h.status}</b>{h.dhpo_result != null && ` (${h.dhpo_result})`}</span>
                      <span className="text-slate-400">{h.by_name || ""} {h.created_at?.slice(0, 16).replace("T", " ")}</span>
                    </li>
                  ))}
                  {!history.length && <li className="py-1.5 text-slate-400">Nothing generated yet.</li>}
                </ul>
              </Section>
            </>
          )}
        </div>

        {f && (
          <footer className="flex flex-wrap items-center gap-2 border-t border-hairline bg-white px-4 py-3">
            <button disabled={!!busy} onClick={() => generate(TEST)} className="rounded-lg bg-ghaf-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-ghaf-700 disabled:opacity-50">
              {busy === "xml" ? "Generating…" : isRiayati ? "Generate JSON" : "Generate XML"}
            </button>
            {isRiayati && <span className="text-xs text-slate-500">Riayati claim — download the JSON; upload is not available.</span>}
            {!isRiayati && (
              <button disabled={!!busy} onClick={() => upload(TEST)} className="rounded-lg border border-ghaf-600 px-3 py-1.5 text-sm font-medium text-ghaf-700 hover:bg-ghaf-50 disabled:opacity-50">
                {busy === "upload" ? "Uploading…" : "Upload to DHPO — TEST"}
              </button>
            )}
            {!isRiayati && isAdmin && (
              <button disabled={!!busy} onClick={() => setConfirmOpen(true)} className="ml-auto rounded-lg bg-denied px-3 py-1.5 text-sm font-semibold text-white hover:bg-red-700 disabled:opacity-50">
                Upload to DHPO — PRODUCTION
              </button>
            )}
          </footer>
        )}

        {confirmOpen && (
          <div className="fixed inset-0 z-[60] flex items-center justify-center bg-ink/50 p-4" onClick={() => setConfirmOpen(false)}>
            <div className="w-full max-w-md rounded-xl bg-white p-5" onClick={(e) => e.stopPropagation()}>
              <div className="font-display text-base font-semibold text-denied">Send to DHPO PRODUCTION</div>
              <p className="mt-2 text-sm text-slate-600">
                This sends the resubmission to the payer ({f?.receiver_id}). It cannot be recalled. Type the claim ID to confirm.
              </p>
              <div className="mt-2 rounded bg-sage px-2 py-1 font-mono text-xs text-ink">{claim.claim_id}</div>
              <input autoFocus className="mt-3 w-full rounded-md border border-sage-line px-2 py-1.5 font-mono text-sm" value={typed} onChange={(e) => setTyped(e.target.value)} placeholder="Claim ID" />
              <div className="mt-4 flex justify-end gap-2">
                <button className="rounded-lg px-3 py-1.5 text-sm text-slate-600 hover:bg-sage" onClick={() => setConfirmOpen(false)}>Cancel</button>
                <button disabled={typed.trim() !== claim.claim_id || !!busy} onClick={() => upload(PROD)}
                  className="rounded-lg bg-denied px-3 py-1.5 text-sm font-semibold text-white disabled:opacity-40">
                  {busy ? "Sending…" : "Upload PRODUCTION"}
                </button>
              </div>
            </div>
          </div>
        )}
      </aside>
    </div>
  );
}
