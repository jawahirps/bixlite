"""
Receipts Router
===============
Daily receipt reconciliation endpoints for the clinic accounting module.

Endpoints:
  GET  /api/receipts/daily          → receipts for a single date
  GET  /api/receipts/range          → receipts across a date range
  GET  /api/receipts/unmatched      → unmatched / short-paid rows
  POST /api/receipts/reconcile      → trigger reconciliation on demand
  GET  /api/receipts/export         → Excel export (DHPO + Riayati combined)
  GET  /api/receipts/payer-mapping  → list payer→GL mappings
  POST /api/receipts/payer-mapping  → add/update a payer mapping
"""
from __future__ import annotations

import io
import json
from datetime import date, datetime, timezone as _tz
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

from ..database import get_db
from ..models import DailyReceipt, Facility, ReceiptPayerMapping
from ..security import get_current_user
from ..services.receipts_service import ReceiptsService

router = APIRouter(prefix="/api/receipts", tags=["receipts"])

# ── Excel styling helpers (matching existing report style) ──────────────────
_HEADER_FILL   = PatternFill("solid", fgColor="1F4E79")
_DHPO_FILL     = PatternFill("solid", fgColor="D6E4F0")
_RIAYATI_FILL  = PatternFill("solid", fgColor="D5F5E3")
_DENIED_FILL   = PatternFill("solid", fgColor="FCE4EC")
_PARTIAL_FILL  = PatternFill("solid", fgColor="FFF9C4")
_WHITE_FONT    = Font(bold=True, color="FFFFFF", size=10)
_BOLD_FONT     = Font(bold=True, size=10)
_NORMAL_FONT   = Font(size=10)
_THIN_BORDER   = Border(
    left=Side(style="thin"),  right=Side(style="thin"),
    top=Side(style="thin"),   bottom=Side(style="thin"),
)
_CENTER = Alignment(horizontal="center", vertical="center")
_LEFT   = Alignment(horizontal="left",   vertical="center")

STATUS_FILL = {
    "reconciled": None,
    "partial":    _PARTIAL_FILL,
    "denied":     _DENIED_FILL,
    "unmatched":  _DENIED_FILL,
}


def _set_col_widths(ws, widths: list[int]):
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w


# ── Pydantic schemas ────────────────────────────────────────────────────────

class DailyReceiptOut(BaseModel):
    id: int
    facility_id: int
    receipt_date: date
    payer_id: str
    payer_name: Optional[str]
    channel: str
    expected_aed: float
    received_aed: float
    denied_aed: float
    patient_share_aed: float
    claim_count: int
    payment_references: Optional[list[str]]
    status: str
    posted_to_accounting: bool
    gl_account_code: Optional[str]

    model_config = {"from_attributes": True}

    @classmethod
    def from_orm_row(cls, r: DailyReceipt) -> "DailyReceiptOut":
        refs = json.loads(r.payment_references) if r.payment_references else []
        return cls(
            id=r.id,
            facility_id=r.facility_id,
            receipt_date=r.receipt_date,
            payer_id=r.payer_id,
            payer_name=r.payer_name,
            channel=r.channel,
            expected_aed=float(r.expected_aed or 0),
            received_aed=float(r.received_aed or 0),
            denied_aed=float(r.denied_aed or 0),
            patient_share_aed=float(r.patient_share_aed or 0),
            claim_count=r.claim_count,
            payment_references=refs,
            status=r.status,
            posted_to_accounting=r.posted_to_accounting,
            gl_account_code=r.gl_account_code,
        )


class PayerMappingIn(BaseModel):
    facility_id: int
    payer_id: str
    payer_name: Optional[str] = None
    channel: str = "ALL"       # 'DHPO' | 'Riayati' | 'ALL'
    gl_account_code: Optional[str] = None
    cost_centre: Optional[str] = None
    tolerance_aed: float = 0.0


# ── Endpoints ───────────────────────────────────────────────────────────────

