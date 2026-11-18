"""
Ghaf App — DHA XML Sync pipeline endpoints.

Workflow:
  1. ClaimSubmission / RemittanceAdvice XML payloads (DHA Shafafiya /
     eClaimLink format) are staged via POST /api/dha-sync/upload, which
     writes a row to `raw_xml_store` with parse_status="pending".
  2. POST /api/dha-sync/trigger runs the DHPOXmlParser over all pending
     records for a facility, normalizing them into claims/remittances and
     recording a FetchLog entry for observability.
  3. GET endpoints expose raw XML and fetch-log history for the
     "DHA XML Sync" view in the React app.
"""
import threading
from datetime import date, datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..database import get_db, SessionLocal
from ..models import Facility, FetchLog, RawXmlStore, ScrubXmlFinding, User
from ..schemas import DhaFetchRequest, DhaSyncTriggerResponse, DhaUploadResponse, FetchLogOut, RawXmlStoreOut
from ..security import get_current_user, require_roles
from ..services.dhpo_parser import DHPOXmlParser
from ..services.fetch_service import DHASyncService
from ..services.riayati_service import RiayatiSyncService, RiayatiInactiveFacilityError

router = APIRouter(prefix="/api/dha-sync", tags=["dha-sync"])


# ---------------------------------------------------------------------------
# Async reparse background worker
# ---------------------------------------------------------------------------
def _run_reparse_background(
    fetch_log_id: int,
    facility_scope,
    target_ids,
    reset_count: int,
):
    """
    Runs in a daemon thread — opens its own DB session (request session is
    closed by the time this executes), parses the targeted XMLs, then updates
    the fetch_log row with the final status.
    """
    db = SessionLocal()
    try:
        parser  = DHPOXmlParser(db)
        summary = parser.run_pending(facility_id=facility_scope, raw_xml_ids=target_ids)

        log = db.get(FetchLog, fetch_log_id)
        if log:
            log.completed_at    = datetime.utcnow()
            log.records_fetched = reset_count
            log.records_parsed  = summary.records_parsed_total
            log.records_failed  = summary.quarantined_records
            log.status = (
                "success" if summary.quarantined_records == 0
                else ("failed" if summary.parsed_records == 0 else "partial")
            )
            db.commit()
    except Exception as exc:
        try:
            log = db.get(FetchLog, fetch_log_id)
            if log:
                log.completed_at = datetime.utcnow()
                log.status       = "failed"
                log.error_detail = {"error": str(exc)}
                db.commit()
        except Exception:
            pass
    finally:
        db.close()

ALLOWED_XML_TYPES = {"ClaimSubmission", "RemittanceAdvice"}


@router.post("/upload", response_model=DhaUploadResponse, status_code=status.HTTP_201_CREATED)
async def upload_xml(
    facility_id: int,
    xml_type: str,
    submission_date: Optional[date] = None,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    _: User = Depends(require_roles("admin", "billing_manager")),
):
    """Stage a DHA ClaimSubmission / RemittanceAdvice XML file for parsing."""
    if xml_type not in ALLOWED_XML_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"xml_type must be one of {sorted(ALLOWED_XML_TYPES)}",
        )

    raw_bytes = await file.read()
    if not raw_bytes:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Uploaded file is empty")

    record = RawXmlStore(
        facility_id=facility_id,
        xml_type=xml_type,
        source_filename=file.filename,
        submission_date=submission_date or date.today(),
        file_size_bytes=len(raw_bytes),
        xml_content=raw_bytes.decode("utf-8", errors="replace"),
        parse_status="pending",
    )
    db.add(record)
    db.commit()
    db.refresh(record)

    return DhaUploadResponse(
        raw_xml_id=record.id,
        xml_type=record.xml_type,
        source_filename=record.source_filename,
        parse_status=record.parse_status,
    )


