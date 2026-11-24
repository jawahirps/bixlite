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
from fastapi import APIRouter, Depends, Header, HTTPException, Query
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

def _get_portal_token(token: Optional[str] = Query(None, description="Portal access token (legacy links)"),
                      x_portal_token: Optional[str] = Header(None, alias="X-Portal-Token"),
                      db: Session = Depends(get_db)) -> PortalToken:
    """Validate the token (X-Portal-Token header preferred, ?token= still accepted) and return the PortalToken."""
    token = x_portal_token or token
    if not token:
        raise HTTPException(status_code=401, detail="Portal token required")
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
    date_basis:   str           = Query("ra", description="ra (RA file date) or encounter, for the KPIs"),
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

    # Summary KPIs on the claim-cycle definition used by the portal dashboard: one row per claim (latest
    # submission), RA rows de-duplicated on (facility, claim, RA file, net, paid, denied), paid = sum of payments
    # capped at net, denied = net - paid. The claim set is the claims with an RA in the filter window (RA file date,
    # like the table) or, with date_basis=encounter, the claims whose encounter falls in the window (as the dashboard).
    kpi_params = {"fids": fac_ids, "d0": df, "d1": dt, "payer": payer_id, "st": claim_status}
    enc = date_basis == "encounter"
    kpi = db.execute(_text(f"""
        WITH c AS (
          SELECT DISTINCT ON (facility_id, claim_id) facility_id, claim_id, coalesce(net_amount, 0) net
          FROM claims WHERE facility_id = ANY(:fids)
            AND (CAST(:payer AS text) IS NULL OR payer_id = :payer)
            {"AND (CAST(:d0 AS date) IS NULL OR encounter_start >= :d0) AND (CAST(:d1 AS date) IS NULL OR encounter_start < CAST(:d1 AS date) + 1)" if enc else ""}
          ORDER BY facility_id, claim_id, submission_date DESC NULLS LAST, id DESC),
        ra AS (
          SELECT DISTINCT ON (r.facility_id, r.claim_id, r.ra_file_id, r.net_amount, r.paid_amount, r.denial_amount)
                 r.facility_id, r.claim_id, coalesce(r.paid_amount, 0) paid,
                 coalesce(r.patient_share_total, 0) ps, coalesce(r.discount_amount, 0) disc,
                 x.submission_date ra_date, r.claim_status_ra st
          FROM remittances r JOIN c USING (facility_id, claim_id)
          LEFT JOIN raw_xml_store x ON x.id = r.raw_xml_id
          ORDER BY r.facility_id, r.claim_id, r.ra_file_id, r.net_amount, r.paid_amount, r.denial_amount, r.id DESC),
        p AS (
          SELECT facility_id, claim_id, sum(paid) paid, sum(ps) ps, sum(disc) disc,
                 bool_or({"TRUE" if enc else "(CAST(:d0 AS date) IS NULL OR ra_date::date >= :d0) AND (CAST(:d1 AS date) IS NULL OR ra_date::date <= :d1)"}
                         AND (CAST(:st AS text) IS NULL OR st = :st)) hit
          FROM ra GROUP BY 1, 2)
        SELECT count(*) claim_count,
               coalesce(sum(least(greatest(p.paid, 0), c.net)), 0) total_paid,
               coalesce(sum(c.net - least(greatest(p.paid, 0), c.net)), 0) total_denied,
               coalesce(sum(p.ps), 0) total_patient_share, coalesce(sum(p.disc), 0) total_discount
        FROM c JOIN p USING (facility_id, claim_id) WHERE p.hit
    """), kpi_params).mappings().one()
    kpi = type("K", (), dict(kpi))

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


