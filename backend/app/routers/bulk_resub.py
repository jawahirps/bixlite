"""
bulk_resub.py — Bulk resubmission screen (tab inside the Resubmission worklist).

  GET  /api/resub/bulk/candidates?facility_id=&search=&batch_id=   denied claims + readiness
  POST /api/resub/bulk/validate          {keys, remarks} → local readiness (TEST- reference)
  POST /api/resub/bulk/remarks           {keys, remarks} → append remark to resub_tracking
  POST /api/resub/bulk/submit            always 501 — production submission is locked
  POST /api/resub/bulk/imports           multipart .xlsx (+ fetch_missing) → import batch
  GET  /api/resub/bulk/imports           my latest 10 imports
  GET  /api/resub/bulk/imports/{id}      batch + rows (100 per page, ?status=&page=)
  POST /api/resub/bulk/imports/{id}/fetch      queue the bounded DHA / Riayati fetch
  GET  /api/resub/bulk/imports/{id}/exceptions  .xlsx of every row not Matched

Facility users are limited to their own facility; import batches are visible only to
the user who uploaded them. Nothing is transmitted to DHA or Riayati.
"""
from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone
from math import ceil
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Facility, User
from ..security import get_current_user
from ..services import bulk_resub as br

router = APIRouter(prefix="/api/resub/bulk", tags=["bulk resubmission"])
ACTIVE_STATES = ("Queued", "Fetching", "Matching")


def _allowed(db: Session, user: User) -> List[int]:
    if user.role != "admin" and user.facility_id is not None:
        return [user.facility_id]
    return [f.id for f in db.query(Facility).filter(Facility.is_active.is_(True)).all() if f.facility_code != "GHAF-001"]


def _own_batch(db: Session, user: User, batch_id: int) -> br.ReconImportBatch:
    b = db.get(br.ReconImportBatch, batch_id)
    if not b or b.user_id != user.id:
        raise HTTPException(404, "Import not found")
    return b


def _batch_out(b: br.ReconImportBatch) -> dict:
    return {c: getattr(b, c) for c in ("id", "filename", "status", "progress", "error", "total_rows", "unique_rows",
                                       "duplicate_rows", "matched_rows", "partial_rows", "missing_rows",
                                       "invalid_rows", "fetch_windows", "uploaded_at", "completed_at")}


class BulkIn(BaseModel):
    keys: List[str]
    remarks: str