@router.get("/daily")
def get_daily_receipts(
    facility_id: int,
    receipt_date: date,
    channel: Optional[str] = None,
    db: Session = Depends(get_db),
    _user=Depends(get_current_user),
):
    """Daily receipt summary for one facility + date."""
    svc = ReceiptsService(db)
    rows = svc.get_daily_receipts(facility_id, receipt_date, receipt_date, channel=channel)
    return [DailyReceiptOut.from_orm_row(r) for r in rows]


@router.get("/range")
def get_receipt_range(
    facility_id: int,
    date_from: date,
    date_to: date,
    channel: Optional[str] = None,
    payer_id: Optional[str] = None,
    db: Session = Depends(get_db),
    _user=Depends(get_current_user),
):
    """Receipt summary across a date range."""
    if (date_to - date_from).days > 366:
        raise HTTPException(400, "Date range cannot exceed 366 days")
    svc = ReceiptsService(db)
    rows = svc.get_daily_receipts(facility_id, date_from, date_to, channel=channel, payer_id=payer_id)
    return [DailyReceiptOut.from_orm_row(r) for r in rows]


@router.get("/unmatched")
def get_unmatched(
    facility_id: int,
    date_from: date,
    date_to: date,
    db: Session = Depends(get_db),
    _user=Depends(get_current_user),
):
    """Unmatched / fully-denied receipts that need investigation."""
    svc = ReceiptsService(db)
    rows = svc.get_unmatched(facility_id, date_from, date_to)
    return [DailyReceiptOut.from_orm_row(r) for r in rows]


@router.post("/reconcile")
def trigger_reconcile(
    facility_id: int,
    date_from: date,
    date_to: date,
    db: Session = Depends(get_db),
    _user=Depends(get_current_user),
):
    """
    Manually trigger reconciliation for a facility + date range.
    Safe to call multiple times (upserts existing rows).
    """
    if (date_to - date_from).days > 366:
        raise HTTPException(400, "Date range cannot exceed 366 days")

    fac = db.get(Facility, facility_id)
    if not fac:
        raise HTTPException(404, f"Facility {facility_id} not found")

    svc = ReceiptsService(db)
    rows = svc.reconcile_range(facility_id, date_from, date_to, upsert=True)

    totals = {
        "rows_written": len(rows),
        "received_aed": sum(float(r.received_aed) for r in rows),
        "denied_aed":   sum(float(r.denied_aed)   for r in rows),
        "claim_count":  sum(r.claim_count          for r in rows),
    }
    return {"status": "ok", "facility": fac.facility_name, "totals": totals}


@router.get("/export")
def export_receipts_excel(
    facility_id: int,
    date_from: date,
    date_to: date,
    db: Session = Depends(get_db),
    _user=Depends(get_current_user),
):
    """
    Export daily receipts as a combined DHPO + Riayati Excel file.

    Sheet 1: Summary (one row per date → DHPO total / Riayati total / combined)
    Sheet 2: DHPO detail (per payer per day)
    Sheet 3: Riayati detail (per payer per day)
    """
    if (date_to - date_from).days > 400:
        raise HTTPException(400, "Date range cannot exceed 400 days")

    fac = db.get(Facility, facility_id)
    if not fac:
        raise HTTPException(404, f"Facility {facility_id} not found")

    svc = ReceiptsService(db)
    all_rows = svc.get_daily_receipts(facility_id, date_from, date_to)

    dhpo_rows    = [r for r in all_rows if r.channel == "DHPO"]
    riayati_rows = [r for r in all_rows if r.channel == "Riayati"]

    wb = openpyxl.Workbook()
    _build_summary_sheet(wb.active, fac, date_from, date_to, dhpo_rows, riayati_rows)
    ws_dhpo = wb.create_sheet("DHPO Detail")
    _build_detail_sheet(ws_dhpo, "DHPO", fac, dhpo_rows)
    ws_riayati = wb.create_sheet("Riayati Detail")
    _build_detail_sheet(ws_riayati, "Riayati", fac, riayati_rows)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    fname = (
        f"Receipts_{fac.facility_code}_{date_from.strftime('%Y%m%d')}"
        f"_to_{date_to.strftime('%Y%m%d')}.xlsx"
    )
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