@router.post("/trigger", response_model=DhaSyncTriggerResponse)
def trigger_sync(
    facility_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("admin", "billing_manager")),
):
    """Run the DHA XML Sync pipeline over all pending raw XML records."""
    facility_scope = facility_id or current_user.facility_id

    fetch_log = FetchLog(
        facility_id=facility_scope or 0,
        fetch_type="Backfill",
        date_from=date.today(),
        date_to=date.today(),
        status="running",
    )
    db.add(fetch_log)
    db.commit()
    db.refresh(fetch_log)

    try:
        parser = DHPOXmlParser(db)
        summary = parser.run_pending(facility_id=facility_scope)

        fetch_log.completed_at = datetime.utcnow()
        fetch_log.records_fetched = summary.pending_records
        fetch_log.records_parsed = summary.records_parsed_total
        fetch_log.records_failed = summary.quarantined_records
        fetch_log.status = (
            "success" if summary.quarantined_records == 0 else
            ("failed" if summary.parsed_records == 0 else "partial")
        )
        db.commit()

        return DhaSyncTriggerResponse(
            fetch_log_id=fetch_log.id,
            status=fetch_log.status,
            pending_records=summary.pending_records,
            parsed_records=summary.parsed_records,
            quarantined_records=summary.quarantined_records,
            message=(
                f"Processed {summary.pending_records} staged file(s): "
                f"{summary.parsed_records} parsed, {summary.quarantined_records} quarantined "
                f"({summary.records_parsed_total} claim/remittance records)."
            ),
        )
    except Exception as e:
        fetch_log.completed_at = datetime.utcnow()
        fetch_log.status = "failed"
        fetch_log.error_detail = {"error": str(e)}
        db.commit()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"DHA sync failed: {e}")


@router.post("/fetch", response_model=FetchLogOut)
def fetch_from_eclaimlink(
    payload: DhaFetchRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("admin", "billing_manager")),
):
    """
    Live eClaimLink (DHPO) fetch for a date range, then parse.

    Pulls ClaimSubmission / RemittanceAdvice transactions for the facility
    and date range via DHASyncService, stages them in raw_xml_store, and
    runs the DHA XML Sync parser over everything pending. See
    app/services/fetch_service.py to wire up the real DHPO SOAP client.
    """
    facility_scope = payload.facility_id or current_user.facility_id
    if facility_scope is None:
        # Admin with no facility_id — default to Apollo Clinics (ID 13)
        facility_scope = 13

    if payload.date_to < payload.date_from:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="date_to must be on or after date_from")

    try:
        worker = DHASyncService(session=db)
        fetch_log = worker.run_sync_pipeline(
            facility_id=facility_scope,
            date_from=payload.date_from,
            date_to=payload.date_to,
        )
        return fetch_log
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"DHA fetch failed: {e}")


@router.post("/reparse")
def reparse_all(
    facility_id: Optional[int] = None,
    xml_type: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("admin", "billing_manager")),
):
    """Reset parsed/quarantined records back to pending so they are re-parsed on next trigger."""
    from ..models import RawXmlStore as RXS
    query = db.query(RXS).filter(RXS.parse_status.in_(["parsed", "quarantined"]))
    facility_scope = facility_id or current_user.facility_id
    if facility_scope is not None:
        query = query.filter(RXS.facility_id == facility_scope)
    if xml_type:
        query = query.filter(RXS.xml_type == xml_type)
    count = query.update({"parse_status": "pending", "records_parsed": None, "parse_error": None},
                         synchronize_session=False)
    db.commit()
    return {"reset": count, "message": f"Reset {count} records to pending. Run Trigger DHA XML Sync to re-parse."}


