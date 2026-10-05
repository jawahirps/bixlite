"""
scrub.py — Pre-submission claim scrub endpoints.

POST /api/scrub/sessions                     Upload CS XML → parse into staging
POST /api/scrub/sessions/{id}/run            Run rule engine on parsed session
GET  /api/scrub/sessions                     List sessions (newest first)
GET  /api/scrub/sessions/{id}/results        Per-claim results for a session
GET  /api/scrub/sessions/{id}/export         Download Excel report of results
GET  /api/scrub/exclusions                   List active exclusion codes
POST /api/scrub/exclusions                   Add a new exclusion code
DELETE /api/scrub/exclusions/{code_id}       Delete an exclusion code
"""
from __future__ import annotations

import io
from datetime import datetime
from typing import List, Optional

import openpyxl
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import ExclusionCode, ScrubClaim, ScrubResult, ScrubSession, ScrubXmlFinding, User
from ..security import get_current_user
from ..services.scrub_extractor import ScrubXmlExtractor
from ..services.scrub_rules import ScrubRuleEngine
from ..services.scrub_xml_service import ScrubXmlService

router = APIRouter(prefix="/api/scrub", tags=["scrub"])


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------

class SessionOut(BaseModel):
    id: int
    facility_id: int
    uploaded_filename: str
    status: str
    claims_count: Optional[int] = None
    parse_error: Optional[str] = None
    uploaded_at: datetime
    run_completed_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class SessionRunOut(BaseModel):
    session_id: int
    status: str
    total: int
    errors: int
    warnings: int
    passed: int
    # XML scrubber layer (Strawberry) — None if XML bytes not available
    xml_status: Optional[str] = None
    xml_blocks: Optional[int] = None
    xml_warnings: Optional[int] = None
    xml_risk_level: Optional[str] = None
    xml_risk_score: Optional[int] = None
    xml_findings_count: Optional[int] = None


class XmlFindingOut(BaseModel):
    id: int
    session_status: str
    risk_level: Optional[str] = None
    risk_score: Optional[int] = None
    severity: str
    rule_id: str
    location: Optional[str] = None
    claim_id: Optional[str] = None
    activity_id: Optional[str] = None
    message: str
    source: Optional[str] = None
    profile: str

    model_config = {"from_attributes": True}


class ClaimResultOut(BaseModel):
    scrub_claim_id: int
    claim_id: str
    emirates_id: Optional[str]
    encounter_start: Optional[str]
    encounter_type: Optional[str]
    rule_code: str
    severity: str
    message: str
    detail_json: Optional[dict] = None


class ExclusionOut(BaseModel):
    id: int
    exclusion_type: int
    code_type: str
    code: str
    description: Optional[str] = None
    is_active: bool

    model_config = {"from_attributes": True}


class ExclusionIn(BaseModel):
    exclusion_type: int          # 1 or 2
    code_type: str               # 'diagnosis' | 'activity'
    code: str
    description: Optional[str] = None


# ---------------------------------------------------------------------------
# Session endpoints
# ---------------------------------------------------------------------------

