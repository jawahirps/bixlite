"""
Ghaf App — Advanced Excel Report Exports.

Six report types available at GET /api/reports/... (direct download)
and POST /api/reports/generate/{type} (staged to report_jobs queue).

  1. claim-summary       → one row per claim with financials & RA
  2. claim-activity      → service-line (CPT) detail per claim
  3. denial-analysis     → denial breakdown grouped by payer + code
  4. resubmission-aging  → AR aging with bucket classification
  5. remittance-activity → activity-level remittance detail
  6. denial-query        → denied claims with CPT code and RA info

All endpoints accept:
  facility_id, payer_id, encounter_type, date_from, date_to,
  date_field ("treatment_date" | "remittance_date")

date_field controls whether date_from/date_to filter on:
  - encounter_start  (treatment date, the default)
  - remittance_date  (RA transaction date)
"""
from __future__ import annotations

import io
import json as json_mod
from datetime import date, datetime, timezone as _tz
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse
from sqlalchemy import and_, func, cast, Date, case
from sqlalchemy.orm import Session, aliased

import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

from ..database import get_db
from ..models import (
    Claim, ClaimActivity, ClaimDiagnosis, Facility, Remittance,
    RemittanceActivityDetail, ReportJob, RawXmlStore,
)
from ..security import get_current_user

router = APIRouter(prefix="/api/reports", tags=["reports"])

# ---------------------------------------------------------------------------
# DHA Denial Code lookup  (Code → description / category / type)
# Source: "Denial Codes 29Oct2024.xlsx"
# category = code family prefix (AUTH, CLAI, MNEC, …)
# type     = Technical | Medical
# ---------------------------------------------------------------------------
DENIAL_CODE_MAP: dict[str, dict] = {
    "AUTH-001": {"description": "Prior approval is required and was not obtained", "category": "AUTH", "type": "Technical"},
    "AUTH-002": {"description": "Services performed are inconsistent with authorized or approved services", "category": "AUTH", "type": "Technical"},
    "AUTH-003": {"description": "PriorAuthorizationNumber is invalid", "category": "AUTH", "type": "Technical"},
    "AUTH-004": {"description": "Service(s) is (are) performed outside authorization validity date", "category": "AUTH", "type": "Technical"},
    "AUTH-005": {"description": "Claim information is inconsistent with pre-certified/authorized services", "category": "AUTH", "type": "Technical"},
    "AUTH-006": {"description": "Alert drug - drug interaction or drug is contra-indicated", "category": "AUTH", "type": "Technical"},
    "AUTH-007": {"description": "Drug duplicate therapy", "category": "AUTH", "type": "Technical"},
    "AUTH-008": {"description": "Inappropriate drug dose", "category": "AUTH", "type": "Technical"},
    "AUTH-009": {"description": "Prescription out of date", "category": "AUTH", "type": "Technical"},
    "AUTH-010": {"description": "Authorization request overlaps or is within the period of another paid claim or approved authorization", "category": "AUTH", "type": "Technical"},
    "AUTH-011": {"description": "Waiting period on pre-existing / specific conditions", "category": "AUTH", "type": "Technical"},
    "AUTH-012": {"description": "Request for information", "category": "AUTH", "type": "Technical"},
    "AUTH-013": {"description": "Missing eRx Reference number", "category": "AUTH", "type": "Technical"},
    "AUTH-014": {"description": "Medication has been added to a prior request that is not in line with Pharmacist Edit Restrictions", "category": "AUTH", "type": "Technical"},
    "BENX-001": {"description": "Service / Medication is above AED threshold", "category": "BENX", "type": "Technical"},
    "BENX-002": {"description": "Benefit maximum for this time period or occurrence has been reached", "category": "BENX", "type": "Technical"},
    "BENX-005": {"description": "Annual limit/sublimit amount exceeded", "category": "BENX", "type": "Technical"},
    "BSMA-001": {"description": "Member not covered under Basmah", "category": "BSMA", "type": "Technical"},
    "BSMA-002": {"description": "Claims is not eligible under Basmah Fund", "category": "BSMA", "type": "Technical"},
    "BSMA-003": {"description": "Prior approval is required but not taken (Basmah)", "category": "BSMA", "type": "Technical"},
    "CLAI-007": {"description": "Claim is a work-related injury/illness and thus the liability of the employer", "category": "CLAI", "type": "Technical"},
    "CLAI-008": {"description": "Claim overlaps inpatient stay. Resubmit only those services rendered outside the inpatient stay", "category": "CLAI", "type": "Technical"},
    "CLAI-009": {"description": "Date of birth follows the date of service", "category": "CLAI", "type": "Technical"},
    "CLAI-010": {"description": "Date of death precedes the date of service", "category": "CLAI", "type": "Technical"},
    "CLAI-011": {"description": "Inpatient admission spans multiple rate periods. Resubmit separate claims", "category": "CLAI", "type": "Technical"},
    "CLAI-012": {"description": "Submission not compliant with contractual agreement between provider & payer", "category": "CLAI", "type": "Technical"},
    "CLAI-013": {"description": "Missing an Observation (Dental Tooth #)", "category": "CLAI", "type": "Technical"},
    "CLAI-014": {"description": "Claim not compliant with Resubmission type (used only for resubmissions)", "category": "CLAI", "type": "Technical"},
    "CLAI-017": {"description": "Services not available on direct billing", "category": "CLAI", "type": "Technical"},
    "CLAI-018": {"description": "Claims Recalled By Provider", "category": "CLAI", "type": "Technical"},
    "CLAI-019": {"description": "Incorrect IR-DRG code", "category": "CLAI", "type": "Technical"},
    "CLAI-020": {"description": "Missing IR-DRG code", "category": "CLAI", "type": "Technical"},
    "CLAI-021": {"description": "Incorrect DRG payment parameter", "category": "CLAI", "type": "Technical"},
    "CLAI-022": {"description": "Missing DRG payment parameter", "category": "CLAI", "type": "Technical"},
    "CLAI-023": {"description": "Submission not compliant with contractual agreement between non-network physician & payer", "category": "CLAI", "type": "Technical"},
    "CODE-001": {"description": "Missing, incomplete or invalid principal diagnosis", "category": "CODE", "type": "Technical"},
    "CODE-002": {"description": "Missing, incomplete or invalid service code", "category": "CODE", "type": "Technical"},
    "CODE-006": {"description": "Diagnosis is inconsistent with the procedure", "category": "CODE", "type": "Medical"},
    "CODE-010": {"description": "Activity/diagnosis inconsistent with clinician specialty", "category": "CODE", "type": "Technical"},
    "CODE-012": {"description": "Encounter type inconsistent with service(s) / diagnosis", "category": "CODE", "type": "Medical"},
    "CODE-013": {"description": "Invalid principal diagnosis", "category": "CODE", "type": "Medical"},
    "CODE-014": {"description": "Activity/diagnosis is inconsistent with the patient's age/gender", "category": "CODE", "type": "Medical"},
    "CODE-015": {"description": "Activity/diagnosis is inconsistent with the provider type", "category": "CODE", "type": "Technical"},
    "CODE-016": {"description": "Activity/diagnosis inconsistent with Service", "category": "CODE", "type": "Medical"},
    "COPY-001": {"description": "Deductible/co-pay not collected from member", "category": "COPY", "type": "Technical"},
    "COPY-002": {"description": "Incorrect Patient Share", "category": "COPY", "type": "Technical"},
    "COPY-003": {"description": "Missing Patient Share", "category": "COPY", "type": "Technical"},
    "DRG-001": {"description": "Incorrect DRG code", "category": "DRG", "type": "Technical"},
    "DRG-002": {"description": "Missing DRG code", "category": "DRG", "type": "Technical"},
    "DRG-003": {"description": "Incorrect Billing Regime", "category": "DRG", "type": "Technical"},
    "DRG-004": {"description": "Missing Activities", "category": "DRG", "type": "Technical"},
    "DRG-005": {"description": "Missing Observation (ActivityCost and/or File Attachment)", "category": "DRG", "type": "Technical"},
    "DUPL-001": {"description": "Claim is a duplicate based on service codes and dates", "category": "DUPL", "type": "Technical"},
    "DUPL-002": {"description": "Payment already made for same/similar service within set time frame", "category": "DUPL", "type": "Technical"},
    "ELIG-001": {"description": "Patient is not a covered member", "category": "ELIG", "type": "Technical"},
    "ELIG-005": {"description": "Services performed after the last date of coverage", "category": "ELIG", "type": "Technical"},
    "ELIG-006": {"description": "Services performed prior to the effective date of coverage", "category": "ELIG", "type": "Technical"},
    "ELIG-007": {"description": "Services performed by a non-network provider", "category": "ELIG", "type": "Technical"},
    "ELIG-008": {"description": "Services performed by a non-network physician", "category": "ELIG", "type": "Technical"},
    "MNEC-003": {"description": "Service is not clinically indicated based on good clinical practice", "category": "MNEC", "type": "Medical"},
    "MNEC-004": {"description": "Service is not clinically indicated based on good clinical practice, without additional supporting diagnoses/activities", "category": "MNEC", "type": "Medical"},
    "MNEC-005": {"description": "Service/supply may be appropriate, but too frequent", "category": "MNEC", "type": "Medical"},
    "MNEC-006": {"description": "Alternative service should have been utilized", "category": "MNEC", "type": "Medical"},
    "MNEC-007": {"description": "Service is not clinically indicated based on good clinical practice, without additional supporting documentation", "category": "MNEC", "type": "Medical"},
    "NCOV-001": {"description": "Diagnosis(es) is (are) not covered", "category": "NCOV", "type": "Medical"},
    "NCOV-002": {"description": "Pre-existing conditions are not covered", "category": "NCOV", "type": "Technical"},
    "NCOV-003": {"description": "Service(s) is (are) not covered", "category": "NCOV", "type": "Medical"},
    "NCOV-009": {"description": "Dental treatments are not covered", "category": "NCOV", "type": "Medical"},
    "NCOV-010": {"description": "Elective procedure is not covered", "category": "NCOV", "type": "Medical"},
    "NCOV-012": {"description": "Eye exams and treatments are not covered", "category": "NCOV", "type": "Medical"},
    "NCOV-014": {"description": "Hearing services are not covered", "category": "NCOV", "type": "Medical"},
    "NCOV-018": {"description": "Immunizations are not covered", "category": "NCOV", "type": "Technical"},
    "NCOV-019": {"description": "Preventative care is not covered", "category": "NCOV", "type": "Technical"},
    "NCOV-020": {"description": "Services resulting from attempted suicide or self-infliction are not covered", "category": "NCOV", "type": "Technical"},
    "NCOV-021": {"description": "Vaccinations are not covered", "category": "NCOV", "type": "Medical"},
    "NCOV-024": {"description": "Chronic Condition Service(s) is (are) not covered", "category": "NCOV", "type": "Medical"},
    "NCOV-025": {"description": "Service(s) is (are) not performed (used after audit)", "category": "NCOV", "type": "Technical"},
    "NCOV-027": {"description": "Non Coverage of certain medication as per policy", "category": "NCOV", "type": "Technical"},
    "PRCE-001": {"description": "Calculation discrepancy", "category": "PRCE", "type": "Technical"},
    "PRCE-002": {"description": "Payment is included in the allowance for another service", "category": "PRCE", "type": "Technical"},
    "PRCE-003": {"description": "Recovery of Payment", "category": "PRCE", "type": "Technical"},
    "PRCE-006": {"description": "Consultation within free follow up period", "category": "PRCE", "type": "Technical"},
    "PRCE-007": {"description": "Service has no contract price", "category": "PRCE", "type": "Technical"},
    "PRCE-008": {"description": "Multiple procedure payment rules incorrectly applied", "category": "PRCE", "type": "Technical"},
    "PRCE-009": {"description": "Charges inconsistent with clinician specialty", "category": "PRCE", "type": "Technical"},
    "PRCE-010": {"description": "Use bundled code", "category": "PRCE", "type": "Technical"},
    "PRCE-011": {"description": "Discount discrepancy", "category": "PRCE", "type": "Technical"},
    "PRCE-012": {"description": "Recovery of Payment – DHIC Authorized", "category": "PRCE", "type": "Technical"},
    "SURC-001": {"description": "Severe drug - drug interaction", "category": "SURC", "type": "Medical"},
    "SURC-002": {"description": "Severe drug - age contraindication", "category": "SURC", "type": "Medical"},
    "SURC-003": {"description": "Severe drug - gender contraindication", "category": "SURC", "type": "Medical"},
    "SURC-004": {"description": "Severe drug - diaganosis contraindication", "category": "SURC", "type": "Medical"},
    "SURC-005": {"description": "Severe procedure/service - diagnosis contraindication", "category": "SURC", "type": "Medical"},
    "SURC-006": {"description": "Severe procedure/service - drug contraindication", "category": "SURC", "type": "Medical"},
    "SURC-007": {"description": "Severe procedure/service - procedure contraindication", "category": "SURC", "type": "Medical"},
    "SURC-008": {"description": "Serious safety issue with drug dose", "category": "SURC", "type": "Medical"},
    "TIME-001": {"description": "Time limit for submission has expired", "category": "TIME", "type": "Technical"},
    "TIME-002": {"description": "Requested additional information was not received or was not received within time limit", "category": "TIME", "type": "Technical"},
    "TIME-003": {"description": "Appeal procedures not followed or time limits not met", "category": "TIME", "type": "Technical"},
    "WRNG-001": {"description": "Wrong submission, receiver is not responsible for the payer within this transaction submission.", "category": "WRNG", "type": "Technical"},
}


def _lookup_denial(code: Optional[str]) -> tuple[str, str, str]:
    """Return (description, category, type) for a denial code, or ('','','')."""
    if not code:
        return "", "", ""
    entry = DENIAL_CODE_MAP.get(code.strip(), {})
    return entry.get("description", ""), entry.get("category", ""), entry.get("type", "")


# ---------------------------------------------------------------------------
# Clinician lookup  (eClaimLink ID → Professional Name)
# Loaded once at import time from data/clinicians.xlsx (both DHA_HRD + MOH sheets)
# ---------------------------------------------------------------------------
import os as _os
import openpyxl as _openpyxl

def _load_clinician_map() -> dict[str, str]:
    data_path = _os.path.join(
        _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
        "..", "data", "clinicians.xlsx"
    )
    data_path = _os.path.normpath(data_path)
    cmap: dict[str, str] = {}
    if not _os.path.exists(data_path):
        return cmap
    try:
        wb = _openpyxl.load_workbook(data_path, read_only=True, data_only=True)
        for sheet in wb.sheetnames:
            ws = wb[sheet]
            for row in ws.iter_rows(min_row=2, values_only=True):
                if row and row[0] and row[1]:
                    cmap[str(row[0]).strip()] = str(row[1]).strip()
        wb.close()
    except Exception:
        pass
    return cmap

_CLINICIAN_MAP: dict[str, str] = _load_clinician_map()


def _clinician_name(cid: Optional[str]) -> str:
    if not cid or cid.strip().upper() == "UNKNOWN":
        return ""
    return _CLINICIAN_MAP.get(cid.strip(), cid.strip())


# ---------------------------------------------------------------------------
# Payer / Receiver lookup
# Sources:
#   data/payers.xlsx   — standard eClaimLink codes (INS001-INS137, TPA001-TPA036, DHA…)
#   data/self_paid.xlsx — self-paid employer codes (SF/SP) with their TPA
#
# Logic:
#   Receiver ID = claim.receiver_id if stored; else for SF/SP payers → their TPA;
#                 else payer_id itself (direct-payer case, Receiver == Payer)
#   Payer Name  = lookup payer_id in combined map
#   Receiver Name = lookup derived receiver_id in combined map
# ---------------------------------------------------------------------------
def _load_payer_map() -> tuple[dict[str, str], dict[str, str]]:
    """Returns (payer_name_map, sp_to_tpa_map)."""
    data_dir = _os.path.normpath(_os.path.join(
        _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
        "..", "data",
    ))
    pmap: dict[str, str] = {}
    sp_tpa: dict[str, str] = {}

    # Standard payers + TPA codes (col 0 = ID, col 1 = Name)
    payers_path = _os.path.join(data_dir, "payers.xlsx")
    if _os.path.exists(payers_path):
        try:
            wb = _openpyxl.load_workbook(payers_path, read_only=True, data_only=True)
            ws = wb.active
            for row in ws.iter_rows(min_row=2, values_only=True):
                if row and row[0] and row[1]:
                    pmap[str(row[0]).strip()] = str(row[1]).strip()
            wb.close()
        except Exception:
            pass

    # Self-paid employer codes (col 0 = SF/SP ID, col 1 = Name, col 2 = TPA)
    sp_path = _os.path.join(data_dir, "self_paid.xlsx")
    if _os.path.exists(sp_path):
        try:
            wb = _openpyxl.load_workbook(sp_path, read_only=True, data_only=True)
            ws = wb.active
            for row in ws.iter_rows(min_row=2, values_only=True):
                if row and row[0]:
                    code = str(row[0]).strip()
                    name = str(row[1]).strip() if row[1] else code
                    tpa  = str(row[2]).strip() if len(row) > 2 and row[2] else ""
                    pmap[code] = name
                    if tpa:
                        sp_tpa[code] = tpa
            wb.close()
        except Exception:
            pass

    return pmap, sp_tpa


_PAYER_MAP: dict[str, str]
_SP_TPA_MAP: dict[str, str]
_PAYER_MAP, _SP_TPA_MAP = _load_payer_map()


def _payer_name(code: Optional[str]) -> str:
    if not code:
        return ""
    return _PAYER_MAP.get(code.strip(), code.strip())


def _submission_level(resubmission_type: Optional[str]) -> str:
    """
    Map the stored resubmission_type value to a human-readable label.
      NULL / empty         → 'Submission'
      Correction           → '1 Resubmission'
      InternalComplaint    → '1 Resubmission'
      Reconciliation       → 'Reconciliation Resubmission'
      anything else        → '1 Resubmission'
    """
    if not resubmission_type:
        return "Submission"
    rt = resubmission_type.strip()
    if rt.lower() == "reconciliation":
        return "Reconciliation Resubmission"
    return "1 Resubmission"


def _derive_receiver_id(raw_receiver_id: Optional[str], payer_id: Optional[str]) -> str:
    """
    Return the correct Receiver ID:
      1. Use stored receiver_id if it's a real eClaimLink code (not a facility DHA-F- license)
      2. For SF/SP self-paid payers, receiver = their TPA code
      3. Otherwise receiver == payer (direct-payer submission)
    """
    rid = (raw_receiver_id or "").strip()
    # Facility DHA license IDs start with DHA-F- — these are NOT valid receivers
    if rid and not rid.upper().startswith("DHA-F-"):
        return rid
    pid = (payer_id or "").strip()
    if pid in _SP_TPA_MAP:
        return _SP_TPA_MAP[pid]
    return pid

# ---------------------------------------------------------------------------
# Styling
# ---------------------------------------------------------------------------
HEADER_FONT = Font(name="Calibri", bold=True, color="FFFFFF", size=10)
HEADER_FILL = PatternFill("solid", fgColor="1E3A5F")
ALT_FILL    = PatternFill("solid", fgColor="EEF2F7")
NORMAL_FONT = Font(name="Calibri", size=10)
THIN        = Side(style="thin", color="CCCCCC")
CELL_BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)

REPORT_LABELS = {
    "claim-summary":            "Claim Summary Report",
    "claim-activity":           "Claim Activity Report",
    "denial-analysis":          "Denial Analysis Report",
    "resubmission-aging":       "Resubmission & Aging Report",
    "remittance-activity":      "Remittance Activity Report",
    "denial-query":             "Denial Query Report",
    "claim-summary-v2":         "Claim Summary V2 Report",
    "claim-activity-v2":        "Claim Activity V2 Report",
    "claim-cycle-v2":           "Claim Cycle V2 Report",
    "remittance-activity-v2":   "Remittance Activity V2 Report",
    "denial-scrub-action":      "Denial Scrub Action Report",
}


def _excel_response(wb: openpyxl.Workbook, filename: str) -> StreamingResponse:
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return StreamingResponse(
        buf,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        },
    )


def _wb_to_bytes(wb: openpyxl.Workbook) -> bytes:
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _write_headers(ws, cols: list[str]) -> None:
    for i, name in enumerate(cols, 1):
        cell = ws.cell(row=1, column=i, value=name)
        cell.font      = HEADER_FONT
        cell.fill      = HEADER_FILL
        cell.border    = CELL_BORDER
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    ws.row_dimensions[1].height = 28


def _write_row(ws, row_idx: int, values: list, alt: bool = False) -> None:
    fill = ALT_FILL if alt else None
    for i, val in enumerate(values, 1):
        cell = ws.cell(row=row_idx, column=i, value=val)
        cell.font   = NORMAL_FONT
        cell.border = CELL_BORDER
        if fill:
            cell.fill = fill
        if isinstance(val, (int, float)):
            cell.alignment = Alignment(horizontal="right")


def _auto_width(ws, cols: list[str]) -> None:
    for i, name in enumerate(cols, 1):
        ws.column_dimensions[get_column_letter(i)].width = min(len(name) + 4, 32)


# DHA XML encounter type codes → human-readable labels
_ENCOUNTER_TYPE_LABELS: dict[str, str] = {
    "1": "OP",   # Outpatient
    "2": "IP",   # Inpatient
    "3": "ER",   # Emergency
    "4": "OT",   # Others
}


def _enc_label(code: Optional[str]) -> str:
    """Map DHA numeric encounter type code to a readable label."""
    if not code:
        return ""
    return _ENCOUNTER_TYPE_LABELS.get(str(code).strip(), str(code).strip())


def _fmt_date(d) -> str:
    if d is None:
        return ""
    return d.strftime("%d/%m/%Y") if hasattr(d, "strftime") else str(d)


def _f(v) -> float | str:
    if v is None:
        return ""
    try:
        return float(v)
    except Exception:
        return str(v)


def _claim_gateway(source_channel: Optional[str], sender_id: Optional[str] = None) -> str:
    """Return human-readable gateway label from source_channel (Riayati split by sender licence: HF-… = SHA)."""
    if source_channel == "API":
        if sender_id:
            return "Riayati SHA" if sender_id.upper().startswith("HF-") else "Riayati RHA (MOH)"
        return "Riayati (MOH)"
    if source_channel == "FILE":
        return "DHA eClaimLink"
    return source_channel or "Unknown"


def _apply_filters(q, facility_id, payer_id, encounter_type,
                   date_from, date_to,
                   date_field: str = "treatment_date",
                   rem_model=None,
                   facility_ids: str = None):
    """Apply standard filters. date_field controls which date column is used.

    rem_model — pass the aliased/base Remittance if already joined and
    date_field == 'remittance_date' should use it.
    facility_ids — comma-separated IDs for multi-facility filtering.
    """
    if facility_ids:
        ids = [int(x) for x in facility_ids.split(",") if x.strip().isdigit()]
        if ids:
            q = q.filter(Claim.facility_id.in_(ids))
    elif facility_id:
        q = q.filter(Claim.facility_id == facility_id)
    if payer_id:
        q = q.filter(Claim.payer_id == payer_id)
    if encounter_type:
        q = q.filter(Claim.encounter_type == encounter_type)

    if date_field == "remittance_date" and rem_model is not None:
        if date_from:
            q = q.filter(rem_model.remittance_date >= date_from)
        if date_to:
            q = q.filter(rem_model.remittance_date <= date_to)
    else:
        # Default: filter on treatment date (encounter_start)
        if date_from:
            q = q.filter(cast(Claim.encounter_start, Date) >= date_from)
        if date_to:
            q = q.filter(cast(Claim.encounter_start, Date) <= date_to)
    return q


# ---------------------------------------------------------------------------
# 1. CLAIM SUMMARY
# ---------------------------------------------------------------------------
CLAIM_SUMMARY_COLS = [
    "Facility Name", "Claim Gateway", "Claim Number", "Receiver Id", "Receiver Name",
    "Payer ID", "Payer Name", "Patient ID", "Member Id",
    "Treatment Date", "Date Of Admission", "Submission Date", "Encounter Type",
    "Clinician Name", "Service Year", "Service Month", "Submission Level",
    "Net Amt - Initial Sub", "RA Net", "Net Amt - Resubmission",
    "Approved Amt", "Initial Sub Rejected Amt", "Rejected Amt - Resubmission",
    "Unsettled Amt", "Payment Status", "Denial Code", "Denial Description",
    "Payment Ref", "Settlement Date", "ID Payer",
    "Submission File", "RA File",
    "RA Date", "TAT (Days)", "Diagnosis",
]


def _build_diagnosis_map(db, claim_ids_by_facility: dict) -> dict:
    """
    Returns {(facility_id, claim_id_str): "formatted_diagnosis_string"}.
    Format mirrors reference col AH:
      PRIMARY_CODE:Desc <--Principal || Secondary--> SEC1_CODE:Desc,SEC2_CODE:Desc

    ClaimDiagnosis links to Claim via claim_id_fk (integer PK), so we join
    through Claim to resolve string claim_id.
    """
    if not claim_ids_by_facility:
        return {}

    from collections import defaultdict
    from sqlalchemy import tuple_

    # Collect all string claim_ids per facility
    fac_ids = list(claim_ids_by_facility.keys())

    # Join ClaimDiagnosis → Claim to get string claim_id.
    # Filter by facility_id only (avoids large claim_id IN clause that hits
    # PostgreSQL's 65535 bind-parameter limit on large datasets).
    claim_alias = aliased(Claim)
    diag_rows = (
        db.query(
            ClaimDiagnosis,
            claim_alias.claim_id.label("str_claim_id"),
            claim_alias.facility_id.label("str_fac_id"),
        )
        .join(claim_alias, ClaimDiagnosis.claim_id_fk == claim_alias.id)
        .filter(
            claim_alias.facility_id.in_(fac_ids),
            claim_alias.resubmission_key == "",  # use initial submission diagnoses only
        )
        .order_by(
            claim_alias.facility_id,
            claim_alias.claim_id,
            case(
                (ClaimDiagnosis.diagnosis_type == "Primary", 0),
                (ClaimDiagnosis.diagnosis_type == "Admitting", 1),
                else_=2,
            ),
            ClaimDiagnosis.id,
        )
        .all()
    )

    grouped: dict = defaultdict(list)
    for d, str_cid, fac_id in diag_rows:
        grouped[(fac_id, str_cid)].append(d)

    result = {}
    for key, diags in grouped.items():
        primary = [d for d in diags if d.diagnosis_type in ("Primary", "Admitting")]
        secondary = [d for d in diags if d.diagnosis_type == "Secondary"]

        def _fmt_diag(d):
            code = d.diagnosis_code or ""
            desc = d.diagnosis_description or ""
            return f"{code}:{desc}" if desc else code

        if primary:
            pri_str = ",".join(_fmt_diag(d) for d in primary)
            if secondary:
                sec_str = ",".join(_fmt_diag(d) for d in secondary)
                result[key] = f"{pri_str} <--Principal || Secondary--> {sec_str}"
            else:
                result[key] = pri_str
        elif secondary:
            result[key] = ",".join(_fmt_diag(d) for d in secondary)

    return result


