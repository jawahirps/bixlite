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
from ..security import resolve_facility, check_facility, allowed_facility_ids, is_clinic_user, get_current_user
from ..services import resub_prepare as rp

router = APIRouter(prefix="/api/resub-prepare", tags=["resubmission"])


def _ready():
    # created lazily on first use, in its own short transaction (never at app import / startup)
    try:
        rp.ensure_table(engine)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"resub_submissions table not ready: {type(exc).__name__}")


def _check_facility(user: User, facility_id: int) -> None:
    check_facility(None, user, facility_id)


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


# ---------------------------------------------------------------------------
# RA last 21 days — every denied claim from RAs received in the SN 03/2019 21-day window, grouped by RA file.
# Stage: candidate (initial claim, first RA with a denial) · reconciliation (1st resubmission done) ·
#        limit (2 used) · awaiting_ra (resubmitted after this RA).
# ---------------------------------------------------------------------------
RA21_SQL = """
WITH ra AS (
  SELECT DISTINCT ON (r.claim_id, r.ra_file_id, r.net_amount, r.paid_amount, r.denial_amount)
         r.claim_id, r.ra_file_id, r.remittance_date, r.raw_xml_id, COALESCE(r.paid_amount,0) paid, COALESCE(r.denial_amount,0) denied
  FROM remittances r
  WHERE r.facility_id = :fid AND r.denial_amount > 0 AND r.remittance_date >= current_date - 21
  ORDER BY r.claim_id, r.ra_file_id, r.net_amount, r.paid_amount, r.denial_amount, r.id),
cl AS (
  SELECT c.claim_id, count(*) FILTER (WHERE COALESCE(c.resubmission_key,'') <> '') AS used,
         max(c.submission_date) AS last_sub,
         (array_agg(c.payer_id ORDER BY c.submission_date DESC NULLS LAST, c.id DESC))[1] AS payer_id,
         (array_agg(c.encounter_start ORDER BY c.submission_date DESC NULLS LAST, c.id DESC))[1] AS encounter_start,
         (array_agg(c.net_amount ORDER BY c.submission_date DESC NULLS LAST, c.id DESC))[1] AS net
  FROM claims c WHERE c.facility_id = :fid AND c.claim_id IN (SELECT claim_id FROM ra) GROUP BY 1),
sf AS (
  SELECT f.claim_id, array_agg(DISTINCT f.rule_id) FILTER (WHERE f.rule_id <> 'ALL_CLEAR'
           AND NOT (f.rule_id = 'OVERLAPPING_ACTIVITY' AND f.message ~ 'in claims (\\S+) and \\1;')) AS rules
  FROM scrub_xml_findings f JOIN claims c ON c.raw_xml_id = f.raw_xml_id AND c.claim_id = f.claim_id AND c.facility_id = :fid
  WHERE f.claim_id IN (SELECT claim_id FROM ra) GROUP BY 1)
SELECT ra.ra_file_id, rx.source_filename AS ra_file, ra.remittance_date, ra.claim_id, ra.paid, ra.denied,
       cl.used, cl.last_sub, cl.payer_id, cl.encounter_start::date AS encounter_date, cl.net,
       -- scrubbed = a file holding this claim went through the scrubber (a clean claim has no finding row of its own)
       (sf.claim_id IS NOT NULL OR EXISTS (SELECT 1 FROM claims c2 JOIN scrub_xml_findings f2 ON f2.raw_xml_id = c2.raw_xml_id
                                           WHERE c2.facility_id = :fid AND c2.claim_id = ra.claim_id)) AS scrubbed,
       COALESCE(sf.rules, '{}') AS scrub_rules
FROM ra LEFT JOIN cl USING (claim_id) LEFT JOIN sf USING (claim_id)
LEFT JOIN raw_xml_store rx ON rx.id = ra.raw_xml_id
ORDER BY ra.remittance_date DESC, ra.ra_file_id, ra.claim_id
"""


@router.get("/ra21")
def get_ra21(facility_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    from datetime import date, timedelta
    from sqlalchemy import text
    _check_facility(user, facility_id)
    today = date.today()
    files: Dict[str, Dict[str, Any]] = {}
    for r in db.execute(text(RA21_SQL), {"fid": facility_id}).mappings():
        ra_d = r["remittance_date"].date() if hasattr(r["remittance_date"], "date") else r["remittance_date"]
        used = int(r["used"] or 0)
        last_sub = r["last_sub"].date() if hasattr(r["last_sub"], "date") else r["last_sub"]
        if last_sub and ra_d and last_sub > ra_d:
            stage = "awaiting_ra"
        elif used >= rp.SN_MAX_RESUBMISSIONS:
            stage = "limit"
        elif used == 1:
            stage = "reconciliation"
        else:
            stage = "candidate"
        due = ra_d + timedelta(days=rp.SN_RESUBMIT_DAYS)
        key = r["ra_file"] or r["ra_file_id"] or "?"   # ra_file_id is per claim; group by the RA XML file
        f = files.setdefault(key, {"ra_file_id": r["ra_file_id"], "ra_file": r["ra_file"] or r["ra_file_id"],
                                   "remittance_date": str(ra_d), "resubmit_by": due.isoformat(),
                                   "days_left": (due - today).days, "claims": [], "denied": 0.0})
        f["denied"] += float(r["denied"] or 0)
        f["claims"].append({
            "claim_id": r["claim_id"], "payer_id": r["payer_id"], "encounter_date": str(r["encounter_date"] or ""),
            "net": float(r["net"] or 0), "paid": float(r["paid"] or 0), "denied": float(r["denied"] or 0),
            "resubs_used": used, "stage": stage, "scrubbed": bool(r["scrubbed"]), "scrub_rules": list(r["scrub_rules"] or []),
        })
    out = sorted(files.values(), key=lambda f: (f["remittance_date"], f["ra_file"] or ""), reverse=True)
    for f in out:
        f["denied"] = round(f["denied"], 2)
    return {"files": out, "window_days": rp.SN_RESUBMIT_DAYS}
