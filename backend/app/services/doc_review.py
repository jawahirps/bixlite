"""
doc_review.py — Documentation review: does the case sheet support each coded Dx (dx_support), per facility and month.

Findings are stored per run and routed by tier:
  A  claim query before submission (also raised by the Claim scrubber on ERP uploads)
  B  monthly doctor feedback
  C  Ghaf internal review
Case-sheet text is PHI: it is read from the stored submission files and kept in this database only.
"""
from __future__ import annotations

import html
import io
import json
import pickle
import re
import threading
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

from sqlalchemy import BigInteger, Date, DateTime, Integer, Numeric, String, Text, func, text
from sqlalchemy.orm import Mapped, Session, mapped_column

from ..database import SessionLocal
from ..models import Base
from .claim_scrubber import dx_support as ds

CACHE = Path.home() / "ghaf_reports" / "dx-support" / "learned"
HISTORY_MONTHS = 8
_lock = threading.Lock()


class DocReviewRun(Base):
    __tablename__ = "doc_review_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    facility_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    month: Mapped[str] = mapped_column(String(7), nullable=False)
    user_id: Mapped[Optional[int]] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="Queued")
    progress: Mapped[Optional[str]] = mapped_column(Text)
    error: Mapped[Optional[str]] = mapped_column(Text)
    claims_checked: Mapped[int] = mapped_column(Integer, default=0)
    claims_no_note: Mapped[int] = mapped_column(Integer, default=0)
    claims_flagged: Mapped[int] = mapped_column(Integer, default=0)
    checked_by_doctor: Mapped[Optional[str]] = mapped_column(Text)   # JSON {clinician_id: consultations checked}
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class DocFinding(Base):
    __tablename__ = "doc_findings"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    facility_id: Mapped[int] = mapped_column(Integer, nullable=False)
    month: Mapped[str] = mapped_column(String(7), nullable=False)
    claim_id: Mapped[str] = mapped_column(String(100), nullable=False)
    encounter_date: Mapped[Optional[date]] = mapped_column(Date)
    payer_id: Mapped[Optional[str]] = mapped_column(String(30))
    clinician_id: Mapped[Optional[str]] = mapped_column(String(40))
    dx_code: Mapped[str] = mapped_column(String(20), nullable=False)
    dx_description: Mapped[Optional[str]] = mapped_column(Text)
    tier: Mapped[str] = mapped_column(String(1), nullable=False)
    category: Mapped[str] = mapped_column(String(60), nullable=False)
    reason: Mapped[Optional[str]] = mapped_column(Text)
    query_text: Mapped[Optional[str]] = mapped_column(Text)
    suggestion: Mapped[Optional[str]] = mapped_column(Text)
    not_coded: Mapped[Optional[str]] = mapped_column(Text)
    services_at_risk: Mapped[Optional[str]] = mapped_column(Text)
    amount_at_risk: Mapped[Optional[float]] = mapped_column(Numeric(12, 2))
    case_sheet: Mapped[Optional[str]] = mapped_column(Text)
    all_dx: Mapped[Optional[str]] = mapped_column(Text)
    review_status: Mapped[Optional[str]] = mapped_column(String(20))   # Agree / Not needed
    review_remark: Mapped[Optional[str]] = mapped_column(Text)
    reviewed_by: Mapped[Optional[int]] = mapped_column(Integer)
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


def ensure_tables(engine) -> None:
    Base.metadata.create_all(engine, tables=[DocReviewRun.__table__, DocFinding.__table__])
    with engine.begin() as c:
        c.execute(text("CREATE INDEX IF NOT EXISTS idx_doc_findings_fac_month ON doc_findings (facility_id, month)"))
        c.execute(text("ALTER TABLE doc_review_runs ADD COLUMN IF NOT EXISTS checked_by_doctor TEXT"))
        c.execute(text("ALTER TABLE scrub_claims ADD COLUMN IF NOT EXISTS case_sheet TEXT"))
        c.execute(text("ALTER TABLE scrub_claims ADD COLUMN IF NOT EXISTS obs_codes TEXT"))


# ── Loading claims from the stored submission files ───────────────────────────
def _tag(s: str, t: str) -> Optional[str]:
    m = re.search(rf"<{t}>([^<]*)</{t}>", s)
    return m.group(1) if m else None


def _parse_dt(v: Optional[str]) -> Optional[date]:
    try:
        return datetime.strptime((v or "").strip()[:10], "%d/%m/%Y").date()
    except ValueError:
        return None