@router.post("/reparse-and-trigger")
def reparse_and_trigger(
    facility_id: Optional[int] = None,
    xml_type: Optional[str] = "RemittanceAdvice",
    date_from: Optional[date] = None,
    date_to:   Optional[date] = None,
    date_field: Optional[str] = "treatment_date",   # "treatment_date" | "remittance_date"
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("admin", "billing_manager")),
):
    """
    Combined: reset parsed/quarantined records → pending, then immediately re-parse.
    Defaults to RemittanceAdvice only. Pass xml_type=None to reparse all types.

    When date_from/date_to are provided, only XMLs linked to claims within
    that date range are targeted (avoids full-table reparse):
    - date_field="treatment_date"  → filters via claims.encounter_start (recommended)
    - date_field="remittance_date" → filters via raw_xml_store.submission_date directly
    """
    sa_text = text  # alias for readability within this function

    facility_scope = facility_id or current_user.facility_id

    # ── 1. Determine which raw_xml_store IDs to target ───────────────────────
    target_ids: Optional[list] = None  # None = all (no date filter)

    if date_from and date_to:
        fac_clause = "AND rx.facility_id = :fid" if facility_scope else ""
        params: dict = {"date_from": date_from, "date_to": date_to}
        if facility_scope:
            params["fid"] = facility_scope

        types_to_scan = [xml_type] if xml_type else ["ClaimSubmission", "RemittanceAdvice"]
        id_set: set = set()

        for xt in types_to_scan:
            params["xt"] = xt
            if date_field == "remittance_date":
                # Simple: filter raw_xml_store.submission_date directly
                rows = db.execute(sa_text(f"""
                    SELECT id FROM raw_xml_store
                    WHERE parse_status IN ('parsed','quarantined')
                      AND xml_type = :xt
                      AND submission_date BETWEEN :date_from AND :date_to
                      {fac_clause.replace('rx.', '')}
                """), params).fetchall()
                id_set.update(r[0] for r in rows)
            else:
                # Treatment date: join through claims table
                if xt == "ClaimSubmission":
                    rows = db.execute(sa_text(f"""
                        SELECT DISTINCT rx.id
                        FROM raw_xml_store rx
                        JOIN claims c ON c.raw_xml_id = rx.id
                        WHERE rx.parse_status IN ('parsed','quarantined')
                          AND rx.xml_type = :xt
                          AND c.encounter_start BETWEEN :date_from AND :date_to
                          {fac_clause}
                    """), params).fetchall()
                else:  # RemittanceAdvice
                    rows = db.execute(sa_text(f"""
                        SELECT DISTINCT rx.id
                        FROM raw_xml_store rx
                        JOIN remittances r ON r.raw_xml_id = rx.id
                        JOIN claims c
                          ON c.facility_id = r.facility_id
                         AND c.claim_id    = r.claim_id
                        WHERE rx.parse_status IN ('parsed','quarantined')
                          AND rx.xml_type = :xt
                          AND c.encounter_start BETWEEN :date_from AND :date_to
                          {fac_clause}
                    """), params).fetchall()
                id_set.update(r[0] for r in rows)

        target_ids = list(id_set)
        if not target_ids:
            return {
                "status": "success", "xml_type": xml_type or "all",
                "reset_count": 0, "parsed_records": 0,
                "records_total": 0, "quarantined": 0,
                "fetch_log_id": None,
                "message": "No XMLs found for the given date range.",
            }

    # ── 2. Reset records to pending ───────────────────────────────────────────
    q = db.query(RawXmlStore).filter(RawXmlStore.parse_status.in_(["parsed", "quarantined"]))
    if facility_scope:
        q = q.filter(RawXmlStore.facility_id == facility_scope)
    if xml_type:
        q = q.filter(RawXmlStore.xml_type == xml_type)
    if target_ids is not None:
        q = q.filter(RawXmlStore.id.in_(target_ids))
    reset_count = q.update(
        {"parse_status": "pending", "records_parsed": None, "parse_error": None},
        synchronize_session=False,
    )
    db.commit()

    # ── 3. Log the job (fetch_type must match DB check constraint) ────────────
    _log_type = xml_type if xml_type in ("ClaimSubmission", "RemittanceAdvice") else "Backfill"
    fetch_log = FetchLog(
        facility_id=facility_scope or 0,
        fetch_type=_log_type,
        date_from=date_from or date(2020, 1, 1),   # sentinel: no filter = all history
        date_to=date_to or date.today(),
        status="running",
    )
    db.add(fetch_log)
    db.commit()
    db.refresh(fetch_log)

    # ── 4. Fire background thread — return immediately ────────────────────────
    threading.Thread(
        target=_run_reparse_background,
        args=(fetch_log.id, facility_scope, target_ids, reset_count),
        daemon=True,
    ).start()

    scope_desc = (
        f"{date_from} → {date_to} ({date_field.replace('_', ' ')})"
        if date_from else "all dates"
    )
    return {
        "status":       "running",
        "fetch_log_id": fetch_log.id,
        "xml_type":     xml_type or "all",
        "scope":        scope_desc,
        "reset_count":  reset_count,
        "message": (
            f"Re-parse queued — {reset_count} record(s) being processed in background "
            f"({scope_desc}). Job ID: {fetch_log.id}. Watch Job History for updates."
        ),
    }