@router.get("/candidates")
def get_candidates(facility_id: Optional[int] = None, search: Optional[str] = None, batch_id: Optional[int] = None,
                   db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    allowed = _allowed(db, user)
    if facility_id is not None:
        if facility_id not in allowed:
            raise HTTPException(403, "Not allowed for this facility")
        allowed = [facility_id]
    only = None
    if batch_id is not None:
        _own_batch(db, user, batch_id)
        only = [(r.facility_id, r.claim_id) for r in db.query(br.ReconImportRow)
                .filter(br.ReconImportRow.batch_id == batch_id, br.ReconImportRow.remittance_pk.isnot(None))
                .distinct(br.ReconImportRow.facility_id, br.ReconImportRow.claim_id).limit(br.MAX_CLAIMS)]
    rows = br.candidates(db, allowed, search, only, limit=br.MAX_CLAIMS if only is not None else 250)
    return {"claims": rows, "max_claims": br.MAX_CLAIMS,
            "production_available": False,
            "production_message": "Submission to DHA / Riayati is locked: this app prepares and records resubmissions only."}


@router.post("/validate")
def validate(body: BulkIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    msg = br.validate_request(body.keys, body.remarks)
    if msg:
        raise HTTPException(400, msg)
    allowed = _allowed(db, user)
    pairs = []
    for k in dict.fromkeys(body.keys):
        fid, _, cid = k.partition("|")
        if not fid.isdigit() or int(fid) not in allowed:
            raise HTTPException(403, "Not allowed for one of the selected facilities")
        pairs.append((int(fid), cid))
    rows = br.candidates(db, allowed, only_keys=pairs, limit=br.MAX_CLAIMS + 1)
    if len(rows) != len(pairs):
        raise HTTPException(400, "One or more selected claims no longer exist or are not denied claims.")
    eligible = sum(r["eligible"] for r in rows)
    return {"ok": eligible == len(rows),
            "reference": f"TEST-{datetime.now(timezone.utc):%Y%m%d%H%M%S}-{uuid.uuid4().hex[:8]}",
            "selected": len(rows), "eligible": eligible, "blocked": len(rows) - eligible,
            "claims": [{k: r[k] for k in ("key", "claim_id", "submission_ref", "remittance_ref", "eligible", "readiness")}
                       for r in rows]}


@router.post("/remarks")
def save_remarks(body: BulkIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    msg = br.validate_request(body.keys, body.remarks)
    if msg:
        raise HTTPException(400, msg)
    try:
        n = br.apply_remarks(db, _allowed(db, user), body.keys, body.remarks, user.email)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True, "message": f"Remarks saved to {n} claim(s). Nothing was sent to DHA or Riayati."}


@router.post("/submit")
def submit(body: BulkIn, user: User = Depends(get_current_user)):
    return JSONResponse(status_code=501, content={
        "ok": False,
        "message": "Production submission is not available. It needs a verified DHA/Riayati upload contract, "
                   "test endpoint, acknowledgements and duplicate protection before it can be enabled."})


@router.post("/imports")
async def upload(workbook: UploadFile = File(...), fetch_missing: bool = Form(False),
                 db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    if not (workbook.filename or "").lower().endswith(".xlsx"):
        raise HTTPException(400, "Only .xlsx workbooks are accepted.")
    data = await workbook.read(br.MAX_UPLOAD_BYTES + 1)
    if not data:
        raise HTTPException(400, "Select a non-empty .xlsx workbook.")
    if len(data) > br.MAX_UPLOAD_BYTES:
        raise HTTPException(400, "Workbook exceeds the 25 MB limit.")
    try:
        b = br.import_workbook(db, data, workbook.filename, user.id, _allowed(db, user))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if fetch_missing and b.fetch_windows > 0 and (b.missing_rows + b.partial_rows) > 0:
        b.status, b.progress = "Queued", "Queued for background fetch and matching"
        db.commit()
        threading.Thread(target=br.fetch_missing, args=(b.id,), daemon=True).start()
    return _batch_out(b)


@router.get("/imports")
def my_imports(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    q = (db.query(br.ReconImportBatch).filter(br.ReconImportBatch.user_id == user.id)
         .order_by(br.ReconImportBatch.id.desc()).limit(10))
    return [_batch_out(b) for b in q]


@router.get("/imports/{batch_id}")
def import_detail(batch_id: int, status: Optional[str] = None, page: int = 1,
                  db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    b = _own_batch(db, user, batch_id)
    q = db.query(br.ReconImportRow).filter(br.ReconImportRow.batch_id == batch_id)
    if status:
        q = q.filter(br.ReconImportRow.match_status == status)
    total = q.count()
    page = max(1, page)
    rows = (q.order_by(br.ReconImportRow.match_status, br.ReconImportRow.facility_external_id, br.ReconImportRow.claim_id)
            .offset((page - 1) * 100).limit(100).all())
    names = {f.id: f.facility_name for f in db.query(Facility).all()}
    cols = ("id", "sheet", "source_row", "facility_external_id", "facility_id", "claim_id", "cpt_code", "receiver_id",
            "payer_id", "denial_code", "comment", "remittance_file", "inferred_date", "date_source", "match_status",
            "message", "submission_ref", "remittance_ref")
    return {"batch": _batch_out(b), "page": page, "total_pages": max(1, ceil(total / 100)), "total": total,
            "can_fetch": b.fetch_windows > 0 and b.status not in ACTIVE_STATES,
            "rows": [{**{c: getattr(r, c) for c in cols},
                      "rejected_amount": float(r.rejected_amount) if r.rejected_amount is not None else None,
                      "facility_name": names.get(r.facility_id)} for r in rows]}


@router.post("/imports/{batch_id}/fetch")
def queue_fetch(batch_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    b = _own_batch(db, user, batch_id)
    if b.fetch_windows <= 0:
        raise HTTPException(400, "No dated Missing/Partial rows to fetch.")
    if b.status in ACTIVE_STATES:
        raise HTTPException(409, "This import is already processing.")
    b.status, b.progress, b.error, b.completed_at = "Queued", "Queued for background fetch and matching", None, None
    db.commit()
    threading.Thread(target=br.fetch_missing, args=(b.id,), daemon=True).start()
    return _batch_out(b)


@router.get("/imports/{batch_id}/exceptions")
def exceptions(batch_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _own_batch(db, user, batch_id)
    return Response(br.exceptions_workbook(db, batch_id),
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition":
                             f'attachment; filename="reconciliation-exceptions-{batch_id}-{datetime.now():%Y%m%d%H%M}.xlsx"'})