def _claims_from_xml(x: str):
    for cl in re.findall(r"<Claim>(.*?)</Claim>", x or "", re.S):
        enc = re.search(r"<Encounter>.*?<Start>([^<]+)</Start>", cl, re.S)
        acts, obs, note, clin = [], set(), [], None
        for a in re.findall(r"<Activity>(.*?)</Activity>", cl, re.S):
            code = _tag(a, "Code")
            acts.append((code, _tag(a, "Net")))
            clin = clin or _tag(a, "Clinician")
            for o in re.findall(r"<Observation>(.*?)</Observation>", a, re.S):
                oc = _tag(o, "Code")
                obs.add(oc)
                if oc == "Presenting-Complaint":
                    note.append(html.unescape(_tag(o, "Value") or ""))
                    clin = _tag(a, "Clinician") or clin
        yield {"claim_id": _tag(cl, "ID"), "date": _parse_dt(enc.group(1) if enc else None), "payer": _tag(cl, "PayerID"),
               "dx": [(c, None) for c in re.findall(r"<Diagnosis>.*?<Code>([^<]+)</Code>.*?</Diagnosis>", cl, re.S)],
               "acts": acts, "obs": obs, "note": "\n".join(note) or None, "clin": clin}


def _claims_from_json(x: str):
    try:
        ent = (json.loads(x) or {}).get("Entity") or {}
    except ValueError:
        return
    for cl in ent.get("Claim") or []:
        acts, obs, note, clin = [], set(), [], None
        for a in cl.get("Activity") or []:
            acts.append((str(a.get("Code") or ""), a.get("Net")))
            clin = clin or a.get("Clinician")
            for o in a.get("Observation") or []:
                oc = o.get("Code")
                obs.add(oc)
                if oc in ("Presenting-Complaint", "Description") and o.get("Type") == "Text":
                    note.append(str(o.get("Value") or ""))
        yield {"claim_id": cl.get("ID"), "date": _parse_dt((cl.get("Encounter") or {}).get("Start")), "payer": cl.get("PayerID"),
               "dx": [(d.get("Code"), None) for d in cl.get("Diagnosis") or [] if d.get("Code")],
               "acts": acts, "obs": obs, "note": "\n".join(note) or None, "clin": clin}


def load_claims(db: Session, facility_id: int, d0: date, d1: date) -> Dict[str, dict]:
    """Latest stored version of every claim with encounter in [d0, d1)."""
    out: Dict[str, dict] = {}
    q = text("""SELECT xml_content FROM raw_xml_store WHERE facility_id = :f AND xml_type = 'ClaimSubmission'
                AND submission_date >= :a AND submission_date < :b ORDER BY id""")
    for (x,) in db.execute(q, {"f": facility_id, "a": d0, "b": d1 + timedelta(days=120)}).yield_per(200):
        gen = _claims_from_json(x) if (x or "").lstrip().startswith("{") else _claims_from_xml(x)
        for c in gen:
            if c["claim_id"] and c["date"] and d0 <= c["date"] < d1:
                out[c["claim_id"]] = c
    return out


def model(db: Session, facility_id: int, before: date):
    """Learned words and preferred codes from the facility's previous months (cached for 30 days)."""
    CACHE.mkdir(parents=True, exist_ok=True)
    p = CACHE / f"facility_{facility_id}.pkl"
    if p.exists() and datetime.now().timestamp() - p.stat().st_mtime < 30 * 86400:
        return pickle.loads(p.read_bytes())
    start = (before.replace(day=1) - timedelta(days=31 * HISTORY_MONTHS)).replace(day=1)
    hist = load_claims(db, facility_id, start, before)
    learned = ds.Learned().fit((c["note"], [d for d, _ in c["dx"]]) for c in hist.values() if c["note"])
    pref = ds.Preferred(d for c in hist.values() for d, _ in c["dx"])
    p.write_bytes(pickle.dumps((learned, pref)))
    return learned, pref


def cached_model(facility_id: int):
    p = CACHE / f"facility_{facility_id}.pkl"
    return pickle.loads(p.read_bytes()) if p.exists() else (None, ds.Preferred([]))


