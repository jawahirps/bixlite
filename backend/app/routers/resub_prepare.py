"""
resub_prepare.py — Prepare Resubmission (one denied DHPO claim → Claim.Submission XML → DHPO upload).

  GET  /api/resub-prepare/draft?facility_id&claim_id        facts, suggestions, requirements
  POST /api/resub-prepare/xml                               {xml, json, errors, warnings, filename, id}
  GET  /api/resub-prepare/xml/download?facility_id&id       saved XML as a file
  POST /api/resub-prepare/upload                            DHPO UploadTransaction (TEST by default)
  GET  /api/resub-prepare/history?facility_id&claim_id      generate / upload log

PRODUCTION uploads: admin only, confirm=true and the claim ID typed exactly.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..database import engine, get_db
from ..models import User
from ..security import get_current_user
from ..services import resub_prepare as rp

router = APIRouter(prefix="/api/resub-prepare", tags=["resubmission"])


def _ready():
    # created lazily on first use, in its own short transaction (never at app import / startup)
    try:
        rp.ensure_table(engine)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"resub_submissions table not ready: {type(exc).__name__}")


def _check_facility(user: User, facility_id: int) -> None:
    if user.role != "admin" and user.facility_id is not None and user.facility_id != facility_id:
        raise HTTPException(status_code=403, detail="Not allowed for this facility")


def _draft(db: Session, facility_id: int, claim_id: str) -> Dict[str, Any]:
    try:
        d = rp.build_draft(db, facility_id, claim_id.strip())
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    db.commit()   # end the read transaction promptly
    return d


@router.get("/draft")
def get_draft(facility_id: int, claim_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _check_facility(user, facility_id)
    return rp.public_draft(_draft(db, facility_id, claim_id))


class XmlIn(BaseModel):
    facility_id: int
    claim_id: str
    disposition: str = rp.TEST
    edits: Dict[str, Any] = Field(default_factory=dict)


@router.post("/xml")
def post_xml(body: XmlIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _check_facility(user, body.facility_id)
    _ready()
    if body.disposition == rp.PRODUCTION and user.role != "admin":
        raise HTTPException(status_code=403, detail="PRODUCTION XML is admin only")
    draft = _draft(db, body.facility_id, body.claim_id)
    out = rp.build_xml(draft, body.edits, body.disposition)
    out["id"] = rp.record(db, body.facility_id, draft["facts"]["claim_id"], body.disposition, out["xml"], out["json"],
                          "generated" if not out["errors"] else "invalid", user.id)
    return out


@router.get("/xml/download")
def download_xml(facility_id: int, id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _check_facility(user, facility_id)
    _ready()
    r = rp.get_xml(db, facility_id, id)
    if not r or not r["xml"]:
        raise HTTPException(status_code=404, detail="Not found")
    name = f"RESUB_{r['claim_id']}_{r['id']}.xml".replace("/", "_")
    return Response(r["xml"], media_type="application/xml",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


class UploadIn(BaseModel):
    facility_id: int
    id: int                       # resub_submissions row from POST /xml
    disposition: str = rp.TEST
    confirm: bool = False
    typed_claim_id: Optional[str] = None


@router.post("/upload")
def post_upload(body: UploadIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _check_facility(user, body.facility_id)
    _ready()
    if body.disposition == rp.PRODUCTION and user.role != "admin":
        raise HTTPException(status_code=403, detail="PRODUCTION upload is admin only")
    r = rp.get_xml(db, body.facility_id, body.id)
    if not r or not r["xml"]:
        raise HTTPException(status_code=404, detail="Generate the XML first")
    filename = f"RESUB_{r['claim_id']}_{r['id']}.xml".replace("/", "_")
    try:
        return rp.upload(db, body.facility_id, r["xml"], filename, body.disposition, user,
                         confirm=body.confirm, typed_claim_id=body.typed_claim_id)
    except rp.UploadRefused as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/history")
def get_history(facility_id: int, claim_id: str, db: Session = Depends(get_db),
                user: User = Depends(get_current_user)) -> List[Dict[str, Any]]:
    _check_facility(user, facility_id)
    _ready()
    return rp.history(db, facility_id, claim_id.strip())
