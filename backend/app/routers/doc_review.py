"""
doc_review.py — Documentation review screen (case sheet vs coded Dx).

  POST  /api/docreview/runs                       {facility_id, month} → run the check in the background
  GET   /api/docreview/runs?facility_id=          latest runs
  GET   /api/docreview/dashboard?facility_id=&month=
  GET   /api/docreview/findings?facility_id=&month=&tier=&category=&clinician=&review=&page=
  PATCH /api/docreview/findings/{id}              {review_status, review_remark}
  GET   /api/docreview/findings.xlsx?facility_id=&month=&tier=
  GET   /api/docreview/feedback.xlsx?facility_id=&month=
  GET   /api/docreview/payer-patterns?facility_id=&month=  payer denial patterns from the Claim scrubber

Facility users see their own facility only. Nothing is sent to the facility or a payer from here.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from math import ceil
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Facility, User
from ..security import resolve_facility, check_facility, allowed_facility_ids, is_clinic_user, get_current_user
from ..services import doc_review as dr
from ..services import resub_worklist as rw

router = APIRouter(prefix="/api/docreview", tags=["documentation review"])
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PAGE = 100


def _check_facility(db: Session, user: User, facility_id: int) -> Facility:
    check_facility(db, user, facility_id)
    fac = db.get(Facility, facility_id)
    if not fac or not fac.is_active or fac.facility_code == "GHAF-001":
        raise HTTPException(404, "Facility not found")
    return fac


def _month(m: str) -> str:
    if not re.fullmatch(r"\d{4}-\d{2}", m or ""):
        raise HTTPException(400, "Month must be YYYY-MM")
    return m


class RunIn(BaseModel):
    facility_id: int
    month: str


class ReviewIn(BaseModel):
    review_status: Optional[str] = None
    review_remark: Optional[str] = None


def _run_out(r: dr.DocReviewRun) -> dict:
    return {c: getattr(r, c) for c in ("id", "facility_id", "month", "status", "progress", "error", "claims_checked",
                                       "claims_flagged", "claims_no_note", "created_at", "completed_at")}


@router.post("/runs")
def start_run(body: RunIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _check_facility(db, user, body.facility_id)
    if db.query(dr.DocReviewRun).filter(dr.DocReviewRun.status.in_(("Queued", "Running"))).count():
        raise HTTPException(409, "A check is already running; try again when it finishes")
    r = dr.DocReviewRun(facility_id=body.facility_id, month=_month(body.month), user_id=user.id, status="Queued")
    db.add(r); db.commit(); db.refresh(r)
    dr.start(r.id)
    return _run_out(r)


@router.get("/runs")
def list_runs(facility_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _check_facility(db, user, facility_id)
    q = db.query(dr.DocReviewRun).filter_by(facility_id=facility_id).order_by(dr.DocReviewRun.id.desc()).limit(12)
    return [_run_out(r) for r in q]


@router.get("/dashboard")
def get_dashboard(facility_id: int, month: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _check_facility(db, user, facility_id)
    return dr.dashboard(db, facility_id, _month(month))


@router.get("/findings")
def list_findings(facility_id: int, month: str, tier: Optional[str] = None, category: Optional[str] = None,
                  clinician: Optional[str] = None, review: Optional[str] = None, page: int = 1,
                  db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _check_facility(db, user, facility_id)
    q = db.query(dr.DocFinding).filter_by(facility_id=facility_id, month=_month(month))
    if tier:
        q = q.filter(dr.DocFinding.tier == tier)
    if category:
        q = q.filter(dr.DocFinding.category == category)
    if clinician:
        q = q.filter(dr.DocFinding.clinician_id == clinician)
    if review == "pending":
        q = q.filter(dr.DocFinding.review_status.is_(None))
    elif review:
        q = q.filter(dr.DocFinding.review_status == review)
    total = q.count()
    rows = (q.order_by(dr.DocFinding.tier, dr.DocFinding.amount_at_risk.desc().nullslast(), dr.DocFinding.encounter_date)
            .offset((max(page, 1) - 1) * PAGE).limit(PAGE).all())
    names = dr.clinician_names(db, facility_id)
    cols = ("id", "tier", "claim_id", "encounter_date", "payer_id", "clinician_id", "dx_code", "dx_description", "category",
            "reason", "query_text", "suggestion", "not_coded", "services_at_risk", "amount_at_risk", "case_sheet", "all_dx",
            "review_status", "review_remark")
    return {"total": total, "pages": ceil(total / PAGE) if total else 0, "page": page,
            "rows": [{**{c: getattr(f, c) for c in cols}, "clinician_name": names.get(f.clinician_id, "")} for f in rows]}


@router.patch("/findings/{finding_id}")
def review_finding(finding_id: int, body: ReviewIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    f = db.get(dr.DocFinding, finding_id)
    if not f:
        raise HTTPException(404, "Finding not found")
    _check_facility(db, user, f.facility_id)
    if body.review_status not in (None, "", "Agree", "Not needed"):
        raise HTTPException(400, "Review must be Agree or Not needed")
    f.review_status = body.review_status or None
    f.review_remark = (body.review_remark or "").strip() or None
    f.reviewed_by, f.reviewed_at = user.id, datetime.now(timezone.utc)
    db.commit()
    return {"id": f.id, "review_status": f.review_status, "review_remark": f.review_remark}


@router.get("/payer-patterns")
def payer_patterns(facility_id: int, month: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Payer denial patterns and the clinician licence watch-list — from the Claim scrubber when the month was
    scrubbed, else from the month's submitted claims. For clinic administration, not claim queries."""
    _check_facility(db, user, facility_id)
    y, m = map(int, _month(month).split("-"))
    rows = db.execute(text("""
        WITH c AS (SELECT DISTINCT ON (sc.claim_id) sc.id, sc.net_amount,
                          upper(coalesce(nullif(sc.receiver_id,''), sc.payer_id, '')) AS rcv
                   FROM scrub_claims sc JOIN scrub_sessions s ON s.id = sc.session_id
                   WHERE s.facility_id = :f AND sc.encounter_start >= make_date(:y, :m, 1)
                     AND sc.encounter_start < make_date(:y, :m, 1) + interval '1 month'
                   ORDER BY sc.claim_id, s.id DESC)
        SELECT q.rule_code AS kind, c.rcv, q.item AS code, q.evidence AS pattern, count(DISTINCT c.id) AS claims,
               sum(c.net_amount) AS net
        FROM scrub_queries q JOIN c ON c.id = q.scrub_claim_id
        WHERE q.rule_code IN ('DENIAL_RISK', 'LICENCE') GROUP BY 1, 2, 3, 4 ORDER BY 1, 5 DESC, 6 DESC"""),
        {"f": facility_id, "y": y, "m": m}).all()
    source = "scrubbed"
    if not rows:      # month never scrubbed: same tests on the submitted claims
        from datetime import date
        from ..services import presub_queries
        d0 = date(y, m, 1)
        rows = presub_queries.month_patterns(db, facility_id, d0, date(y + m // 12, m % 12 + 1, 1))
        source = "submitted"
    payer_names, receiver_names = rw.party_names(db)
    return [{"kind": "licence" if r.kind == "LICENCE" else "denial", "payer": r.rcv, "payer_name": receiver_names.get(r.rcv) or payer_names.get(r.rcv) or "",
             "code": r.code, "pattern": (r.pattern or "").removeprefix("Payer pattern: ").removeprefix("Payer history: "),
             "claims": r.claims, "net": float(r.net or 0), "source": source} for r in rows]


def _fname(fac: Facility, what: str, month: str) -> str:
    return f"{re.sub(r'[^A-Za-z0-9]+', '-', fac.facility_name).strip('-')}_{what}_{month}.xlsx"


@router.get("/findings.xlsx")
def findings_xlsx(facility_id: int, month: str, tier: Optional[str] = None,
                  db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    fac = _check_facility(db, user, facility_id)
    what = f"Documentation_Tier{tier}" if tier else "Documentation_findings"
    return Response(dr.findings_workbook(db, facility_id, _month(month), tier), media_type=XLSX,
                    headers={"Content-Disposition": f'attachment; filename="{_fname(fac, what, month)}"'})


@router.get("/feedback.xlsx")
def feedback_xlsx(facility_id: int, month: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    fac = _check_facility(db, user, facility_id)
    return Response(dr.feedback_workbook(db, facility_id, _month(month)), media_type=XLSX,
                    headers={"Content-Disposition": f'attachment; filename="{_fname(fac, "Doctor_feedback", month)}"'})