@router.post("/sessions", response_model=SessionOut, status_code=status.HTTP_201_CREATED)
async def upload_xml(
    file: UploadFile = File(...),
    facility_id: int = Form(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Upload a ClaimSubmission XML file.  Creates a ScrubSession, parses claims
    into scrub_* staging tables, and returns the session record.
    """
    if not file.filename or not file.filename.lower().endswith(".xml"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only .xml files are accepted.",
        )

    xml_bytes = await file.read()
    if not xml_bytes:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Uploaded file is empty.",
        )

    # Create session record
    scrub_session = ScrubSession(
        facility_id=facility_id,
        uploaded_by=current_user.id,
        uploaded_filename=file.filename,
        status="uploaded",
    )
    db.add(scrub_session)
    db.flush()

    # Parse XML into staging tables
    try:
        extractor = ScrubXmlExtractor(db, session_id=scrub_session.id, facility_id=facility_id)
        count = extractor.parse(xml_bytes)
        scrub_session.claims_count = count
        scrub_session.status = "parsed"
        db.commit()
        db.refresh(scrub_session)
    except ValueError as exc:
        scrub_session.status = "failed"
        scrub_session.parse_error = str(exc)
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"XML parse error: {exc}",
        ) from exc
    except Exception as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Unexpected error during parse: {exc}",
        ) from exc

    # ── Run Strawberry XML scrubber (structural + coding checks) ──────────
    # Runs immediately on upload so findings are ready before /run is called.
    try:
        xml_svc = ScrubXmlService(db)
        xml_svc.run(session_id=scrub_session.id, xml_bytes=xml_bytes)
        db.commit()
    except Exception as exc:
        # Non-fatal: log and continue — business rules still run via /run
        import logging
        logging.getLogger(__name__).warning(
            "XML scrubber failed for session %d: %s", scrub_session.id, exc
        )

    return scrub_session


@router.post("/sessions/{session_id}/run", response_model=SessionRunOut)
def run_session(
    session_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Run the rule engine on a parsed ScrubSession.  Writes ScrubResult rows.
    """
    scrub_session: ScrubSession | None = db.get(ScrubSession, session_id)
    if scrub_session is None:
        raise HTTPException(status_code=404, detail="Session not found.")
    if scrub_session.status not in ("parsed", "completed", "failed"):
        raise HTTPException(
            status_code=400,
            detail=f"Session status is '{scrub_session.status}'; must be 'parsed' to run.",
        )

    scrub_session.status = "running"
    db.commit()

    try:
        engine = ScrubRuleEngine(db, session_id=session_id)
        summary = engine.run()
        scrub_session.status = "completed"
        scrub_session.run_completed_at = datetime.utcnow()
        db.commit()
    except Exception as exc:
        scrub_session.status = "failed"
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Rule engine error: {exc}",
        ) from exc

    # Attach XML scrubber summary from findings stored at upload time
    xml_summary: dict = {}
    xml_first = db.execute(
        text("""
            SELECT session_status, risk_level, risk_score,
                   COUNT(*) FILTER (WHERE severity = 'BLOCK') AS blocks,
                   COUNT(*) FILTER (WHERE severity = 'WARN')  AS warnings,
                   COUNT(*) FILTER (WHERE rule_id <> 'ALL_CLEAR') AS total_findings
            FROM scrub_xml_findings
            WHERE session_id = :sid
            GROUP BY session_status, risk_level, risk_score
            LIMIT 1
        """),
        {"sid": session_id},
    ).mappings().first()
    if xml_first:
        xml_summary = {
            "xml_status": xml_first["session_status"],
            "xml_blocks": xml_first["blocks"],
            "xml_warnings": xml_first["warnings"],
            "xml_risk_level": xml_first["risk_level"],
            "xml_risk_score": xml_first["risk_score"],
            "xml_findings_count": xml_first["total_findings"],
        }

    return SessionRunOut(
        session_id=session_id,
        status="completed",
        **summary,
        **xml_summary,
    )


@router.get("/sessions", response_model=List[SessionOut])
def list_sessions(
    facility_id: Optional[int] = None,
    limit: int = 50,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List scrub sessions, newest first.  Optionally filter by facility."""
    q = db.query(ScrubSession).order_by(ScrubSession.uploaded_at.desc())
    if facility_id is not None:
        q = q.filter(ScrubSession.facility_id == facility_id)
    return q.limit(limit).all()


@router.get("/sessions/{session_id}/results", response_model=List[ClaimResultOut])
def session_results(
    session_id: int,
    severity: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Return per-claim results for a session.  Optionally filter by severity
    (ERROR | WARN | PASS).
    """
    scrub_session = db.get(ScrubSession, session_id)
    if scrub_session is None:
        raise HTTPException(status_code=404, detail="Session not found.")

    filters = "WHERE sr.session_id = :sid"
    params: dict = {"sid": session_id}
    if severity:
        filters += " AND sr.severity = :sev"
        params["sev"] = severity.upper()

    rows = db.execute(
        text(f"""
            SELECT sr.id, sr.scrub_claim_id, sc.claim_id, sc.emirates_id,
                   sc.encounter_start::text AS encounter_start,
                   sc.encounter_type,
                   sr.rule_code, sr.severity, sr.message, sr.detail_json
            FROM scrub_results sr
            JOIN scrub_claims sc ON sc.id = sr.scrub_claim_id
            {filters}
            ORDER BY
                CASE sr.severity WHEN 'ERROR' THEN 1 WHEN 'WARN' THEN 2 ELSE 3 END,
                sc.claim_id
        """),
        params,
    ).mappings().all()

    return [
        ClaimResultOut(
            scrub_claim_id=r["scrub_claim_id"],
            claim_id=r["claim_id"],
            emirates_id=r["emirates_id"],
            encounter_start=r["encounter_start"],
            encounter_type=r["encounter_type"],
            rule_code=r["rule_code"],
            severity=r["severity"],
            message=r["message"],
            detail_json=r["detail_json"],
        )
        for r in rows
    ]


@router.get("/sessions/{session_id}/xml-findings", response_model=List[XmlFindingOut])
def session_xml_findings(
    session_id: int,
    severity: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Return XML scrubber findings (structural, arithmetic, coding, MUE, frequency)
    from the Strawberry scrubber layer.  These complement the DB-backed rule results.

    Optional filter: severity=BLOCK|WARN|PASS
    """
    scrub_session = db.get(ScrubSession, session_id)
    if scrub_session is None:
        raise HTTPException(status_code=404, detail="Session not found.")

    q = db.query(ScrubXmlFinding).filter(ScrubXmlFinding.session_id == session_id)
    if severity:
        q = q.filter(ScrubXmlFinding.severity == severity.upper())
    # Exclude the ALL_CLEAR sentinel unless explicitly requesting PASS
    if not severity or severity.upper() != "PASS":
        q = q.filter(ScrubXmlFinding.rule_id != "ALL_CLEAR")
    q = q.order_by(
        # BLOCK first, then WARN, then PASS
        ScrubXmlFinding.severity.desc(),
        ScrubXmlFinding.id,
    )
    return q.all()


@router.post("/sessions/{session_id}/re-scrub-xml", response_model=dict)
async def re_scrub_xml(
    session_id: int,
    file: UploadFile = File(...),
    enable_nas: bool = False,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Re-run the XML scrubber on a new file for an existing session.
    Useful if you want to test a revised XML without creating a new session.
    """
    scrub_session = db.get(ScrubSession, session_id)
    if scrub_session is None:
        raise HTTPException(status_code=404, detail="Session not found.")

    xml_bytes = await file.read()
    if not xml_bytes:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")

    try:
        xml_svc = ScrubXmlService(db)
        result = xml_svc.run(
            session_id=session_id,
            xml_bytes=xml_bytes,
            enable_nas=enable_nas,
        )
        db.commit()
        return result
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"XML scrubber error: {exc}") from exc


@router.get("/sessions/{session_id}/rule-summary")
def rule_summary(
    session_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Return per-rule counts for a session so the UI can show a coverage table.

    Response shape:
      {
        "total_claims": N,
        "rules": [
          {"rule_code": "FREE_FOLLOWUP", "errors": 0, "warnings": 0, "passed": N},
          ...
        ]
      }
    """
    scrub_session = db.get(ScrubSession, session_id)
    if scrub_session is None:
        raise HTTPException(status_code=404, detail="Session not found.")

    total_claims = db.execute(
        text("SELECT COUNT(*) FROM scrub_claims WHERE session_id = :sid"),
        {"sid": session_id},
    ).scalar()

    rows = db.execute(
        text("""
            SELECT rule_code,
                   SUM(CASE WHEN severity = 'ERROR' THEN 1 ELSE 0 END) AS errors,
                   SUM(CASE WHEN severity = 'WARN'  THEN 1 ELSE 0 END) AS warnings,
                   SUM(CASE WHEN severity = 'PASS'  THEN 1 ELSE 0 END) AS passed
            FROM scrub_results
            WHERE session_id = :sid
            GROUP BY rule_code
            ORDER BY rule_code
        """),
        {"sid": session_id},
    ).mappings().all()

    rule_order = ["FREE_FOLLOWUP", "DUPLICATE", "MISSING_AUTH", "EXCLUDE_1", "EXCLUDE_2"]
    rule_map = {r["rule_code"]: r for r in rows}

    rules = []
    for rc in rule_order:
        if rc in rule_map:
            r = rule_map[rc]
            rules.append({
                "rule_code": rc,
                "errors": r["errors"],
                "warnings": r["warnings"],
                "passed": r["passed"],
            })
        else:
            # Rule produced no rows yet (session not run)
            rules.append({"rule_code": rc, "errors": 0, "warnings": 0, "passed": 0})

    return {"total_claims": total_claims, "rules": rules}


@router.get("/sessions/{session_id}/export")
def export_session(
    session_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Download an Excel report of scrub results for a session."""
    scrub_session = db.get(ScrubSession, session_id)
    if scrub_session is None:
        raise HTTPException(status_code=404, detail="Session not found.")

    rows = db.execute(
        text("""
            SELECT sc.claim_id, sc.emirates_id,
                   sc.encounter_start::text AS encounter_start,
                   sc.encounter_type, sc.member_id,
                   sr.rule_code, sr.severity, sr.message
            FROM scrub_results sr
            JOIN scrub_claims sc ON sc.id = sr.scrub_claim_id
            WHERE sr.session_id = :sid
            ORDER BY
                CASE sr.severity WHEN 'ERROR' THEN 1 WHEN 'WARN' THEN 2 ELSE 3 END,
                sc.claim_id
        """),
        {"sid": session_id},
    ).mappings().all()

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Scrub Results"

    headers = [
        "Claim ID", "Emirates ID", "Member ID",
        "Encounter Start", "Encounter Type",
        "Rule", "Severity", "Message",
    ]
    ws.append(headers)

    # Header styling
    from openpyxl.styles import Font, PatternFill, Alignment
    header_fill = PatternFill("solid", fgColor="1F3864")
    header_font = Font(bold=True, color="FFFFFF")
    for cell in ws[1]:
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center")

    # Severity colours
    _fill = {
        "ERROR": PatternFill("solid", fgColor="FFCCCC"),
        "WARN":  PatternFill("solid", fgColor="FFF3CD"),
        "PASS":  PatternFill("solid", fgColor="D4EDDA"),
    }

    for r in rows:
        ws.append([
            r["claim_id"], r["emirates_id"], r["member_id"],
            r["encounter_start"], r["encounter_type"],
            r["rule_code"], r["severity"], r["message"],
        ])
        sev_fill = _fill.get(r["severity"] or "PASS")
        if sev_fill:
            for cell in ws[ws.max_row]:
                cell.fill = sev_fill

    # Column widths
    for col, width in zip("ABCDEFGH", [18, 18, 18, 15, 16, 16, 10, 60]):
        ws.column_dimensions[col].width = width

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    ts = datetime.utcnow().strftime("%Y%m%d_%H%M")
    filename = f"ScrubReport_{session_id}_{ts}.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# Exclusion code endpoints
# ---------------------------------------------------------------------------

@router.get("/exclusions", response_model=List[ExclusionOut])
def list_exclusions(
    exclusion_type: Optional[int] = None,
    code_type: Optional[str] = None,
    active_only: bool = True,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    q = db.query(ExclusionCode)
    if active_only:
        q = q.filter(ExclusionCode.is_active == True)  # noqa: E712
    if exclusion_type is not None:
        q = q.filter(ExclusionCode.exclusion_type == exclusion_type)
    if code_type:
        q = q.filter(ExclusionCode.code_type == code_type)
    return q.order_by(ExclusionCode.exclusion_type, ExclusionCode.code_type, ExclusionCode.code).all()


@router.post("/exclusions", response_model=ExclusionOut, status_code=status.HTTP_201_CREATED)
def add_exclusion(
    body: ExclusionIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if body.exclusion_type not in (1, 2):
        raise HTTPException(status_code=400, detail="exclusion_type must be 1 or 2.")
    if body.code_type not in ("diagnosis", "activity"):
        raise HTTPException(status_code=400, detail="code_type must be 'diagnosis' or 'activity'.")

    existing = (
        db.query(ExclusionCode)
        .filter(
            ExclusionCode.exclusion_type == body.exclusion_type,
            ExclusionCode.code_type == body.code_type,
            ExclusionCode.code == body.code.strip(),
        )
        .first()
    )
    if existing:
        # Re-activate if it was soft-deleted
        existing.is_active = True
        existing.description = body.description
        db.commit()
        db.refresh(existing)
        return existing

    ec = ExclusionCode(
        exclusion_type=body.exclusion_type,
        code_type=body.code_type,
        code=body.code.strip(),
        description=body.description,
        is_active=True,
    )
    db.add(ec)
    db.commit()
    db.refresh(ec)
    return ec


@router.delete("/exclusions/{code_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_exclusion(
    code_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ec = db.get(ExclusionCode, code_id)
    if ec is None:
        raise HTTPException(status_code=404, detail="Exclusion code not found.")
    ec.is_active = False   # soft delete
    db.commit()