@router.get("/raw-xml", response_model=List[RawXmlStoreOut])
def list_raw_xml(
    facility_id: Optional[int] = None,
    parse_status: Optional[str] = None,
    xml_type: Optional[str] = None,
    limit: int = 100,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    query = db.query(RawXmlStore)

    facility_scope = facility_id or current_user.facility_id
    if facility_scope is not None:
        query = query.filter(RawXmlStore.facility_id == facility_scope)
    if parse_status:
        query = query.filter(RawXmlStore.parse_status == parse_status)
    if xml_type:
        query = query.filter(RawXmlStore.xml_type == xml_type)

    return query.order_by(RawXmlStore.fetched_at.desc()).limit(min(limit, 500)).all()


# ---------------------------------------------------------------------------
# Riayati (MOH) fetch  —  requires Oracle-hosted backend (whitelisted IP)
# ---------------------------------------------------------------------------

@router.post("/riayati-fetch", response_model=FetchLogOut)
def fetch_from_riayati(
    payload: DhaFetchRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("admin", "billing_manager")),
):
    """
    Live Riayati (MOH TMB API) fetch for a facility + date range.

    IMPORTANT: tmbapi.riayati.ae:8083 only accepts connections from the
    Oracle Cloud server IP whitelisted by MOH.  This endpoint will fail
    with a connection error if called from a non-whitelisted host (e.g.
    a developer's local Mac).  Deploy the backend on the Oracle server
    before using this endpoint.

    On success, returns the FetchLog row (same schema as /fetch).
    On inactive facility, returns HTTP 422 with an actionable message.
    """
    facility_scope = payload.facility_id or current_user.facility_id
    if facility_scope is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="facility_id is required for Riayati fetch",
        )

    if payload.date_to < payload.date_from:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="date_to must be on or after date_from",
        )

    # Block demo facility — never include GHAF-001 in real fetches
    facility = db.get(Facility, facility_scope)
    if facility and facility.facility_code == "GHAF-001":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Demo facility (GHAF-001) cannot be used for live Riayati fetches.",
        )

    try:
        worker = RiayatiSyncService(session=db)
        fetch_log = worker.run_sync_pipeline(
            facility_id=facility_scope,
            date_from=payload.date_from,
            date_to=payload.date_to,
        )
        return fetch_log
    except RiayatiInactiveFacilityError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Riayati facility is inactive: {e}. "
                "Email RiayatiPO@mohap.gov.ae to reactivate the facility license."
            ),
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Riayati fetch failed: {e}",
        )