# ── A run ─────────────────────────────────────────────────────────────────────
def run(run_id: int) -> None:
    with _lock:
        db = SessionLocal()
        r = db.get(DocReviewRun, run_id)
        try:
            y, m = map(int, r.month.split("-"))
            d0, d1 = date(y, m, 1), date(y + (m == 12), m % 12 + 1, 1)
            r.status, r.progress = "Running", "Learning from previous months"; db.commit()
            learned, pref = model(db, r.facility_id, d0)
            r.progress = "Reading the month's claims"; db.commit()
            claims = load_claims(db, r.facility_id, d0, d1)
            db.execute(text("DELETE FROM doc_findings WHERE facility_id = :f AND month = :m"), {"f": r.facility_id, "m": r.month})
            rows, checked, flagged, per_doc = [], 0, 0, Counter()
            for c in claims.values():
                if not c["note"] and not any(ds.CONSULT.match(a or "") for a, _ in c["acts"]):
                    continue                                    # lab / pharmacy-only claims have no case sheet
                checked += 1
                per_doc[c["clin"] or "-"] += 1
                found = ds.evaluate(c["dx"], c["note"], c["obs"], c["acts"], learned, pref)
                flagged += bool(found)
                for f in found:
                    rows.append(DocFinding(run_id=r.id, facility_id=r.facility_id, month=r.month, claim_id=c["claim_id"],
                                           encounter_date=c["date"], payer_id=c["payer"], clinician_id=c["clin"],
                                           dx_code=f["code"], dx_description=f["description"], tier=f["tier"],
                                           category=f["category"], reason=f["reason"], query_text=f["query"],
                                           suggestion=f["suggestion"], not_coded=f["not_coded"],
                                           services_at_risk=f["services"], amount_at_risk=f["amount_at_risk"],
                                           case_sheet=(c["note"] or "").replace("\r", ""),
                                           all_dx="\n".join(f"{d} {ds.describe(d)}" for d, _ in c["dx"])))
            db.add_all(rows)
            r.claims_checked, r.claims_flagged, r.checked_by_doctor = checked, flagged, json.dumps(per_doc)
            r.claims_no_note = sum(1 for c in claims.values() if not c["note"] and any(ds.CONSULT.match(a or "") for a, _ in c["acts"]))
            r.status, r.progress, r.completed_at = "Completed", f"{len(rows)} findings on {flagged} claims", datetime.now(timezone.utc)
            db.commit()
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            r = db.get(DocReviewRun, run_id)
            r.status, r.error = "Failed", str(exc)[:500]
            db.commit()
        finally:
            db.close()


def start(run_id: int) -> None:
    threading.Thread(target=run, args=(run_id,), daemon=True).start()


# ── Dashboard figures ─────────────────────────────────────────────────────────
def clinician_names(db: Session, facility_id: int) -> Dict[str, str]:
    return dict(db.execute(text("""SELECT clinician_id, max(clinician_name) FROM claims
                                   WHERE facility_id = :f AND clinician_id IS NOT NULL GROUP BY 1"""), {"f": facility_id}).all())


def dashboard(db: Session, facility_id: int, month: str) -> dict:
    run = (db.query(DocReviewRun).filter_by(facility_id=facility_id, month=month, status="Completed")
           .order_by(DocReviewRun.id.desc()).first())
    if not run:
        return {"run": None}
    P = {"f": facility_id, "m": month}
    tiers = {t: {"findings": n, "claims": c, "amount": float(a or 0)} for t, n, c, a in db.execute(text(
        """SELECT tier, count(*), count(DISTINCT claim_id), sum(amount_at_risk) FROM doc_findings
           WHERE facility_id = :f AND month = :m GROUP BY 1"""), P)}
    cats = [{"category": k, "tier": t, "findings": n} for k, t, n in db.execute(text(
        "SELECT category, min(tier), count(*) FROM doc_findings WHERE facility_id = :f AND month = :m GROUP BY 1 ORDER BY 1"), P)]
    names = clinician_names(db, facility_id)
    per_doc = db.execute(text("""SELECT coalesce(clinician_id, '-'), count(DISTINCT claim_id) claims, count(*) findings,
                                        count(*) FILTER (WHERE tier = 'A') a, count(*) FILTER (WHERE tier = 'B') b,
                                        count(*) FILTER (WHERE tier = 'C') c
                                 FROM doc_findings WHERE facility_id = :f AND month = :m GROUP BY 1"""), P).all()
    checked = json.loads(run.checked_by_doctor or "{}")
    docs = []
    for cid, claims, n, a, b, c in per_doc:
        tot = checked.get(cid) or claims
        top = db.execute(text("""SELECT dx_code, count(*) FROM doc_findings WHERE facility_id = :f AND month = :m
                                 AND coalesce(clinician_id,'-') = :c GROUP BY 1 ORDER BY 2 DESC LIMIT 3"""), {**P, "c": cid}).all()
        docs.append({"clinician_id": cid, "name": names.get(cid, ""), "checked": tot, "claims_flagged": claims,
                     "rate": round(claims / tot, 3) if tot else None, "findings": n, "a": a, "b": b, "c": c,
                     "top": [f"{d} {ds.describe(d)} ({k})" for d, k in top]})
    docs.sort(key=lambda d: -(d["rate"] or 0))
    trend = [{"month": m, "checked": ck, "flagged": fl, "rate": round(fl / ck, 3) if ck else None,
              "tier_a": db.execute(text("SELECT count(*) FROM doc_findings WHERE facility_id = :f AND month = :m AND tier = 'A'"),
                                   {"f": facility_id, "m": m}).scalar()}
             for m, ck, fl in db.execute(text("""SELECT DISTINCT ON (month) month, claims_checked, claims_flagged FROM doc_review_runs
                                                WHERE facility_id = :f AND status = 'Completed' ORDER BY month DESC, id DESC LIMIT 12"""),
                                           {"f": facility_id})]
    return {"run": {"id": run.id, "checked": run.claims_checked, "flagged": run.claims_flagged, "no_note": run.claims_no_note,
                    "completed_at": run.completed_at},
            "tiers": tiers, "categories": cats, "doctors": docs, "trend": sorted(trend, key=lambda t: t["month"])}