# ── Payer mapping CRUD ──────────────────────────────────────────────────────

@router.get("/payer-mapping")
def list_payer_mappings(
    facility_id: int,
    db: Session = Depends(get_db),
    _user=Depends(get_current_user),
):
    rows = (
        db.query(ReceiptPayerMapping)
        .filter_by(facility_id=facility_id, is_active=True)
        .order_by(ReceiptPayerMapping.payer_id)
        .all()
    )
    return [
        {
            "id": r.id,
            "payer_id": r.payer_id,
            "payer_name": r.payer_name,
            "channel": r.channel,
            "gl_account_code": r.gl_account_code,
            "cost_centre": r.cost_centre,
            "tolerance_aed": float(r.tolerance_aed),
        }
        for r in rows
    ]


@router.post("/payer-mapping")
def upsert_payer_mapping(
    body: PayerMappingIn,
    db: Session = Depends(get_db),
    _user=Depends(get_current_user),
):
    existing = (
        db.query(ReceiptPayerMapping)
        .filter_by(facility_id=body.facility_id, payer_id=body.payer_id, channel=body.channel)
        .first()
    )
    if existing:
        existing.payer_name      = body.payer_name or existing.payer_name
        existing.gl_account_code = body.gl_account_code
        existing.cost_centre     = body.cost_centre
        existing.tolerance_aed   = body.tolerance_aed
        existing.is_active       = True
        db.commit()
        return {"status": "updated", "id": existing.id}
    else:
        row = ReceiptPayerMapping(
            facility_id=body.facility_id,
            payer_id=body.payer_id,
            payer_name=body.payer_name,
            channel=body.channel,
            gl_account_code=body.gl_account_code,
            cost_centre=body.cost_centre,
            tolerance_aed=body.tolerance_aed,
        )
        db.add(row)
        db.commit()
        return {"status": "created", "id": row.id}


# ── Excel sheet builders ─────────────────────────────────────────────────────

def _hdr(ws, row: int, col: int, value, width=None):
    c = ws.cell(row=row, column=col, value=value)
    c.font   = _WHITE_FONT
    c.fill   = _HEADER_FILL
    c.border = _THIN_BORDER
    c.alignment = _CENTER
    return c


def _cell(ws, row: int, col: int, value, fill=None, bold=False, num_format=None, align=_LEFT):
    c = ws.cell(row=row, column=col, value=value)
    c.font      = _BOLD_FONT if bold else _NORMAL_FONT
    c.border    = _THIN_BORDER
    c.alignment = align
    if fill:
        c.fill = fill
    if num_format:
        c.number_format = num_format
    return c