@router.get("/raw-xml/{record_id}/peek")
def peek_raw_xml(
    record_id: int,
    chars: int = 800,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """Return the first N characters of a staged XML record — for debugging parser issues."""
    record = db.get(RawXmlStore, record_id)
    if not record:
        raise HTTPException(status_code=404, detail="Record not found")
    return {"id": record.id, "xml_type": record.xml_type,
            "source_filename": record.source_filename,
            "preview": (record.xml_content or "")[:chars]}


@router.get("/fetch-logs", response_model=List[FetchLogOut])
def list_fetch_logs(
    facility_id: Optional[int] = None,
    limit: int = 50,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    query = db.query(FetchLog)

    # Admins with no explicit facility_id see all jobs; non-admins always see only their facility
    if facility_id is not None:
        query = query.filter(FetchLog.facility_id == facility_id)
    elif current_user.role != "admin":
        if current_user.facility_id is not None:
            query = query.filter(FetchLog.facility_id == current_user.facility_id)

    return query.order_by(FetchLog.started_at.desc()).limit(min(limit, 200)).all()


@router.get("/fetch-health")
def fetch_health(
    stale_hours: int = 26,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Per facility and source (DHA / Riayati): last run, last success and an alert flag.

    A source is flagged when its last run did not succeed or nothing succeeded
    within `stale_hours`. Only sources that ran in the last 30 days are listed.
    """
    params = {"stale": stale_hours}
    fac_filter = ""
    if current_user.role != "admin" and current_user.facility_id is not None:
        fac_filter = "AND f.facility_id = :fid"
        params["fid"] = current_user.facility_id
    rows = db.execute(text(f"""
        WITH f AS (
            SELECT facility_id,
                   CASE WHEN fetch_type = 'Riayati' THEN 'Riayati' ELSE 'DHA' END AS source,
                   status, started_at, completed_at, error_detail
            FROM fetch_log
            WHERE started_at > now() - interval '30 days'
              AND fetch_type IN ('Backfill', 'Riayati', 'ClaimSubmission', 'RemittanceAdvice')
        ), last_run AS (
            SELECT DISTINCT ON (facility_id, source) facility_id, source, status,
                   started_at, error_detail
            FROM f WHERE status <> 'running'
            ORDER BY facility_id, source, started_at DESC
        ), last_ok AS (
            SELECT facility_id, source, max(coalesce(completed_at, started_at)) AS ok_at
            FROM f WHERE status = 'success' GROUP BY 1, 2
        )
        SELECT f.facility_id, fa.facility_name, f.source, f.status AS last_status,
               f.started_at AS last_run_at, o.ok_at AS last_success_at,
               f.error_detail
        FROM last_run f
        JOIN facilities fa ON fa.id = f.facility_id AND fa.is_active
        LEFT JOIN last_ok o USING (facility_id, source)
        WHERE fa.facility_code <> 'GHAF-001' {fac_filter}
        ORDER BY fa.facility_name, f.source
    """), params).mappings().all()
    now = datetime.now().astimezone()
    out = []
    for r in rows:
        ok_at = r["last_success_at"]
        hours = round((now - ok_at).total_seconds() / 3600, 1) if ok_at else None
        reasons = []
        if r["last_status"] != "success":
            reasons.append(f"last run {r['last_status']}")
        if hours is None or hours > stale_hours:
            reasons.append("no success in 30 days" if hours is None else f"no success for {hours:.0f} h")
        err = None
        if r["error_detail"] and r["last_status"] != "success":
            errs = (r["error_detail"] or {}).get("errors") or []
            err = str(errs[0])[:200] if errs else str(r["error_detail"])[:200]
        out.append({**{k: r[k] for k in ("facility_id", "facility_name", "source", "last_status",
                                           "last_run_at", "last_success_at")},
                    "hours_since_success": hours, "alert": bool(reasons),
                    "reason": "; ".join(reasons) or None, "error": err})
    return {"stale_hours": stale_hours, "generated_at": now, "items": out,
            "alerts": sum(1 for o in out if o["alert"])}


# ---------------------------------------------------------------------------
# Layer 2: XML scrub findings for a raw_xml_store record
# ---------------------------------------------------------------------------
class RawXmlFindingOut(BaseModel):
    id: int
    raw_xml_id: int
    session_status: str
    risk_level: Optional[str]
    risk_score: Optional[int]
    severity: str
    rule_id: str
    location: Optional[str]
    claim_id: Optional[str]
    activity_id: Optional[str]
    message: str
    source: Optional[str]
    profile: str
    scrubbed_at: datetime
    encounter_date: Optional[str] = None   # joined from claims table

    class Config:
        from_attributes = True


@router.get("/scrub-summaries")
def get_scrub_summaries(
    facility_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """
    Return scrub summaries for ALL ClaimSubmission records of a facility in one call.
    Response: {raw_xml_id: {scrubbed, status, risk_level, risk_score, blocks, warnings, profile, scrubbed_at}}
    Used to pre-populate the client-side scrubCache so filters work immediately.
    """
    # Collect all ClaimSubmission raw_xml_ids for this facility
    id_rows = (
        db.query(RawXmlStore.id)
        .filter(
            RawXmlStore.facility_id == facility_id,
            RawXmlStore.xml_type.in_(["ClaimSubmission", "RiayatiClaimSubmission"]),
        )
        .all()
    )
    raw_ids = [r[0] for r in id_rows]
    if not raw_ids:
        return {}

    # First finding per raw_xml_id (carries session-level risk data)
    first_rows = db.execute(
        text("""
            SELECT DISTINCT ON (raw_xml_id)
                raw_xml_id, session_status, risk_level, risk_score, profile, scrubbed_at
            FROM scrub_xml_findings
            WHERE raw_xml_id = ANY(:ids)
            ORDER BY raw_xml_id, id
        """),
        {"ids": raw_ids},
    ).mappings().all()

    # Block / warning counts per raw_xml_id
    count_rows = db.execute(
        text("""
            SELECT raw_xml_id,
                   SUM(CASE WHEN severity='BLOCK' THEN 1 ELSE 0 END)::int AS blocks,
                   SUM(CASE WHEN severity='WARN'  THEN 1 ELSE 0 END)::int AS warnings
            FROM scrub_xml_findings
            WHERE raw_xml_id = ANY(:ids)
            GROUP BY raw_xml_id
        """),
        {"ids": raw_ids},
    ).mappings().all()
    counts = {r["raw_xml_id"]: r for r in count_rows}

    result = {}
    for row in first_rows:
        rid = row["raw_xml_id"]
        c = counts.get(rid, {})
        result[str(rid)] = {
            "scrubbed":   True,
            "status":     row["session_status"],
            "risk_level": row["risk_level"],
            "risk_score": row["risk_score"],
            "blocks":     c.get("blocks", 0),
            "warnings":   c.get("warnings", 0),
            "profile":    row["profile"],
            "scrubbed_at": str(row["scrubbed_at"]) if row["scrubbed_at"] else None,
        }

    # Mark every ID that has no findings as not-scrubbed
    scrubbed_ids = set(result.keys())
    for rid in raw_ids:
        if str(rid) not in scrubbed_ids:
            result[str(rid)] = {"scrubbed": False}

    return result


@router.get("/raw-xml/{record_id}/xml-findings", response_model=List[RawXmlFindingOut])
def get_raw_xml_findings(
    record_id: int,
    severity: Optional[str] = None,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """Return XML scrub findings for a raw_xml_store ClaimSubmission record (Layer 2)."""
    record = db.get(RawXmlStore, record_id)
    if not record:
        raise HTTPException(status_code=404, detail="Record not found")

    q = db.query(ScrubXmlFinding).filter(ScrubXmlFinding.raw_xml_id == record_id)
    if severity:
        q = q.filter(ScrubXmlFinding.severity == severity.upper())
    else:
        q = q.filter(ScrubXmlFinding.rule_id != "ALL_CLEAR")

    findings = q.order_by(ScrubXmlFinding.id).all()

    # Attach encounter_date from claims table (one lookup per raw_xml_id)
    enc_dates = _encounter_dates_for_raw(db, record_id)
    result = []
    for f in findings:
        d = {c.name: getattr(f, c.name) for c in f.__table__.columns}
        d["encounter_date"] = enc_dates.get(f.claim_id or "", None)
        result.append(d)
    return result


@router.get("/raw-xml/{record_id}/xml-scrub-summary")
def get_raw_xml_scrub_summary(
    record_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """Return a one-line scrub summary for a raw_xml_store ClaimSubmission record."""
    record = db.get(RawXmlStore, record_id)
    if not record:
        raise HTTPException(status_code=404, detail="Record not found")

    # First finding row has session-level risk fields
    first = (
        db.query(ScrubXmlFinding)
        .filter(ScrubXmlFinding.raw_xml_id == record_id)
        .order_by(ScrubXmlFinding.id)
        .first()
    )
    if not first:
        return {"scrubbed": False}

    blocks = db.query(ScrubXmlFinding).filter(
        ScrubXmlFinding.raw_xml_id == record_id,
        ScrubXmlFinding.severity == "BLOCK",
    ).count()
    warnings = db.query(ScrubXmlFinding).filter(
        ScrubXmlFinding.raw_xml_id == record_id,
        ScrubXmlFinding.severity == "WARN",
    ).count()

    return {
        "scrubbed": True,
        "status": first.session_status,
        "risk_level": first.risk_level,
        "risk_score": first.risk_score,
        "blocks": blocks,
        "warnings": warnings,
        "profile": first.profile,
        "scrubbed_at": first.scrubbed_at,
    }


# ---------------------------------------------------------------------------
# Layer 2: batch re-scrub all unscubbed ClaimSubmission files for a facility
# ---------------------------------------------------------------------------
@router.post("/batch-scrub")
def batch_scrub(
    facility_id: int,
    limit: int = 200,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """
    Run the XML scrubber on ClaimSubmission files that have no findings yet.
    Used to retroactively scrub files fetched before Layer 2 was deployed.
    Returns counts of scrubbed / failed / skipped files.
    """
    from ..services.scrub_xml_service import ScrubXmlService

    # Find ClaimSubmission records with no findings
    already_scrubbed_ids = db.execute(
        text("SELECT DISTINCT raw_xml_id FROM scrub_xml_findings WHERE raw_xml_id IS NOT NULL")
    ).scalars().all()

    records = (
        db.query(RawXmlStore)
        .filter(
            RawXmlStore.facility_id == facility_id,
            RawXmlStore.xml_type == "ClaimSubmission",
            RawXmlStore.parse_status == "parsed",
            ~RawXmlStore.id.in_(already_scrubbed_ids) if already_scrubbed_ids else text("TRUE"),
        )
        .order_by(RawXmlStore.id.desc())
        .limit(limit)
        .all()
    )

    if not records:
        return {"scrubbed": 0, "failed": 0, "skipped": 0, "total": 0}

    svc = ScrubXmlService(db)
    scrubbed = failed = 0

    for rec in records:
        try:
            xml_bytes = (rec.xml_content or "").encode("utf-8")
            svc.run_for_raw(
                raw_xml_id=rec.id,
                xml_bytes=xml_bytes,
                facility_id=facility_id,
            )
            db.commit()
            scrubbed += 1
        except Exception as exc:
            db.rollback()
            failed += 1

    return {
        "scrubbed": scrubbed,
        "failed": failed,
        "skipped": 0,
        "total": len(records),
    }


# ---------------------------------------------------------------------------
# Excel export — single file or multi-file bulk
# ---------------------------------------------------------------------------
def _findings_to_workbook(rows_by_file: list[dict]):
    """Build an openpyxl workbook from a list of {filename, xml_type, submission_date, findings, encounter_dates}."""
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Scrub Findings"

    HDR_FILL  = PatternFill("solid", fgColor="1E3A5F")
    HDR_FONT  = Font(color="FFFFFF", bold=True, size=10)
    COLS = [
        ("File", 35), ("Submission Date", 16), ("Severity", 10),
        ("Rule ID", 22), ("Claim ID", 18), ("Encounter Date", 16), ("Activity ID", 14),
        ("Message", 60), ("Location", 20), ("Profile", 16), ("Scrubbed At", 20),
    ]

    # Header
    for col_idx, (hdr, width) in enumerate(COLS, 1):
        cell = ws.cell(row=1, column=col_idx, value=hdr)
        cell.font = HDR_FONT
        cell.fill = HDR_FILL
        cell.alignment = Alignment(horizontal="center", vertical="center")
        ws.column_dimensions[get_column_letter(col_idx)].width = width
    ws.row_dimensions[1].height = 18

    SEV_FILLS = {
        "BLOCK": PatternFill("solid", fgColor="FEE2E2"),
        "WARN":  PatternFill("solid", fgColor="FEF9C3"),
        "PASS":  PatternFill("solid", fgColor="DCFCE7"),
    }

    row_num = 2
    for item in rows_by_file:
        fname        = item["filename"]
        sub_dt       = item["submission_date"]
        enc_dates    = item.get("encounter_dates", {})   # claim_id → encounter_start date str
        for f in item["findings"]:
            sev = f.severity
            enc_date = enc_dates.get(f.claim_id or "", "") if f.claim_id else ""
            vals = [
                fname,
                str(sub_dt) if sub_dt else "",
                sev,
                f.rule_id,
                f.claim_id or "",
                str(enc_date) if enc_date else "",
                f.activity_id or "",
                f.message,
                f.location or "",
                f.profile,
                str(f.scrubbed_at)[:19] if f.scrubbed_at else "",
            ]
            for col_idx, val in enumerate(vals, 1):
                cell = ws.cell(row=row_num, column=col_idx, value=val)
                if sev in SEV_FILLS:
                    cell.fill = SEV_FILLS[sev]
                cell.alignment = Alignment(wrap_text=(col_idx == 8))
            row_num += 1

    if row_num == 2:
        ws.cell(row=2, column=1, value="No findings (all files passed scrub).")

    return wb


def _encounter_dates_for_raw(db: Session, raw_xml_id: int) -> dict:
    """Return {claim_id: encounter_start_date_str} for all claims linked to a raw_xml_id."""
    rows = db.execute(
        text("SELECT claim_id, encounter_start::date AS enc_date FROM claims WHERE raw_xml_id = :rid"),
        {"rid": raw_xml_id},
    ).mappings().all()
    return {r["claim_id"]: str(r["enc_date"]) for r in rows if r["claim_id"]}


@router.get("/raw-xml/{record_id}/export-excel")
def export_single_excel(
    record_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """Download scrub findings for one ClaimSubmission file as an Excel workbook."""
    import io
    from fastapi.responses import StreamingResponse

    record = db.get(RawXmlStore, record_id)
    if not record:
        raise HTTPException(status_code=404, detail="Record not found")

    findings = (
        db.query(ScrubXmlFinding)
        .filter(
            ScrubXmlFinding.raw_xml_id == record_id,
            ScrubXmlFinding.rule_id != "ALL_CLEAR",
        )
        .order_by(ScrubXmlFinding.severity, ScrubXmlFinding.id)
        .all()
    )

    wb = _findings_to_workbook([{
        "filename": record.source_filename or f"raw_{record_id}",
        "submission_date": record.submission_date,
        "findings": findings,
        "encounter_dates": _encounter_dates_for_raw(db, record_id),
    }])

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    safe_name = (record.source_filename or f"raw_{record_id}").replace(".xml", "")
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="scrub_{safe_name}.xlsx"'},
    )


class BulkExportRequest(BaseModel):
    raw_xml_ids: List[int]


@router.post("/export-excel-bulk")
def export_bulk_excel(
    payload: BulkExportRequest,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """Download scrub findings for multiple ClaimSubmission files as one Excel workbook."""
    import io
    from fastapi.responses import StreamingResponse

    if not payload.raw_xml_ids:
        raise HTTPException(status_code=400, detail="No record IDs provided")
    if len(payload.raw_xml_ids) > 200:
        raise HTTPException(status_code=400, detail="Maximum 200 files per export")

    records = (
        db.query(RawXmlStore)
        .filter(RawXmlStore.id.in_(payload.raw_xml_ids))
        .order_by(RawXmlStore.id)
        .all()
    )
    rec_map = {r.id: r for r in records}

    rows_by_file = []
    for rid in payload.raw_xml_ids:
        rec = rec_map.get(rid)
        if not rec:
            continue
        findings = (
            db.query(ScrubXmlFinding)
            .filter(
                ScrubXmlFinding.raw_xml_id == rid,
                ScrubXmlFinding.rule_id != "ALL_CLEAR",
            )
            .order_by(ScrubXmlFinding.severity, ScrubXmlFinding.id)
            .all()
        )
        rows_by_file.append({
            "filename": rec.source_filename or f"raw_{rid}",
            "submission_date": rec.submission_date,
            "findings": findings,
            "encounter_dates": _encounter_dates_for_raw(db, rid),
        })

    wb = _findings_to_workbook(rows_by_file)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="scrub_findings_export.xlsx"'},
    )
