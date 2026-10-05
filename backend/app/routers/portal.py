"""
Ghaf App — Public Client Portal Router

Provides token-gated, read-only endpoints for external clients.
No login required — access is controlled by a PortalToken (UUID in URL param).

Endpoints:
  GET /portal/verify           — validate token, return client info + facility list
  GET /portal/remittance       — paginated JSON remittance activity (table data)
  GET /portal/remittance/export — Excel download (same data, .xlsx)

All endpoints scope queries to the token's facility_ids.
"""
from __future__ import annotations

import json
import io
from datetime import datetime, date, timedelta
from decimal import Decimal
from typing import Optional, List

import openpyxl
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import and_, cast, Date, func, text as _text
from sqlalchemy.orm import Session, aliased

from app.database import get_db
from app.models import (
    Claim, ClaimActivity, Facility, PortalToken,
    Remittance, RemittanceActivityDetail, RawXmlStore,
)

router = APIRouter(prefix="/portal", tags=["portal"])

# ---------------------------------------------------------------------------
# Token validation dependency
# ---------------------------------------------------------------------------

def _get_portal_token(token: str = Query(..., description="Portal access token"),
                      db: Session = Depends(get_db)) -> PortalToken:
    """Validate the token and return the PortalToken record."""
    pt = db.query(PortalToken).filter(
        PortalToken.token == token,
        PortalToken.is_active == True,
    ).first()
    if not pt:
        raise HTTPException(status_code=401, detail="Invalid or inactive portal token")
    if pt.expires_at and pt.expires_at < date.today():
        raise HTTPException(status_code=401, detail="Portal token has expired")
    # Update last used
    pt.last_used_at = datetime.utcnow()
    db.commit()
    return pt


def _facility_ids(pt: PortalToken) -> List[int]:
    return json.loads(pt.facility_ids)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

PAYER_NAMES: dict[str, str] = {
    "INS001": "Daman",
    "INS002": "AXA",
    "INS003": "Cigna",
    "INS004": "Oman Insurance",
    "INS005": "Al Sagr",
    "INS006": "Nextcare",
    "INS007": "MSH",
    "INS008": "Neuron",
    "INS009": "MedNet",
    "INS010": "Thiqa / ADNIC",
    "INS011": "NAS",
    "INS012": "GlobeMed",
    "INS013": "Al Ain Ahlia",
    "INS014": "RAK Insurance",
    "INS044": "DHA",
}

def _payer_name(code: Optional[str]) -> Optional[str]:
    if not code or code.upper() in ("UNKNOWN", "N/A", ""):
        return None
    return PAYER_NAMES.get(code, code)

def _receiver_name(receiver_id: Optional[str], fallback_payer_id: Optional[str]) -> Optional[str]:
    """Receiver = the insurance/payer entity that receives and adjudicates the claim."""
    code = receiver_id or fallback_payer_id
    return _payer_name(code)

def _fmt(v) -> Optional[float]:
    if v is None:
        return None
    return float(v)

def _fmt_date(d) -> Optional[str]:
    if d is None:
        return None
    if isinstance(d, datetime):
        return d.strftime("%d/%m/%Y")
    return d.strftime("%d/%m/%Y")


# ---------------------------------------------------------------------------
# GET /portal/verify
# ---------------------------------------------------------------------------

@router.get("/verify")
def verify_token(pt: PortalToken = Depends(_get_portal_token),
                 db: Session = Depends(get_db)):
    """Validate token and return client metadata."""
    fac_ids = _facility_ids(pt)
    facilities = db.query(Facility).filter(Facility.id.in_(fac_ids)).all()
    return {
        "valid": True,
        "client_name": pt.client_name,
        "facility_ids": fac_ids,
        "facilities": [{"id": f.id, "name": f.facility_name, "code": f.facility_code}
                       for f in facilities],
        "expires_at": pt.expires_at.isoformat() if pt.expires_at else None,
    }


# ---------------------------------------------------------------------------
# Core query builder (shared by JSON + Excel endpoints)
# ---------------------------------------------------------------------------

def _remittance_query(db: Session, fac_ids: List[int],
                      date_from: Optional[date], date_to: Optional[date],
                      payer_id: Optional[str], claim_status: Optional[str],
                      search: Optional[str]):
    """Build the remittance activity query scoped to given facility IDs."""
    ra_xml = aliased(RawXmlStore)

    q = (
        db.query(RemittanceActivityDetail, Remittance, Claim, ra_xml)
        .join(Remittance, Remittance.id == RemittanceActivityDetail.remittance_id_fk)
        # LEFT OUTER JOIN — remittances appear even when the original ClaimSubmission
        # was not ingested through our app (e.g. submitted directly via eClaimLink portal).
        .outerjoin(Claim, and_(
            Claim.facility_id == Remittance.facility_id,
            Claim.claim_id    == Remittance.claim_id,
        ))
        .outerjoin(ra_xml, ra_xml.id == Remittance.raw_xml_id)
        .filter(Remittance.facility_id.in_(fac_ids))
    )

    if date_from:
        q = q.filter(ra_xml.submission_date >= date_from)   # filter by RA file date (TransactionDate) — matches client software
    if date_to:
        q = q.filter(ra_xml.submission_date <= date_to)     # filter by RA file date (TransactionDate) — matches client software
    if payer_id:
        q = q.filter(Remittance.payer_id == payer_id)
    if claim_status:
        q = q.filter(Remittance.claim_status_ra == claim_status)
    if search:
        like = f"%{search}%"
        q = q.filter(
            Remittance.claim_id.ilike(like) |           # always present
            Claim.member_id.ilike(like) |               # NULL-safe: returns false when Claim is None
            Remittance.ra_file_id.ilike(like)
        )

    return q.order_by(ra_xml.submission_date.desc(), Remittance.claim_id)


