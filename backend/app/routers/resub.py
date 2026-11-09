"""
resub.py — Resubmission Worklist screen.

  GET  /api/resub/worklist?facility_id=X        items + summary (open, closed, expired)
  POST /api/resub/tracking                      save status / assigned / remarks / facility reply
  GET  /api/resub/playbook                      denial-code playbook
  PUT  /api/resub/playbook/{id}                 edit a playbook rule (admin / billing manager)
  POST /api/resub/playbook                      add a playbook rule (admin / billing manager)
  GET  /api/resub/export?facility_id=X&audience=ghaf|facility   Excel download
  GET  /api/resub/ra-workbook?facility_id=X&date_from=&date_to=  RA activity report (by RA date) + resub remarks

Facility users (a user tied to one facility, not admin) only see their own
facility and only the items that need facility attention; they can answer
with a facility reply.
"""
from __future__ import annotations

import io
from datetime import date
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Facility, ResubPlaybook, User
from ..security import get_current_user
from ..services import resub_ra_workbook as rawb
from ..services import resub_worklist as rw

router = APIRouter(prefix="/api/resub", tags=["resubmission"])

EDITOR_ROLES = ("admin", "billing_manager")
TEXT_FIELDS = ["action", "facility_request", "documents", "draft_comment", "recon_remark", "denial_reason",
               "category", "receiver", "payer", "clinician"]


def _is_facility_user(user: User) -> bool:
    return user.role != "admin" and user.facility_id is not None


def _check_facility(user: User, facility_id: int) -> None:
    if _is_facility_user(user) and user.facility_id != facility_id:
        raise HTTPException(status_code=403, detail="Not allowed for this facility")