# ── Downloads ─────────────────────────────────────────────────────────────────
HEAD = ["Tier", "Claim ID", "Encounter date", "Payer", "Clinician", "Flagged Dx", "Dx description", "Category", "Detail",
        "Ghaf query", "Suggested action", "Supported in case sheet, not coded", "Services at risk", "Amount at risk",
        "Chief complaint / case sheet", "All Dx on claim", "Review", "Reviewer remarks"]


def findings_workbook(db: Session, facility_id: int, month: str, tier: Optional[str] = None) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    names = clinician_names(db, facility_id)
    q = db.query(DocFinding).filter_by(facility_id=facility_id, month=month)
    if tier:
        q = q.filter(DocFinding.tier == tier)
    wb = Workbook(); ws = wb.active; ws.title = f"Tier {tier}" if tier else "Findings"
    ws.append(HEAD)
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF"); c.fill = PatternFill("solid", fgColor="1F4E5A"); c.alignment = Alignment(wrap_text=True)
    for f in q.order_by(DocFinding.tier, DocFinding.encounter_date, DocFinding.claim_id):
        ws.append([f.tier, f.claim_id, f.encounter_date.strftime("%d/%m/%Y") if f.encounter_date else "", f.payer_id,
                   f"{f.clinician_id or ''} {names.get(f.clinician_id, '')}".strip(), f.dx_code, f.dx_description, f.category,
                   f.reason, f.query_text, f.suggestion, f.not_coded, f.services_at_risk,
                   float(f.amount_at_risk) if f.amount_at_risk else None, f.case_sheet, f.all_dx, f.review_status, f.review_remark])
    for col, w in zip("ABCDEFGHIJKLMNOPQR", [5, 28, 11, 9, 26, 9, 28, 26, 38, 50, 45, 32, 22, 10, 60, 36, 11, 28]):
        ws.column_dimensions[col].width = w
    for row in ws.iter_rows(min_row=2):
        for c in row:
            c.alignment = Alignment(wrap_text=True, vertical="top")
    ws.freeze_panes = "C2"; ws.auto_filter.ref = ws.dimensions
    buf = io.BytesIO(); wb.save(buf); return buf.getvalue()