# ---------------------------------------------------------------------------
# GET /portal/dashboard — facility-facing summary dashboard (no patient data)
# ---------------------------------------------------------------------------
@router.get("/dashboard")
def portal_dashboard(months: int = Query(12, ge=3, le=24), facility_id: Optional[int] = None,
                     pt: PortalToken = Depends(_get_portal_token), db: Session = Depends(get_db)):
    """Totals by encounter month, denial reasons and payers for the token's facility. Summaries only:
    no claim IDs, patient, member or Emirates IDs leave the server."""
    from app.routers.dashboard import _SQL, COUNTS, AMOUNTS
    from app.routers.alerts import _dd_desc
    from app.services.payer_names import payer_name as _pn
    fids = _facility_ids(pt)
    fid = facility_id if facility_id in fids else fids[0]
    fac = db.get(Facility, fid)
    d1 = date.today()
    y, m = divmod(d1.year * 12 + d1.month - 1 - (months - 1), 12)   # exactly `months` calendar months
    d0 = date(y, m + 1, 1)
    rows = db.execute(_text(_SQL), {"f": fid, "d0": d0, "d1": d1 + timedelta(days=1)}).mappings().all()
    ms = [{"month": r["m"], **{k: int(r[k] or 0) for k in COUNTS}, **{k: round(float(r[k] or 0), 2) for k in AMOUNTS}}
          for r in rows]
    total = {k: sum(m[k] for m in ms) for k in COUNTS + AMOUNTS}
    # Unpaid amount by denial code and payer, reconciled to the Denied card: per service line, net minus all
    # payments across RAs (identical RA loads once), coded with the line's latest denial code; then each claim's
    # lines are scaled so they add up to that claim's Denied amount (net - all payments, as on the card).
    agg = db.execute(_text("""
        WITH c AS (SELECT DISTINCT ON (claim_id) claim_id, coalesce(net_amount, 0) net,
                          upper(coalesce(nullif(receiver_id, ''), payer_id)) payer
                   FROM claims WHERE facility_id = :f AND encounter_start >= :d0 AND encounter_start < :d1
                   ORDER BY claim_id, submission_date DESC NULLS LAST, id DESC),
        ra AS (SELECT DISTINCT ON (r.claim_id, r.ra_file_id, r.net_amount, r.paid_amount, r.denial_amount)
                      r.id, r.claim_id, coalesce(r.paid_amount, 0) paid, r.remittance_date
               FROM remittances r JOIN c USING (claim_id) WHERE r.facility_id = :f),
        cd AS (SELECT c.claim_id, c.payer, c.net - least(greatest(sum(ra.paid), 0), c.net) denied
               FROM c JOIN ra USING (claim_id) GROUP BY c.claim_id, c.payer, c.net),
        ln AS (SELECT ra.claim_id, ad.activity_id,
                      max(coalesce(ad.net, 0)) net, sum(coalesce(ad.paid_amount, 0)) paid,
                      (array_agg(nullif(ad.denial_code, '') ORDER BY ra.remittance_date DESC, ra.id DESC))[1] code
               FROM ra JOIN remittance_activity_details ad ON ad.remittance_id_fk = ra.id
               GROUP BY ra.claim_id, ad.activity_id),
        lu AS (SELECT claim_id, coalesce(code, 'No code') code, greatest(net - paid, 0) amt FROM ln),
        s AS (SELECT claim_id, sum(amt) tot FROM lu GROUP BY 1)
        SELECT lu.code, cd.payer,
               sum(CASE WHEN s.tot > 0.01 THEN lu.amt * cd.denied / s.tot ELSE 0 END) amt, count(*) n
        FROM lu JOIN s USING (claim_id) JOIN cd USING (claim_id)
        WHERE cd.denied > 0.01 AND lu.amt > 0.01 GROUP BY 1, 2
    """), {"f": fid, "d0": d0, "d1": d1 + timedelta(days=1)}).all()
    tot = float(total.get("denied") or 0) or 1
    by_code, by_payer = {}, {}
    for r in agg:
        by_code.setdefault(r.code, [0.0, 0]); by_code[r.code][0] += float(r.amt); by_code[r.code][1] += r.n
        by_payer.setdefault(r.payer, [0.0, 0]); by_payer[r.payer][0] += float(r.amt); by_payer[r.payer][1] += r.n
    def top(d):   # top 7 + "Other" so the rows always add up to the Denied card
        items = sorted(d.items(), key=lambda kv: -kv[1][0])
        head = items[:7]
        rest = tot - sum(v[0] for _, v in head)
        if rest >= 1 and tot > 1:
            head.append(("Other", [rest, sum(v[1] for _, v in items[7:])]))
        return head
    return {
        "facility": fac.facility_name if fac else "", "facilities": [
            {"id": f.id, "name": f.facility_name} for f in db.query(Facility).filter(Facility.id.in_(fids))],
        "facility_id": fid, "date_from": d0.isoformat(), "date_to": d1.isoformat(), "total": total, "months": ms,
        "denial_reasons": [{"code": k, "description": ("All other reasons" if k == "Other" else _dd_desc(k)), "lines": v[1], "amount": round(v[0], 2),
                            "share_pct": round(100 * v[0] / tot)} for k, v in top(by_code)],
        "payers": [{"payer": _pn(k) or k, "lines": v[1], "amount": round(v[0], 2), "share_pct": round(100 * v[0] / tot)}
                   for k, v in top(by_payer)],
        "as_of": datetime.now().strftime("%d %b %Y %H:%M"),
    }