def _build_summary_sheet(ws, fac: Facility, date_from: date, date_to: date, dhpo: list, riayati: list):
    ws.title = "Summary"

    # Title
    ws.merge_cells("A1:I1")
    t = ws["A1"]
    t.value     = f"{fac.facility_name} — Daily Receipt Summary"
    t.font      = Font(bold=True, size=13, color="FFFFFF")
    t.fill      = _HEADER_FILL
    t.alignment = _CENTER

    ws.merge_cells("A2:I2")
    sub = ws["A2"]
    sub.value     = f"Period: {date_from.strftime('%d %b %Y')} → {date_to.strftime('%d %b %Y')}"
    sub.font      = Font(italic=True, size=10)
    sub.alignment = _CENTER

    # Headers row 4
    headers = [
        "Date", "DHPO Claims", "DHPO Received (AED)", "DHPO Denied (AED)",
        "Riayati Claims", "Riayati Received (AED)", "Riayati Denied (AED)",
        "Total Received (AED)", "Total Denied (AED)",
    ]
    for col, h in enumerate(headers, 1):
        _hdr(ws, 4, col, h)

    # Group by date
    from collections import defaultdict
    dhpo_by_date: dict[date, dict]    = defaultdict(lambda: {"received": 0, "denied": 0, "claims": 0})
    riayati_by_date: dict[date, dict] = defaultdict(lambda: {"received": 0, "denied": 0, "claims": 0})

    for r in dhpo:
        d = dhpo_by_date[r.receipt_date]
        d["received"] += float(r.received_aed)
        d["denied"]   += float(r.denied_aed)
        d["claims"]   += r.claim_count

    for r in riayati:
        d = riayati_by_date[r.receipt_date]
        d["received"] += float(r.received_aed)
        d["denied"]   += float(r.denied_aed)
        d["claims"]   += r.claim_count

    all_dates = sorted(set(list(dhpo_by_date.keys()) + list(riayati_by_date.keys())))
    data_row = 5
    totals = [0.0] * 6

    for d in all_dates:
        dh = dhpo_by_date.get(d, {"received": 0, "denied": 0, "claims": 0})
        ri = riayati_by_date.get(d, {"received": 0, "denied": 0, "claims": 0})
        row_vals = [
            d, dh["claims"], dh["received"], dh["denied"],
            ri["claims"], ri["received"], ri["denied"],
            dh["received"] + ri["received"],
            dh["denied"]   + ri["denied"],
        ]
        for col, val in enumerate(row_vals, 1):
            fmt = '#,##0.00' if col in (3, 4, 6, 7, 8, 9) else None
            _cell(ws, data_row, col, val, num_format=fmt, align=_CENTER)
        totals[0] += dh["received"]; totals[1] += dh["denied"]
        totals[2] += ri["received"]; totals[3] += ri["denied"]
        totals[4] += dh["received"] + ri["received"]
        totals[5] += dh["denied"]   + ri["denied"]
        data_row += 1

    # Totals row
    total_row = data_row
    _cell(ws, total_row, 1, "TOTAL", bold=True, fill=PatternFill("solid", fgColor="E8F5E9"), align=_CENTER)
    ws.merge_cells(start_row=total_row, start_column=1, end_row=total_row, end_column=2)
    for ci, val in zip([3, 4, 6, 7, 8, 9], totals):
        _cell(ws, total_row, ci, val, bold=True, num_format='#,##0.00',
              fill=PatternFill("solid", fgColor="E8F5E9"), align=_CENTER)

    _set_col_widths(ws, [14, 13, 20, 18, 15, 22, 20, 20, 18])
    ws.freeze_panes = "A5"


def _build_detail_sheet(ws, channel: str, fac: Facility, rows: list[DailyReceipt]):
    fill = _DHPO_FILL if channel == "DHPO" else _RIAYATI_FILL

    ws.merge_cells("A1:J1")
    t = ws["A1"]
    t.value     = f"{fac.facility_name} — {channel} Receipts Detail"
    t.font      = Font(bold=True, size=12, color="FFFFFF")
    t.fill      = _HEADER_FILL
    t.alignment = _CENTER

    headers = [
        "Date", "Payer ID", "Payer Name", "Claims",
        "Expected (AED)", "Received (AED)", "Denied (AED)",
        "Patient Share (AED)", "Payment References", "Status",
    ]
    for col, h in enumerate(headers, 1):
        _hdr(ws, 3, col, h)

    for data_row, r in enumerate(rows, 4):
        st_fill = STATUS_FILL.get(r.status)
        refs = json.loads(r.payment_references) if r.payment_references else []

        vals = [
            r.receipt_date,
            r.payer_id,
            r.payer_name or "",
            r.claim_count,
            float(r.expected_aed),
            float(r.received_aed),
            float(r.denied_aed),
            float(r.patient_share_aed),
            ", ".join(refs),
            r.status.upper(),
        ]
        for col, val in enumerate(vals, 1):
            fmt = '#,##0.00' if col in (5, 6, 7, 8) else None
            row_fill = st_fill if col == 10 else (fill if data_row % 2 == 0 else None)
            _cell(ws, data_row, col, val, fill=row_fill, num_format=fmt, align=_CENTER if col != 3 else _LEFT)

    _set_col_widths(ws, [14, 14, 28, 9, 17, 17, 15, 18, 40, 13])
    ws.freeze_panes = "A4"