def feedback_workbook(db: Session, facility_id: int, month: str, min_claims: int = 50) -> bytes:
    """One sheet per doctor whose query rate is above the facility average (Tier A + B findings)."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    dash = dashboard(db, facility_id, month)
    fac = db.execute(text("SELECT facility_name FROM facilities WHERE id = :f"), {"f": facility_id}).scalar()
    avg = (dash["run"]["flagged"] / dash["run"]["checked"]) if dash.get("run") and dash["run"]["checked"] else 0
    H, F, W = Font(bold=True, color="FFFFFF"), PatternFill("solid", fgColor="1F4E5A"), Alignment(wrap_text=True, vertical="top")
    wb = Workbook(); ov = wb.active; ov.title = "Overview"
    label = datetime.strptime(month, "%Y-%m").strftime("%B %Y")
    ov.append([f"Case sheet documentation feedback – {fac} – treatment month {label}"]); ov["A1"].font = Font(bold=True, size=13)
    ov.append(["Each coded diagnosis should be supported by the case sheet (chief complaint, findings) or by a result on the claim. "
               "Symptoms that are part of a confirmed diagnosis on the claim are accepted."])
    ov.append([f"Facility average: {avg:.1%} of checked consultations had at least one unsupported diagnosis."]); ov.append([])
    ov.append(["Clinician", "Consultations", "With unsupported Dx", "Rate", "Most-flagged diagnoses"])
    for c in ov[5]:
        c.font = H; c.fill = F
    picked = [d for d in dash.get("doctors", []) if d["checked"] >= min_claims and (d["rate"] or 0) > avg]
    for d in picked:
        ov.append([d["name"] or d["clinician_id"], d["checked"], d["claims_flagged"], d["rate"], "; ".join(d["top"])])
        ov.cell(ov.max_row, 4).number_format = "0.0%"
    for col, w in zip("ABCDE", [30, 13, 16, 8, 100]):
        ov.column_dimensions[col].width = w
    for d in picked:
        nm = d["name"] or d["clinician_id"]
        ws = wb.create_sheet(re.sub(r"[\\/*?:\[\]]", "", ("Dr " + nm))[:31])
        ws.append([f"Dr {nm}  ({d['clinician_id']})"]); ws["A1"].font = Font(bold=True, size=13)
        ws.append([f"{label} · {d['checked']} consultations · {d['claims_flagged']} ({d['rate']:.0%}) with an unsupported diagnosis · facility average {avg:.0%}"])
        cats = db.execute(text("""SELECT category, count(*) FROM doc_findings WHERE facility_id = :f AND month = :m
                                  AND coalesce(clinician_id,'-') = :c AND tier IN ('A','B') GROUP BY 1 ORDER BY 1"""),
                          {"f": facility_id, "m": month, "c": d["clinician_id"]}).all()
        ws.append([]); ws.append(["Most-flagged", "; ".join(d["top"])]); ws.append(["By category", "; ".join(f"{k[3:]} {n}" for k, n in cats)])
        ws.append(["Pattern (Ghaf)", ""]); ws.append(["Suggested change", ""])
        for r in ws.iter_rows(min_row=4, max_row=7):
            r[0].font = Font(bold=True); r[1].alignment = W
        ws.append([]); ws.append(["Examples"]); ws.cell(ws.max_row, 1).font = Font(bold=True, size=12)
        ws.append(["Claim ID", "Date", "Diagnosis not supported", "Why", "Case sheet (as written)", "All Dx on claim"])
        for c in ws[ws.max_row]:
            c.font = H; c.fill = F; c.alignment = W
        ex = db.execute(text("""SELECT claim_id, min(encounter_date), string_agg(dx_code || ' ' || coalesce(dx_description,''), E'\\n'),
                                       string_agg(substr(category, 4) || ': ' || coalesce(reason,''), E'\\n'), max(case_sheet), max(all_dx),
                                       bool_or(category LIKE '1.%') neg
                                FROM doc_findings WHERE facility_id = :f AND month = :m AND coalesce(clinician_id,'-') = :c
                                  AND tier IN ('A','B') GROUP BY claim_id ORDER BY neg DESC, count(*) DESC LIMIT 5"""),
                        {"f": facility_id, "m": month, "c": d["clinician_id"]}).all()
        for e in ex:
            ws.append([e[0], e[1].strftime("%d/%m/%Y") if e[1] else "", e[2], e[3], e[4], e[5]])
            for c in ws[ws.max_row]:
                c.alignment = W
        for col, w in zip("ABCDEF", [28, 11, 34, 40, 60, 38]):
            ws.column_dimensions[col].width = w
        for r in (4, 5, 6, 7):
            ws.merge_cells(start_row=r, start_column=2, end_row=r, end_column=6); ws.row_dimensions[r].height = 40
        ws.page_setup.orientation = "landscape"; ws.sheet_properties.pageSetUpPr.fitToPage = True; ws.page_setup.fitToHeight = 0
    buf = io.BytesIO(); wb.save(buf); return buf.getvalue()


def resume_on_startup() -> None:
    db = SessionLocal()
    try:
        db.execute(text("UPDATE doc_review_runs SET status = 'Failed', error = 'Interrupted by a restart' "
                        "WHERE status IN ('Queued','Running')"))
        db.commit()
    finally:
        db.close()