# ---------------------------------------------------------------------------
# GET /portal/remittance  — JSON paginated table
# ---------------------------------------------------------------------------

@router.get("/remittance")
def remittance_activity(
    date_from:    Optional[str] = Query(None, description="YYYY-MM-DD"),
    date_to:      Optional[str] = Query(None, description="YYYY-MM-DD"),
    payer_id:     Optional[str] = Query(None),
    claim_status: Optional[str] = Query(None, description="Paid/Partially Paid/Rejected"),
    search:       Optional[str] = Query(None, description="Claim ID / Member ID / RA File"),
    page:         int           = Query(1, ge=1),
    page_size:    int           = Query(50, ge=10, le=200),
    pt: PortalToken             = Depends(_get_portal_token),
    db: Session                 = Depends(get_db),
):
    fac_ids = _facility_ids(pt)

    df = date.fromisoformat(date_from) if date_from else None
    dt = date.fromisoformat(date_to)   if date_to   else None

    q = _remittance_query(db, fac_ids, df, dt, payer_id, claim_status, search)
    total = q.count()

    rows = q.offset((page - 1) * page_size).limit(page_size).all()

    # Summary KPIs (full result set, not just current page)
    kpi_q = (
        db.query(
            func.count(Remittance.id.distinct()).label("claim_count"),
            func.coalesce(func.sum(Remittance.paid_amount), 0).label("total_paid"),
            func.coalesce(func.sum(Remittance.denial_amount), 0).label("total_denied"),
            func.coalesce(func.sum(Remittance.patient_share_total), 0).label("total_patient_share"),
            func.coalesce(func.sum(Remittance.discount_amount), 0).label("total_discount"),
        )
        # LEFT OUTER JOIN — match _remittance_query so KPI totals include all remittances
        .outerjoin(Claim, and_(
            Claim.facility_id == Remittance.facility_id,
            Claim.claim_id    == Remittance.claim_id,
        ))
        .outerjoin(RawXmlStore, RawXmlStore.id == Remittance.raw_xml_id)
        .filter(Remittance.facility_id.in_(fac_ids))
    )
    if df:
        kpi_q = kpi_q.filter(RawXmlStore.submission_date >= df)   # filter by RA file date (TransactionDate)
    if dt:
        kpi_q = kpi_q.filter(RawXmlStore.submission_date <= dt)   # filter by RA file date (TransactionDate)
    if payer_id:
        kpi_q = kpi_q.filter(Remittance.payer_id == payer_id)
    if claim_status:
        kpi_q = kpi_q.filter(Remittance.claim_status_ra == claim_status)

    kpi = kpi_q.one()

    transactions = []
    for (rad, rem, claim, ra_xml) in rows:
        # claim may be None when the ClaimSubmission was not ingested through our app
        transactions.append({
            "remittance_date":  _fmt_date(ra_xml.submission_date if ra_xml else None),  # RA file date (TransactionDate)
            "settlement_date":  _fmt_date(rem.remittance_date),                          # actual payment settlement date
            "claim_id":         claim.claim_id        if claim else rem.claim_id,
            "patient_id":       claim.patient_id      if claim else None,
            "member_id":        claim.member_id       if claim else None,
            "encounter_start":  _fmt_date(claim.encounter_start)  if claim else None,
            "submission_date":  _fmt_date(claim.submission_date)  if claim else None,
            "payment_ref":      rem.ra_file_id,
            "ra_file_name":     ra_xml.source_filename if ra_xml else None,
            "ra_file_id":       rem.ra_file_id,
            "receiver_name":    _receiver_name(claim.receiver_id, claim.payer_id) if claim else None,
            "payer_id":         rem.payer_id,
            "payer_name":       (_payer_name(rem.payer_id) or _payer_name(claim.payer_id)) if claim
                                    else _payer_name(rem.payer_id),
            "claim_status":     rem.claim_status_ra or None,
            "activity_id":      rad.activity_id,
            "activity_code":    rad.activity_code,
            "activity_type":    rad.activity_type,
            "gross":            _fmt(rad.gross),
            "net":              _fmt(rad.net),
            "paid":             _fmt(rad.paid_amount),
            "patient_share":    _fmt(rad.patient_share),
            "denial_code":      rad.denial_code,
            "denial_desc":      rad.denial_description,
        })

    return {
        "summary": {
            "claim_count":         int(kpi.claim_count),
            "total_paid":          float(kpi.total_paid),
            "total_denied":        float(kpi.total_denied),
            "total_patient_share": float(kpi.total_patient_share),
            "total_discount":      float(kpi.total_discount),
        },
        "pagination": {
            "page":       page,
            "page_size":  page_size,
            "total":      total,
            "pages":      (total + page_size - 1) // page_size,
        },
        "transactions": transactions,
    }