def _build_claim_summary(db, facility_id, payer_id, encounter_type,
                         date_from, date_to, date_field,
                      facility_ids=None):
    # fac_map: id → (facility_name, license_id)  — used for receiver fallback
    fac_map = {f.id: (f.facility_name, f.license_id) for f in db.query(Facility).all()}

    # ── Ranked remittances per claim ────────────────────────────────────────────
    # ra_rank=1 → 1st adjudication (initial submission)
    # ra_rank=2 → 2nd adjudication (after resubmission)
    rem_ranked_sq = (
        db.query(
            Remittance.facility_id,
            Remittance.claim_id,
            Remittance.id.label("rem_id"),
            func.row_number().over(
                partition_by=[Remittance.facility_id, Remittance.claim_id],
                order_by=Remittance.id,
            ).label("ra_rank"),
        ).subquery()
    )
    row_rem = aliased(Remittance)

    # ── Resubmission net per (facility_id, claim_id) ────────────────────────────
    resub_net_sq = (
        db.query(
            Claim.facility_id.label("fid"),
            Claim.claim_id.label("cid"),
            Claim.net_amount.label("resub_net"),
        )
        .filter(Claim.resubmission_key != "")
        .subquery()
    )

    # ── Main query: claim + ranked RA + resub_net (NO XML store joins) ──────────
    # XML filenames are fetched separately via bulk lookup to avoid slow JOINs.
    q = (
        db.query(Claim, row_rem, resub_net_sq.c.resub_net)
        .outerjoin(rem_ranked_sq, and_(
            Claim.facility_id  == rem_ranked_sq.c.facility_id,
            Claim.claim_id     == rem_ranked_sq.c.claim_id,
            rem_ranked_sq.c.ra_rank == case(
                (Claim.resubmission_key == "",              1),
                (Claim.resubmission_key == "reconciliation", 3),
                else_=2,
            ),
        ))
        .outerjoin(row_rem, row_rem.id == rem_ranked_sq.c.rem_id)
        .outerjoin(resub_net_sq, and_(
            Claim.facility_id      == resub_net_sq.c.fid,
            Claim.claim_id         == resub_net_sq.c.cid,
            Claim.resubmission_key == "",
        ))
    )
    q = _apply_filters(q, facility_id, payer_id, encounter_type,
                       date_from, date_to, date_field, rem_model=row_rem,
                       facility_ids=facility_ids)
    rows = q.order_by(
        Claim.submission_date.desc(),
        Claim.claim_id,
        Claim.resubmission_key,
    ).all()

    # ── Bulk XML filename lookup (claim XML + RA XML) ────────────────────────────
    # Collect all raw_xml_ids from claims and remittances, fetch in one query.
    xml_ids: set = set()
    for claim, rem, _ in rows:
        if claim.raw_xml_id:
            xml_ids.add(claim.raw_xml_id)
        if rem and rem.raw_xml_id:
            xml_ids.add(rem.raw_xml_id)
    xml_filename_map: dict = {}
    if xml_ids:
        for xs in db.query(RawXmlStore.id, RawXmlStore.source_filename).filter(
            RawXmlStore.id.in_(xml_ids)
        ).all():
            xml_filename_map[xs.id] = xs.source_filename or ""

    # ── Rank-1 RA fallback for resubmission rows awaiting adjudication ──────────
    # When a resubmission has no rank-2 RA yet, show the original (rank-1) RA
    # so the user can see the denial reason that triggered the resubmission.
    from sqlalchemy import tuple_ as _tuple_
    resub_pairs = [
        (c.facility_id, c.claim_id)
        for c, _, _ in rows
        if c.resubmission_key != ""
    ]
    fallback_ra_map: dict = {}
    if resub_pairs:
        all_rank1 = (
            db.query(Remittance)
            .filter(
                _tuple_(Remittance.facility_id, Remittance.claim_id).in_(resub_pairs)
            )
            .order_by(Remittance.facility_id, Remittance.claim_id, Remittance.id)
            .all()
        )
        _seen: set = set()
        for _r in all_rank1:
            _k = (_r.facility_id, _r.claim_id)
            if _k not in _seen:
                fallback_ra_map[_k] = _r
                _seen.add(_k)

    # ── Initial submission date map: (facility_id, claim_id) → submission_date ──
    # TAT is always measured from the INITIAL submission, even on resubmission rows.
    # Query initial submission dates by facility_id only (avoids PostgreSQL's
    # 65535 bind-parameter limit when using tuple_ IN with large claim sets).
    init_sub_map: dict = {}
    fac_ids_in_rows = list({c.facility_id for c, _, _ in rows})
    if fac_ids_in_rows:
        init_claims = (
            db.query(Claim.facility_id, Claim.claim_id, Claim.submission_date)
            .filter(
                Claim.facility_id.in_(fac_ids_in_rows),
                Claim.resubmission_key == "",
            )
            .all()
        )
        for ic in init_claims:
            init_sub_map[(ic.facility_id, ic.claim_id)] = ic.submission_date

    # ── Unassigned RA paid ──────────────────────────────────────────────────────
    # Each row joins only the RA at its fixed rank (initial→1, resub→2, recon→3).
    # Remittances at any other rank would otherwise be dropped from Approved Amt:
    #   • extra re-adjudications (more RAs than submissions) → added to the row
    #     with the highest rank below that RA (the submission it re-adjudicated);
    #   • RAs before the first row present (initial submission file never
    #     fetched) → added to the claim's first row.
    # Keeps the claim's total Approved equal to the sum of all its RA paid,
    # which is what ClaimCycleV2 "Paid Amount" reports.
    from collections import defaultdict as _dd
    _row_rank = lambda c: 1 if c.resubmission_key == "" else (3 if c.resubmission_key == "reconciliation" else 2)
    claim_row_ranks: dict = _dd(set)
    for c, _, _ in rows:
        claim_row_ranks[(c.facility_id, c.claim_id)].add(_row_rank(c))
    extra_paid: dict = _dd(float)   # (facility_id, claim_id, row_rank) → paid
    if fac_ids_in_rows:
        _ra_rows = (
            db.query(Remittance.facility_id, Remittance.claim_id, Remittance.paid_amount)
            .filter(Remittance.facility_id.in_(fac_ids_in_rows))
            .order_by(Remittance.facility_id, Remittance.claim_id, Remittance.id)
            .all()
        )
        _rank_ctr: dict = _dd(int)
        for _r in _ra_rows:
            _k = (_r.facility_id, _r.claim_id)
            _rank_ctr[_k] += 1
            ranks = claim_row_ranks.get(_k)
            k = _rank_ctr[_k]
            if not ranks or k in ranks:
                continue
            lower = [x for x in ranks if x < k]
            target = max(lower) if lower else min(ranks)
            extra_paid[(_k[0], _k[1], target)] += float(_r.paid_amount or 0)

    # ── Diagnosis map: (facility_id, claim_id) → formatted string ──────────────
    fac_claims: dict = _dd(set)
    for claim, _, _ in rows:
        fac_claims[claim.facility_id].add(claim.claim_id)
    diag_map = _build_diagnosis_map(db, dict(fac_claims))

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Claim Summary"
    ws.freeze_panes = "A2"
    _write_headers(ws, CLAIM_SUMMARY_COLS)

    for row_idx, (claim, rem, resub_net) in enumerate(rows, start=2):
        fac_name, fac_license = fac_map.get(claim.facility_id, ("", ""))
        sub_level = _submission_level(claim.resubmission_type)
        is_resub  = claim.resubmission_key != ""

        # For resubmissions with no rank-2 RA yet, fall back to rank-1 RA so
        # the user can see the original denial reason that triggered the resubmission.
        fallback_rem = (
            fallback_ra_map.get((claim.facility_id, claim.claim_id))
            if is_resub and rem is None else None
        )
        # Use fallback only for denial/context columns; rem remains None so
        # financial aggregations correctly reflect "no second RA received yet".
        ctx_rem = rem or fallback_rem

        # ── Financial calculations ─────────────────────────────────────────────
        claim_net = float(claim.net_amount) if claim.net_amount is not None else None
        # RA Net: rank-appropriate RA net; for awaiting resubmission → 0.0
        ra_net    = (
            float(rem.net_amount) if rem and rem.net_amount is not None
            else (claim_net if rem else 0.0)
        )
        paid      = float(rem.paid_amount) if rem and rem.paid_amount is not None else None
        _extra    = extra_paid.get((claim.facility_id, claim.claim_id, _row_rank(claim)), 0.0)
        if _extra and paid is not None:
            paid = round(paid + _extra, 2)

        # "Net Amt - Initial Sub": always from claims.net_amount for initial rows.
        # For resubmission rows: always 0 in this column.
        net_sub = 0.0 if is_resub else claim_net

        # Row awaiting its own RA but holding paid from an earlier RA (initial
        # submission file missing): show that paid in Approved only.
        approved   = _f(paid) if paid is not None else (_f(_extra) if _extra else 0.0)
        unsettled  = ""
        pay_status = ""

        # Net Amt - Resubmission: only on resubmission rows (matches old report)
        resub_col = _f(claim_net) if is_resub and claim_net is not None else 0.0

        # Use claims.net_amount for all rejected/unsettled calculations.
        this_net = claim_net

        if this_net is not None and paid is not None:
            rejected_amt = round(max(0.0, this_net - paid), 2)
            if paid >= this_net:
                pay_status = "Fully Paid"
            elif paid == 0.0:
                pay_status = "Fully Rejected"
            else:
                pay_status = "Partially Paid"
        elif rem:
            rejected_amt = 0.0
            pay_status = rem.claim_status_ra or ""
        elif is_resub:
            rejected_amt = 0.0
            pay_status = "Unsettled"   # awaiting new RA — matches old report
        else:
            rejected_amt = 0.0
            has_resub  = resub_net is not None
            pay_status = "Resub Awaiting Adjudication" if has_resub else "Unpaid"

        # Split rejected into initial vs resubmission columns
        initial_rejected = rejected_amt if not is_resub else 0.0
        resub_rejected   = rejected_amt if is_resub     else 0.0

        has_resub = resub_net is not None
        if pay_status == "Fully Paid":
            unsettled = 0.0
        elif is_resub and rem is None:
            # Awaiting adjudication resubmission — full resubmitted amount is unsettled
            unsettled = _f(claim_net) if claim_net is not None else 0.0
        elif not is_resub and has_resub:
            # Initial row whose rejected amount was resubmitted —
            # unsettled tracking moves to the resubmission row; avoid double-counting.
            unsettled = 0.0
        else:
            # Unsettled = submitted − paid − denied (matches ClaimCycleV2 Unsettled/AR);
            # amounts the payer has already denied are not counted as unsettled.
            if this_net is not None and paid is not None:
                unsettled = round(max(0.0, this_net - paid - rejected_amt), 2)
            elif this_net is not None:
                unsettled = this_net
            else:
                unsettled = 0.0

        # denial / date / TAT use ctx_rem so that awaiting-adjudication resubmissions
        # show the original denial reason (rank-1 RA) while financial cols stay blank.
        den_code  = ctx_rem.denial_code        or "" if ctx_rem else ""
        den_desc  = ctx_rem.denial_description or "" if ctx_rem else ""
        ra_date   = _fmt_date(ctx_rem.remittance_date) if ctx_rem else ""
        # TAT always uses initial submission date (not resubmission date)
        init_sub_date = init_sub_map.get(
            (claim.facility_id, claim.claim_id), claim.submission_date
        )
        tat = 0
        if is_resub and rem is None:
            # Awaiting second RA — TAT = days from initial submission to today
            if init_sub_date:
                try:
                    from datetime import date as _date
                    tat = (_date.today() - init_sub_date).days
                except Exception:
                    pass
        elif ctx_rem and ctx_rem.remittance_date and init_sub_date:
            try:
                tat = (ctx_rem.remittance_date - init_sub_date).days
            except Exception:
                pass

        diagnosis = diag_map.get((claim.facility_id, claim.claim_id), "")

        # New columns derived from encounter_start and RA/XML store
        svc_year  = claim.encounter_start.year          if claim.encounter_start else ""
        svc_month = claim.encounter_start.strftime("%B") if claim.encounter_start else ""
        pay_ref        = rem.ra_file_id       if rem       else ""
        settlement_dt  = _fmt_date(rem.remittance_date) if rem else ""
        id_payer       = rem.claim_id_payer   if rem       else ""
        sub_file = xml_filename_map.get(claim.raw_xml_id, "") if claim.raw_xml_id else ""
        ra_file  = xml_filename_map.get(rem.raw_xml_id, "")  if rem and rem.raw_xml_id else ""

        _write_row(ws, row_idx, [
            fac_name,                                                       # Facility Name
            _claim_gateway(claim.source_channel, claim.sender_id),                           # Claim Gateway
            claim.claim_id,                                                 # Claim Number
            _derive_receiver_id(claim.receiver_id, claim.payer_id),        # Receiver Id
            _payer_name(_derive_receiver_id(claim.receiver_id, claim.payer_id)),  # Receiver Name
            claim.payer_id,                                                 # Payer ID
            _payer_name(claim.payer_id),                                   # Payer Name
            claim.patient_id or "",                                         # Patient ID
            claim.member_id,                                                # Member Id
            _fmt_date(claim.encounter_start),                               # Treatment Date
            _fmt_date(claim.encounter_start),                               # Date Of Admission
            _fmt_date(claim.submission_date),                               # Submission Date
            _enc_label(claim.encounter_type),                               # Encounter Type
            _clinician_name(claim.clinician_id or "") or claim.clinician_name or "",  # Clinician Name
            svc_year,                                                       # Service Year
            svc_month,                                                      # Service Month
            sub_level,                                                      # Submission Level
            _f(net_sub) if net_sub is not None else "",                     # Net Amt - Initial Sub
            ra_net,                                                         # RA Net
            resub_col,                                                      # Net Amt - Resubmission
            approved,                                                       # Approved Amt
            initial_rejected,                                               # Initial Sub Rejected Amt
            resub_rejected,                                                 # Rejected Amt - Resubmission
            unsettled,                                                      # Unsettled Amt
            pay_status,                                                     # Payment Status
            den_code,                                                       # Denial Code
            den_desc,                                                       # Denial Description
            pay_ref,                                                        # Payment Ref
            settlement_dt,                                                  # Settlement Date
            id_payer,                                                       # ID Payer
            sub_file,                                                       # Submission File
            ra_file,                                                        # RA File
            ra_date,                                                        # RA Date
            tat,                                                            # TAT (Days)
            diagnosis,                                                      # Diagnosis
        ], alt=(row_idx % 2 == 0))

    _auto_width(ws, CLAIM_SUMMARY_COLS)
    ts = datetime.now().strftime("%Y%m%d_%H%M")
    return wb, f"ClaimSummary_{ts}.xlsx", len(rows)