@router.get("/worklist")
def get_worklist(facility_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _check_facility(user, facility_id)
    data = rw.build_worklist(db, facility_id)
    items = data["items"]
    if _is_facility_user(user):
        items = [x for x in items if x["owner"] == "Facility" or x["bucket"] == "reconciliation"]
    # long texts repeat across thousands of rows: send each once and reference it by index
    texts, ix = [], {}
    slim = []
    for x in items:
        y = dict(x)
        for f in TEXT_FIELDS:
            v = y.get(f)
            if v:
                if v not in ix:
                    ix[v] = len(texts); texts.append(v)
                y[f] = ix[v]
        slim.append(y)
    return {
        "items": slim,
        "texts": texts,
        "text_fields": TEXT_FIELDS,
        "summary": rw.summarize(data["items"]),
        "overall_rate": data["overall_rate"],
        "window_days": data["window_days"],
        "statuses": rw.STATUSES,
        "facility_view": _is_facility_user(user),
    }


class TrackingUpdate(BaseModel):
    claim_id: str
    activity_id: str
    ra_date: str
    status: Optional[str] = None
    owner_override: Optional[str] = None
    assigned_to: Optional[str] = None
    remarks: Optional[str] = None
    facility_reply: Optional[str] = None


class TrackingBatch(BaseModel):
    facility_id: int
    updates: List[TrackingUpdate]


@router.post("/tracking")
def save_tracking(body: TrackingBatch, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _check_facility(user, body.facility_id)
    updates = [u.model_dump(exclude_unset=True) for u in body.updates]
    if _is_facility_user(user):
        # facility staff answer requests; they don't re-route or close Ghaf's work
        allowed = {"claim_id", "activity_id", "ra_date", "facility_reply", "status"}
        updates = [{k: v for k, v in u.items() if k in allowed} for u in updates]
        if any(u.get("status") not in (None, "Facility replied", "Collect from patient", "Facility discussing with payer",
                                  rw.RECON_AGREED, "Not agreed with payer (write-off)") for u in updates):
            raise HTTPException(status_code=403, detail="Facility users can only reply")
    try:
        n = rw.save_tracking(db, body.facility_id, updates, user.email)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"saved": n}


class PlaybookIn(BaseModel):
    sort_order: Optional[int] = None
    pattern: Optional[str] = None
    activity_pattern: Optional[str] = None
    reason_pattern: Optional[str] = None
    category: Optional[str] = None
    owner: Optional[str] = None
    facility_role: Optional[str] = None
    action: Optional[str] = None
    facility_request: Optional[str] = None
    documents: Optional[str] = None
    draft_comment: Optional[str] = None
    is_active: Optional[bool] = None


def _pb_out(p: ResubPlaybook) -> dict:
    return {c: getattr(p, c) for c in ("id", "sort_order", "pattern", "activity_pattern", "reason_pattern", "category", "owner", "facility_role",
                                       "action", "facility_request", "documents", "draft_comment",
                                       "is_active", "updated_by")} | {
        "updated_at": p.updated_at.isoformat() if p.updated_at else None}


def _validate_pb(data: dict) -> None:
    import re
    if "owner" in data and data["owner"] not in ("Ghaf", "Facility"):
        raise HTTPException(status_code=400, detail="Owner must be Ghaf or Facility")
    for k in ("pattern", "activity_pattern", "reason_pattern"):
        if data.get(k):
            try:
                re.compile(data[k])
            except re.error as exc:
                raise HTTPException(status_code=400, detail=f"Invalid {k.replace('_', ' ')}: {exc}")


@router.get("/playbook")
def get_playbook(db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    return [_pb_out(p) for p in rw.ensure_playbook(db)]


@router.put("/playbook/{pb_id}")
def update_playbook(pb_id: int, body: PlaybookIn, db: Session = Depends(get_db),
                    user: User = Depends(get_current_user)):
    if user.role not in EDITOR_ROLES:
        raise HTTPException(status_code=403, detail="Only admins and billing managers can edit the playbook")
    p = db.get(ResubPlaybook, pb_id)
    if not p:
        raise HTTPException(status_code=404, detail="Rule not found")
    data = body.model_dump(exclude_unset=True)
    _validate_pb(data)
    for k, v in data.items():
        setattr(p, k, v)
    p.updated_by = user.email
    db.commit(); db.refresh(p)
    return _pb_out(p)


@router.post("/playbook")
def add_playbook(body: PlaybookIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    if user.role not in EDITOR_ROLES:
        raise HTTPException(status_code=403, detail="Only admins and billing managers can edit the playbook")
    data = body.model_dump(exclude_unset=True)
    if not data.get("pattern") or not data.get("category") or not data.get("action"):
        raise HTTPException(status_code=400, detail="Pattern, category and action are required")
    _validate_pb(data)
    p = ResubPlaybook(**data, updated_by=user.email)
    db.add(p); db.commit(); db.refresh(p)
    return _pb_out(p)


@router.get("/export")
def export(facility_id: int, audience: str = "ghaf", db: Session = Depends(get_db),
           user: User = Depends(get_current_user)):
    _check_facility(user, facility_id)
    if audience not in ("ghaf", "facility", "reconciliation"):
        raise HTTPException(status_code=400, detail="audience must be ghaf, facility or reconciliation")
    if _is_facility_user(user) and audience == "ghaf":
        audience = "facility"
    fac = db.get(Facility, facility_id)
    if not fac:
        raise HTTPException(status_code=404, detail="Facility not found")
    data = rw.build_worklist(db, facility_id)
    wb = rw.to_workbook(fac.facility_name, data, audience)
    buf = io.BytesIO(); wb.save(buf); buf.seek(0)
    label = {"ghaf": "Resub_Worklist_Ghaf", "facility": "Resub_Facility_Requests", "reconciliation": "Reconciliation"}[audience]
    name = f"{label}_{fac.facility_code}.xlsx"
    return StreamingResponse(buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.get("/ra-workbook")
def ra_workbook(facility_id: int, date_from: date, date_to: date, db: Session = Depends(get_db),
                user: User = Depends(get_current_user)):
    """Remittance Activity report for RA transaction dates date_from..date_to, every service line with a remark
    on how to resubmit it, plus a consultation code-correction sheet for the facility."""
    from .reports import _build_remittance_activity
    _check_facility(user, facility_id)
    if date_from > date_to:
        raise HTTPException(status_code=400, detail="date_from is after date_to")
    fac = db.get(Facility, facility_id)
    if not fac:
        raise HTTPException(status_code=404, detail="Facility not found")
    wb, _, n = _build_remittance_activity(db, facility_id, None, None, date_from, date_to, "remittance_date")
    if not n:
        raise HTTPException(status_code=404, detail="No RA lines for this facility in that RA date range")
    rawb.add_remarks(db, facility_id, wb)
    buf = io.BytesIO(); wb.save(buf); buf.seek(0)
    name = f"Resub_Workbook_{fac.facility_code}_{date_from:%Y%m%d}-{date_to:%Y%m%d}.xlsx"
    return StreamingResponse(buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})