# ---------------------------------------------------------------------------
# GET /portal/remittance/export  — Excel download
# ---------------------------------------------------------------------------

@router.get("/remittance/export")
def remittance_export(
    date_from:    Optional[str] = Query(None),
    date_to:      Optional[str] = Query(None),
    payer_id:     Optional[str] = Query(None),
    claim_status: Optional[str] = Query(None),
    search:       Optional[str] = Query(None),
    pt: PortalToken             = Depends(_get_portal_token),
    db: Session                 = Depends(get_db),
):
    fac_ids = _facility_ids(pt)
    df = date.fromisoformat(date_from) if date_from else None
    dt = date.fromisoformat(date_to)   if date_to   else None

    rows = _remittance_query(db, fac_ids, df, dt, payer_id, claim_status, search).all()

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Remittance Activity"

    headers = [
        "Remittance Date", "Date of Settlement",
        "Claim ID", "Patient ID", "Member ID", "Encounter Date", "Submission Date",
        "Payment Reference", "RA File",
        "Receiver Name", "Payer Name",
        "Claim Status", "Activity ID", "Activity Code", "Activity Type",
        "Gross (AED)", "Net (AED)", "Paid (AED)", "Patient Share (AED)",
        "Denial Code", "Denial Description",
    ]

    # Header row styling
    from openpyxl.styles import Font, PatternFill, Alignment
    hdr_font = Font(bold=True, color="FFFFFF")
    hdr_fill = PatternFill("solid", fgColor="0E7490")

    for col_idx, h in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col_idx, value=h)
        cell.font = hdr_font
        cell.fill = hdr_fill
        cell.alignment = Alignment(horizontal="center")

    ws.freeze_panes = "A2"

    light_fill = PatternFill("solid", fgColor="F0FDFE")
    for row_idx, (rad, rem, claim, ra_xml) in enumerate(rows, start=2):
        # claim may be None when ClaimSubmission was not ingested through our app
        data = [
            _fmt_date(ra_xml.submission_date if ra_xml else None),  # Remittance Date = RA file date (TransactionDate)
            _fmt_date(rem.remittance_date),                          # Date of Settlement = actual payment date
            (claim.claim_id if claim else rem.claim_id) or "",
            (claim.patient_id if claim else "") or "",
            (claim.member_id  if claim else "") or "",
            _fmt_date(claim.encounter_start)  if claim else "",
            _fmt_date(claim.submission_date)  if claim else "",
            rem.ra_file_id or "",
            ra_xml.source_filename if ra_xml else (rem.ra_file_id or ""),
            (_receiver_name(claim.receiver_id, claim.payer_id) if claim else "") or "",
            ((_payer_name(rem.payer_id) or _payer_name(claim.payer_id)) if claim
                else _payer_name(rem.payer_id)) or "",
            rem.claim_status_ra or "",
            rad.activity_id or "", rad.activity_code or "", rad.activity_type or "",
            _fmt(rad.gross), _fmt(rad.net), _fmt(rad.paid_amount),
            _fmt(rad.patient_share),
            rad.denial_code or "", rad.denial_description or "",
        ]
        for col_idx, val in enumerate(data, 1):
            cell = ws.cell(row=row_idx, column=col_idx, value=val)
            if row_idx % 2 == 0:
                cell.fill = light_fill

    # Auto-width
    for col in ws.columns:
        max_len = max((len(str(c.value or "")) for c in col), default=10)
        ws.column_dimensions[col[0].column_letter].width = min(max_len + 4, 40)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    ts = datetime.now().strftime("%Y%m%d_%H%M")
    filename = f"RemittanceActivity_{pt.client_name.replace(' ', '_')}_{ts}.xlsx"

    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# GET /portal/remittance/payers — for filter dropdown
# ---------------------------------------------------------------------------

@router.get("/remittance/payers")
def remittance_payers(
    pt: PortalToken = Depends(_get_portal_token),
    db: Session     = Depends(get_db),
):
    fac_ids = _facility_ids(pt)
    rows = (
        db.query(Remittance.payer_id)
        .filter(Remittance.facility_id.in_(fac_ids), Remittance.payer_id.isnot(None))
        .distinct()
        .order_by(Remittance.payer_id)
        .all()
    )
    return [{"payer_id": r[0], "payer_name": _payer_name(r[0])} for r in rows]