@router.get("/claim-summary")
def export_claim_summary(
    facility_id:    Optional[int]  = None,
    payer_id:       Optional[str]  = None,
    encounter_type: Optional[str]  = None,
    date_from:      Optional[date] = None,
    date_to:        Optional[date] = None,
    date_field:     Optional[str]  = "treatment_date",
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    wb, filename, _ = _build_claim_summary(
        db, facility_id, payer_id, encounter_type, date_from, date_to, date_field)
    return _excel_response(wb, filename)


# ---------------------------------------------------------------------------
# 2. CLAIM ACTIVITY
# ---------------------------------------------------------------------------
CLAIM_ACTIVITY_COLS = [
    "Facility Name", "Claim Gateway", "Claim Number", "Receiver Id", "Receiver Name",
    "Payer ID", "Payer Name", "Member Id",
    "Encounter Start", "Transaction Date",
    "Clinician ID", "Clinician Name", "Prior Auth ID",
    "CPT Type", "Claim Activity Number", "CPT Code",
    "Quantity", "Net Amount",
    "Payment Status", "Denial Code", "Denial Description", "Rejection Amount",
]


def _build_claim_activity(db, facility_id, payer_id, encounter_type,
                          date_from, date_to, date_field,
                      facility_ids=None):
    fac_map = {f.id: (f.facility_name, f.license_id) for f in db.query(Facility).all()}

    # Payment status / denial / rejection are per ACTIVITY, taken from the
    # remittance activity detail of the rank-appropriate RA (same join as
    # Claim Activity V2). Previously the claim-level RA values were copied onto
    # every activity line, which repeated the rejection amount per line.
    rem_ranked_sq = (
        db.query(
            Remittance.facility_id,
            Remittance.claim_id,
            Remittance.id.label("rem_id"),
            func.row_number().over(
                partition_by=[Remittance.facility_id, Remittance.claim_id],
                order_by=Remittance.id,
            ).label("ra_rank"),
        ).subquery()
    )
    rad_alias = aliased(RemittanceActivityDetail)

    q = (
        db.query(ClaimActivity, Claim, rad_alias)
        .join(Claim, Claim.id == ClaimActivity.claim_id_fk)
        .outerjoin(
            rem_ranked_sq,
            and_(
                rem_ranked_sq.c.facility_id == Claim.facility_id,
                rem_ranked_sq.c.claim_id    == Claim.claim_id,
                rem_ranked_sq.c.ra_rank     == case(
                    (Claim.resubmission_key == "",              1),
                    (Claim.resubmission_key == "reconciliation", 3),
                    else_=2,
                ),
            )
        )
        .outerjoin(
            rad_alias,
            (rad_alias.remittance_id_fk == rem_ranked_sq.c.rem_id) &
            (rad_alias.activity_id      == ClaimActivity.activity_id)
        )
    )
    if facility_ids:
        _fids = [int(x) for x in facility_ids.split(',') if x.strip().isdigit()]
        if _fids:
            q = q.filter(Claim.facility_id.in_(_fids))
    elif facility_id:
        q = q.filter(Claim.facility_id == facility_id)
    if payer_id:
        q = q.filter(Claim.payer_id == payer_id)
    if encounter_type:
        q = q.filter(Claim.encounter_type == encounter_type)
    # Date filter uses Claim.encounter_start to stay consistent with Claim Summary.
    # (Previously used ClaimActivity.activity_start which caused boundary-month
    #  discrepancies when a claim's encounter date differed from its activity dates.)
    if date_from:
        q = q.filter(cast(Claim.encounter_start, Date) >= date_from)
    if date_to:
        q = q.filter(cast(Claim.encounter_start, Date) <= date_to)

    rows = q.order_by(Claim.submission_date.desc(), ClaimActivity.sequence_no).all()

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Claim Activity"
    ws.freeze_panes = "A2"
    _write_headers(ws, CLAIM_ACTIVITY_COLS)

    for row_idx, (act, claim, rad) in enumerate(rows, start=2):
        fac_name, fac_license = fac_map.get(claim.facility_id, ("", ""))

        # Activity status, as in Claim Activity V2
        if rad is None:
            pay_status = "Pending"
        elif rad.denial_code and float(rad.paid_amount) == 0:
            pay_status = "Denied"
        elif act.net is not None and float(rad.paid_amount) >= float(act.net):
            pay_status = "Fully Paid"
        elif float(rad.paid_amount) > 0:
            pay_status = "Partially Paid"
        else:
            pay_status = "Pending"
        den_code = rad.denial_code or "" if rad else ""
        # Rejection = RA net − paid for this activity (sums to V2 Initial Denied)
        rej_amt = (round(float(rad.net if rad.net is not None else act.net or 0)
                         - float(rad.paid_amount), 2) if rad else 0.0)
        _write_row(ws, row_idx, [
            fac_name,
            _claim_gateway(claim.source_channel, claim.sender_id),
            claim.claim_id,
            _derive_receiver_id(claim.receiver_id, claim.payer_id),
            _payer_name(_derive_receiver_id(claim.receiver_id, claim.payer_id)),
            claim.payer_id,
            _payer_name(claim.payer_id),
            claim.member_id,
            _fmt_date(act.activity_start),
            _fmt_date(act.activity_start),
            act.clinician_id or claim.clinician_id or "",
            _clinician_name(act.clinician_id or claim.clinician_id or ""),
            act.authorization_id or "",
            act.activity_type or "",
            act.sequence_no or "",
            act.activity_code or "",
            _f(act.quantity),
            _f(act.net),
            pay_status,
            den_code,
            (rad.denial_description or _lookup_denial(den_code)[0]) if den_code else "",
            rej_amt,
        ], alt=(row_idx % 2 == 0))

    _auto_width(ws, CLAIM_ACTIVITY_COLS)
    ts = datetime.now().strftime("%Y%m%d_%H%M")
    return wb, f"ClaimActivity_{ts}.xlsx", len(rows)


@router.get("/claim-activity")
def export_claim_activity(
    facility_id:    Optional[int]  = None,
    payer_id:       Optional[str]  = None,
    encounter_type: Optional[str]  = None,
    date_from:      Optional[date] = None,
    date_to:        Optional[date] = None,
    date_field:     Optional[str]  = "treatment_date",
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    wb, filename, _ = _build_claim_activity(
        db, facility_id, payer_id, encounter_type, date_from, date_to, date_field)
    return _excel_response(wb, filename)


# ---------------------------------------------------------------------------
# 3. DENIAL ANALYSIS
# ---------------------------------------------------------------------------
DENIAL_COLS = [
    "Facility Name", "Payer ID", "Payer Name",
    "Denial Code", "Denial Description",
    "Claim Count", "Total Denied (AED)", "% of Total Denied",
]


def _build_denial_analysis(db, facility_id, payer_id, encounter_type,
                           date_from, date_to, date_field,
                      facility_ids=None):
    fac_map = {f.id: f.facility_name for f in db.query(Facility).all()}

    q = (
        db.query(
            Claim.facility_id,
            Claim.payer_id,
            Claim.payer_name,
            Remittance.denial_code,
            Remittance.denial_description,
            func.count(Claim.id).label("claim_count"),
            func.coalesce(func.sum(Remittance.denial_amount), 0).label("total_denied"),
        )
        .join(Remittance, and_(
            Claim.facility_id == Remittance.facility_id,
            Claim.claim_id    == Remittance.claim_id,
        ))
        .filter(Remittance.denial_code.isnot(None), Remittance.denial_code != "")
    )
    if facility_ids:
        _fids = [int(x) for x in facility_ids.split(',') if x.strip().isdigit()]
        if _fids:
            q = q.filter(Claim.facility_id.in_(_fids))
    elif facility_id:
        q = q.filter(Claim.facility_id == facility_id)
    if payer_id:
        q = q.filter(Claim.payer_id == payer_id)
    if encounter_type:
        q = q.filter(Claim.encounter_type == encounter_type)

    # For denial analysis, date_field controls which date column
    if date_field == "remittance_date":
        if date_from:
            q = q.filter(Remittance.remittance_date >= date_from)
        if date_to:
            q = q.filter(Remittance.remittance_date <= date_to)
    else:
        if date_from:
            q = q.filter(cast(Claim.encounter_start, Date) >= date_from)
        if date_to:
            q = q.filter(cast(Claim.encounter_start, Date) <= date_to)

    q = q.group_by(
        Claim.facility_id, Claim.payer_id, Claim.payer_name,
        Remittance.denial_code, Remittance.denial_description,
    ).order_by(func.sum(Remittance.denial_amount).desc())

    rows = q.all()
    grand_total = sum(float(r.total_denied or 0) for r in rows)

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Denial Analysis"
    ws.freeze_panes = "A2"
    _write_headers(ws, DENIAL_COLS)

    for row_idx, r in enumerate(rows, start=2):
        denied = float(r.total_denied or 0)
        pct = round(denied / grand_total * 100, 2) if grand_total else 0
        _write_row(ws, row_idx, [
            fac_map.get(r.facility_id, ""),
            r.payer_id,
            r.payer_name or "",
            r.denial_code or "",
            r.denial_description or "",
            r.claim_count,
            denied, pct,
        ], alt=(row_idx % 2 == 0))

    _auto_width(ws, DENIAL_COLS)
    ts = datetime.now().strftime("%Y%m%d_%H%M")
    return wb, f"DenialAnalysis_{ts}.xlsx", len(rows)


@router.get("/denial-analysis")
def export_denial_analysis(
    facility_id:    Optional[int]  = None,
    payer_id:       Optional[str]  = None,
    encounter_type: Optional[str]  = None,
    date_from:      Optional[date] = None,
    date_to:        Optional[date] = None,
    date_field:     Optional[str]  = "treatment_date",
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    wb, filename, _ = _build_denial_analysis(
        db, facility_id, payer_id, encounter_type, date_from, date_to, date_field)
    return _excel_response(wb, filename)


# ---------------------------------------------------------------------------
# 4. RESUBMISSION & AR AGING
# ---------------------------------------------------------------------------
AGING_COLS = [
    "Facility Name", "Claim Number", "Payer ID", "Payer Name",
    "Member Id", "Submission Date", "Encounter Type",
    "Net Amount (AED)", "Approved (AED)", "Unsettled (AED)",
    "Resubmission Count", "Days Outstanding",
    "Aging Bucket", "Payment Status", "Denial Code",
]

BUCKETS = [
    (30, "0-30 days"), (60, "31-60 days"), (90, "61-90 days"),
    (180, "91-180 days"), (365, "181-365 days"), (9999, "365+ days"),
]


def _bucket(days: int) -> str:
    for cutoff, label in BUCKETS:
        if days <= cutoff:
            return label
    return "365+ days"


def _build_resubmission_aging(db, facility_id, payer_id, encounter_type,
                              date_from, date_to, date_field,
                              unsettled_only: bool = True,
                      facility_ids=None):
    fac_map = {f.id: f.facility_name for f in db.query(Facility).all()}
    today   = date.today()

    q = (
        db.query(Claim, Remittance)
        .outerjoin(Remittance, and_(
            Claim.facility_id == Remittance.facility_id,
            Claim.claim_id    == Remittance.claim_id,
        ))
    )
    q = _apply_filters(q, facility_id, payer_id, encounter_type,
                       date_from, date_to, date_field, rem_model=Remittance,
                       facility_ids=facility_ids)
    rows = q.order_by(Claim.submission_date).all()

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Resubmission & Aging"
    ws.freeze_panes = "A2"
    _write_headers(ws, AGING_COLS)

    row_idx   = 2
    row_count = 0
    for claim, rem in rows:
        net       = float(claim.net_amount or 0)
        approved  = float(rem.paid_amount or 0) if rem else 0
        unsettled = round(net - approved, 2)

        if unsettled_only and unsettled <= 0:
            continue

        days_out = (today - claim.submission_date).days if claim.submission_date else 0

        _write_row(ws, row_idx, [
            fac_map.get(claim.facility_id, ""),
            claim.claim_id,
            claim.payer_id,
            claim.payer_name or "",
            claim.member_id,
            _fmt_date(claim.submission_date),
            _enc_label(claim.encounter_type),
            net, approved, unsettled,
            _submission_level(claim.resubmission_type),
            days_out,
            _bucket(days_out),
            rem.claim_status_ra or "Pending" if rem else "Pending",
            rem.denial_code or "" if rem else "",
        ], alt=(row_idx % 2 == 0))
        row_idx   += 1
        row_count += 1

    _auto_width(ws, AGING_COLS)
    ts = datetime.now().strftime("%Y%m%d_%H%M")
    return wb, f"ResubmissionAging_{ts}.xlsx", row_count


@router.get("/resubmission-aging")
def export_resubmission_aging(
    facility_id:    Optional[int]  = None,
    payer_id:       Optional[str]  = None,
    encounter_type: Optional[str]  = None,
    date_from:      Optional[date] = None,
    date_to:        Optional[date] = None,
    date_field:     Optional[str]  = "treatment_date",
    unsettled_only: bool = True,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    wb, filename, _ = _build_resubmission_aging(
        db, facility_id, payer_id, encounter_type, date_from, date_to, date_field,
        unsettled_only)
    return _excel_response(wb, filename)


# ---------------------------------------------------------------------------
# 5. REMITTANCE ACTIVITY
# ---------------------------------------------------------------------------
REMITTANCE_ACTIVITY_COLS = [
    "Facility ID", "Facility Name", "Claim Gateway", "Receiver ID", "Receiver Name",
    "Payer ID", "Payer Name", "Patient ID", "Member Id", "Emirates Id",
    "Encounter Start Date", "Encounter End Date",
    "Encounter Start Year", "Encounter Start Month",
    "Last Submission Date", "First Submission Date", "Transaction Date",
    "Claim Number", "Encounter Type", "Encounter End Type",
    "IDPayer", "Payment Reference", "Date Settlement",
    "Service Date", "Clinician", "Ordering Clinician",
    "Activity Code", "Activity Type", "Activity Type Name",
    "No Of Unit", "Gross Amt", "Net Amt", "Patient Share",
    "Payment Amt", "Denial Code", "Denial Description", "Denial Category",
    "Denial Type",
    "Activity Detail ID",
    "RASubmission File Name",
    "RAFille Id",
    "Claim Submission File Name",
    "Initial Submission File Name",
    "Response Comments",
    "RAPament For",
]


def _ra_payment_for(rem_id: int, ra_rank_map: dict, dup_rem_ids: set) -> str:
    """Universal RAPament For computation — facility and filename agnostic.

    Based purely on remittance chronology per claim:
      - Duplicate RA   : same ra_file_id processed >1 time for same claim
      - 1st RA         : earliest remittance for this claim
      - Resubmission RA Claim Level - N : Nth subsequent remittance (N >= 1)
    """
    if rem_id in dup_rem_ids:
        return "Duplicate RA"
    rank = ra_rank_map.get(rem_id, 1)
    if rank == 1:
        return "1st RA"
    return f"Resubmission RA Claim Level - {rank - 1}"


def _submission_history(db, rows) -> dict:
    """(facility_id, claim_id) -> [(submission_date, file name, resubmission_key)] oldest first, for the
    claims in an RA report. Used for the initial submission and the submission each RA answers."""
    from collections import defaultdict as _dd
    from sqlalchemy import text as _text
    keys = {(rem.facility_id, rem.claim_id) for _, rem, *_ in rows}
    out = _dd(list)
    by_fac = _dd(list)
    for f, c in keys:
        by_fac[f].append(c)
    for f, cids in by_fac.items():
        for r in db.execute(_text("""
            SELECT c.claim_id, c.submission_date, coalesce(c.resubmission_key, '') k, rx.source_filename fn
            FROM claims c LEFT JOIN raw_xml_store rx ON rx.id = c.raw_xml_id
            WHERE c.facility_id = :f AND c.claim_id = ANY(:ids)
            ORDER BY c.submission_date NULLS LAST, c.id"""), {"f": f, "ids": cids}):
            out[(f, r.claim_id)].append((r.submission_date, r.fn or "", r.k))
    return out


def _initial_sub(subs):
    """The original submission (no resubmission type); the earliest one if none is marked original."""
    return next((s for s in subs if not s[2]), subs[0]) if subs else (None, "", "")


def _sub_for_ra(subs, ra_day):
    """The submission an RA answers: the latest sent on or before the RA date (else the earliest)."""
    if not subs:
        return (None, "", "")
    if ra_day is None:
        return subs[-1]
    day = lambda d: d.date() if hasattr(d, "date") else d
    before = [s for s in subs if s[0] and day(s[0]) <= day(ra_day)]
    return before[-1] if before else subs[0]


def _build_remittance_activity(db, facility_id, payer_id, encounter_type,
                               date_from, date_to, date_field,
                      facility_ids=None):
    fac_map = {f.id: (f.facility_name, f.license_id) for f in db.query(Facility).all()}

    # ------------------------------------------------------------------
    # Pre-compute RA rank and duplicate IDs — scoped to only the
    # remittances that will appear in the result set to save memory.
    # ------------------------------------------------------------------
    from sqlalchemy import text as _text

    # Build the date filter clause for the scoping subquery
    date_clauses = ""
    date_params: dict = {}
    if date_field == "treatment_date":
        if date_from:
            date_clauses += " AND DATE(c.encounter_start) >= :date_from"
            date_params["date_from"] = str(date_from)
        if date_to:
            date_clauses += " AND DATE(c.encounter_start) <= :date_to"
            date_params["date_to"] = str(date_to)
    else:
        # Filter by RA file date (TransactionDate / submission_date) — matches client software
        if date_from:
            date_clauses += " AND rx.submission_date >= :date_from"
            date_params["date_from"] = str(date_from)
        if date_to:
            date_clauses += " AND rx.submission_date <= :date_to"
            date_params["date_to"] = str(date_to)

    fac_clause = ""
    if facility_ids:
        _fids = [int(x) for x in facility_ids.split(",") if x.strip().isdigit()]
        if _fids:
            placeholders = ", ".join(f":fid{i}" for i in range(len(_fids)))
            date_clauses += f" AND r.facility_id IN ({placeholders})"
            for i, fid in enumerate(_fids):
                date_params[f"fid{i}"] = fid
    elif facility_id:
        date_clauses += " AND r.facility_id = :facility_id"
        date_params["facility_id"] = facility_id

    # rank over the claim's whole RA history, so a 2026 RA after a 2024 one is not "1st RA"
    rank_rows = db.execute(_text(f"""
        WITH scoped AS (
            SELECT r.id, r.facility_id, r.claim_id
            FROM remittances r
            JOIN LATERAL (SELECT c.encounter_start FROM claims c
                          WHERE c.facility_id = r.facility_id AND c.claim_id = r.claim_id
                          ORDER BY c.id LIMIT 1) c ON true
            LEFT JOIN raw_xml_store rx ON rx.id = r.raw_xml_id
            WHERE 1=1 {date_clauses}
        ), ranked AS (
            SELECT r.id, ROW_NUMBER() OVER (PARTITION BY r.facility_id, r.claim_id
                                            ORDER BY r.remittance_date, r.id) AS ra_rank
            FROM remittances r
            WHERE (r.facility_id, r.claim_id) IN (SELECT DISTINCT facility_id, claim_id FROM scoped)
        )
        SELECT ranked.id, ranked.ra_rank FROM ranked JOIN scoped USING (id)
    """), date_params).fetchall()
    ra_rank_map = {row[0]: row[1] for row in rank_rows}

    scoped_ids = list(ra_rank_map.keys())
    if scoped_ids:
        dup_rows = db.execute(_text("""
            SELECT r.id
            FROM remittances r
            JOIN (
                SELECT facility_id, claim_id, ra_file_id
                FROM remittances
                WHERE ra_file_id IS NOT NULL
                  AND id = ANY(:ids)
                GROUP BY facility_id, claim_id, ra_file_id
                HAVING COUNT(*) > 1
            ) dup ON r.facility_id = dup.facility_id
                  AND r.claim_id   = dup.claim_id
                  AND r.ra_file_id = dup.ra_file_id
        """), {"ids": scoped_ids}).fetchall()
    else:
        dup_rows = []
    dup_rem_ids = {row[0] for row in dup_rows}

    ra_xml_store    = aliased(RawXmlStore)
    claim_xml_store = aliased(RawXmlStore)
    act_alias       = aliased(ClaimActivity)

    # Each RA line is paired with ONE submission of the claim: the latest sent on or before the RA date
    # (no submission before it → the earliest). Joining on claim number alone listed the line once per
    # submission and doubled the totals for resubmitted claims.
    c2 = aliased(Claim)
    ra_day = func.coalesce(cast(ra_xml_store.submission_date, Date), cast(Remittance.remittance_date, Date))
    sent_before = cast(c2.submission_date, Date) <= ra_day
    paired_claim = (
        db.query(c2.id)
        .filter(c2.facility_id == Remittance.facility_id, c2.claim_id == Remittance.claim_id)
        .order_by(sent_before.desc().nullslast(),
                  case((sent_before, c2.submission_date)).desc().nullslast(),
                  c2.submission_date, c2.id.desc())
        .limit(1).correlate(Remittance, ra_xml_store).scalar_subquery()
    )

    q = (
        db.query(RemittanceActivityDetail, Remittance, Claim,
                 ra_xml_store, claim_xml_store, act_alias)
        .join(Remittance, Remittance.id == RemittanceActivityDetail.remittance_id_fk)
        .outerjoin(ra_xml_store,    ra_xml_store.id    == Remittance.raw_xml_id)
        .join(Claim, Claim.id == paired_claim)
        .outerjoin(claim_xml_store, claim_xml_store.id == Claim.raw_xml_id)
        .outerjoin(act_alias, and_(
            act_alias.claim_id_fk  == Claim.id,
            act_alias.activity_id  == RemittanceActivityDetail.activity_id,
        ))
    )
    if facility_ids:
        _fids = [int(x) for x in facility_ids.split(",") if x.strip().isdigit()]
        if _fids:
            q = q.filter(Remittance.facility_id.in_(_fids))
    elif facility_id:
        q = q.filter(Remittance.facility_id == facility_id)
    if payer_id:
        q = q.filter(Remittance.payer_id == payer_id)
    if encounter_type:
        q = q.filter(Claim.encounter_type == encounter_type)

    # Remittance Activity: filter by RA file date (TransactionDate) by default — matches client software
    # treatment_date mode: filter on encounter_start instead
    if date_field == "treatment_date":
        if date_from:
            q = q.filter(cast(Claim.encounter_start, Date) >= date_from)
        if date_to:
            q = q.filter(cast(Claim.encounter_start, Date) <= date_to)
    else:
        # Default: RA file date (submission_date / TransactionDate)
        if date_from:
            q = q.filter(ra_xml_store.submission_date >= date_from)
        if date_to:
            q = q.filter(ra_xml_store.submission_date <= date_to)

    from sqlalchemy.orm import defer as _defer   # file names / dates only, never the XML contents
    q = q.options(_defer(ra_xml_store.xml_content), _defer(claim_xml_store.xml_content))
    rows = q.order_by(ra_xml_store.submission_date.desc(), Claim.claim_id).all()

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "RemittanceActivityReportTemplat"
    ws.freeze_panes = "A2"
    _write_headers(ws, REMITTANCE_ACTIVITY_COLS)
    subs = _submission_history(db, rows)

    for row_idx, (rad, rem, claim, ra_xml_row, claim_xml_row, act_row) in enumerate(rows, start=2):
        init_date, init_file, _ = _initial_sub(subs.get((rem.facility_id, rem.claim_id)))
        enc_start = claim.encounter_start
        enc_year  = enc_start.year  if enc_start else ""
        enc_month = enc_start.month if enc_start else ""

        fac_name, fac_license = fac_map.get(rem.facility_id, ("", ""))

        # Denial lookups from DHA code set
        den_code = rad.denial_code or ""
        den_desc, den_cat, den_type = _lookup_denial(den_code)

        # Clinician: prefer activity-level ID, fall back to claim-level
        act_clin_id = (act_row.clinician_id if act_row else None) or claim.clinician_id
        ord_clin_id = (
            getattr(act_row, "ordering_clinician_id", None) if act_row else None
        ) or act_clin_id
        clinician     = _clinician_name(act_clin_id)
        ord_clinician = _clinician_name(ord_clin_id)

        _write_row(ws, row_idx, [
            (fac_map.get(rem.facility_id, ("", ""))[1] or rem.facility_id), fac_name,
            _claim_gateway(claim.source_channel, claim.sender_id),
            _derive_receiver_id(claim.receiver_id, claim.payer_id),
            _payer_name(_derive_receiver_id(claim.receiver_id, claim.payer_id)),
            claim.payer_id, _payer_name(claim.payer_id),
            claim.member_id, claim.member_id, claim.emirates_id or "",
            _fmt_date(enc_start), _fmt_date(claim.encounter_end),
            enc_year, enc_month,
            _fmt_date(claim.submission_date),  # Last Submission Date = the submission this RA answers
            _fmt_date(init_date),              # First Submission Date = the original submission
            _fmt_date(ra_xml_row.submission_date if ra_xml_row else None),  # Transaction Date = RA file date
            claim.claim_id, _enc_label(claim.encounter_type), claim.end_type or "",
            claim.claim_id_payer or "", rem.ra_file_id or "",
            _fmt_date(rem.remittance_date),  # Date Settlement = actual payment settlement date
            _fmt_date(enc_start),
            clinician, ord_clinician,
            rad.activity_code or "", rad.activity_type or "", rad.activity_type or "",
            _f(rad.quantity_approved), _f(rad.gross), _f(rad.net),
            _f(rad.patient_share), _f(rad.paid_amount),
            den_code, den_desc, den_cat,
            den_type,                                                # Denial Type
            rad.activity_id or "",                                   # Activity Detail ID
            ra_xml_row.source_filename    if ra_xml_row    else "",  # RASubmission File Name
            rem.ra_file_id or "",                                    # RAFille Id
            claim_xml_row.source_filename if claim_xml_row else "",  # Claim Submission File Name
            init_file,                                               # Initial Submission File Name
            rad.denial_description or rem.denial_description or "",    # Response Comments
            _ra_payment_for(rem.id, ra_rank_map, dup_rem_ids),        # RAPament For
        ], alt=(row_idx % 2 == 0))

    _auto_width(ws, REMITTANCE_ACTIVITY_COLS)
    ts = datetime.now().strftime("%Y%m%d_%H%M")
    return wb, f"RemittanceActivity_{ts}.xlsx", len(rows)


@router.get("/remittance-activity")
def export_remittance_activity(
    facility_id:    Optional[int]  = None,
    payer_id:       Optional[str]  = None,
    encounter_type: Optional[str]  = None,
    date_from:      Optional[date] = None,
    date_to:        Optional[date] = None,
    date_field:     Optional[str]  = "remittance_date",
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    wb, filename, _ = _build_remittance_activity(
        db, facility_id, payer_id, encounter_type, date_from, date_to, date_field)
    return _excel_response(wb, filename)


# ---------------------------------------------------------------------------
# 6. DENIAL QUERY
# ---------------------------------------------------------------------------
DENIAL_QUERY_COLS = [
    "RAHistory", "Submission Level", "License No",
    "Facility Name", "Claim Gateway", "Receiver Id", "Receiver Name",
    "Payer ID", "Payer Name", "Member Id", "Emirates Id",
    "Invoice No", "Payment Status", "RAFile Name",
    "Service Date", "CPTCode",
    "Claim Amt", "Paid Amt", "Denied Amt",
    "Remittance Date", "Last Resub Date",
    "Denial Code", "F1comments", "R1Comments",
]


def _build_denial_query(db, facility_id, payer_id, encounter_type,
                        date_from, date_to, date_field,
                      facility_ids=None):
    fac_map = {f.id: (f.facility_name, f.license_id) for f in db.query(Facility).all()}

    first_cpt_sq = (
        db.query(
            ClaimActivity.claim_id_fk,
            func.min(ClaimActivity.activity_code).label("first_cpt"),
        )
        .filter(ClaimActivity.activity_type == "CPT")
        .group_by(ClaimActivity.claim_id_fk)
        .subquery()
    )

    q = (
        db.query(Claim, Remittance, first_cpt_sq.c.first_cpt)
        .join(Remittance, and_(
            Claim.facility_id == Remittance.facility_id,
            Claim.claim_id    == Remittance.claim_id,
        ))
        .outerjoin(first_cpt_sq, first_cpt_sq.c.claim_id_fk == Claim.id)
        .filter(
            Remittance.denial_code.isnot(None),
            Remittance.denial_code != "",
        )
    )
    if facility_ids:
        _fids = [int(x) for x in facility_ids.split(',') if x.strip().isdigit()]
        if _fids:
            q = q.filter(Claim.facility_id.in_(_fids))
    elif facility_id:
        q = q.filter(Claim.facility_id == facility_id)
    if payer_id:
        q = q.filter(Claim.payer_id == payer_id)
    if encounter_type:
        q = q.filter(Claim.encounter_type == encounter_type)

    # Denial query defaults to remittance date; respects date_field
    if date_field == "treatment_date":
        if date_from:
            q = q.filter(cast(Claim.encounter_start, Date) >= date_from)
        if date_to:
            q = q.filter(cast(Claim.encounter_start, Date) <= date_to)
    else:
        if date_from:
            q = q.filter(Remittance.remittance_date >= date_from)
        if date_to:
            q = q.filter(Remittance.remittance_date <= date_to)

    rows = q.order_by(Remittance.remittance_date.desc(), Claim.claim_id).all()

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "DenialQueryReport"
    ws.freeze_panes = "A2"
    _write_headers(ws, DENIAL_QUERY_COLS)

    for row_idx, (claim, rem, first_cpt) in enumerate(rows, start=2):
        fac_name, lic_no = fac_map.get(claim.facility_id, ("", ""))
        sub_level  = _submission_level(claim.resubmission_type)
        ra_history = 1 if claim.resubmission_type else 0

        _write_row(ws, row_idx, [
            ra_history, sub_level, lic_no, fac_name,
            _claim_gateway(claim.source_channel, claim.sender_id),
            _derive_receiver_id(claim.receiver_id, claim.payer_id),
            _payer_name(_derive_receiver_id(claim.receiver_id, claim.payer_id)),
            claim.payer_id, _payer_name(claim.payer_id),
            claim.member_id, claim.emirates_id or "",
            claim.claim_id,
            rem.claim_status_ra or "", rem.ra_file_id or "",
            _fmt_date(claim.encounter_start),
            first_cpt or "",
            _f(claim.net_amount), _f(rem.paid_amount), _f(rem.denial_amount),
            _fmt_date(rem.remittance_date),
            _fmt_date(claim.submission_date),
            rem.denial_code or "", "", "",
        ], alt=(row_idx % 2 == 0))

    _auto_width(ws, DENIAL_QUERY_COLS)
    ts = datetime.now().strftime("%Y%m%d_%H%M")
    return wb, f"DenialQuery_{ts}.xlsx", len(rows)


@router.get("/denial-query")
def export_denial_query(
    facility_id:    Optional[int]  = None,
    payer_id:       Optional[str]  = None,
    encounter_type: Optional[str]  = None,
    date_from:      Optional[date] = None,
    date_to:        Optional[date] = None,
    date_field:     Optional[str]  = "remittance_date",
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    wb, filename, _ = _build_denial_query(
        db, facility_id, payer_id, encounter_type, date_from, date_to, date_field)
    return _excel_response(wb, filename)


# ---------------------------------------------------------------------------
# Report builder dispatch map
# ---------------------------------------------------------------------------
REPORT_BUILDERS = {
    "claim-summary":         _build_claim_summary,
    "claim-activity":        _build_claim_activity,
    "denial-analysis":       _build_denial_analysis,
    "resubmission-aging":    _build_resubmission_aging,
    "remittance-activity":   _build_remittance_activity,
    "denial-query":          _build_denial_query,
    # V2 entries registered below after the functions are defined
}


# ---------------------------------------------------------------------------
# STAGING QUEUE ENDPOINTS
# ---------------------------------------------------------------------------

# job_id → Postgres backend PID of the session building that report.
# Lets the Cancel endpoint stop the running SQL, not just flag the job.
_RUNNING_REPORT_PIDS: dict[int, int] = {}


def _descriptive_filename(db, filename, facility_id, facility_ids, date_from, date_to):
    """ReportName_<facility>_<from>_to_<to>_<timestamp>.xlsx — facility and period in the file name."""
    import re as _re
    from sqlalchemy import text as _text
    if not filename:
        return filename
    m = _re.match(r"^(.*?)_(\d{8}_\d{4})\.xlsx$", filename)
    base, ts = (m.group(1), m.group(2)) if m else (filename.rsplit(".", 1)[0], datetime.now().strftime("%Y%m%d_%H%M"))
    raw = facility_ids if facility_ids else ([facility_id] if facility_id else [])
    if isinstance(raw, str):                     # "13" or "5,6,7" from the queue
        raw = raw.split(",")
    ids = sorted({int(str(i).strip()) for i in raw if str(i).strip()})
    if len(ids) == 1:
        name = db.execute(_text("SELECT facility_name FROM facilities WHERE id = :i"), {"i": ids[0]}).scalar() or f"Facility{ids[0]}"
        fac = _re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-")[:40]
    elif ids:
        fac = f"{len(ids)}-facilities"
    else:
        fac = "All-facilities"
    period = f"{date_from}_to_{date_to}" if date_from and date_to else (f"from_{date_from}" if date_from else
                                                                     (f"to_{date_to}" if date_to else ""))
    return "_".join(x for x in (base, fac, period, ts) if x) + ".xlsx"


def _run_report_background(
    job_id: int,
    builder,
    facility_id, payer_id, encounter_type, date_from, date_to, date_field,
    facility_ids=None,
):
    """Build report in background and update the DB row when done."""
    from ..database import SessionLocal
    from sqlalchemy import text as _text
    db = SessionLocal()
    try:
        _RUNNING_REPORT_PIDS[job_id] = db.execute(_text("SELECT pg_backend_pid()")).scalar()
        db.commit()
    except Exception:
        db.rollback()
    try:
        wb, filename, row_count = builder(
            db, facility_id, payer_id, encounter_type, date_from, date_to, date_field,
            facility_ids=facility_ids)
        filename = _descriptive_filename(db, filename, facility_id, facility_ids, date_from, date_to)
        file_bytes = _wb_to_bytes(wb)
        status    = "completed"
        error_msg = None
    except Exception:
        import traceback
        file_bytes = None
        filename   = None
        row_count  = 0
        status     = "error"
        error_msg  = traceback.format_exc()
        try:
            db.rollback()  # clear aborted transaction before status update
        except Exception:
            pass

    _RUNNING_REPORT_PIDS.pop(job_id, None)
    try:
        now = datetime.now(_tz.utc)
        # status = 'pending' guard: never overwrite a job the user cancelled
        db.execute(
            _text(
                "UPDATE report_jobs SET status=:s, file_data=:fd, filename=:fn, "
                "row_count=:rc, error_message=:em, completed_at=:ca "
                "WHERE id=:id AND status IN ('pending', 'running')"
            ),
            {
                "s":  status,
                "fd": file_bytes,
                "fn": filename,
                "rc": row_count,
                "em": error_msg,
                "ca": now,
                "id": job_id,
            },
        )
        db.commit()
    except Exception:
        import traceback; traceback.print_exc()
    finally:
        db.close()


@router.post("/generate/{report_type}")
def generate_report_job(
    report_type:     str,
    background_tasks: BackgroundTasks,
    facility_id:    Optional[int]  = None,
    payer_id:       Optional[str]  = None,
    encounter_type: Optional[str]  = None,
    date_from:      Optional[date] = None,
    date_to:        Optional[date] = None,
    date_field:     Optional[str]  = "treatment_date",
    facility_ids:   Optional[str]  = None,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Queue a report job and return immediately. Generation runs in background."""
    builder = REPORT_BUILDERS.get(report_type)
    if not builder:
        raise HTTPException(status_code=404, detail=f"Unknown report type: {report_type}")

    filters_dict = {
        "facility_id":    facility_id,
        "payer_id":       payer_id,
        "encounter_type": encounter_type,
        "date_from":      str(date_from) if date_from else None,
        "date_to":        str(date_to)   if date_to   else None,
        "date_field":     date_field,
        "facility_ids":   facility_ids,
    }
    user_id = getattr(current_user, "id", None) or getattr(current_user, "user_id", None)
    now = datetime.now(_tz.utc)

    job = ReportJob(
        user_id      = user_id,
        report_type  = report_type,
        report_label = REPORT_LABELS.get(report_type, report_type),
        filters_json = json_mod.dumps(filters_dict),
        status       = "pending",
        file_data    = None,
        filename     = None,
        row_count    = 0,
        error_message= None,
        completed_at = None,
    )
    db.add(job)
    db.flush()
    job_id = job.id
    db.commit()

    background_tasks.add_task(
        _run_report_background,
        job_id, builder,
        facility_id, payer_id, encounter_type, date_from, date_to, date_field,
        facility_ids=facility_ids,
    )

    return {
        "id":          job_id,
        "report_type": report_type,
        "report_label":REPORT_LABELS.get(report_type, report_type),
        "status":      "pending",
        "filename":    None,
        "row_count":   0,
        "error":       None,
        "created_at":  now.isoformat(),
        "completed_at":None,
        "filters":     filters_dict,
    }


@router.get("/jobs")
def list_report_jobs(
    limit: int = 50,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """List all report jobs (most recent first)."""
    try:
        # Build facility id → name map for display
        fac_map = {f.id: f.facility_name for f in db.query(Facility).all()}

        # Select only non-BYTEA columns to avoid loading large file data on list
        from sqlalchemy import text as _text
        rows = db.execute(
            _text(
                "SELECT id, user_id, report_type, report_label, filters_json, "
                "status, filename, row_count, error_message, "
                "created_at, completed_at, "
                "(file_data IS NOT NULL) AS has_file "
                "FROM report_jobs ORDER BY created_at DESC LIMIT :lim"
            ),
            {"lim": limit},
        ).mappings().all()

        result = []
        for r in rows:
            filters = json_mod.loads(r["filters_json"]) if r["filters_json"] else {}
            # facility_ids is a comma-separated string when set via the multi-select;
            # facility_id is the legacy single-int path. Support both.
            fac_ids_str = filters.get("facility_ids") or ""
            fac_id_single = filters.get("facility_id")
            if fac_ids_str:
                ids = [int(x) for x in fac_ids_str.split(",") if x.strip().isdigit()]
                if len(ids) == 1:
                    fac_name = fac_map.get(ids[0], "All Facilities")
                else:
                    fac_name = "All Facilities"
            elif fac_id_single:
                fac_name = fac_map.get(fac_id_single, "All Facilities")
            else:
                fac_name = "All Facilities"
            result.append({
                "id":            r["id"],
                "report_type":   r["report_type"],
                "report_label":  r["report_label"],
                "facility_name": fac_name,
                "status":        r["status"],
                "filename":      r["filename"],
                "row_count":     r["row_count"],
                "error":         r["error_message"],
                "has_file":      bool(r["has_file"]),
                "created_at":    r["created_at"].isoformat() if r["created_at"] else None,
                "completed_at":  r["completed_at"].isoformat() if r["completed_at"] else None,
                "filters":       filters,
            })
        return result
    except Exception as exc:
        import traceback
        raise HTTPException(status_code=500, detail=traceback.format_exc())


@router.post("/jobs/{job_id}/cancel")
def cancel_report_job(
    job_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Cancel a pending or running report job."""
    from sqlalchemy import text as _text
    row = db.execute(
        _text("SELECT id, status FROM report_jobs WHERE id = :id"),
        {"id": job_id},
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Report job not found")
    if row.status not in ("pending", "running"):
        raise HTTPException(
            status_code=400,
            detail=f"Cannot cancel job with status '{row.status}'",
        )
    db.execute(
        _text(
            "UPDATE report_jobs SET status='failed', error_message='Cancelled by user', "
            "completed_at=NOW() WHERE id=:id"
        ),
        {"id": job_id},
    )
    db.commit()
    # Stop the SQL still running for this job (otherwise it keeps burning CPU)
    pid = _RUNNING_REPORT_PIDS.get(job_id)
    if pid:
        try:
            db.execute(_text("SELECT pg_cancel_backend(:pid)"), {"pid": pid})
            db.commit()
        except Exception:
            db.rollback()
    return {"id": job_id, "status": "failed", "message": "Job cancelled"}


@router.get("/jobs/{job_id}/download")
def download_report_job(
    job_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Download the Excel file for a previously generated report job."""
    import traceback as _tb
    try:
        from sqlalchemy import text as _text
        row = db.execute(
            _text("SELECT file_data, filename, status FROM report_jobs WHERE id = :id"),
            {"id": job_id},
        ).fetchone()

        if not row:
            raise HTTPException(status_code=404, detail="Report job not found")
        if not row.file_data:
            raise HTTPException(
                status_code=400,
                detail=f"No file available for this job (status: {row.status})",
            )

        raw = row.file_data
        if isinstance(raw, memoryview):
            data = bytes(raw)
        elif isinstance(raw, bytes):
            data = raw
        elif isinstance(raw, str):
            # PostgreSQL hex-bytea format: '\x504b...'
            hex_str = raw[2:] if raw.startswith("\\x") else raw
            data = bytes.fromhex(hex_str)
        else:
            raise ValueError(f"Unexpected file_data type: {type(raw)}")

        return StreamingResponse(
            io.BytesIO(data),
            headers={
                "Content-Disposition": f'attachment; filename="{row.filename or "report.xlsx"}"',
                "Content-Type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            },
        )
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=500, detail=_tb.format_exc())


# ===========================================================================
# V2 REPORTS  (ClaimSummaryV2, ClaimActivityV2, RemittanceActivityV2)
# ===========================================================================
# These extend the V1 reports with richer financial fields derived from the
# DHA eClaimLink XML schema:
#   - Gross Submitted / Patient Share (CS) from ClaimSubmission.Claim
#   - Allowed Amount / Patient Share (RA) from RA.Activity.List (rad.gross)
#   - Variance = Net Submitted − Total Paid
#   - TPA Comments from RA.Claim/Comments (remittances.tpa_comments)
#   - Emirates ID, Encounter End Date, Resubmission Comment
#   - Activity Count, Denied Activity Count (ClaimSummaryV2)
#   - Submitted Net, Activity Variance, Activity Status (ClaimActivityV2)
#   - Days to Settlement (RemittanceActivityV2)
# ===========================================================================

CLAIM_SUMMARY_V2_COLS = [
    "Facility Name", "Claim Gateway", "Claim Number", "Receiver Id", "Receiver Name",
    "Payer ID", "Payer Name", "Patient ID", "Member Id", "Emirates ID",
    "Treatment Date", "Encounter End Date", "Submission Date", "Encounter Type",
    "Clinician Name", "Service Year", "Service Month", "Submission Level",
    "Gross Submitted", "Patient Share (CS)", "Net Amt - Initial Sub",
    "Net Amt - Resubmission", "Allowed Amount", "Patient Share (RA)", "RA Net",
    "Approved Amt", "Variance",
    "Initial Sub Rejected Amt", "Rejected Amt - Resubmission",
    "Unsettled Amt", "Payment Status",
    "Denial Code", "Denial Description", "TPA Comments", "Resubmission Comment",
    "Activity Count", "Denied Activity Count",
    "Payment Ref", "Settlement Date", "ID Payer",
    "Submission File", "Resub File Name", "RA File", "RA Date", "TAT (Days)", "Diagnosis",
]

CLAIM_ACTIVITY_V2_COLS = [
    "Facility Name", "Claim Gateway", "Claim Number", "Receiver Id", "Receiver Name",
    "Payer ID", "Payer Name", "Member Id", "Emirates ID",
    "Encounter Start", "Transaction Date",
    "Clinician ID", "Clinician Name", "Prior Auth ID",
    "CPT Type", "Activity Sequence No", "CPT Code",
    "Quantity", "Submitted Net",
    "Allowed Amount", "Patient Share (RA)", "Approved Net", "Payment Amount",
    "Activity Variance", "Activity Status",
    "Denial Code (Activity)", "Denial Description",
    "TPA Comments", "Claim Payment Status",
]

CLAIM_CYCLE_V2_COLS = [
    "Facility Name", "Claim Gateway", "Claim Number",
    "Receiver Id", "Receiver Name", "Payer ID", "Payer Name",
    "Member Id", "Emirates ID",
    "Encounter Start", "Encounter End", "Encounter Type",
    "Year", "Month",           # derived from encounter_start for easy pivot/filter
    "Submission Count", "Current Submission Stage", "Sub Rank",
    "RA Count", "Current RA Rank",
    "Claim Cycle Status",
    "Net Submitted",          # initial submission net only (S1)
    "RA Net Amount",          # remittances.net_amount from the FIRST RA received; 0 if no RA
    "Resub Amount",           # net from 1st resubmission (S2); 0 if none
    "Recons Amount",          # net from reconciliation resubmission (S3); 0 if none
    "Paid Amount",            # SUM(paid_amount) across ALL RA files received
    "Initial Denied",         # RA Net Amount − Paid Amount; 0 if no RA received
    "Variance",               # Net Submitted − Total Paid
    "Final Rejection",        # Denial Amount when no resubmission filed (claim settled as denied)
    "Resub Receivable",       # Outstanding − Unsettled/AR; actively contested amount expected to recover
    "Resub Rejected",         # min(Resub, Denial) when resub filed AND payer 2nd RA still denies; 0 otherwise
    "Unsettled/AR",           # Net Submitted if no RA received yet; 0 once RA arrives
    "Outstanding",            # Unsettled contested amount (4-scenario rule)
    "Payment Status",
    "Denial Code", "Denial Description",
    "TPA Comments",
    "Activity Count", "Denied Activity Count",
    "Diagnosis Type",          # pipe-separated types: Primary | Secondary …
    "Diagnosis Code",          # pipe-separated ICD codes in same order
    "Prior Authorization ID",  # pipe-separated distinct auth IDs from activities
    "Submission File",         # source filename of first CS XML
    "Last Resub File",         # source filename of latest resubmission (S2/S3); blank if none
    "Last Resub Type",         # Internal / Correction / Reconciliation; blank if no resub
    "Resub Comments",          # free-text provider comment from resubmission XML
    "Latest RA File",          # source filename of most-recent RA XML
    "Latest RA File Date",     # remittance_date from most-recent RA
]

REMITTANCE_ACTIVITY_V2_COLS = [
    "Facility ID", "Facility Name", "Claim Gateway", "Receiver ID", "Receiver Name",
    "Payer ID", "Payer Name", "Patient ID", "Member Id", "Emirates Id",
    "Encounter Start Date", "Encounter End Date",
    "Encounter Start Year", "Encounter Start Month",
    "Initial Submission Date", "Initial Submission File",
    "RA For Submission Date", "RA For Submission File",
    "Latest Submission Date", "Latest Submission File",
    "Transaction Date", "Date Settlement", "Days to Settlement",
    "Claim Number", "Encounter Type", "Encounter End Type",
    "Submission Count", "Current Submission Stage", "Sub Rank",
    "RA Count", "Current RA Rank", "Claim Cycle Status",
    "IDPayer", "Payment Reference",
    "Clinician", "Ordering Clinician",
    "Activity Code", "Activity Type", "Activity Type Name",
    "Submitted Net", "Allowed Amount", "Net Amt", "Patient Share",
    "Payment Amt", "Activity Variance",
    "Denial Code", "Denial Description", "Denial Category", "Denial Type",
    "TPA Comments",
    "Activity Detail ID",
    "RASubmission File Name", "RAFille Id",
    "Response Comments",
    "RAPament For",
]


# ---------------------------------------------------------------------------
# 7. CLAIM SUMMARY V2
# ---------------------------------------------------------------------------

def _build_claim_summary_v2(db, facility_id, payer_id, encounter_type,
                             date_from, date_to, date_field,
                             facility_ids=None):
    from collections import defaultdict as _dd
    from datetime import date as _date

    fac_map = {f.id: (f.facility_name, f.license_id) for f in db.query(Facility).all()}

    # ── Facility filter pushed into every subquery ────────────────────────────
    # Without this, each subquery scans ALL facilities before joining,
    # causing multi-minute hangs on large databases.
    if facility_ids:
        _fac_ids = [int(x) for x in facility_ids.split(",") if x.strip().isdigit()]
        _sq_claim_fac = Claim.facility_id.in_(_fac_ids)
        _sq_rem_fac   = Remittance.facility_id.in_(_fac_ids)
        _sq_rad_fac   = RemittanceActivityDetail.facility_id.in_(_fac_ids)
    else:
        _sq_claim_fac = (Claim.facility_id == facility_id)
        _sq_rem_fac   = (Remittance.facility_id == facility_id)
        _sq_rad_fac   = (RemittanceActivityDetail.facility_id == facility_id)

    # ── Scope: claim keys that can appear in the report ──────────────────────
    # Every subquery below is joined to this set so it only aggregates the
    # claims in the selected period, not the facility's whole history
    # (that caused 20+ minute runs for large facilities). It is a superset —
    # the exact filters are still applied to the main query by _apply_filters.
    _scope_q = db.query(
        Claim.facility_id.label("fid"), Claim.claim_id.label("cid"),
    ).filter(_sq_claim_fac)
    if date_field == "remittance_date":
        _scope_q = db.query(
            Remittance.facility_id.label("fid"), Remittance.claim_id.label("cid"),
        ).filter(_sq_rem_fac)
        if date_from:
            _scope_q = _scope_q.filter(Remittance.remittance_date >= date_from)
        if date_to:
            _scope_q = _scope_q.filter(Remittance.remittance_date <= date_to)
    else:
        # Range on the raw column (no cast) so the encounter_start index can be used
        if date_from:
            _scope_q = _scope_q.filter(Claim.encounter_start >= cast(date_from, Date))
        if date_to:
            _scope_q = _scope_q.filter(Claim.encounter_start < cast(date_to, Date) + 1)
    scope_cte = _scope_q.distinct().cte("cs_v2_scope")

    def _in_scope(fid_col, cid_col):
        return and_(fid_col == scope_cte.c.fid, cid_col == scope_cte.c.cid)

    # ── Sub-rank helper: S1=Initial, S2=Resubmission, S3=Reconciliation ──────
    _sub_rank = case(
        (Claim.resubmission_key == "",              1),
        (Claim.resubmission_key == "reconciliation", 3),
        else_=2,
    )

    # ── Latest submission per claim (one row per claim_id) ───────────────────
    # Highest sub_rank wins; tie-break on latest submission_date
    latest_rn_sq = (
        db.query(
            Claim.facility_id.label("fid"),
            Claim.claim_id.label("cid"),
            Claim.id.label("claim_pk"),
            func.row_number().over(
                partition_by=[Claim.facility_id, Claim.claim_id],
                order_by=[_sub_rank.desc(), Claim.submission_date.desc()],
            ).label("latest_rn"),
        )
        .join(scope_cte, _in_scope(Claim.facility_id, Claim.claim_id))
        .filter(_sq_claim_fac)
        .subquery()
    )

    # ── Initial submission: original amounts, sub date, xml id ───────────────
    initial_sub_sq = (
        db.query(
            Claim.facility_id.label("fid"),
            Claim.claim_id.label("cid"),
            Claim.net_amount.label("initial_net"),
            Claim.gross_amount.label("initial_gross"),
            Claim.patient_share_total.label("initial_ps"),
            Claim.submission_date.label("initial_sub_date"),
            Claim.raw_xml_id.label("initial_xml_id"),
            func.row_number().over(
                partition_by=[Claim.facility_id, Claim.claim_id],
                order_by=Claim.id.asc(),
            ).label("init_rn"),
        )
        .join(scope_cte, _in_scope(Claim.facility_id, Claim.claim_id))
        .filter(_sq_claim_fac, Claim.resubmission_key == "")
        .subquery()
    )

    # ── Latest resubmission row (S3 preferred over S2): net, comment, xml ────
    resub_rn_sq = (
        db.query(
            Claim.facility_id.label("fid"),
            Claim.claim_id.label("cid"),
            Claim.net_amount.label("resub_net"),
            Claim.resubmission_comment.label("resub_comment"),
            Claim.raw_xml_id.label("resub_xml_id"),
            func.row_number().over(
                partition_by=[Claim.facility_id, Claim.claim_id],
                order_by=_sub_rank.desc(),
            ).label("resub_rn"),
        )
        .join(scope_cte, _in_scope(Claim.facility_id, Claim.claim_id))
        .filter(_sq_claim_fac, Claim.resubmission_key != "")
        .subquery()
    )

    # ── First RA received (lowest id): RA Net ────────────────────────────────
    first_ra_rn_sq = (
        db.query(
            Remittance.facility_id.label("fid"),
            Remittance.claim_id.label("cid"),
            Remittance.id.label("rem_id"),
            func.row_number().over(
                partition_by=[Remittance.facility_id, Remittance.claim_id],
                order_by=Remittance.id.asc(),
            ).label("first_rn"),
        )
        .join(scope_cte, _in_scope(Remittance.facility_id, Remittance.claim_id))
        .filter(_sq_rem_fac)
        .subquery()
    )
    first_ra_rem = aliased(Remittance)

    # ── Latest RA received (highest id): denial, TPA comments, file ──────────
    latest_ra_rn_sq = (
        db.query(
            Remittance.facility_id.label("fid"),
            Remittance.claim_id.label("cid"),
            Remittance.id.label("rem_id"),
            func.row_number().over(
                partition_by=[Remittance.facility_id, Remittance.claim_id],
                order_by=Remittance.id.desc(),
            ).label("latest_rn"),
        )
        .join(scope_cte, _in_scope(Remittance.facility_id, Remittance.claim_id))
        .filter(_sq_rem_fac)
        .subquery()
    )
    latest_ra_rem = aliased(Remittance)

    # ── All RAs aggregate: total paid, ra count ───────────────────────────────
    ra_all_agg_sq = (
        db.query(
            Remittance.facility_id.label("fid"),
            Remittance.claim_id.label("cid"),
            func.count(Remittance.id).label("ra_count"),
            func.sum(Remittance.paid_amount).label("total_paid"),
        )
        .join(scope_cte, _in_scope(Remittance.facility_id, Remittance.claim_id))
        .filter(_sq_rem_fac)
        .group_by(Remittance.facility_id, Remittance.claim_id)
        .subquery()
    )

    # ── Allowed + patient share from RA activities (all RAs) ─────────────────
    allowed_sq = (
        db.query(
            RemittanceActivityDetail.facility_id.label("fid"),
            Remittance.claim_id.label("cid"),
            func.sum(RemittanceActivityDetail.gross).label("allowed_total"),
            func.sum(RemittanceActivityDetail.patient_share).label("ps_total_ra"),
        )
        .join(Remittance, Remittance.id == RemittanceActivityDetail.remittance_id_fk)
        .join(scope_cte, _in_scope(Remittance.facility_id, Remittance.claim_id))
        .filter(_sq_rad_fac)
        .group_by(RemittanceActivityDetail.facility_id, Remittance.claim_id)
        .subquery()
    )

    # ── Activity count from latest submission ─────────────────────────────────
    act_count_sq = (
        db.query(
            ClaimActivity.claim_id_fk.label("claim_pk"),
            func.count(ClaimActivity.id).label("act_count"),
        )
        .join(Claim, Claim.id == ClaimActivity.claim_id_fk)
        .join(scope_cte, _in_scope(Claim.facility_id, Claim.claim_id))
        .filter(_sq_claim_fac)
        .group_by(ClaimActivity.claim_id_fk)
        .subquery()
    )

    # ── Denied activity count across all RAs ─────────────────────────────────
    denied_act_sq = (
        db.query(
            RemittanceActivityDetail.facility_id.label("fid"),
            Remittance.claim_id.label("cid"),
            func.count(RemittanceActivityDetail.id).label("denied_count"),
        )
        .join(Remittance, Remittance.id == RemittanceActivityDetail.remittance_id_fk)
        .join(scope_cte, _in_scope(Remittance.facility_id, Remittance.claim_id))
        .filter(
            _sq_rad_fac,
            RemittanceActivityDetail.paid_amount == 0,
            RemittanceActivityDetail.denial_code.isnot(None),
        )
        .group_by(RemittanceActivityDetail.facility_id, Remittance.claim_id)
        .subquery()
    )

    # ── Main query: ONE ROW PER CLAIM_ID (latest submission only) ────────────
    q = (
        db.query(
            Claim,                                    # [0]
            initial_sub_sq.c.initial_net,             # [1]
            initial_sub_sq.c.initial_gross,           # [2]
            initial_sub_sq.c.initial_ps,              # [3]
            initial_sub_sq.c.initial_sub_date,        # [4]
            initial_sub_sq.c.initial_xml_id,          # [5]
            resub_rn_sq.c.resub_net,                  # [6]
            resub_rn_sq.c.resub_comment,              # [7]
            resub_rn_sq.c.resub_xml_id,               # [8]
            first_ra_rem,                             # [9]
            latest_ra_rem,                            # [10]
            ra_all_agg_sq.c.ra_count,                 # [11]
            ra_all_agg_sq.c.total_paid,               # [12]
            allowed_sq.c.allowed_total,               # [13]
            allowed_sq.c.ps_total_ra,                 # [14]
            act_count_sq.c.act_count,                 # [15]
            denied_act_sq.c.denied_count,             # [16]
        )
        # INNER JOIN: keep only the latest submission row per claim_id
        .join(latest_rn_sq, and_(
            Claim.facility_id == latest_rn_sq.c.fid,
            Claim.claim_id    == latest_rn_sq.c.cid,
            Claim.id          == latest_rn_sq.c.claim_pk,
            latest_rn_sq.c.latest_rn == 1,
        ))
        .outerjoin(initial_sub_sq, and_(
            Claim.facility_id == initial_sub_sq.c.fid,
            Claim.claim_id    == initial_sub_sq.c.cid,
            initial_sub_sq.c.init_rn == 1,
        ))
        .outerjoin(resub_rn_sq, and_(
            Claim.facility_id     == resub_rn_sq.c.fid,
            Claim.claim_id        == resub_rn_sq.c.cid,
            resub_rn_sq.c.resub_rn == 1,
        ))
        .outerjoin(first_ra_rn_sq, and_(
            Claim.facility_id          == first_ra_rn_sq.c.fid,
            Claim.claim_id             == first_ra_rn_sq.c.cid,
            first_ra_rn_sq.c.first_rn  == 1,
        ))
        .outerjoin(first_ra_rem, first_ra_rem.id == first_ra_rn_sq.c.rem_id)
        .outerjoin(latest_ra_rn_sq, and_(
            Claim.facility_id            == latest_ra_rn_sq.c.fid,
            Claim.claim_id               == latest_ra_rn_sq.c.cid,
            latest_ra_rn_sq.c.latest_rn  == 1,
        ))
        .outerjoin(latest_ra_rem, latest_ra_rem.id == latest_ra_rn_sq.c.rem_id)
        .outerjoin(ra_all_agg_sq, and_(
            Claim.facility_id == ra_all_agg_sq.c.fid,
            Claim.claim_id    == ra_all_agg_sq.c.cid,
        ))
        .outerjoin(allowed_sq, and_(
            Claim.facility_id == allowed_sq.c.fid,
            Claim.claim_id    == allowed_sq.c.cid,
        ))
        .outerjoin(act_count_sq, act_count_sq.c.claim_pk == Claim.id)
        .outerjoin(denied_act_sq, and_(
            Claim.facility_id == denied_act_sq.c.fid,
            Claim.claim_id    == denied_act_sq.c.cid,
        ))
    )
    q = _apply_filters(q, facility_id, payer_id, encounter_type,
                       date_from, date_to, date_field, rem_model=latest_ra_rem,
                       facility_ids=facility_ids)
    # Same safety net as Claim Cycle V2: on long periods the planner picks nested loops over the
    # subqueries and one facility-year ran for over an hour; hash joins finish in minutes.
    from sqlalchemy import text as _text
    db.execute(_text("SET LOCAL enable_nestloop = off"))
    rows = q.order_by(Claim.submission_date.desc(), Claim.claim_id).all()
    db.execute(_text("SET LOCAL enable_nestloop = on"))

    # ── Bulk XML filename lookup ──────────────────────────────────────────────
    xml_ids: set = set()
    for row in rows:
        claim = row[0]
        first_rem  = row[9]
        latest_rem = row[10]
        init_xml   = row[5]
        resub_xml  = row[8]
        if claim.raw_xml_id:  xml_ids.add(claim.raw_xml_id)
        if first_rem  and first_rem.raw_xml_id:  xml_ids.add(first_rem.raw_xml_id)
        if latest_rem and latest_rem.raw_xml_id: xml_ids.add(latest_rem.raw_xml_id)
        if init_xml:   xml_ids.add(init_xml)
        if resub_xml:  xml_ids.add(resub_xml)
    xml_filename_map: dict = {}
    if xml_ids:
        for xs in db.query(RawXmlStore.id, RawXmlStore.source_filename).filter(
            RawXmlStore.id.in_(xml_ids)
        ).all():
            xml_filename_map[xs.id] = xs.source_filename or ""

    # ── Diagnosis map ─────────────────────────────────────────────────────────
    fac_claims: dict = _dd(set)
    for row in rows:
        claim = row[0]
        fac_claims[claim.facility_id].add(claim.claim_id)
    diag_map = _build_diagnosis_map(db, dict(fac_claims))

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Claim Summary V2"
    ws.freeze_panes = "A2"
    _write_headers(ws, CLAIM_SUMMARY_V2_COLS)

    for row_idx, row in enumerate(rows, start=2):
        (claim,
         initial_net_raw, initial_gross_raw, initial_ps_raw,
         initial_sub_date, initial_xml_id,
         resub_net_raw, resub_comment_raw, resub_xml_id,
         first_rem, latest_rem,
         ra_count_raw, total_paid_raw,
         allowed_total_raw, ps_ra_raw,
         act_count_raw, denied_count_raw) = row

        fac_name, _ = fac_map.get(claim.facility_id, ("", ""))
        sub_level    = _submission_level(claim.resubmission_type)
        is_resub     = claim.resubmission_key != ""

        # ── Financial amounts ────────────────────────────────────────────────
        initial_net  = float(initial_net_raw)   if initial_net_raw   is not None else 0.0
        initial_gross= float(initial_gross_raw) if initial_gross_raw is not None else 0.0
        initial_ps   = float(initial_ps_raw)    if initial_ps_raw    is not None else 0.0
        resub_net    = float(resub_net_raw)      if resub_net_raw     is not None else None
        ra_count     = int(ra_count_raw)         if ra_count_raw      is not None else 0
        total_paid   = float(total_paid_raw)     if total_paid_raw    is not None else 0.0
        allowed_f    = float(allowed_total_raw)  if allowed_total_raw is not None else 0.0
        ps_ra_f      = float(ps_ra_raw)          if ps_ra_raw         is not None else 0.0

        # RA Net = net_amount from FIRST RA (0 if no RA received)
        ra_net_val   = float(first_rem.net_amount) if first_rem and first_rem.net_amount is not None else 0.0

        # Approved Amt = cumulative paid across ALL RA files
        approved     = _f(total_paid)

        # Variance = Net Submitted − Total Paid
        variance     = round(initial_net - total_paid, 2)

        # Unsettled = max(0, initial_net − total_paid)
        unsettled    = round(max(0.0, initial_net - total_paid), 2)

        # Rejection split: initial vs resubmission row
        rejected_amt = round(max(0.0, initial_net - total_paid), 2)
        initial_rejected = rejected_amt if not is_resub else 0.0
        resub_rejected   = rejected_amt if is_resub     else 0.0

        # ── Payment Status (mirrors ClaimCycleV2 logic) ─────────────────────
        if ra_count == 0:
            pay_status = "Pending"
        elif total_paid >= initial_net and initial_net > 0:
            pay_status = "Fully Paid"
        elif total_paid == 0 and latest_rem and latest_rem.denial_code:
            pay_status = "Fully Denied"
        elif total_paid == 0:
            pay_status = "Fully Rejected"
        else:
            pay_status = "Partially Paid"

        # ── Denial / TPA from latest RA ──────────────────────────────────────
        den_code     = (latest_rem.denial_code        or "") if latest_rem else ""
        den_desc     = (latest_rem.denial_description or "") if latest_rem else ""
        tpa_comments = (latest_rem.tpa_comments       or "") if latest_rem else ""
        ra_date_val  = _fmt_date(latest_rem.remittance_date) if latest_rem else ""
        pay_ref      = latest_rem.ra_file_id     if latest_rem else ""
        settle_dt    = _fmt_date(latest_rem.remittance_date) if latest_rem else ""
        id_payer     = latest_rem.claim_id_payer if latest_rem else ""

        # ── TAT ──────────────────────────────────────────────────────────────
        tat = 0
        init_date = initial_sub_date or claim.submission_date
        if ra_count == 0 and init_date:
            try:
                tat = (_date.today() - init_date).days
            except Exception:
                pass
        elif latest_rem and latest_rem.remittance_date and init_date:
            try:
                tat = (latest_rem.remittance_date - init_date).days
            except Exception:
                pass

        # ── Misc ─────────────────────────────────────────────────────────────
        svc_year     = claim.encounter_start.year           if claim.encounter_start else ""
        svc_month    = claim.encounter_start.strftime("%B") if claim.encounter_start else ""
        sub_file     = xml_filename_map.get(initial_xml_id, "") if initial_xml_id else ""
        resub_file   = xml_filename_map.get(resub_xml_id,   "") if resub_xml_id   else ""
        ra_file      = xml_filename_map.get(latest_rem.raw_xml_id, "") if latest_rem and latest_rem.raw_xml_id else ""
        resub_comment= resub_comment_raw or ""
        diagnosis    = diag_map.get((claim.facility_id, claim.claim_id), "")

        _write_row(ws, row_idx, [
            fac_name,                                                          # Facility Name
            _claim_gateway(claim.source_channel, claim.sender_id),                              # Claim Gateway
            claim.claim_id,                                                    # Claim Number
            _derive_receiver_id(claim.receiver_id, claim.payer_id),           # Receiver Id
            _payer_name(_derive_receiver_id(claim.receiver_id, claim.payer_id)), # Receiver Name
            claim.payer_id,                                                    # Payer ID
            _payer_name(claim.payer_id),                                       # Payer Name
            claim.patient_id or "",                                            # Patient ID
            claim.member_id,                                                   # Member Id
            claim.emirates_id or "",                                           # Emirates ID
            _fmt_date(claim.encounter_start),                                  # Treatment Date
            _fmt_date(claim.encounter_end),                                    # Encounter End Date
            _fmt_date(claim.submission_date),                                  # Submission Date (latest)
            _enc_label(claim.encounter_type),                                  # Encounter Type
            _clinician_name(claim.clinician_id or "") or claim.clinician_name or "", # Clinician Name
            svc_year,                                                          # Service Year
            svc_month,                                                         # Service Month
            sub_level,                                                         # Submission Level (latest)
            _f(initial_gross) if initial_gross else "",                        # Gross Submitted (S1)
            _f(initial_ps),                                                    # Patient Share CS (S1)
            _f(initial_net),                                                   # Net Amt - Initial Sub
            _f(resub_net) if resub_net is not None else 0.0,                   # Net Amt - Resubmission
            _f(allowed_f) if allowed_f else "",                                # Allowed Amount
            _f(ps_ra_f)   if ps_ra_f   else "",                                # Patient Share RA
            ra_net_val,                                                        # RA Net (first RA)
            approved,                                                          # Approved Amt (cumulative paid)
            variance,                                                          # Variance
            initial_rejected,                                                  # Initial Sub Rejected Amt
            resub_rejected,                                                    # Rejected Amt - Resubmission
            unsettled,                                                         # Unsettled Amt
            pay_status,                                                        # Payment Status
            den_code,                                                          # Denial Code
            den_desc,                                                          # Denial Description
            tpa_comments,                                                      # TPA Comments
            resub_comment,                                                     # Resubmission Comment
            act_count_raw or 0,                                                # Activity Count
            denied_count_raw or 0,                                             # Denied Activity Count
            pay_ref,                                                           # Payment Ref
            settle_dt,                                                         # Settlement Date
            id_payer,                                                          # ID Payer
            sub_file,                                                          # Submission File (S1 xml)
            resub_file,                                                        # Resub File Name
            ra_file,                                                           # RA File (latest RA xml)
            ra_date_val,                                                       # RA Date
            tat,                                                               # TAT (Days)
            diagnosis,                                                         # Diagnosis
        ], alt=(row_idx % 2 == 0))

    _auto_width(ws, CLAIM_SUMMARY_V2_COLS)
    ts = datetime.now().strftime("%Y%m%d_%H%M")
    return wb, f"ClaimSummaryV2_{ts}.xlsx", len(rows)


@router.get("/claim-summary-v2")
def export_claim_summary_v2(
    facility_id:    Optional[int]  = None,
    payer_id:       Optional[str]  = None,
    encounter_type: Optional[str]  = None,
    date_from:      Optional[date] = None,
    date_to:        Optional[date] = None,
    date_field:     Optional[str]  = "treatment_date",
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Claim Summary V2 — enhanced with Emirates ID, financials, variance,
    TPA comments, resubmission comment, activity counts."""
    wb, filename, _ = _build_claim_summary_v2(
        db, facility_id, payer_id, encounter_type, date_from, date_to, date_field)
    return _excel_response(wb, filename)


# ---------------------------------------------------------------------------
# 8. CLAIM ACTIVITY V2
# ---------------------------------------------------------------------------

def _build_claim_activity_v2(db, facility_id, payer_id, encounter_type,
                              date_from, date_to, date_field,
                              facility_ids=None):
    fac_map = {f.id: (f.facility_name, f.license_id) for f in db.query(Facility).all()}

    # ── Ranked remittances per claim (mirrors ClaimSummaryV2 approach) ─────────
    # ra_rank=1 → first (initial) RA ordered by id
    # ra_rank=2 → second (resubmission) RA ordered by id
    # Each activity row joins to the rank-appropriate RA:
    #   initial claim  (resubmission_key == "")  → ra_rank = 1
    #   resubmission   (resubmission_key != "")  → ra_rank = 2
    # This eliminates duplicate rows and ensures TPA comments / denial codes
    # are drawn from the correct adjudication pass for each activity.
    rem_ranked_sq = (
        db.query(
            Remittance.facility_id,
            Remittance.claim_id,
            Remittance.id.label("rem_id"),
            Remittance.paid_amount.label("rem_paid"),
            Remittance.denial_code.label("rem_denial"),
            Remittance.claim_status_ra.label("rem_claim_status"),
            Remittance.tpa_comments.label("rem_tpa"),
            func.row_number().over(
                partition_by=[Remittance.facility_id, Remittance.claim_id],
                order_by=Remittance.id,
            ).label("ra_rank"),
        ).subquery()
    )
    rem_alias = aliased(Remittance)
    rad_alias = aliased(RemittanceActivityDetail)

    q = (
        db.query(ClaimActivity, Claim, rem_alias, rad_alias)
        .join(Claim, Claim.id == ClaimActivity.claim_id_fk)
        # Join ranked remittance: initial claim → rank 1, resubmission → rank 2
        # DHA eClaimLink: each ClaimSubmission resubmission is a distinct claim_id
        # namespace; its RA is the next sequential remittance for that claim_id.
        .outerjoin(
            rem_ranked_sq,
            and_(
                rem_ranked_sq.c.facility_id == Claim.facility_id,
                rem_ranked_sq.c.claim_id    == Claim.claim_id,
                rem_ranked_sq.c.ra_rank     == case(
                    (Claim.resubmission_key == "",              1),
                    (Claim.resubmission_key == "reconciliation", 3),
                    else_=2,
                ),
            )
        )
        .outerjoin(rem_alias, rem_alias.id == rem_ranked_sq.c.rem_id)
        # Join RAD via remittance id + activity_id (both VARCHAR).
        # claim_activities.activity_id == remittance_activity_details.activity_id
        # Both store the <Activity><ID> string from their respective XML documents.
        # Do NOT use sequence_no — it is a loop counter, not the XML activity ID.
        .outerjoin(
            rad_alias,
            (rad_alias.remittance_id_fk == rem_alias.id) &
            (rad_alias.activity_id      == ClaimActivity.activity_id)
        )
    )
    if facility_ids:
        _fids = [int(x) for x in facility_ids.split(',') if x.strip().isdigit()]
        if _fids:
            q = q.filter(Claim.facility_id.in_(_fids))
    elif facility_id:
        q = q.filter(Claim.facility_id == facility_id)
    if payer_id:
        q = q.filter(Claim.payer_id == payer_id)
    if encounter_type:
        q = q.filter(Claim.encounter_type == encounter_type)
    # Date filter on encounter_start for boundary consistency (NOT activity_start)
    if date_from:
        q = q.filter(cast(Claim.encounter_start, Date) >= date_from)
    if date_to:
        q = q.filter(cast(Claim.encounter_start, Date) <= date_to)

    rows = q.order_by(Claim.submission_date.desc(), ClaimActivity.sequence_no).all()

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Claim Activity V2"
    ws.freeze_panes = "A2"
    _write_headers(ws, CLAIM_ACTIVITY_V2_COLS)

    for row_idx, (act, claim, rem, rad) in enumerate(rows, start=2):
        fac_name, _ = fac_map.get(claim.facility_id, ("", ""))

        # Activity-level financials
        sub_net     = _f(act.net) if act.net is not None else ""
        allowed_amt = _f(rad.gross)         if rad and rad.gross         is not None else ""
        ps_ra       = _f(rad.patient_share) if rad and rad.patient_share is not None else ""
        appr_net    = _f(rad.net)           if rad and rad.net           is not None else ""
        pay_amt     = _f(rad.paid_amount)   if rad and rad.paid_amount   is not None else ""

        # Activity variance: submitted − paid
        if act.net is not None and rad and rad.paid_amount is not None:
            act_var = round(float(act.net) - float(rad.paid_amount), 2)
        else:
            act_var = ""

        # Activity status
        if rad is None:
            act_status = "Pending"
        elif rad.denial_code and float(rad.paid_amount) == 0:
            act_status = "Denied"
        elif act.net is not None and rad.paid_amount is not None and float(rad.paid_amount) >= float(act.net):
            act_status = "Fully Paid"
        elif rad.paid_amount is not None and float(rad.paid_amount) > 0:
            act_status = "Partially Paid"
        else:
            act_status = "Pending"

        den_code_act  = rad.denial_code or ""   if rad else ""
        den_desc_act  = _lookup_denial(den_code_act)[0] if den_code_act else ""
        tpa_comments  = rem.tpa_comments or ""  if rem else ""
        claim_pay_st  = rem.claim_status_ra or "" if rem else ""

        _write_row(ws, row_idx, [
            fac_name,
            _claim_gateway(claim.source_channel, claim.sender_id),
            claim.claim_id,
            _derive_receiver_id(claim.receiver_id, claim.payer_id),
            _payer_name(_derive_receiver_id(claim.receiver_id, claim.payer_id)),
            claim.payer_id,
            _payer_name(claim.payer_id),
            claim.member_id,
            claim.emirates_id or "",                                           # Emirates ID (NEW)
            _fmt_date(act.activity_start),
            _fmt_date(act.activity_start),
            act.clinician_id or claim.clinician_id or "",
            _clinician_name(act.clinician_id or claim.clinician_id or ""),
            act.authorization_id or "",
            act.activity_type or "",
            act.sequence_no or "",
            act.activity_code or "",
            _f(act.quantity),
            sub_net,                                                           # Submitted Net (NEW label)
            allowed_amt,                                                       # Allowed Amount (NEW)
            ps_ra,                                                             # Patient Share RA (NEW)
            appr_net,                                                          # Approved Net (NEW)
            pay_amt,                                                           # Payment Amount (NEW)
            act_var,                                                           # Activity Variance (NEW)
            act_status,                                                        # Activity Status (NEW)
            den_code_act,                                                      # Denial Code Activity (NEW)
            den_desc_act,                                                      # Denial Description (NEW)
            tpa_comments,                                                      # TPA Comments (NEW)
            claim_pay_st,                                                      # Claim Payment Status
        ], alt=(row_idx % 2 == 0))

    _auto_width(ws, CLAIM_ACTIVITY_V2_COLS)
    ts = datetime.now().strftime("%Y%m%d_%H%M")
    return wb, f"ClaimActivityV2_{ts}.xlsx", len(rows)


@router.get("/claim-activity-v2")
def export_claim_activity_v2(
    facility_id:    Optional[int]  = None,
    payer_id:       Optional[str]  = None,
    encounter_type: Optional[str]  = None,
    date_from:      Optional[date] = None,
    date_to:        Optional[date] = None,
    date_field:     Optional[str]  = "treatment_date",
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Claim Activity V2 — enhanced with submitted net, allowed amount,
    patient share, payment amount, activity variance/status, TPA comments."""
    wb, filename, _ = _build_claim_activity_v2(
        db, facility_id, payer_id, encounter_type, date_from, date_to, date_field)
    return _excel_response(wb, filename)


# ---------------------------------------------------------------------------
# 9. CLAIM CYCLE V2
# One row per claim_id — aggregated lifecycle view.
# Shows the current Sub Rank (highest submission received) and RA Rank
# (highest adjudication received) with a plain-language Claim Cycle Status.
#
# Sub Rank logic (mirrors the ranked RA join used in all V2 reports):
#   resubmission_key = ""              → Sub Rank 1 (Initial Submission)
#   resubmission_key = "internal
#     complaint" | "correction" | …   → Sub Rank 2 (1st Resubmission)
#   resubmission_key = "reconciliation"→ Sub Rank 3 (Reconciliation Resubmission)
#
# RA Rank = ROW_NUMBER() OVER (PARTITION BY facility_id, claim_id ORDER BY id)
# Pairing rule: RA Rank always equals the Sub Rank it adjudicates.
# ---------------------------------------------------------------------------

def _sub_stage_label(resub_key):
    if not resub_key or resub_key == "":
        return "Initial Submission"
    if str(resub_key).lower() == "reconciliation":
        return "Reconciliation Resubmission"
    return "1st Resubmission"


def _cycle_status(sub_rank, ra_rank, ra_count, net_amt, paid_amt, denial_code):
    """Human-readable lifecycle status combining Sub Rank + RA Rank + payment."""
    sub_labels  = {1: "Initial Submission", 2: "1st Resubmission", 3: "Reconciliation Resubmission"}
    next_labels = {1: "1st Resubmission",   2: "Reconciliation Resubmission"}
    sub_lbl     = sub_labels.get(sub_rank, f"Sub {sub_rank}")

    if not ra_count or ra_count == 0:
        return f"{sub_lbl} — Awaiting RA"

    if ra_rank < sub_rank:
        return f"{sub_lbl} — Awaiting RA  ({ra_rank}/{sub_rank} RAs received)"

    # ra_rank == sub_rank: latest RA has been received for latest submission
    if paid_amt is None:
        return f"{sub_lbl} — Pending"

    paid = float(paid_amt)
    net  = float(net_amt) if net_amt else 0.0

    if paid >= net:
        pay = "Fully Paid"
    elif paid == 0 and denial_code:
        pay = "Fully Denied"
    elif paid == 0:
        pay = "Fully Rejected"
    else:
        pay = "Partially Paid"

    if pay == "Fully Paid":
        return f"Closed — Fully Paid  ({sub_lbl})"
    if sub_rank >= 3:
        return f"Final — {pay}  (Reconciliation)"
    nxt = next_labels.get(sub_rank, "Escalation")
    return f"{pay} — {nxt} Window Open"


RESUB_WINDOW_DAYS = 21  # DHA: resubmission must be posted within 21 days of the RA


def _build_claim_cycle_v2(db, facility_id, payer_id, encounter_type,
                           date_from, date_to, date_field,
                           facility_ids=None):
    from sqlalchemy import text as _text

    fac_map = {f.id: (f.facility_name, f.license_id) for f in db.query(Facility).all()}

    # ── Dynamic WHERE clauses ─────────────────────────────────────────────────
    claim_where = "1=1"
    rem_where   = "1=1"
    params: dict = {}

    if facility_ids:
        _fids = [int(x) for x in facility_ids.split(",") if x.strip().isdigit()]
        if _fids:
            ph = ", ".join(f":fid{i}" for i in range(len(_fids)))
            claim_where += f" AND c.facility_id IN ({ph})"
            rem_where   += f" AND r.facility_id IN ({ph})"
            for i, fv in enumerate(_fids):
                params[f"fid{i}"] = fv
    elif facility_id:
        claim_where += " AND c.facility_id = :facility_id"
        rem_where   += " AND r.facility_id = :facility_id"
        params["facility_id"] = facility_id

    if payer_id:
        claim_where += " AND c.payer_id = :payer_id"
        params["payer_id"] = payer_id

    if encounter_type:
        claim_where += " AND c.encounter_type = :encounter_type"
        params["encounter_type"] = encounter_type

    # Date filter always on encounter_start (consistent with all V2 reports)
    # Plain range on the column (not DATE(...)) so the planner estimates the row count
    # correctly; a wrapped column made it expect ~10 rows and pick quadratic nested loops.
    if date_from:
        claim_where += " AND c.encounter_start >= CAST(:date_from AS date)"
        params["date_from"] = str(date_from)
    if date_to:
        claim_where += " AND c.encounter_start < CAST(:date_to AS date) + 1"
        params["date_to"] = str(date_to)

    sql = _text(f"""
        WITH scope AS (
            -- Claims in the selected period: remittance CTEs are limited to these,
            -- instead of scanning every RA the facility has ever received.
            SELECT DISTINCT c.facility_id, c.claim_id
            FROM claims c
            WHERE {claim_where}
        ),
        sub_ranked AS (
            -- Rank each claim row by its submission level (1=Initial, 2=Resub, 3=Recons).
            -- latest_sub_rn = 1 identifies the highest submission for each claim_id.
            SELECT
                c.id                                                    AS claim_pk,
                c.facility_id,
                c.claim_id,
                c.payer_id,
                c.receiver_id,
                c.source_channel,
                c.sender_id,
                c.member_id,
                c.emirates_id,
                c.encounter_start,
                c.encounter_end,
                c.encounter_type,
                c.resubmission_key,
                CASE c.resubmission_key
                    WHEN ''               THEN 1
                    WHEN 'reconciliation' THEN 3
                    ELSE                       2
                END AS sub_rank,
                ROW_NUMBER() OVER (
                    PARTITION BY c.facility_id, c.claim_id
                    ORDER BY CASE c.resubmission_key
                        WHEN ''               THEN 1
                        WHEN 'reconciliation' THEN 3
                        ELSE                       2
                    END DESC,
                    c.id DESC   -- tie-break: latest row wins when same sub_rank
                ) AS latest_sub_rn
            FROM claims c
            WHERE {claim_where}
        ),
        sub_counts AS (
            -- Total distinct submission levels per claim
            SELECT facility_id, claim_id, COUNT(*) AS sub_count
            FROM claims c
            WHERE {claim_where}
            GROUP BY facility_id, claim_id
        ),
        initial_sub AS (
            -- Earliest initial submission row (resubmission_key = '').
            -- DISTINCT ON prevents fan-out when a claim has >1 S1 row.
            SELECT DISTINCT ON (c.facility_id, c.claim_id)
                c.facility_id,
                c.claim_id,
                c.net_amount          AS initial_net,
                rx.source_filename    AS sub_file
            FROM claims c
            LEFT JOIN raw_xml_store rx ON rx.id = c.raw_xml_id
            WHERE c.resubmission_key = ''
              AND {claim_where}
            ORDER BY c.facility_id, c.claim_id, c.id ASC
        ),
        resub_sub AS (
            -- Latest S2 resubmission row (non-empty, non-reconciliation key).
            -- DISTINCT ON prevents fan-out when a claim has >1 S2 row (root cause of duplicates).
            SELECT DISTINCT ON (c.facility_id, c.claim_id)
                c.facility_id,
                c.claim_id,
                c.net_amount  AS resub_net
            FROM claims c
            WHERE c.resubmission_key NOT IN ('', 'reconciliation')
              AND {claim_where}
            ORDER BY c.facility_id, c.claim_id, c.id DESC
        ),
        recons_sub AS (
            -- Latest S3 reconciliation row.
            -- DISTINCT ON prevents fan-out when a claim has >1 reconciliation row.
            SELECT DISTINCT ON (c.facility_id, c.claim_id)
                c.facility_id,
                c.claim_id,
                c.net_amount  AS recons_net
            FROM claims c
            WHERE c.resubmission_key = 'reconciliation'
              AND {claim_where}
            ORDER BY c.facility_id, c.claim_id, c.id DESC
        ),
        ra_all_agg AS (
            -- Aggregate across ALL remittances for the claim:
            -- total paid = sum of paid_amount from every RA file received.
            SELECT
                r.facility_id,
                r.claim_id,
                COUNT(*)                          AS ra_count,
                COALESCE(SUM(r.paid_amount), 0)   AS total_paid,
                COALESCE(SUM(r.net_amount),  0)   AS total_ra_net
            FROM remittances r
            JOIN scope sp ON sp.facility_id = r.facility_id AND sp.claim_id = r.claim_id
            WHERE {rem_where}
            GROUP BY r.facility_id, r.claim_id
        ),
        first_ra AS (
            -- FIRST RA received per claim (lowest r.id = earliest stored RA).
            -- DISTINCT ON is faster than ROW_NUMBER() for large remittances tables.
            SELECT DISTINCT ON (r.facility_id, r.claim_id)
                   r.facility_id, r.claim_id, r.net_amount AS first_ra_net
            FROM remittances r
            JOIN scope sp ON sp.facility_id = r.facility_id AND sp.claim_id = r.claim_id
            WHERE {rem_where}
            ORDER BY r.facility_id, r.claim_id, r.id ASC
        ),
        latest_ra AS (
            -- Most-recent RA per claim: provides denial info, TPA comments, RA file name,
            -- and net_amount (used as RA Net Amount — latest payer acknowledgment).
            SELECT
                r.facility_id,
                r.claim_id,
                r.id              AS rem_id,
                r.net_amount      AS latest_ra_net,
                r.denial_code,
                r.denial_description,
                r.claim_status_ra,
                r.tpa_comments,
                r.remittance_date,
                rx.source_filename AS ra_file,
                ROW_NUMBER() OVER (
                    PARTITION BY r.facility_id, r.claim_id
                    ORDER BY r.id DESC
                ) AS latest_rn
            FROM remittances r
            JOIN scope sp ON sp.facility_id = r.facility_id AND sp.claim_id = r.claim_id
            LEFT JOIN raw_xml_store rx ON rx.id = r.raw_xml_id
            WHERE {rem_where}
        ),
        rad_agg AS (
            -- Denied activity count from the latest RA's activity details.
            SELECT
                lr.facility_id,
                lr.claim_id,
                COUNT(CASE WHEN rad.paid_amount = 0
                           AND  rad.denial_code IS NOT NULL THEN 1 END) AS denied_act_count
            FROM latest_ra lr
            JOIN remittance_activity_details rad ON rad.remittance_id_fk = lr.rem_id
            WHERE lr.latest_rn = 1
            GROUP BY lr.facility_id, lr.claim_id
        ),
        act_counts AS (
            -- Activity count from the latest submission's claim row.
            SELECT
                sr.facility_id,
                sr.claim_id,
                COUNT(ca.id) AS act_count
            FROM sub_ranked sr
            LEFT JOIN claim_activities ca ON ca.claim_id_fk = sr.claim_pk
            WHERE sr.latest_sub_rn = 1
            GROUP BY sr.facility_id, sr.claim_id
        ),
        auth_ids AS (
            -- Distinct prior auth IDs across all submissions, pipe-separated.
            SELECT
                c.facility_id,
                c.claim_id,
                STRING_AGG(DISTINCT ca.authorization_id, ' | ') AS auth_id_list
            FROM claim_activities ca
            JOIN claims c ON c.id = ca.claim_id_fk
            WHERE ca.authorization_id IS NOT NULL
              AND ca.authorization_id <> ''
              AND {claim_where}
            GROUP BY c.facility_id, c.claim_id
        ),
        last_resub_ranked AS (
            -- Rank resubmission rows (S2 / S3) to find the latest one.
            SELECT
                c.facility_id,
                c.claim_id,
                rx.source_filename      AS resub_file,
                c.resubmission_type     AS resub_type,
                c.resubmission_comment  AS resub_comment,
                ROW_NUMBER() OVER (
                    PARTITION BY c.facility_id, c.claim_id
                    ORDER BY CASE c.resubmission_key
                        WHEN 'reconciliation' THEN 3
                        ELSE 2
                    END DESC,
                    c.id DESC   -- tie-break: latest row wins when same sub_rank
                ) AS rn
            FROM claims c
            LEFT JOIN raw_xml_store rx ON rx.id = c.raw_xml_id
            WHERE c.resubmission_key <> ''
              AND {claim_where}
        ),
        last_resub_sub AS (
            -- Only the highest resubmission (S3 if exists, else S2).
            SELECT facility_id, claim_id, resub_file, resub_type, resub_comment
            FROM last_resub_ranked
            WHERE rn = 1
        ),
        diag_agg AS (
            -- Diagnosis type and code from the initial submission, ordered Primary first.
            SELECT
                c.facility_id,
                c.claim_id,
                STRING_AGG(
                    cd.diagnosis_type,
                    ' | '
                    ORDER BY
                        CASE cd.diagnosis_type
                            WHEN 'Primary'   THEN 0
                            WHEN 'Admitting' THEN 1
                            ELSE 2
                        END,
                        cd.id
                ) AS diag_types,
                STRING_AGG(
                    cd.diagnosis_code,
                    ' | '
                    ORDER BY
                        CASE cd.diagnosis_type
                            WHEN 'Primary'   THEN 0
                            WHEN 'Admitting' THEN 1
                            ELSE 2
                        END,
                        cd.id
                ) AS diag_codes
            FROM claim_diagnoses cd
            JOIN claims c ON c.id = cd.claim_id_fk
            WHERE c.resubmission_key = ''
              AND {claim_where}
            GROUP BY c.facility_id, c.claim_id
        )
        SELECT
            sr.facility_id,
            sr.claim_id,
            sr.payer_id,
            sr.receiver_id,
            sr.source_channel,
            sr.sender_id,
            sr.member_id,
            sr.emirates_id,
            sr.encounter_start,
            sr.encounter_end,
            sr.encounter_type,
            sr.resubmission_key,
            sr.sub_rank,
            sc.sub_count,
            isub.initial_net,
            isub.sub_file,
            COALESCE(rs.resub_net,          0) AS resub_net,
            COALESCE(rc.recons_net,         0) AS recons_net,
            COALESCE(ra.ra_count,           0) AS ra_count,
            COALESCE(ra.total_paid,         0) AS total_paid,
            COALESCE(ra.total_ra_net,       0) AS total_ra_net,
            fra.first_ra_net,
            lr.latest_ra_net,
            lr.denial_code,
            lr.denial_description,
            lr.claim_status_ra,
            lr.tpa_comments,
            lr.ra_file,
            lr.remittance_date              AS ra_date,
            COALESCE(rd.denied_act_count,   0) AS denied_act_count,
            COALESCE(ac.act_count,          0) AS act_count,
            da.diag_types,
            da.diag_codes,
            ai.auth_id_list,
            lrs.resub_file,
            lrs.resub_type,
            lrs.resub_comment
        FROM sub_ranked sr
        JOIN sub_counts sc
            ON  sc.facility_id = sr.facility_id
            AND sc.claim_id    = sr.claim_id
        LEFT JOIN initial_sub isub
            ON  isub.facility_id = sr.facility_id
            AND isub.claim_id    = sr.claim_id
        LEFT JOIN resub_sub rs
            ON  rs.facility_id   = sr.facility_id
            AND rs.claim_id      = sr.claim_id
        LEFT JOIN recons_sub rc
            ON  rc.facility_id   = sr.facility_id
            AND rc.claim_id      = sr.claim_id
        LEFT JOIN ra_all_agg ra
            ON  ra.facility_id = sr.facility_id
            AND ra.claim_id    = sr.claim_id
        LEFT JOIN first_ra fra
            ON  fra.facility_id = sr.facility_id
            AND fra.claim_id    = sr.claim_id
        LEFT JOIN latest_ra lr
            ON  lr.facility_id = sr.facility_id
            AND lr.claim_id    = sr.claim_id
            AND lr.latest_rn   = 1
        LEFT JOIN rad_agg rd
            ON  rd.facility_id = sr.facility_id
            AND rd.claim_id    = sr.claim_id
        LEFT JOIN act_counts ac
            ON  ac.facility_id = sr.facility_id
            AND ac.claim_id    = sr.claim_id
        LEFT JOIN diag_agg da
            ON  da.facility_id = sr.facility_id
            AND da.claim_id    = sr.claim_id
        LEFT JOIN auth_ids ai
            ON  ai.facility_id = sr.facility_id
            AND ai.claim_id    = sr.claim_id
        LEFT JOIN last_resub_sub lrs
            ON  lrs.facility_id = sr.facility_id
            AND lrs.claim_id    = sr.claim_id
        WHERE sr.latest_sub_rn = 1
        ORDER BY sr.encounter_start DESC, sr.claim_id
    """)

    # Safety net: the CTE joins are large; hash/merge joins are always the right choice here.
    db.execute(_text("SET LOCAL enable_nestloop = off"))
    rows = db.execute(sql, params).fetchall()
    db.execute(_text("SET LOCAL enable_nestloop = on"))

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Claim Cycle V2"
    ws.freeze_panes = "A2"
    _write_headers(ws, CLAIM_CYCLE_V2_COLS)

    for row_idx, r in enumerate(rows, start=2):
        fac_name, _ = fac_map.get(r.facility_id, ("", ""))

        sub_rank = int(r.sub_rank)
        ra_count = int(r.ra_count)

        sub_stage = _sub_stage_label(r.resubmission_key)

        # initial_net = net_amount from the first/initial submission
        initial_net   = float(r.initial_net)   if r.initial_net   is not None else 0.0
        total_paid    = float(r.total_paid)    if r.total_paid    is not None else 0.0
        # In UAE DHA eClaimLink, the payer's RA always acknowledges the full claim net.
        # Payers sometimes store only an adjustment amount in RA <Net>, not the full claim.
        # Business rule: RA Net Amount = Net Submitted when at least one RA has been received.
        # Using initial_net directly avoids all payer-specific RA net_amount inconsistencies.
        # (latest_ra_net is fetched from DB but not used for display — kept for debug/audit.)
        latest_ra_net = float(r.latest_ra_net) if r.latest_ra_net is not None else 0.0
        # Bug fix 1: for old claims whose net_amount was never backfilled (= 0 in DB),
        # use latest_ra_net as proxy — in UAE DHA, RA net ≈ submitted claim net.
        if initial_net == 0.0 and latest_ra_net > 0.0:
            initial_net = latest_ra_net
        # Bug fix 2: initial_net cannot be less than total_paid.
        # Handles PRCE-001 / delta-submission cases where the ClaimSubmission XML stores
        # only the tariff-adjustment delta (e.g. 5.58) instead of the full claim net
        # (e.g. 121.22). The payer can never pay more than was submitted, so total_paid
        # is the minimum floor for the real claim net.
        if total_paid > initial_net:
            initial_net = total_paid
        # RA Net = Net Submitted when RA exists; 0 when no RA received yet.
        ra_net_effective = initial_net if ra_count > 0 else 0.0
        # denial_calc = Net Submitted − Paid Amount; 0 when no RA received yet
        denial_calc = round(ra_net_effective - total_paid, 2) if ra_count > 0 else 0.0

        cycle_st = _cycle_status(
            sub_rank, ra_count, ra_count,   # ra_rank = ra_count (latest = highest rank)
            initial_net, total_paid, r.denial_code,
        )

        # Payment Status — uses initial net vs aggregate paid across all RAs
        if ra_count == 0:
            pay_status = "Pending"
        elif total_paid >= initial_net and initial_net > 0:
            pay_status = "Fully Paid"
        elif total_paid == 0 and r.denial_code:
            pay_status = "Fully Denied"
        elif total_paid == 0:
            pay_status = "Fully Rejected"
        else:
            pay_status = "Partially Paid"

        # Raw resub/recons amounts needed for Outstanding logic before wrapping in _f()
        resub_net_raw  = float(r.resub_net)   if r.resub_net   else 0.0
        recons_net_raw = float(r.recons_net)  if r.recons_net  else 0.0

        # Variance = Net Submitted − Total Paid (always; shows total payment gap)
        variance = round(initial_net - total_paid, 2) if initial_net > 0 else ""

        # Resub Rejected: Resub was filed AND payer's subsequent RA (ra_count ≥ 2) still denied.
        # The amount that was resubmitted but rejected again — eligible for S3 Reconciliation.
        # Trigger: resub exists + at least 2 RAs received + still a denial amount outstanding.
        if resub_net_raw > 0 and ra_count >= 2 and denial_calc > 0:
            resub_rejected_val = max(0.0, round(min(resub_net_raw, denial_calc), 2))
        else:
            resub_rejected_val = 0.0

        # Outstanding — 4-scenario rule:
        #  • No RA yet                          → Net Submitted − Paid  (fully unsettled)
        #  • S3 Reconciliation filed             → min(Recons Amt, Denial)  (open S3 contest)
        #  • Resub filed + awaiting 2nd RA       → min(Resub Amt, Denial)  (open S2 contest)
        #  • Resub filed + 2nd RA denied         → 0  (captured in Resub Rejected column)
        #  • No Resub (RA received, settled)     → 0
        if ra_count == 0:
            outstanding = round(initial_net - total_paid, 2) if initial_net > 0 else ""
        elif recons_net_raw > 0:
            outstanding = max(0.0, round(min(recons_net_raw, denial_calc), 2))
        elif resub_net_raw > 0 and ra_count == 1:
            # S2 filed, awaiting 2nd RA — actively contested
            outstanding = max(0.0, round(min(resub_net_raw, denial_calc), 2))
        elif resub_net_raw > 0 and ra_count >= 2 and denial_calc > 0:
            # Resub denied — moves to Resub Rejected; Outstanding is 0
            outstanding = 0.0
        else:
            outstanding = 0.0

        # Final Rejection: denied, NO resubmission filed, AND the 21-day resubmission window
        # from the latest RA has closed. Denials still inside the window stay only in
        # Initial Denied (the claim may yet be resubmitted).
        _ra_dt = r.ra_date.date() if hasattr(r.ra_date, 'date') else r.ra_date
        _window_open = _ra_dt is not None and (date.today() - _ra_dt).days <= RESUB_WINDOW_DAYS
        if denial_calc > 0 and resub_net_raw == 0 and not _window_open:
            final_rejection = _f(denial_calc)
        else:
            final_rejection = _f(0)

        # Unsettled/AR raw value needed for Resub Receivable before formatting
        unsettled_ar_raw = round(initial_net - ra_net_effective, 2)

        # Resub Receivable = Outstanding − Unsettled/AR (floored at 0)
        # When RA + Resub awaiting 2nd RA: Unsettled/AR = 0, so = Outstanding = min(Resub, Denial)
        # When Resub Rejected (2nd RA denied): Outstanding = 0, so Resub Receivable = 0
        # When no RA: Outstanding − initial_net ≤ 0, floor gives 0
        outstanding_raw = outstanding if isinstance(outstanding, (int, float)) else 0.0
        resub_receivable = _f(max(0.0, round(outstanding_raw - unsettled_ar_raw, 2)))

        # Resub Rejected (formatted)
        resub_rejected = _f(resub_rejected_val)

        # Unsettled/AR = Net Submitted − RA Net (= 0 when RA received, since RA Net = Net Sub)
        unsettled_ar = _f(unsettled_ar_raw)

        net_sub    = _f(initial_net)
        resub_amt  = _f(resub_net_raw)
        recons_amt = _f(float(r.recons_net)) if r.recons_net else _f(0)
        paid_amt   = _f(total_paid)
        den_code = r.denial_code or ""
        den_desc = _lookup_denial(den_code)[0] if den_code else ""
        tpa_cmts = r.tpa_comments or ""
        diag_types   = r.diag_types   or ""
        diag_codes   = r.diag_codes   or ""
        auth_id      = r.auth_id_list or ""
        sub_file     = r.sub_file    or ""
        resub_file   = r.resub_file  or ""
        _resub_type_map = {
            "InternalComplaint": "Internal",
            "Correction":        "Correction",
            "Reconciliation":    "Reconciliation",
        }
        resub_type    = _resub_type_map.get(r.resub_type or "", r.resub_type or "")
        resub_comment = r.resub_comment or ""
        ra_file       = r.ra_file    or ""
        ra_date       = _fmt_date(r.ra_date) if r.ra_date else ""

        enc_year  = r.encounter_start.year  if r.encounter_start else ""
        enc_month = r.encounter_start.strftime("%b") if r.encounter_start else ""
        # RA Net = Net Submitted when RA exists (UAE DHA rule); 0 if no RA yet.
        ra_net_amt   = _f(ra_net_effective) if ra_count > 0 else _f(0)

        _write_row(ws, row_idx, [
            fac_name,
            _claim_gateway(r.source_channel, getattr(r, "sender_id", None)),
            r.claim_id,
            _derive_receiver_id(r.receiver_id, r.payer_id),
            _payer_name(_derive_receiver_id(r.receiver_id, r.payer_id)),
            r.payer_id,
            _payer_name(r.payer_id),
            r.member_id,
            r.emirates_id or "",
            _fmt_date(r.encounter_start),
            _fmt_date(r.encounter_end),
            _enc_label(r.encounter_type),
            enc_year,                                  # Year (from encounter_start)
            enc_month,                                 # Month e.g. "May", "Jun"
            int(r.sub_count),
            sub_stage,
            f"S{sub_rank}",
            ra_count,
            f"R{ra_count}" if ra_count > 0 else "",  # e.g. R1, R2, R3
            cycle_st,
            net_sub,
            ra_net_amt,                               # RA Net Amount (latest RA, capped at Net Submitted)
            resub_amt,
            recons_amt,
            paid_amt,
            _f(denial_calc),                          # Denial Amount = RA Net − Paid
            variance,
            final_rejection,                          # Final Rejection = Denial when no Resub exists
            resub_receivable,                         # Resub Receivable = Outstanding − Unsettled/AR
            resub_rejected,                           # Resub Rejected = min(Resub,Denial) when 2nd RA also denied
            unsettled_ar,                             # Unsettled/AR = Net Submitted if no RA yet
            outstanding,
            pay_status,
            den_code,
            den_desc,
            tpa_cmts,
            int(r.act_count),
            int(r.denied_act_count),
            diag_types,
            diag_codes,
            auth_id,
            sub_file,
            resub_file,
            resub_type,
            resub_comment,
            ra_file,
            ra_date,
        ], alt=(row_idx % 2 == 0))

    _auto_width(ws, CLAIM_CYCLE_V2_COLS)
    ts = datetime.now().strftime("%Y%m%d_%H%M")
    return wb, f"ClaimCycleV2_{ts}.xlsx", len(rows)


@router.get("/claim-cycle-v2")
def export_claim_cycle_v2(
    facility_id:    Optional[int]  = None,
    payer_id:       Optional[str]  = None,
    encounter_type: Optional[str]  = None,
    date_from:      Optional[date] = None,
    date_to:        Optional[date] = None,
    date_field:     Optional[str]  = "treatment_date",
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Claim Cycle V2 — one row per claim_id, showing current Sub Rank, RA Rank
    and plain-language Claim Cycle Status across the full DHA eClaimLink lifecycle."""
    wb, filename, _ = _build_claim_cycle_v2(
        db, facility_id, payer_id, encounter_type, date_from, date_to, date_field)
    return _excel_response(wb, filename)


# ---------------------------------------------------------------------------
# 10. REMITTANCE ACTIVITY V2
# ---------------------------------------------------------------------------

def _build_remittance_activity_v2(db, facility_id, payer_id, encounter_type,
                                   date_from, date_to, date_field,
                                   facility_ids=None):
    from sqlalchemy import text as _text

    fac_map = {f.id: (f.facility_name, f.license_id) for f in db.query(Facility).all()}

    # ── Pre-compute RA rank + dup detection (same as V1) ─────────────────────
    date_clauses = ""
    date_params: dict = {}
    if date_field == "treatment_date":
        if date_from:
            date_clauses += " AND DATE(c.encounter_start) >= :date_from"
            date_params["date_from"] = str(date_from)
        if date_to:
            date_clauses += " AND DATE(c.encounter_start) <= :date_to"
            date_params["date_to"] = str(date_to)
    else:
        if date_from:
            date_clauses += " AND rx.submission_date >= :date_from"
            date_params["date_from"] = str(date_from)
        if date_to:
            date_clauses += " AND rx.submission_date <= :date_to"
            date_params["date_to"] = str(date_to)

    if facility_ids:
        _fids = [int(x) for x in facility_ids.split(",") if x.strip().isdigit()]
        if _fids:
            placeholders = ", ".join(f":fid{i}" for i in range(len(_fids)))
            date_clauses += f" AND r.facility_id IN ({placeholders})"
            for i, fid in enumerate(_fids):
                date_params[f"fid{i}"] = fid
    elif facility_id:
        date_clauses += " AND r.facility_id = :facility_id"
        date_params["facility_id"] = facility_id

    rank_rows = db.execute(_text(f"""
        SELECT r.id,
               ROW_NUMBER() OVER (
                   PARTITION BY r.facility_id, r.claim_id
                   ORDER BY r.remittance_date, r.id
               ) AS ra_rank
        FROM remittances r
        JOIN claims c ON c.facility_id = r.facility_id AND c.claim_id = r.claim_id
        LEFT JOIN raw_xml_store rx ON rx.id = r.raw_xml_id
        WHERE 1=1 {date_clauses}
    """), date_params).fetchall()
    ra_rank_map = {row[0]: row[1] for row in rank_rows}

    scoped_ids = list(ra_rank_map.keys())
    if scoped_ids:
        dup_rows = db.execute(_text("""
            SELECT r.id
            FROM remittances r
            JOIN (
                SELECT facility_id, claim_id, ra_file_id
                FROM remittances
                WHERE ra_file_id IS NOT NULL
                  AND id = ANY(:ids)
                GROUP BY facility_id, claim_id, ra_file_id
                HAVING COUNT(*) > 1
            ) dup ON r.facility_id = dup.facility_id
                  AND r.claim_id   = dup.claim_id
                  AND r.ra_file_id = dup.ra_file_id
        """), {"ids": scoped_ids}).fetchall()
    else:
        dup_rows = []
    dup_rem_ids = {row[0] for row in dup_rows}

    # ── Pre-compute lifecycle per (facility_id, claim_id) ────────────────────
    # Same CTE logic as ClaimCycleV2 — used to populate the 6 lifecycle columns.
    lifecycle_rows = db.execute(_text(f"""
        WITH scoped AS (
            SELECT DISTINCT r.facility_id, r.claim_id
            FROM remittances r
            JOIN claims c ON c.facility_id = r.facility_id AND c.claim_id = r.claim_id
            LEFT JOIN raw_xml_store rx ON rx.id = r.raw_xml_id
            WHERE 1=1 {date_clauses}
        ),
        sub_ranked AS (
            SELECT
                c.facility_id, c.claim_id, c.resubmission_key, c.net_amount,
                CASE c.resubmission_key
                    WHEN ''               THEN 1
                    WHEN 'reconciliation' THEN 3
                    ELSE                       2
                END AS sub_rank,
                ROW_NUMBER() OVER (
                    PARTITION BY c.facility_id, c.claim_id
                    ORDER BY CASE c.resubmission_key
                        WHEN ''               THEN 1
                        WHEN 'reconciliation' THEN 3
                        ELSE                       2
                    END DESC
                ) AS latest_sub_rn
            FROM claims c
            JOIN scoped s ON s.facility_id = c.facility_id AND s.claim_id = c.claim_id
        ),
        sub_counts AS (
            SELECT c.facility_id, c.claim_id, COUNT(*) AS sub_count
            FROM claims c
            JOIN scoped s ON s.facility_id = c.facility_id AND s.claim_id = c.claim_id
            GROUP BY c.facility_id, c.claim_id
        ),
        ra_ranked AS (
            SELECT
                r.facility_id, r.claim_id, r.paid_amount, r.denial_code,
                ROW_NUMBER() OVER (PARTITION BY r.facility_id, r.claim_id ORDER BY r.id) AS ra_rank,
                COUNT(*)        OVER (PARTITION BY r.facility_id, r.claim_id)             AS ra_count
            FROM remittances r
            JOIN scoped s ON s.facility_id = r.facility_id AND s.claim_id = r.claim_id
        ),
        latest_ra AS (
            SELECT * FROM ra_ranked WHERE ra_rank = ra_count
        )
        SELECT
            sr.facility_id, sr.claim_id, sr.resubmission_key, sr.sub_rank, sr.net_amount,
            sc.sub_count,
            COALESCE(lr.ra_rank,  0) AS ra_rank,
            COALESCE(lr.ra_count, 0) AS ra_count,
            lr.paid_amount, lr.denial_code
        FROM sub_ranked sr
        JOIN sub_counts sc
            ON  sc.facility_id = sr.facility_id AND sc.claim_id = sr.claim_id
        LEFT JOIN latest_ra lr
            ON  lr.facility_id = sr.facility_id AND lr.claim_id = sr.claim_id
        WHERE sr.latest_sub_rn = 1
    """), date_params).fetchall()

    lifecycle_map: dict = {
        (r.facility_id, r.claim_id): r for r in lifecycle_rows
    }


    ra_xml_store    = aliased(RawXmlStore)
    claim_xml_store = aliased(RawXmlStore)
    act_alias       = aliased(ClaimActivity)

    q = (
        db.query(RemittanceActivityDetail, Remittance, Claim,
                 ra_xml_store, claim_xml_store, act_alias)
        .join(Remittance, Remittance.id == RemittanceActivityDetail.remittance_id_fk)
        .join(Claim, and_(
            Claim.facility_id == Remittance.facility_id,
            Claim.claim_id    == Remittance.claim_id,
        ))
        .outerjoin(ra_xml_store,    ra_xml_store.id    == Remittance.raw_xml_id)
        .outerjoin(claim_xml_store, claim_xml_store.id == Claim.raw_xml_id)
        .outerjoin(act_alias, and_(
            act_alias.claim_id_fk == Claim.id,
            act_alias.activity_id == RemittanceActivityDetail.activity_id,
        ))
    )
    if facility_ids:
        _fids = [int(x) for x in facility_ids.split(",") if x.strip().isdigit()]
        if _fids:
            q = q.filter(Remittance.facility_id.in_(_fids))
    elif facility_id:
        q = q.filter(Remittance.facility_id == facility_id)
    if payer_id:
        q = q.filter(Remittance.payer_id == payer_id)
    if encounter_type:
        q = q.filter(Claim.encounter_type == encounter_type)

    if date_field == "treatment_date":
        if date_from:
            q = q.filter(cast(Claim.encounter_start, Date) >= date_from)
        if date_to:
            q = q.filter(cast(Claim.encounter_start, Date) <= date_to)
    else:
        if date_from:
            q = q.filter(ra_xml_store.submission_date >= date_from)
        if date_to:
            q = q.filter(ra_xml_store.submission_date <= date_to)

    # Keep only the latest submission row per claim — avoids duplicate report rows
    # when a claim has been resubmitted (multiple rows in `claims` with the same
    # claim_id+facility_id).  The filter retains the row whose sub_rank equals
    # the maximum sub_rank across all submissions of that claim.
    q = q.filter(_text("""
        (CASE claims.resubmission_key
             WHEN ''               THEN 1
             WHEN 'reconciliation' THEN 3
             ELSE                       2
         END)
        = (
            SELECT MAX(CASE c2.resubmission_key
                           WHEN ''               THEN 1
                           WHEN 'reconciliation' THEN 3
                           ELSE                       2
                       END)
            FROM claims c2
            WHERE c2.facility_id = claims.facility_id
              AND c2.claim_id    = claims.claim_id
        )
    """))

    # Only the file name and date of each XML are used — never load the file contents (large DHA batch
    # files once took a one-month Sabah report past 2 GB and macOS killed it).
    from sqlalchemy.orm import defer as _defer
    q = q.options(_defer(ra_xml_store.xml_content), _defer(claim_xml_store.xml_content))
    rows = q.order_by(ra_xml_store.submission_date.desc(), Claim.claim_id).all()

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Remittance Activity V2"
    ws.freeze_panes = "A2"
    _write_headers(ws, REMITTANCE_ACTIVITY_V2_COLS)
    subs = _submission_history(db, rows)

    for row_idx, (rad, rem, claim, ra_xml_row, claim_xml_row, act_row) in enumerate(rows, start=2):
        hist = subs.get((rem.facility_id, rem.claim_id))
        init_date, init_file, _ = _initial_sub(hist)
        for_date, for_file, _ = _sub_for_ra(
            hist, (ra_xml_row.submission_date if ra_xml_row and ra_xml_row.submission_date else rem.remittance_date))
        enc_start = claim.encounter_start
        enc_year  = enc_start.year  if enc_start else ""
        enc_month = enc_start.month if enc_start else ""

        fac_name, _ = fac_map.get(rem.facility_id, ("", ""))

        den_code = rad.denial_code or ""
        den_desc, den_cat, den_type = _lookup_denial(den_code)

        act_clin_id = (act_row.clinician_id if act_row else None) or claim.clinician_id
        ord_clin_id = (
            getattr(act_row, "ordering_clinician_id", None) if act_row else None
        ) or act_clin_id
        clinician     = _clinician_name(act_clin_id)
        ord_clinician = _clinician_name(ord_clin_id)

        # Days to settlement: RA TransactionDate − DateSettlement
        days_to_settle = ""
        if ra_xml_row and ra_xml_row.submission_date and rem.remittance_date:
            try:
                days_to_settle = (ra_xml_row.submission_date - rem.remittance_date).days
            except Exception:
                pass

        # Submitted net per activity from ClaimActivity
        sub_net_act = _f(act_row.net) if act_row and act_row.net is not None else ""

        # Activity variance: submitted − paid
        if act_row and act_row.net is not None and rad.paid_amount is not None:
            act_var = round(float(act_row.net) - float(rad.paid_amount), 2)
        else:
            act_var = ""

        tpa_comments = rem.tpa_comments or ""

        # ── Lifecycle columns from pre-computed map ───────────────────────────
        lc = lifecycle_map.get((rem.facility_id, claim.claim_id))
        if lc:
            lc_sub_rank    = int(lc.sub_rank)
            lc_ra_rank     = int(lc.ra_rank)
            lc_ra_count    = int(lc.ra_count)
            lc_sub_count   = int(lc.sub_count)
            lc_sub_stage   = _sub_stage_label(lc.resubmission_key)
            lc_cycle_st    = _cycle_status(
                lc_sub_rank, lc_ra_rank, lc_ra_count,
                lc.net_amount, lc.paid_amount, lc.denial_code,
            )
        else:
            lc_sub_rank = lc_ra_rank = lc_ra_count = lc_sub_count = 0
            lc_sub_stage = lc_cycle_st = ""


        _write_row(ws, row_idx, [
            (fac_map.get(rem.facility_id, ("", ""))[1] or rem.facility_id), fac_name,
            _claim_gateway(claim.source_channel, claim.sender_id),
            _derive_receiver_id(claim.receiver_id, claim.payer_id),
            _payer_name(_derive_receiver_id(claim.receiver_id, claim.payer_id)),
            claim.payer_id, _payer_name(claim.payer_id),
            claim.member_id, claim.member_id, claim.emirates_id or "",
            _fmt_date(enc_start), _fmt_date(claim.encounter_end),
            enc_year, enc_month,
            _fmt_date(init_date), init_file,                                   # Initial Submission
            _fmt_date(for_date), for_file,                                     # submission this RA answers
            _fmt_date(claim.submission_date),                                  # Latest Submission
            claim_xml_row.source_filename if claim_xml_row else "",
            _fmt_date(ra_xml_row.submission_date if ra_xml_row else None),    # Transaction Date
            _fmt_date(rem.remittance_date),                                    # Date Settlement
            days_to_settle,                                                    # Days to Settlement (NEW)
            claim.claim_id, _enc_label(claim.encounter_type), claim.end_type or "",
            lc_sub_count, lc_sub_stage, lc_sub_rank,          # lifecycle cols
            lc_ra_count, lc_ra_rank, lc_cycle_st,
            claim.claim_id_payer or "", rem.ra_file_id or "",
            clinician, ord_clinician,
            rad.activity_code or "", rad.activity_type or "", rad.activity_type or "",
            sub_net_act,                                                       # Submitted Net (NEW)
            _f(rad.gross),                                                     # Allowed Amount
            _f(rad.net),                                                       # Net Amt
            _f(rad.patient_share),                                             # Patient Share
            _f(rad.paid_amount),                                               # Payment Amt
            act_var,                                                           # Activity Variance (NEW)
            den_code, den_desc, den_cat,
            den_type,
            tpa_comments,                                                      # TPA Comments (NEW)
            rad.activity_id or "",
            ra_xml_row.source_filename    if ra_xml_row    else "",
            rem.ra_file_id or "",
            rad.denial_description or rem.denial_description or "",
            _ra_payment_for(rem.id, ra_rank_map, dup_rem_ids),
        ], alt=(row_idx % 2 == 0))

    _auto_width(ws, REMITTANCE_ACTIVITY_V2_COLS)
    ts = datetime.now().strftime("%Y%m%d_%H%M")
    return wb, f"RemittanceActivityV2_{ts}.xlsx", len(rows)


@router.get("/remittance-activity-v2")
def export_remittance_activity_v2(
    facility_id:    Optional[int]  = None,
    payer_id:       Optional[str]  = None,
    encounter_type: Optional[str]  = None,
    date_from:      Optional[date] = None,
    date_to:        Optional[date] = None,
    date_field:     Optional[str]  = "remittance_date",
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Remittance Activity V2 — enhanced with TPA comments, days to settlement,
    submitted net per activity, activity variance. Default date_field=remittance_date
    (RA file TransactionDate — matches DHPO Remittances tab)."""
    wb, filename, _ = _build_remittance_activity_v2(
        db, facility_id, payer_id, encounter_type, date_from, date_to, date_field)
    return _excel_response(wb, filename)


# ---------------------------------------------------------------------------
# Register V2 builders (must be after function definitions)
# ---------------------------------------------------------------------------
REPORT_BUILDERS["claim-summary-v2"]       = _build_claim_summary_v2
REPORT_BUILDERS["claim-activity-v2"]      = _build_claim_activity_v2
REPORT_BUILDERS["claim-cycle-v2"]         = _build_claim_cycle_v2
REPORT_BUILDERS["remittance-activity-v2"] = _build_remittance_activity_v2


# ===========================================================================
# Denial Scrub Action Report
# ===========================================================================
# ALL activity lines for claims that have at least one denied activity,
# plus scrub findings on the same claim, with auto-generated Resubmission
# Action (internal team instructions) and Reappeal Comment (TPA-addressed
# appeal response). Designed to be run right after each DHPO fetch.
# ===========================================================================

# ---------------------------------------------------------------------------
# Internal resubmission action — what the billing team must do
# ---------------------------------------------------------------------------
_SCRUB_RULE_ACTIONS: dict[str, str] = {
    "CONSULT_FOLLOWUP_7D":          "Change to follow-up consultation CPT code — service is within 7 days of a prior visit.",
    "CONSULT_FOLLOWUP_28D":         "Change to follow-up consultation CPT code — service is within 28 days of a prior visit.",
    "CONSULT_SAME_DAY_DUPLICATE":   "Remove duplicate same-day consultation or document distinct clinical justification for the separate encounter.",
    "OVERLAPPING_ACTIVITY":         "Review overlapping activity codes on the same date; remove true duplicates or attach separate clinical documentation for each.",
    "BUNDLING_SEQUENCE_REVIEW":     "Review CPT bundling and modifier — correct sequence, add access-site/stop-time detail, then resubmit.",
    "CPT_CODE_FORMAT":              "Correct CPT code format (leading zero, invalid length, or unrecognised code) before resubmitting.",
    "DUPLICATE_CLAIM":              "Verify claim ID and activity IDs; suppress confirmed duplicate or provide distinct encounter evidence.",
    "MISSING_DIAGNOSIS_LINK":       "Link every CPT code to a supporting ICD-10 diagnosis before resubmitting.",
    "AUTH_MISSING":                 "Obtain prior authorisation reference and include in the resubmission.",
    "TARIFF_MISMATCH":              "Verify contracted tariff rate and resubmit at the agreed price.",
    "FREQUENCY_LIMIT":              "Document clinical necessity for frequency exceeding policy limits and attach supporting clinical notes.",
    "NON_COVERED_SERVICE":          "Verify benefit coverage; if covered under a special plan, attach the policy exception documentation.",
    "ENCOUNTER_TYPE_MISMATCH":      "Review and correct the encounter type (OP/IP/ER) to match the actual care setting.",
}

_DENIAL_CAT_ACTIONS: dict[str, str] = {
    "MNEC": "Attach clinical justification / medical necessity documentation (physician notes, investigation results, or clinical guidelines) supporting the service.",
    "TIME": "Gather all missing documents cited in the TPA comment and resubmit immediately — verify the 21-day window deadline.",
    "DUPL": "Verify activity IDs and service dates; remove confirmed duplicates or provide evidence of distinct clinical encounters.",
    "AUTH": "Obtain prior authorisation or attach the PA reference number; resubmit with correct auth detail.",
    "ELIG": "Confirm member eligibility on the date of service with the payer; resubmit with eligibility proof if confirmed eligible.",
    "CODE": "Review and correct the diagnosis/procedure code — ensure the primary diagnosis clinically supports the service.",
    "PRCE": "Check the applicable contracted tariff rate and resubmit at the correct price.",
    "NCOV": "Verify policy coverage; if the service is covered under an exception, attach supporting plan documentation.",
    "COPY": "Verify correct patient share amount and resubmit with the corrected co-pay line.",
    "BENX": "Verify if the annual or per-occurrence benefit limit is exceeded; prepare a medical necessity appeal if clinically justified.",
    "CLAI": "Review the claim-level error in the TPA comment and correct the structural issue before resubmitting.",
    "DRG":  "Correct the DRG code or missing DRG parameter as indicated by the TPA comment.",
}


def _auto_resubmission_action(
    denial_code: Optional[str],
    scrub_rules: list,
    scrub_messages: list,
    tpa_comment: Optional[str],
) -> str:
    """Internal team action: what to fix before resubmitting."""
    parts: list[str] = []
    for rule in (scrub_rules or []):
        action = _SCRUB_RULE_ACTIONS.get(str(rule).upper())
        if action and action not in parts:
            parts.append(f"[Scrub – {rule}] {action}")
    cat = (denial_code or "")[:4].upper() if denial_code else ""
    if cat and cat in _DENIAL_CAT_ACTIONS:
        act = f"[{denial_code}] {_DENIAL_CAT_ACTIONS[cat]}"
        if act not in parts:
            parts.append(act)
    if not parts:
        parts.append("Review TPA comments and resubmit with appropriate corrections within the 21-day resubmission window.")
    return " | ".join(parts)


# ---------------------------------------------------------------------------
# Reappeal comment — TPA-addressed written response
# ---------------------------------------------------------------------------
_SCRUB_RULE_REAPPEAL: dict[str, str] = {
    "CONSULT_FOLLOWUP_7D":
        "We acknowledge the denial and wish to clarify that the service billed has been reviewed and corrected to a follow-up consultation code, as the encounter falls within 7 days of the patient's prior visit. Kindly re-adjudicate the corrected resubmission accordingly.",
    "CONSULT_FOLLOWUP_28D":
        "We acknowledge the denial and confirm that the activity code has been corrected to a follow-up consultation, as the encounter falls within 28 days of the patient's initial visit. We request kindly re-adjudication of this resubmission.",
    "CONSULT_SAME_DAY_DUPLICATE":
        "We respectfully submit that both consultations on this date represent distinct clinical encounters for separate conditions, documented individually in the patient's medical record. We kindly request re-adjudication with reference to the enclosed clinical notes.",
    "OVERLAPPING_ACTIVITY":
        "We confirm that the activities billed on the same date are distinct services rendered at different times and are supported by separate clinical documentation. We kindly request re-adjudication on the basis of the attached supporting records.",
    "BUNDLING_SEQUENCE_REVIEW":
        "We respectfully request re-adjudication of this claim. The service codes and modifiers have been reviewed and corrected in line with current CPT bundling guidelines. Kindly review and settle accordingly.",
    "CPT_CODE_FORMAT":
        "We acknowledge the denial and confirm that the activity code has been corrected to the valid CPT format. We kindly request re-adjudication of this corrected resubmission.",
    "DUPLICATE_CLAIM":
        "We respectfully submit that this claim is not a duplicate. Each activity billed represents a distinct clinical service rendered on the stated date. Please find enclosed the clinical records confirming the separate encounters. We kindly request re-adjudication.",
    "FREQUENCY_LIMIT":
        "We respectfully appeal this denial and submit that the frequency of services provided is clinically justified based on the patient's condition and treatment protocol. Supporting clinical notes are enclosed for your review. Kindly reconsider and settle accordingly.",
}

_DENIAL_CAT_REAPPEAL: dict[str, str] = {
    "MNEC": (
        "We respectfully appeal this denial on the grounds of medical necessity. The service billed was clinically indicated based on the patient's presenting condition and is consistent with established clinical guidelines. "
        "Supporting documentation including physician notes and relevant investigation results are enclosed. We kindly request your reassessment and favourable re-adjudication."
    ),
    "TIME": (
        "We acknowledge this denial and wish to confirm that the required documentation has now been compiled and is enclosed with this resubmission. "
        "We kindly request re-adjudication with reference to the attached documents and request that the claim be processed accordingly."
    ),
    "DUPL": (
        "We respectfully submit that this claim is not a duplicate. The activities billed represent distinct services for which separate clinical documentation exists. "
        "We request that your team review the enclosed records and re-adjudicate this claim as a unique encounter."
    ),
    "AUTH": (
        "We respectfully submit this appeal with the prior authorisation reference enclosed. The service rendered was clinically necessary and authorised under the patient's policy. "
        "We kindly request re-adjudication of this claim with reference to the enclosed authorisation details."
    ),
    "ELIG": (
        "We respectfully appeal this denial and confirm that the member was eligible for coverage on the date of service. "
        "We request that your team verify the eligibility records and re-adjudicate this claim. Supporting eligibility documentation is enclosed."
    ),
    "CODE": (
        "We acknowledge the coding concern raised and confirm that the diagnosis and procedure codes have been reviewed and corrected. "
        "The corrected codes accurately reflect the clinical service provided. We kindly request re-adjudication of this resubmission."
    ),
    "PRCE": (
        "We respectfully draw your attention to the contracted tariff applicable for this service under the existing agreement. "
        "We have verified the rate and resubmitted at the agreed contracted price. Kindly re-adjudicate accordingly."
    ),
    "NCOV": (
        "We respectfully appeal this denial and request a review of the member's policy coverage with respect to the service provided. "
        "The clinical indication is well established and the service is considered standard of care. We request your favourable reconsideration."
    ),
    "COPY": (
        "We acknowledge the co-pay discrepancy and confirm that the patient share has been reviewed and corrected in this resubmission. "
        "Kindly re-adjudicate with the corrected patient share amount as submitted."
    ),
    "BENX": (
        "We respectfully appeal this denial and submit that this service is medically necessary and does not constitute an abuse of the member's benefit limit. "
        "Supporting clinical documentation establishing necessity is enclosed. We kindly request re-adjudication."
    ),
    "CLAI": (
        "We have reviewed the claim-level issue identified and have corrected the submission accordingly. "
        "We kindly request re-adjudication of this corrected claim and thank you for your consideration."
    ),
    "DRG": (
        "We acknowledge the DRG-related concern and confirm that the DRG code and relevant parameters have been reviewed and corrected in this resubmission. "
        "Kindly re-adjudicate the corrected claim accordingly."
    ),
}


def _auto_reappeal_comment(
    denial_code: Optional[str],
    scrub_rules: list,
    tpa_comment: Optional[str],
) -> str:
    """TPA-addressed written reappeal response auto-generated for the team to review and post."""
    parts: list[str] = []
    # Scrub-specific reappeal (most specific — rule matches denial pattern)
    for rule in (scrub_rules or []):
        text = _SCRUB_RULE_REAPPEAL.get(str(rule).upper())
        if text and text not in parts:
            parts.append(text)
    # Denial-category reappeal (general category guidance)
    cat = (denial_code or "")[:4].upper() if denial_code else ""
    if cat and cat in _DENIAL_CAT_REAPPEAL and not parts:
        # Only add category reappeal if no scrub-specific one was generated
        parts.append(_DENIAL_CAT_REAPPEAL[cat])
    if not parts:
        parts.append(
            "We respectfully appeal this denial and request your team to kindly review the enclosed documentation "
            "and re-adjudicate the claim accordingly. We remain available to provide any additional information required."
        )
    return " | ".join(parts)


def _calibration_msg(
    denial_code: Optional[str],
    denial_desc: Optional[str],
    tpa_comment: Optional[str],
    activity_code: Optional[str],
    freq_paid: list,                   # PAID dates for same member + activity_code (180 days)
    freq_any: list,                    # all claim dates for same member + activity_code (180 days)
    scrub_rules: list,
    scrub_messages: list,
    encounter_start=None,              # date of current claim encounter
    prior_visits: list = (),           # list of (enc_date, enc_type) prior encounters for same member
    overlap_claim_dates: dict = None,  # {claim_id: {"enc_date":..,"paid_date":..}} for OVERLAPPING_ACTIVITY
    claim_id: Optional[str] = None,   # current claim ID — used to detect intra-claim overlap
):
    # Returns (msg_text: str, recall_reasons: list[str])
    # recall_reasons is non-empty when DB confirms the denial is valid / high audit risk
    """
    Cross-references denial reason, TPA comment, DB frequency data, and prior-visit
    history to produce a specific, actionable calibration finding for the billing team.

    Priority order:
      1. Frequency / excess         → DB frequency check (paid vs none vs prior-only)
      2. Coding / guideline / revisit → DB prior-visit check (is patient new or returning?)
      3. Authorization
      4. Eligibility
      5. Not-covered
      6. Scrubber findings (fallback)
      7. Generic fallback
    """
    combined = (
        (denial_desc  or "").lower() + " " +
        (tpa_comment  or "").lower() + " " +
        (denial_code  or "").lower()
    )
    parts: list[str] = []
    recall_reasons: list[str] = []   # populated when DB confirms a problem → triggers recall flag
    code_lbl = activity_code or "activity"

    # ── Consultation code detection ──────────────────────────────────────────
    # DSL consultation codes (DHA) — ONLY these trigger the 28-day window check.
    # All other codes (CPT, drug, lab, etc.) use the 180-day freq_map.
    _CONSULT_CODES = {"9", "9.01", "9.02", "10", "10.01", "10.02", "11"}
    _is_consult_code = (activity_code or "").strip() in _CONSULT_CODES
    _CONSULT_WINDOW = 28   # 4 weeks per DHA / Nextcare rules

    def _consult_correct_code(act_code: str, gap_days: int) -> str:
        """Return the correct revisit code given the base activity code and gap."""
        if gap_days <= 7:
            # Free follow-up period — 9.01/10.01
            if act_code in ("9", "9.01", "9.02"):
                return "9.01 (Free GP Follow-up)"
            return "10.01 (Free Specialist Follow-up)"
        # 8–28 days → repeat consultation
        if act_code in ("9", "9.01", "9.02"):
            return "9.02 (Consultation GP – Repeat)"
        if act_code in ("10", "10.01", "10.02"):
            return "10.02 (Consultation Specialist – Repeat)"
        if act_code in ("11", "11.02"):
            return "11.02 (Consultation Consultant – Repeat)"
        return "9.02/10.02/11.02 (Repeat Consultation)"

    def _fmt_freq(entries: list, limit: int = 3) -> str:
        """Format (billed_date, paid_date) tuples into 'billed: X, paid: Y' strings."""
        parts_f = []
        for _e in entries[:limit]:
            if isinstance(_e, (list, tuple)) and len(_e) == 2:
                _b, _p = _e
                parts_f.append(f"billed: {_b}, paid: {_p}" if _p else f"billed: {_b}")
            else:
                parts_f.append(str(_e))
        return " | ".join(parts_f)

    def _billed_dates_only(entries: list, limit: int = 3) -> str:
        """Extract just billed dates from (billed, paid) tuples."""
        out = []
        for _e in entries[:limit]:
            if isinstance(_e, (list, tuple)) and len(_e) >= 1:
                out.append(str(_e[0]))
            else:
                out.append(str(_e))
        return ", ".join(out)

    # ── 0. DUPL — Duplicate payment / previous submission ────────────────────
    # Handle before generic frequency so we give a targeted duplicate message.
    is_dupl = (
        (denial_code or "").upper().startswith("DUPL") or
        any(kw in combined for kw in [
            "payment already made", "already paid", "previous submission",
            "paid as per tariff", "same/similar service", "duplicate payment"
        ])
    )
    if is_dupl:
        from datetime import date as _date_dupl

        def _dupl_format_entries(entries, enc_date, limit=4):
            """
            Format freq entries for DUPL display.
            - Excludes the current encounter date (that's THIS claim, not a prior one).
            - Shows billed date, paid date, and signed gap ("Nd before" / "Nd after").
            - Sorted: "before" entries first (most recent), then "after" entries.
            """
            scored = []
            for _e in entries:
                if isinstance(_e, (list, tuple)) and len(_e) == 2:
                    _b, _p = _e
                else:
                    _b, _p = _e, None
                # Skip if billed date IS the current encounter (that's this claim itself)
                if enc_date and _b and str(_b) == str(enc_date):
                    continue
                _signed = None
                _interval = ""
                if enc_date and _b:
                    try:
                        _signed = (
                            _date_dupl.fromisoformat(str(enc_date)) -
                            _date_dupl.fromisoformat(str(_b))
                        ).days
                        if _signed > 0:
                            _interval = f" ({_signed}d before)"
                        elif _signed < 0:
                            _interval = f" ({abs(_signed)}d after)"
                    except Exception:
                        pass
                _paid_str = f", paid: {_p}" if _p else ""
                _label = f"{_b}{_paid_str}{_interval}"
                # sort key: before entries (positive signed) first, after entries last
                _sort_key = (0 if (_signed is not None and _signed > 0) else 1, -(_signed or 0))
                scored.append((_sort_key, _label))
            scored.sort(key=lambda x: x[0])
            return [lbl for _, lbl in scored[:limit]]

        if freq_paid:
            recall_reasons.append("DUPLICATE — prior payment confirmed in DB")
            _entries = _dupl_format_entries(freq_paid, encounter_start)
            if _entries:
                enc_list = "; ".join(_entries)
                parts.append(
                    f"DB CHECK – DUPLICATE CONFIRMED: Activity code {code_lbl} has paid activity "
                    f"encounters on {enc_list}. "
                    "Action: distinct encounters — attach separate clinical notes proving "
                    "distinct presentations and reappeal."
                )
            else:
                # All paid entries were the current encounter itself → no true prior
                parts.append(
                    f"DB CHECK – DUPLICATE: No paid prior encounter found other than current DOS. "
                    "Payer may be referencing cross-facility data. "
                    "Action: reappeal with confirmation that no prior claim exists for this member."
                )
        elif freq_any:
            _entries = _dupl_format_entries(freq_any, encounter_start)
            if _entries:
                enc_list = "; ".join(_entries)
                parts.append(
                    f"DB CHECK – PRIOR CLAIM EXISTS (payment status unclear): "
                    f"Activity code {code_lbl} has claim history on {enc_list}. "
                    "Verify if prior claim was paid. If paid → attach separate clinical notes "
                    "proving distinct presentations and reappeal."
                )
            else:
                parts.append(
                    f"DB CHECK – NO PRIOR CLAIM FOUND: No prior {code_lbl} claim other than "
                    "current DOS found in the past 180 days. "
                    "Action: request payer to provide specific claimref of alleged prior payment."
                )
        else:
            parts.append(
                f"DB CHECK – NO PRIOR CLAIM FOUND: No prior {code_lbl} claim found for this "
                "member in the past 180 days. "
                "Action: request payer to provide the specific claimref. "
                "Reappeal with statement of no duplicate found."
            )

    # ── 1. Frequency / Excess (non-duplicate) ────────────────────────────────
    is_freq = (not is_dupl) and any(kw in combined for kw in [
        "excess", "frequen", "quantity", "same service",
        "already claimed", "repeated", "too soon", "too frequent",
        "mue value", "maximum quantity", "maximum qunatity"
    ])
    _consult_handled = False   # prevents Sections 2 / 5b from double-firing
    if is_freq:
        if _is_consult_code:
            # ── Consultation frequency: 28-day (4-week) window via prior_visits ──
            from datetime import date as _date_cv
            enc = encounter_start
            prior_before = [
                (d, t) for d, t in (prior_visits or [])
                if enc is not None and d is not None and str(d) < str(enc)
            ]
            if prior_before:
                last_d, last_t = prior_before[0]
                try:
                    gap = (_date_cv.fromisoformat(str(enc)) - _date_cv.fromisoformat(str(last_d))).days
                except Exception:
                    gap = None

                if gap is not None and gap <= _CONSULT_WINDOW:
                    correct_code = _consult_correct_code(activity_code or "", gap)
                    _consult_handled = True
                    if gap <= 7:
                        recall_reasons.append(f"WRONG CODE — prior consult {gap}d ago, within free 7-day period")
                        parts.append(
                            f"DB CHECK – CONSULTATION FREQUENCY: A paid visit is in the DB "
                            f"{gap} day(s) before this encounter (within 7-day free period) — "
                            f"Encounter Date: {last_d}. "
                            f"Action: Correct in EMR to {correct_code} → resubmit as Correction within 21 days of RA date."
                        )
                    else:
                        if freq_paid:
                            recall_reasons.append(
                                f"WRONG CODE — prior paid consultation {gap}d ago, revisit code required"
                            )
                            parts.append(
                                f"DB CHECK – CONSULTATION FREQUENCY: A paid visit is in the DB "
                                f"{gap} day(s) before this encounter (within 28-day repeat window) — "
                                f"Encounter Date: {last_d}. "
                                f"Action: Correct in EMR to {correct_code} → resubmit as Correction within 21 days of RA date."
                            )
                        else:
                            parts.append(
                                f"DB CHECK – CONSULTATION FREQUENCY: A visit is in the DB "
                                f"{gap} day(s) before this encounter (within 28-day window) — "
                                f"Encounter Date: {last_d}. Payment status unconfirmed. "
                                f"If prior claim is paid → Correct in EMR to {correct_code} → resubmit as Correction."
                            )
                else:
                    gap_str = f"{gap}" if gap is not None else "unknown"
                    parts.append(
                        f"DB CHECK – CONSULTATION FREQUENCY: Prior visit found {gap_str} day(s) before "
                        f"this encounter — Encounter Date: {last_d}. "
                        f"Gap exceeds 28-day repeat window — initial consultation code appears valid. "
                        "Action: reappeal with clinical notes confirming new presenting complaint."
                    )
            else:
                parts.append(
                    f"DB CHECK – CONSULTATION FREQUENCY: No prior encounter found in DB before "
                    f"this encounter. Frequency denial may reference cross-facility data. "
                    "Action: reappeal with clinical notes confirming no prior visit."
                )
        else:
            # ── Non-consultation codes (labs, procedures): 180-day freq_map ──
            from datetime import date as _date_freq

            def _freq_fmt_with_interval(entries, enc_date, limit=5):
                """Format freq entries excluding current encounter, with Nd before/after interval."""
                result = []
                for _e in entries:
                    _b, _p = (_e[0], _e[1]) if isinstance(_e, (list, tuple)) and len(_e) == 2 else (_e, None)
                    # Exclude the current encounter itself
                    if enc_date and _b and str(_b) == str(enc_date):
                        continue
                    _interval = ""
                    if enc_date and _b:
                        try:
                            _signed = (
                                _date_freq.fromisoformat(str(enc_date)) -
                                _date_freq.fromisoformat(str(_b))
                            ).days
                            if _signed > 0:
                                _interval = f" ({_signed}d before)"
                            elif _signed < 0:
                                _interval = f" ({abs(_signed)}d after)"
                        except Exception:
                            pass
                    _paid_str = f", paid: {_p}" if _p else ""
                    result.append((_signed if _signed is not None else 0, f"{_b}{_paid_str}{_interval}"))
                # Sort: before (positive) first, after (negative) last
                result.sort(key=lambda x: (-x[0], x[1]))
                return [lbl for _, lbl in result[:limit]]

            _freq_paid_filtered = _freq_fmt_with_interval(freq_paid, encounter_start)
            _freq_any_filtered  = _freq_fmt_with_interval(freq_any,  encounter_start)

            if _freq_paid_filtered:
                recall_reasons.append("FREQUENCY CONFLICT — prior paid activity confirmed in DB")
                enc_list = "; ".join(_freq_paid_filtered)
                parts.append(
                    f"DB CHECK – FREQUENCY CONFLICT: Activity code {code_lbl} has paid encounters on "
                    f"{enc_list} (within 180 days). Denial appears valid. "
                    "Action: obtain clinical justification confirming medical necessity for repeated "
                    "service. Attach physician notes + investigation results to reappeal."
                )
            elif freq_paid:
                # All paid entries were the current encounter — no genuine prior
                parts.append(
                    f"DB CHECK – FREQUENCY: No prior paid encounter found for {code_lbl} other than "
                    "current DOS. Payer may be referencing cross-facility data. "
                    "Action: reappeal with confirmation that no prior claim exists."
                )
            elif _freq_any_filtered:
                enc_list = "; ".join(_freq_any_filtered)
                parts.append(
                    f"DB CHECK – PRIOR CLAIM EXISTS (payment status unclear): Activity code "
                    f"{code_lbl} has claim history on {enc_list} (within 180 days). "
                    "Verify payment status in RA. If paid → attach clinical justification. "
                    "If not paid → reappeal with RA evidence."
                )
            else:
                parts.append(
                    f"DB CHECK – NO RECENT MATCH: No prior paid or claimed {code_lbl} for this "
                    "member in the past 180 days. Payer may be referencing cross-facility data. "
                    "Action: reappeal with no prior claim evidence + clinical justification."
                )

    # ── 2. Wrong code / Guideline / Revisit ──────────────────────────────────
    is_coding = any(kw in combined for kw in [
        "guideline", "must be used", "code for", "revisit", "must use",
        "should be", "use code", "use activity", "consultation",
        "alternative service", "should have been"
    ])
    if is_coding and not _consult_handled:
        # Sub-detect if it's a visit-type classification issue (new vs revisit)
        is_revisit_kw = any(kw in combined for kw in [
            "revisit", "follow-up", "follow up", "followup", "review consult",
            "9.02", "10.02", "11.02"
        ])
        is_newpt_kw = any(kw in combined for kw in [
            "new patient", "new consult", "first visit", "new visit"
        ])

        # Filter prior visits to those BEFORE this encounter
        enc = encounter_start
        prior_before = [
            (d, t) for d, t in (prior_visits or [])
            if enc is None or (d and str(d) < str(enc))
        ]

        if is_revisit_kw:
            if prior_before:
                recall_reasons.append("WRONG CODE — prior visits confirm revisit code required")
                last_pv_d, last_pv_t = prior_before[0]
                _gap_rv = None
                if enc is not None and last_pv_d is not None:
                    try:
                        from datetime import date as _date_rv
                        _gap_rv = (_date_rv.fromisoformat(str(enc)) - _date_rv.fromisoformat(str(last_pv_d))).days
                    except Exception:
                        pass
                correct_code_rv = _consult_correct_code(activity_code or "", _gap_rv or 99) if _is_consult_code else "9.02/10.02/11.02"
                if _is_consult_code:
                    # Concise format matching Section 1 consultation style
                    _consult_handled = True
                    _gap_str_rv = f"{_gap_rv} day(s)" if _gap_rv is not None else "unknown days"
                    parts.append(
                        f"DB CHECK – PRIOR VISITS CONFIRMED: A visit is in the DB "
                        f"{_gap_str_rv} before this encounter — Encounter Date: {last_pv_d}. "
                        f"Action: Correct in EMR to {correct_code_rv} → resubmit as Correction within 21 days of RA date."
                    )
                else:
                    interval_str = f" ({_gap_rv} days interval)" if _gap_rv is not None else ""
                    prior_dates = ", ".join(
                        f"{d} ({t})" if t else str(d)
                        for d, t in prior_before[:3]
                    )
                    parts.append(
                        f"DB CHECK – PRIOR VISITS CONFIRMED: Member has prior encounters on "
                        f"{prior_dates}. Most recent: {last_pv_d}{interval_str}. "
                        "TPA guidance is valid — patient is a returning patient. "
                        f"✏ Correct code: {activity_code or code_lbl} → {correct_code_rv}. "
                        "Correct in EMR → re-scrub corrected XML → resubmit as Correction "
                        "within 21 days of RA date."
                    )
            else:
                parts.append(
                    "DB CHECK – NO PRIOR VISITS FOUND: No prior encounter found for this member "
                    "in our system before this encounter. "
                    "TPA says 'revisit consultation' but our records show no prior visit. "
                    "Two options: (1) If this is genuinely a first visit — challenge TPA denial "
                    "by attaching documentation confirming new patient status. "
                    "(2) If a prior visit exists outside our system (e.g. another facility) — "
                    "obtain those records, attach as evidence, correct the code, and resubmit."
                )
        elif is_newpt_kw:
            if prior_before:
                prior_dates = ", ".join(
                    f"{d} ({t})" if t else str(d)
                    for d, t in prior_before[:3]
                )
                parts.append(
                    f"DB CHECK – EXISTING PATIENT: Member has prior encounters on {prior_dates}. "
                    "Billing as 'new patient' when prior visits exist is the likely denial reason. "
                    "Action: change to Follow-up/Revisit consultation code and resubmit as Correction."
                )
            else:
                parts.append(
                    "DB CHECK – NO PRIOR VISITS: No prior encounters found — patient may "
                    "genuinely be new. Review TPA comment to confirm exact required code. "
                    "If TPA still insists on revisit code, request clarification from payer "
                    "before resubmitting."
                )
        else:
            # Generic coding fix with guideline reference extracted from TPA comment
            # Try to extract guideline numbers from TPA comment e.g. "(9.02,10.02,11.02)"
            import re as _re
            glref = _re.findall(r'\d+\.\d+', tpa_comment or "")
            glref_str = ", ".join(glref[:4]) if glref else "see TPA comment"
            parts.append(
                f"CODING FIX: TPA indicates wrong activity code (guideline ref: {glref_str}). "
                "Review the TPA comment for the correct code. "
                "Correct in EMR → re-scrub corrected XML → resubmit as Correction "
                "within 21 days of RA date."
            )

    # ── 2b. CODE — Diagnosis-procedure mismatch ──────────────────────────────
    is_dx_mismatch = any(kw in combined for kw in [
        "does not match with any of the diagnosis",
        "not match.*diagnosis", "procedure code.*diagnosis",
        "invalid principal diagnosis", "invalid diagnosis",
        "diagnosis mismatch", "principal diagnosis"
    ])
    if is_dx_mismatch:
        import re as _re4
        # Extract procedure code and diagnosis codes from TPA comment
        proc_codes = _re4.findall(r'\b(\d{5})\b', tpa_comment or "")
        diag_codes = _re4.findall(r'\b([A-Z]\d{2,3}(?:\.\d+)?)\b', tpa_comment or "")
        proc_str = ", ".join(dict.fromkeys(proc_codes))[:60] if proc_codes else "see TPA comment"
        diag_str = ", ".join(dict.fromkeys(diag_codes))[:80] if diag_codes else "see TPA comment"
        parts.append(
            f"DIAGNOSIS-PROCEDURE MISMATCH: Procedure code {proc_str} does not match "
            f"diagnosis {diag_str}. "
            "Clinical review required: "
            "(1) If the procedure is correct — add or correct the supporting diagnosis code "
            "that justifies the procedure (ICD-10 linkage). "
            "(2) If the diagnosis is correct — the wrong procedure code was billed; "
            "correct the activity code. "
            "Correct in EMR → re-scrub → resubmit as Correction within 21 days."
        )

    # ── 2c. CLAI-012 — Contractual agreement / GP referral / same-condition ─────
    _denial_up = (denial_code or "").upper()
    _tpa_lower = (tpa_comment or "").lower()

    if _denial_up == "CLAI-014":
        # Resubmission type error — specific fix
        parts.append(
            "RESUBMISSION TYPE ERROR (CLAI-014): Claim was submitted with an incorrect "
            "resubmission type. Action: (1) If this is an original submission → set Type to 1 "
            "(New claim). (2) If this is a correction → attach the original claim reference "
            "and resubmit as Correction (Type 2). Correct in EMR and resubmit within 21 days."
        )
    elif _denial_up == "CLAI-012":
        if "gp referral required" in _tpa_lower or "gp referral" in _tpa_lower:
            parts.append(
                "GP REFERRAL MISSING (CLAI-012): GP referral was required per contractual "
                "agreement but not found. Action: Obtain valid GP referral letter, attach to "
                "claim and resubmit as Correction within 21 days of RA date."
            )
        elif ("initial consultation code" in _tpa_lower or "op initial consultation" in _tpa_lower
              or "28 days" in _tpa_lower or "billed within 28" in _tpa_lower):
            if _is_consult_code:
                # Consultation code + 28-day rule → handled by Section 1 (freq) or Section 2 (revisit)
                # Only add if nothing already captured above
                if not _consult_handled:
                    parts.append(
                        f"CLAI-012 – CONSULTATION 28-DAY RULE: TPA states initial consultation "
                        f"({code_lbl}) cannot be re-billed within 28 days for the same condition. "
                        "Check DB for prior consultation within 28 days and correct to repeat code "
                        "(9.02/10.02). Resubmit as Correction within 21 days."
                    )
            else:
                # Non-consultation code with consultation TPA comment → TPA may be incorrectly applied
                parts.append(
                    f"CLAI-012 – REVIEW REQUIRED: TPA references consultation code rules "
                    f"(28-day window) but activity code {code_lbl} is not a consultation code "
                    "(not in 9/9.01/9.02/10/10.01/10.02/11). "
                    "Action: Review if this denial is correctly applied to this activity code. "
                    "If incorrectly denied → reappeal clarifying that this code is not subject "
                    "to the consultation frequency rule."
                )
        else:
            parts.append(
                "CLAI-012 – CONTRACTUAL AGREEMENT: Submission not compliant with provider-payer "
                "contractual agreement. Review TPA comment for specific requirement. "
                "Action: correct the identified issue and resubmit as Correction within 21 days."
            )

    # ── 2c-b. Same-condition rule (non-CLAI specific, clinical keywords only) ──
    is_same_condition = (
        not _denial_up.startswith("CLAI") and
        any(kw in combined for kw in [
            "same condition cannot", "same condition",
            "op initial consultation", "initial consultation code",
        ]) and
        (denial_code or "").upper().startswith(("CODE", "MNEC"))
    )
    if is_same_condition and _is_consult_code:
        prior_consults = [
            (d, t) for d, t in (prior_visits or [])
            if t and ("consult" in (t or "").lower() or "op" in (t or "").lower())
            and (encounter_start is None or str(d) < str(encounter_start))
        ]
        if prior_consults:
            last_c = prior_consults[0]
            parts.append(
                f"DB CHECK – SAME CONDITION RULE: Member had prior consultation on "
                f"{last_c[0]} ({last_c[1]}). "
                "Action: bill follow-up as revisit code (9.02/10.02). "
                "Correct and resubmit as Correction."
            )
        else:
            parts.append(
                "SAME CONDITION RULE: Payer flagged duplicate initial consultation. "
                "DB shows no prior consultation of record. "
                "Action: reappeal with clinical notes confirming distinct presenting complaint."
            )

    # ── 2d. MNEC — Not clinically indicated / medically necessary ────────────
    is_not_clinical = any(kw in combined for kw in [
        "not clinically indicated", "not medically necessary",
        "not indicated based on", "excessive and not indicated",
        "not medically justified", "clinically not indicated",
        "service is not clinically"
    ])
    if is_not_clinical:
        if freq_paid:
            dates = _fmt_freq(freq_paid, 2)
            parts.append(
                f"DB CHECK – CLINICAL INDICATION (prior paid): Same {code_lbl} was PAID for "
                f"this member — {dates}. Use prior paid RA as precedent in reappeal. "
                "Attach: physician clinical notes, diagnosis justification, investigation results. "
                "Reappeal stating consistent clinical necessity and referencing prior approval."
            )
        else:
            parts.append(
                f"DB CHECK – CLINICAL INDICATION (no prior paid): No recent paid {code_lbl} "
                "found for this member. "
                "Attach clinical justification: physician notes, lab/imaging results, "
                "diagnosis details directly linking the service to the patient's condition. "
                "If service follows a guideline (e.g. DHA/MOH clinical protocol), cite it explicitly."
            )

    # ── 3. Authorization ──────────────────────────────────────────────────────
    if any(kw in combined for kw in [
        "author", "pre-auth", "pre auth", "prior approval", "approval required",
        "pa required", "no prior approval"
    ]):
        # Try to extract GP/service type from TPA comment
        import re as _re5
        svc_match = _re5.search(
            r'(general practitioner|gp|specialist|dental|optical|surgical|physio)',
            (tpa_comment or "").lower()
        )
        svc_type = svc_match.group(1).title() if svc_match else "the requested service"
        parts.append(
            f"AUTHORIZATION – {svc_type}: Prior authorization was required but not obtained. "
            "Actions: "
            "(1) Check if retrospective (post-service) authorization is permissible under the policy. "
            "(2) If retro-auth is available: submit auth request to payer immediately with clinical notes. "
            "(3) If not: reappeal explaining clinical urgency / emergency nature if applicable. "
            "Attach: referral letter, physician request, and any urgency documentation."
        )

    # ── 4a. Eligibility — date after member leave ─────────────────────────────
    is_elig_date = any(kw in combined for kw in [
        "service date is after", "after the member leave", "leave date",
        "date after the member", "membership expired", "policy expired"
    ])
    if is_elig_date:
        parts.append(
            "ELIGIBILITY – DATE OUT OF COVERAGE: Member's insurance coverage had ended "
            "before the date of service. "
            "Action: (1) Verify exact member leave/termination date with payer. "
            "(2) If member was still active on DOS, attach eligibility confirmation letter "
            "from the payer/TPA to reappeal. "
            "(3) If genuinely after leave date, no resubmission possible — inform patient "
            "that charges are their personal liability."
        )

    # ── 4b. Eligibility — activity code not in provider agreement ─────────────
    is_elig_contract = any(kw in combined for kw in [
        "not exist in provider agreement", "not in provider agreement",
        "activity code does not exist", "not covered under.*agreement",
        "provider agreement"
    ])
    if is_elig_contract:
        import re as _re6
        # Extract activity codes from TPA: e.g. "(76705) Activity code..."
        agreement_codes = _re6.findall(r'\((\d{4,6})\)', tpa_comment or "")
        code_ref = ", ".join(dict.fromkeys(agreement_codes))[:60] if agreement_codes else code_lbl
        parts.append(
            f"PROVIDER AGREEMENT: Activity code {code_ref} is not listed in the provider "
            "agreement for this facility/payer. "
            "Actions: "
            "(1) Check if the service should be billed under a different activity code "
            "that IS in the agreement. "
            "(2) If the service is legitimately provided: escalate to contracts/admin team "
            "to verify agreement schedule and request inclusion or correction. "
            "(3) No resubmission until contract scope is confirmed."
        )

    # ── 4c. Eligibility — general ─────────────────────────────────────────────
    if (not is_elig_date and not is_elig_contract) and any(kw in combined for kw in [
        "eligib", "not eligible", "membership", "coverage on", "member not",
        "member id", "member is not"
    ]):
        parts.append(
            "ELIGIBILITY: Verify member status on date of service via DHPO eligibility portal. "
            "Attach eligibility confirmation to reappeal."
        )

    # ── 5. Not covered ────────────────────────────────────────────────────────
    if not _consult_handled and any(kw in combined for kw in [
        "not covered", "not a benefit", "exclusion", "ncov", "benefit limit",
        "not payable"
    ]):
        parts.append(
            "NOT COVERED: Review member's benefit schedule for exclusions. "
            "Reappeal only if a clinical exception or policy override applies."
        )

    # ── 5c. PRCE — PC cap / manual processing ────────────────────────────────
    is_pc_cap = any(kw in combined for kw in [
        "pc cap", "manual processing", "capitation", "primary care cap",
        "will remain manual"
    ])
    if is_pc_cap:
        parts.append(
            "PC CAP / MANUAL PROCESSING: This claim falls under Primary Care capitation "
            "arrangement and requires manual adjudication by the payer. "
            "No resubmission required. "
            "Action: follow up directly with payer/TPA for manual settlement. "
            "Track this claim separately until payer confirms manual payment."
        )

    # ── 5b. PRCE-006 only — Free follow-up period / 7-day rule ──────────────
    # PRCE-001/002/003 are tariff/price errors — do NOT trigger a prior-visit check.
    # Only PRCE-006 is the free follow-up period denial.
    is_prce_followup = (
        (denial_code or "").upper() == "PRCE-006" or
        any(kw in combined for kw in [
            "free follow", "follow up period", "7-day", "first 7",
            "follow ups are not covered", "free period", "within free"
        ])
    )
    if is_prce_followup and not _consult_handled:
        import re as _re2
        # Extract number of days from TPA comment e.g. "first 7 days"
        day_match = _re2.search(r'first\s+(\d+)\s+day', (tpa_comment or "").lower())
        free_days  = int(day_match.group(1)) if day_match else 7

        enc = encounter_start
        prior_before = [
            (d, t) for d, t in (prior_visits or [])
            if enc is not None and d is not None and str(d) < str(enc)
        ]
        if prior_before:
            last_visit_date, last_visit_type = prior_before[0]
            try:
                from datetime import date as _date
                gap = (
                    _date.fromisoformat(str(enc)) -
                    _date.fromisoformat(str(last_visit_date))
                ).days
            except Exception:
                gap = None

            if gap is not None and gap < free_days:
                recall_reasons.append(f"FREE FOLLOW-UP PERIOD — last visit {gap}d ago (within {free_days}d window)")
                parts.append(
                    f"DB CHECK – WITHIN FREE FOLLOW-UP PERIOD: Member's last encounter was on "
                    f"{last_visit_date} ({last_visit_type or 'unknown type'}), "
                    f"{gap} day(s) before this claim — within the {free_days}-day free period. "
                    "Denial is valid per payer agreement. "
                    "Action: this follow-up cannot be billed separately. "
                    "If services rendered were clinically distinct (e.g. different complaint), "
                    "attach clinical notes proving distinct medical necessity and reappeal."
                )
            elif gap is not None:
                parts.append(
                    f"DB CHECK – OUTSIDE FREE PERIOD: Last encounter was {gap} day(s) before "
                    f"(>{free_days} days). Free follow-up period has elapsed. "
                    "Denial appears incorrect. Action: reappeal attaching the prior RA date "
                    f"({last_visit_date}) proving the {free_days}-day window had passed."
                )
            else:
                parts.append(
                    f"DB CHECK – PRIOR VISIT FOUND: Last encounter on {last_visit_date}. "
                    f"Could not calculate exact gap. Manually verify gap vs {free_days}-day rule."
                )
        else:
            parts.append(
                f"DB CHECK – NO PRIOR VISIT FOUND: No prior encounter for this member in our "
                f"DB before this encounter date. "
                f"If this is a first visit, the {free_days}-day free-period rule cannot apply. "
                "Action: reappeal stating no prior consultation on record; attach clinical notes "
                "confirming this is an independent medical encounter."
            )

    # ── 6a. OVERLAPPING_ACTIVITY scrub — extract conflicting claims + billed/paid dates + interval ─
    if "OVERLAPPING_ACTIVITY" in (scrub_rules or []):
        import re as _re3
        from datetime import date as _date_ov
        overlap_claims = list(dict.fromkeys(
            _re3.findall(r'OPC-[A-Z]-[A-Z0-9-]+', " ".join(scrub_messages or []))
        ))
        _ov_dates = overlap_claim_dates or {}

        # Split: intra-claim (same claim ID as current) vs inter-claim (different claim)
        _intra = [c for c in overlap_claims if claim_id and c == claim_id]
        _inter = [c for c in overlap_claims if not (claim_id and c == claim_id)]

        recall_reasons.append("OVERLAPPING ACTIVITY — proactive audit risk")

        if _intra and not _inter:
            # All flagged IDs are THIS claim → two activity lines within same claim overlap
            parts.append(
                f"SCRUB – OVERLAPPING_ACTIVITY (INTRA-CLAIM): Two or more activity lines "
                f"within this claim ({claim_id}) overlap at the same encounter date. "
                "⚠ PROACTIVE RECALL — facility review required. "
                "Action: Review if all activity lines were genuinely rendered. "
                "If any line is a duplicate → remove it and resubmit as Correction. "
                "If all lines are distinct services → attach separate clinical notes proving "
                "each service was independently rendered on the same date."
            )
        else:
            # Inter-claim overlap (different claim IDs)
            _targets = _inter or overlap_claims  # fallback if claim_id not set
            if _targets:
                claim_detail_parts = []
                _same_date_count = 0
                _diff_date_count = 0
                for _cid in _targets[:4]:
                    _ov_info = _ov_dates.get(_cid)
                    if isinstance(_ov_info, dict):
                        _ov_enc  = _ov_info.get("enc_date")
                        _ov_paid = _ov_info.get("paid_date")
                    else:
                        _ov_enc  = _ov_info
                        _ov_paid = None
                    if _ov_enc and encounter_start:
                        try:
                            _ov_gap = abs(
                                (_date_ov.fromisoformat(str(encounter_start)) -
                                 _date_ov.fromisoformat(str(_ov_enc))).days
                            )
                            _paid_str = f", paid: {_ov_paid}" if _ov_paid else ""
                            if _ov_gap == 0:
                                _same_date_count += 1
                                claim_detail_parts.append(
                                    f"{_cid} (billed: {_ov_enc}{_paid_str}, SAME DATE)"
                                )
                            else:
                                _diff_date_count += 1
                                claim_detail_parts.append(
                                    f"{_cid} (billed: {_ov_enc}{_paid_str}, {_ov_gap}d apart)"
                                )
                        except Exception:
                            claim_detail_parts.append(f"{_cid} (billed: {_ov_enc})")
                    elif _ov_enc:
                        claim_detail_parts.append(f"{_cid} (billed: {_ov_enc})")
                    else:
                        claim_detail_parts.append(_cid)
                claim_detail = "; ".join(claim_detail_parts)
                if _same_date_count > 0 and _diff_date_count == 0:
                    _ov_action = (
                        "SAME DATE confirmed → likely duplicate billing. "
                        "Remove duplicate line and resubmit as Correction."
                    )
                elif _diff_date_count > 0 and _same_date_count == 0:
                    _ov_action = (
                        "DIFFERENT DATES confirmed → distinct encounters. "
                        "Attach separate clinical notes per encounter proving distinct clinical events. "
                        "Reappeal with documentation."
                    )
                else:
                    _ov_action = (
                        "Verify if same or different encounter dates. "
                        "If SAME → remove duplicate. If DIFFERENT → attach separate clinical notes."
                    )
            else:
                claim_detail = "see scrub message"
                _ov_action = "Verify conflicting claim dates and take corrective action."
            parts.append(
                f"SCRUB – OVERLAPPING_ACTIVITY: Same member + activity code billed in "
                f"claim(s): {claim_detail}. "
                f"⚠ PROACTIVE RECALL — facility review required. "
                f"Action: {_ov_action} "
                "(Escalate to facility immediately within 21-day RA correction window.)"
            )

    # ── 6b. Scrubber cross-reference (fallback when no other pattern matched) ─
    remaining_scrub = [r for r in (scrub_rules or []) if r != "OVERLAPPING_ACTIVITY"]
    if remaining_scrub and not parts:
        msgs = "; ".join(scrub_messages[:2]) if scrub_messages else ""
        parts.append(
            f"SCRUBBER ALERT – {', '.join(remaining_scrub)}: "
            + (f"{msgs}. " if msgs else "")
            + "Resolve scrubber findings before resubmission to avoid repeat denial."
        )

    # ── 7. Generic fallback ───────────────────────────────────────────────────
    if not parts:
        parts.append(
            "Review TPA comment and denial description for specific corrective action. "
            "Attach supporting documentation and resubmit as Correction within 21 days of RA date."
        )

    return " | ".join(parts), recall_reasons


def _build_denial_scrub_report(
    db,
    facility_id: Optional[int],
    payer_id: Optional[str],
    encounter_type: Optional[str],
    date_from: Optional[date],
    date_to: Optional[date],
    date_field: Optional[str] = "remittance_date",
    facility_ids: Optional[str] = None,
):
    """
    Denial Scrub Action Report.

    Scope: ALL activity lines belonging to claims that have at least one
    denied activity (denial_code IS NOT NULL or net − paid > 0).
    This matches how resubmission works — the full claim is resubmitted as
    a correction, so all lines need to be visible.

    Columns: RA fields + Activity line + Scrub findings (claim-level) +
             Resubmission Action (internal) + Reappeal Comment (TPA text).
    """
    from sqlalchemy import text as _text

    fac_map = {f.id: f.facility_name for f in db.query(Facility).all()}

    # ── Build filter clauses for the CTE scope ───────────────────────────────
    scope_clauses: list[str] = ["1=1"]
    params: dict = {}

    if facility_ids:
        _fids = [int(x) for x in facility_ids.split(",") if x.strip().isdigit()]
        if _fids:
            placeholders = ", ".join(f":fid{i}" for i in range(len(_fids)))
            scope_clauses.append(f"r.facility_id IN ({placeholders})")
            for i, fid in enumerate(_fids):
                params[f"fid{i}"] = fid
    elif facility_id:
        scope_clauses.append("r.facility_id = :facility_id")
        params["facility_id"] = facility_id

    if payer_id:
        scope_clauses.append("r.payer_id = :payer_id")
        params["payer_id"] = payer_id

    if encounter_type:
        scope_clauses.append("c.encounter_type = :encounter_type")
        params["encounter_type"] = encounter_type

    if date_field == "treatment_date":
        if date_from:
            scope_clauses.append("DATE(c.encounter_start) >= :date_from")
            params["date_from"] = str(date_from)
        if date_to:
            scope_clauses.append("DATE(c.encounter_start) <= :date_to")
            params["date_to"] = str(date_to)
    else:  # remittance_date
        if date_from:
            scope_clauses.append("r.remittance_date >= :date_from")
            params["date_from"] = str(date_from)
        if date_to:
            scope_clauses.append("r.remittance_date <= :date_to")
            params["date_to"] = str(date_to)

    scope_sql = " AND ".join(scope_clauses)

    # ── Main query ────────────────────────────────────────────────────────────
    # CTE 1: find all (facility_id, claim_id, remittance.id) that have a denial
    # CTE 2: fetch ALL activity lines for those remittance records + scrub data
    sql = f"""
        WITH denied_rem AS (
            -- Remittance records that contain at least one denied activity line
            SELECT DISTINCT rad2.remittance_id_fk
            FROM remittance_activity_details rad2
            JOIN remittances r2  ON r2.id = rad2.remittance_id_fk
            JOIN claims c2       ON c2.facility_id = r2.facility_id
                                AND c2.claim_id    = r2.claim_id
            WHERE (rad2.denial_code IS NOT NULL
                   OR (COALESCE(rad2.net,0) - COALESCE(rad2.paid_amount,0)) > 0.001)
              AND {scope_sql.replace('r.', 'r2.').replace('c.', 'c2.')}
        ),
        scrub_per_claim AS (
            -- Scrub findings aggregated at (raw_xml_id, claim_id) level
            SELECT
                sxf.raw_xml_id,
                sxf.claim_id,
                array_agg(DISTINCT sxf.severity ORDER BY sxf.severity)
                    FILTER (WHERE sxf.severity IN ('BLOCK','WARN')) AS scrub_severities,
                array_agg(DISTINCT sxf.rule_id  ORDER BY sxf.rule_id)
                    FILTER (WHERE sxf.severity IN ('BLOCK','WARN')) AS scrub_rules,
                array_agg(sxf.message ORDER BY sxf.id)
                    FILTER (WHERE sxf.severity IN ('BLOCK','WARN')) AS scrub_messages,
                max(sxf.profile)      AS scrub_profile,
                max(sxf.scrubbed_at)  AS scrubbed_at
            FROM scrub_xml_findings sxf
            WHERE sxf.raw_xml_id IS NOT NULL
            GROUP BY sxf.raw_xml_id, sxf.claim_id
        )
        SELECT
            r.facility_id,
            r.claim_id,
            r.payer_id,
            r.remittance_date,
            r.ra_file_id,
            rax.source_filename      AS ra_file_name,
            r.tpa_comments           AS claim_tpa_comment,
            r.denial_code            AS claim_denial_code,
            r.denial_description     AS claim_denial_desc,
            r.gross_amount           AS claim_gross,
            r.net_amount             AS claim_net,
            r.paid_amount            AS claim_paid,
            r.denial_amount          AS claim_denied,
            c.patient_id,
            c.member_id,
            c.emirates_id,
            DATE(c.encounter_start)  AS encounter_start,
            DATE(c.encounter_end)    AS encounter_end,
            c.encounter_type,
            c.clinician_id,
            c.clinician_name,
            rx.source_filename       AS submission_file,
            DATE(rx.submission_date) AS submission_date,
            rad.id                   AS rad_id,
            rad.activity_id,
            rad.activity_code,
            rad.activity_type,
            COALESCE(rad.gross, 0)   AS act_gross,
            COALESCE(rad.net,   0)   AS act_net,
            COALESCE(rad.paid_amount,0) AS act_paid,
            (COALESCE(rad.net,0) - COALESCE(rad.paid_amount,0)) AS act_denied,
            rad.denial_code          AS act_denial_code,
            rad.denial_description   AS act_denial_desc,
            rad.denial_type          AS act_denial_type,
            sc.scrub_severities,
            sc.scrub_rules,
            sc.scrub_messages,
            sc.scrub_profile,
            sc.scrubbed_at
        FROM remittance_activity_details rad
        JOIN denied_rem             ON denied_rem.remittance_id_fk = rad.remittance_id_fk
        JOIN remittances r          ON r.id = rad.remittance_id_fk
        JOIN (
            SELECT DISTINCT ON (facility_id, claim_id)
                facility_id, claim_id, raw_xml_id,
                patient_id, member_id, emirates_id,
                encounter_start, encounter_end, encounter_type,
                clinician_id, clinician_name
            FROM claims
            ORDER BY facility_id, claim_id, id DESC
        ) c                         ON c.facility_id = r.facility_id
                                   AND c.claim_id    = r.claim_id
        LEFT JOIN raw_xml_store rx  ON rx.id = c.raw_xml_id
        LEFT JOIN raw_xml_store rax ON rax.id = r.raw_xml_id
        LEFT JOIN scrub_per_claim sc ON sc.raw_xml_id = c.raw_xml_id
                                    AND sc.claim_id   = c.claim_id
        ORDER BY r.remittance_date DESC, r.ra_file_id, r.claim_id, rad.id
    """

    rows = db.execute(_text(sql), params).fetchall()

    # ── Frequency / excess DB check ───────────────────────────────────────────
    # For denied rows collect unique (member_id, activity_code) pairs, then query
    # remittance_activity_details for the past 180 days to see if the same member
    # had the same activity paid/claimed recently.
    from collections import defaultdict as _dd

    denied_member_ids: set = set()
    denied_act_codes:  set = set()
    for _r in rows:
        _dcode = _r.act_denial_code or _r.claim_denial_code or ""
        _denied_line = bool(_dcode) or (
            (float(_r.act_net or 0) - float(_r.act_paid or 0)) > 0.001
        )
        if _denied_line and _r.member_id and _r.activity_code:
            denied_member_ids.add(_r.member_id)
            denied_act_codes.add(_r.activity_code)

    # freq_map stores (billed_date, paid_date) tuples so calibration msg can show both
    freq_map: dict = {}   # (member_id, activity_code) -> {"paid": [(billed,paid)], "any": [(billed,paid)]}
    if denied_member_ids and denied_act_codes:
        freq_sql = """
            SELECT
                c2.member_id,
                rad2.activity_code,
                c2.encounter_start::date AS billed_date,
                r2.remittance_date       AS paid_date,
                COALESCE(rad2.paid_amount, 0) AS paid_amount
            FROM remittance_activity_details rad2
            JOIN remittances r2 ON r2.id = rad2.remittance_id_fk
            JOIN (
                SELECT DISTINCT ON (facility_id, claim_id)
                    facility_id, claim_id, member_id,
                    encounter_start
                FROM claims
                ORDER BY facility_id, claim_id, id DESC
            ) c2 ON c2.facility_id = r2.facility_id AND c2.claim_id = r2.claim_id
            WHERE c2.member_id     = ANY(:mids)
              AND rad2.activity_code = ANY(:acts)
              AND r2.remittance_date >= CURRENT_DATE - INTERVAL '180 days'
            ORDER BY r2.remittance_date DESC
        """
        freq_rows = db.execute(
            _text(freq_sql),
            {"mids": list(denied_member_ids), "acts": list(denied_act_codes)},
        ).fetchall()

        _tmp: dict = _dd(lambda: {"paid": [], "any": []})
        for _fr in freq_rows:
            _key = (_fr.member_id, _fr.activity_code)
            _entry = (_fr.billed_date, _fr.paid_date)
            _tmp[_key]["any"].append(_entry)
            if float(_fr.paid_amount or 0) > 0.001:
                _tmp[_key]["paid"].append(_entry)
        freq_map = dict(_tmp)

    # ── Prior-visit lookup (for revisit / PRCE / DUPL calibration) ───────────
    # Fetch all encounter dates per member so _calibration_msg can determine
    # whether a patient is new vs returning and check free-follow-up windows.
    prior_visits_map: dict = {}   # member_id -> [(enc_date, enc_type), ...]
    if denied_member_ids:
        pv_sql = """
            SELECT DISTINCT member_id,
                            encounter_start::date AS enc_date,
                            encounter_type
            FROM claims
            WHERE member_id = ANY(:mids)
              AND encounter_start IS NOT NULL
            ORDER BY member_id, encounter_start::date DESC
        """
        pv_rows = db.execute(_text(pv_sql), {"mids": list(denied_member_ids)}).fetchall()
        from collections import defaultdict as _dd2
        _pv_tmp: dict = _dd2(list)
        for _pv in pv_rows:
            _pv_tmp[_pv.member_id].append((_pv.enc_date, _pv.encounter_type))
        prior_visits_map = dict(_pv_tmp)

    # ── Overlapping claim encounter-date lookup ────────────────────────────────
    # Extract claim IDs referenced in OVERLAPPING_ACTIVITY scrub messages,
    # then fetch their encounter dates so the Calibration Msg can show
    # "conflicting claim OPC-R-MH26-036010 has encounter on 2026-09-08".
    import re as _re_ov
    _overlap_cids: set = set()
    for _r in rows:
        if _r.scrub_rules and "OVERLAPPING_ACTIVITY" in (_r.scrub_rules or []):
            for _msg in (_r.scrub_messages or []):
                _overlap_cids.update(_re_ov.findall(r'OPC-[A-Z]-[A-Z0-9-]+', _msg or ""))

    overlap_enc_dates: dict = {}   # claim_id -> {"enc_date": date, "paid_date": date|None}
    if _overlap_cids:
        ov_sql = """
            SELECT DISTINCT ON (c3.claim_id)
                c3.claim_id,
                c3.encounter_start::date        AS enc_date,
                MAX(r3.remittance_date)
                    OVER (PARTITION BY c3.claim_id) AS paid_date
            FROM claims c3
            LEFT JOIN remittances r3
                ON r3.facility_id = c3.facility_id
               AND r3.claim_id    = c3.claim_id
            WHERE c3.claim_id = ANY(:cids)
              AND c3.encounter_start IS NOT NULL
            ORDER BY c3.claim_id, c3.id DESC
        """
        for _ov in db.execute(_text(ov_sql), {"cids": list(_overlap_cids)}).fetchall():
            overlap_enc_dates[_ov.claim_id] = {
                "enc_date":  _ov.enc_date,
                "paid_date": _ov.paid_date,
            }

    # ── Build workbook ────────────────────────────────────────────────────────
    wb  = openpyxl.Workbook()
    ws  = wb.active
    ws.title = "Denial Scrub Action"

    hdr_fill   = PatternFill("solid", fgColor="1F3864")
    hdr_font   = Font(bold=True, color="FFFFFF", size=10)
    hdr_align  = Alignment(horizontal="center", vertical="center", wrap_text=True)
    thin_side  = Side(style="thin", color="CCCCCC")
    thin_bdr   = Border(left=thin_side, right=thin_side, bottom=thin_side)

    fill_denied = PatternFill("solid", fgColor="FFE8E8")   # red  — denied activity
    fill_block  = PatternFill("solid", fgColor="FFECD0")   # orange — denied + BLOCK scrub
    fill_warn   = PatternFill("solid", fgColor="FFF9E6")   # yellow — denied + WARN scrub
    fill_paid   = PatternFill("solid", fgColor="F0FFF4")   # green — paid activity on a denied claim
    action_font = Font(color="1A1A6C", size=9, italic=True)
    appeal_font = Font(color="145A32", size=9, italic=True)

    # Column definitions: (header, width, col_letter for wrap/format)
    COLS = [
        # ── Claim ──────────────────────────────────────────────────────────
        ("Facility",              20),
        ("Claim ID",              26),
        ("Patient ID",            18),
        ("Member ID",             18),
        ("Emirates ID",           20),
        ("Enc. Start",            12),
        ("Enc. End",              12),
        ("Enc. Type",             10),
        ("Clinician",             20),
        # ── RA ─────────────────────────────────────────────────────────────
        ("RA Date",               12),
        ("RA File",               32),
        ("Submission File",       32),
        ("Sub. Date",             12),
        ("Payer",                 14),
        # ── Activity line ──────────────────────────────────────────────────
        ("Activity ID",           20),
        ("Activity Code",         14),
        ("Activity Type",         12),
        ("Submitted Net",         13),
        ("Paid Amount",           13),
        ("Denied Amount",         13),
        ("Activity Status",       12),   # DENIED / PAID / PARTIAL
        ("Denial Code",           12),
        ("Denial Description",    36),
        ("Denial Category",       14),
        ("Denial Type",           12),
        ("TPA Comment",           42),   # col 26 (AS)
        # ── Scrub ──────────────────────────────────────────────────────────
        ("Scrub Status",          12),
        ("Scrub Rules",           32),
        ("Scrub Messages",        52),
        ("Scrub Profile",         12),
        ("Scrubbed At",           17),
        # ── Action + Appeal ────────────────────────────────────────────────
        ("Resubmission Action",   55),   # col 32 (AF)
        ("Reappeal Comment",      60),   # col 33 (AG) — AX equiv: team posts this to TPA
        ("Calibration Msg",       70),   # col 34 (AH) — DB-backed calibration finding
        ("Recall Flag",           36),   # col 35 (AI) — 🚨 if DB confirms recall needed
    ]

    ws.row_dimensions[1].height = 36
    for col_idx, (col_name, col_w) in enumerate(COLS, start=1):
        cell = ws.cell(1, col_idx, col_name)
        cell.font      = hdr_font
        cell.fill      = hdr_fill
        cell.alignment = hdr_align
        cell.border    = thin_bdr
        ws.column_dimensions[get_column_letter(col_idx)].width = col_w

    ws.freeze_panes = "A2"

    # Track which claim_ids have scrub findings (for colour logic)
    for row_data in rows:
        scrub_rules    = list(row_data.scrub_rules    or [])
        scrub_messages = list(row_data.scrub_messages or [])
        scrub_sevs     = list(row_data.scrub_severities or [])
        worst_sev      = "BLOCK" if "BLOCK" in scrub_sevs else ("WARN" if scrub_sevs else None)

        act_denied_amt   = float(row_data.act_denied or 0)
        act_denial_code  = (row_data.act_denial_code or "").strip()
        is_denied_line   = bool(act_denial_code or act_denied_amt > 0.001)

        # Use the activity-level denial code; fall back to claim-level
        denial_code_eff  = act_denial_code or (row_data.claim_denial_code or "")
        dcode_info       = DENIAL_CODE_MAP.get(denial_code_eff, {})
        denial_cat       = dcode_info.get("category", denial_code_eff[:4] if denial_code_eff else "")
        denial_type_str  = (row_data.act_denial_type or dcode_info.get("type", ""))
        denial_desc_str  = (row_data.act_denial_desc or dcode_info.get("description", "") or "")

        # Activity status label
        if is_denied_line and act_denied_amt > 0.001 and float(row_data.act_paid or 0) > 0.001:
            act_status = "PARTIAL"
        elif is_denied_line:
            act_status = "DENIED"
        else:
            act_status = "PAID"

        # Auto comments — only populated on denied lines (paid lines carry same claim scrub data
        # but action/appeal only makes sense for what's actually denied)
        _freq_key  = (row_data.member_id, row_data.activity_code)
        _freq_info = freq_map.get(_freq_key, {"paid": [], "any": []})

        # Per-row overlap claim dates (for OVERLAPPING_ACTIVITY Calibration Msg)
        # Each entry: {claim_id: {"enc_date": date, "paid_date": date|None}}
        _overlap_dates_row: dict = {}
        if "OVERLAPPING_ACTIVITY" in (scrub_rules or []):
            for _msg in (scrub_messages or []):
                for _cid in _re_ov.findall(r'OPC-[A-Z]-[A-Z0-9-]+', _msg or ""):
                    if _cid in overlap_enc_dates:
                        _overlap_dates_row[_cid] = overlap_enc_dates[_cid]

        if is_denied_line:
            action_text = _auto_resubmission_action(
                denial_code_eff, scrub_rules, scrub_messages, row_data.claim_tpa_comment
            )
            appeal_text = _auto_reappeal_comment(
                denial_code_eff, scrub_rules, row_data.claim_tpa_comment
            )
            calib_text, _recall_reasons = _calibration_msg(
                denial_code_eff,
                row_data.act_denial_desc or row_data.claim_denial_desc,
                row_data.claim_tpa_comment,
                row_data.activity_code,
                _freq_info["paid"],
                _freq_info["any"],
                scrub_rules,
                scrub_messages,
                encounter_start=row_data.encounter_start,
                prior_visits=prior_visits_map.get(row_data.member_id, []),
                overlap_claim_dates=_overlap_dates_row,
                claim_id=row_data.claim_id,
            )
            recall_flag_text = ("🚨 " + " | ".join(_recall_reasons)) if _recall_reasons else "— Review"
        else:
            action_text = "—"
            appeal_text = "—"
            calib_text  = "—"
            recall_flag_text = "—"

        row_vals = [
            fac_map.get(row_data.facility_id, str(row_data.facility_id)),
            row_data.claim_id,
            row_data.patient_id or "",
            row_data.member_id or "",
            row_data.emirates_id or "",
            str(row_data.encounter_start)  if row_data.encounter_start  else "",
            str(row_data.encounter_end)    if row_data.encounter_end    else "",
            row_data.encounter_type        or "",
            row_data.clinician_name or row_data.clinician_id or "",
            str(row_data.remittance_date)  if row_data.remittance_date  else "",
            row_data.ra_file_name or row_data.ra_file_id or "",
            row_data.submission_file       or "",
            str(row_data.submission_date)  if row_data.submission_date  else "",
            row_data.payer_id              or "",
            row_data.activity_id           or "",
            row_data.activity_code         or "",
            # CDT (dental) codes always start with 'D'. If activity_type is "Dental"
            # but the code is numeric/CPT, correct it to "Service".
            (row_data.activity_type or "")
            if not (
                (row_data.activity_type or "").lower() == "dental"
                and not str(row_data.activity_code or "").upper().startswith("D")
            )
            else "Service",
            float(row_data.act_net  or 0),
            float(row_data.act_paid or 0),
            act_denied_amt,
            act_status,
            denial_code_eff,
            denial_desc_str,
            denial_cat,
            denial_type_str,
            row_data.claim_tpa_comment     or "",
            worst_sev                      or "—",
            ", ".join(scrub_rules)         if scrub_rules    else "—",
            " | ".join(scrub_messages[:3]) if scrub_messages else "—",
            row_data.scrub_profile         or "—",
            str(row_data.scrubbed_at)[:16] if row_data.scrubbed_at else "—",
            action_text,
            appeal_text,
            calib_text,
            recall_flag_text,
        ]

        r_idx = ws.max_row + 1

        # Row colour: denied lines by scrub severity, paid lines green
        if is_denied_line:
            if worst_sev == "BLOCK":
                row_fill = fill_block
            elif worst_sev == "WARN":
                row_fill = fill_warn
            else:
                row_fill = fill_denied
        else:
            row_fill = fill_paid

        # Wrap columns: TPA Comment (26), Scrub Messages (29), Action (32), Appeal (33), Calib (34)
        WRAP_COLS = {26, 29, 32, 33, 34, 35}
        MONEY_COLS = {18, 19, 20}

        for c_idx, val in enumerate(row_vals, start=1):
            cell = ws.cell(r_idx, c_idx, val)
            cell.alignment = Alignment(vertical="top", wrap_text=(c_idx in WRAP_COLS))
            cell.border    = thin_bdr
            cell.fill      = row_fill
            if c_idx in MONEY_COLS:
                cell.number_format = "#,##0.00"
            if c_idx == 32:
                cell.font = action_font
            if c_idx == 33:
                cell.font = appeal_font
            if c_idx == 34:
                cell.font = Font(color="7B2D00", size=9, italic=True, bold=False)
            if c_idx == 35 and val and val != "—":
                cell.font = Font(color="C00000", size=9, bold=True)
                cell.fill = PatternFill("solid", fgColor="FFF0F0")

    ws.auto_filter.ref = f"A1:{get_column_letter(len(COLS))}1"

    fac_label = fac_map.get(facility_id, str(facility_id)) if facility_id else "multi"
    df_label  = (str(date_from) or "").replace("-", "") or "all"
    dt_label  = (str(date_to)   or "").replace("-", "") or "all"
    filename  = f"DenialScrubAction_{fac_label}_{df_label}_{dt_label}.xlsx"

    return wb, filename, ws.max_row - 1


# ── Direct download endpoint ──────────────────────────────────────────────────
@router.get("/denial-scrub-action")
def export_denial_scrub_action(
    facility_id:    Optional[int]  = None,
    payer_id:       Optional[str]  = None,
    encounter_type: Optional[str]  = None,
    date_from:      Optional[date] = None,
    date_to:        Optional[date] = None,
    date_field:     Optional[str]  = "remittance_date",
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Denial Scrub Action Report — all activities for denied claims + scrub + action + reappeal."""
    wb, filename, _ = _build_denial_scrub_report(
        db, facility_id, payer_id, encounter_type, date_from, date_to, date_field)
    return _excel_response(wb, filename)


REPORT_BUILDERS["denial-scrub-action"] = _build_denial_scrub_report
