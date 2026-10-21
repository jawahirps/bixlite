"""
scrub_extractor.py — Lightweight CS XML parser for pre-submission scrub staging.

Parses a ClaimSubmission XML file and writes into the scrub_* tables ONLY.
Does NOT touch any production tables (claims, claim_activities, etc.).
Uses the same XML parsing approach as DHPOXmlParser but is intentionally
minimal — we only extract the fields the rule engine needs.
"""
from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal, InvalidOperation
from io import BytesIO
from typing import Optional
from xml.etree import ElementTree as ET

from sqlalchemy.orm import Session

from app.models import ScrubSession, ScrubClaim, ScrubClaimActivity, ScrubClaimDiagnosis

logger = logging.getLogger(__name__)


class ScrubXmlExtractor:
    """
    Extract claims from a raw CS XML bytes payload and persist them into the
    scrub staging tables under a given ScrubSession.

    Usage:
        extractor = ScrubXmlExtractor(db, session_id=42, facility_id=1)
        count = extractor.parse(xml_bytes)
    """

    def __init__(self, db: Session, session_id: int, facility_id: int) -> None:
        self.db = db
        self.session_id = session_id
        self.facility_id = facility_id

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def parse(self, xml_bytes: bytes) -> int:
        """
        Parse *xml_bytes* (raw CS XML) and insert ScrubClaim rows.
        Returns the number of claims successfully parsed.
        """
        try:
            root = ET.parse(BytesIO(xml_bytes)).getroot()
        except ET.ParseError as exc:
            raise ValueError(f"Invalid XML: {exc}") from exc

        # Strip namespace prefixes so we can use plain tag names
        _strip_ns(root)

        self.receiver_id = (_text(root, "Header/ReceiverID") or "").strip() or None

        claim_elements = root.findall(".//Claim")
        if not claim_elements:
            # Some files wrap claims under ClaimSubmission/Claims/Claim
            claim_elements = root.findall(".//Claims/Claim")

        count = 0
        for elem in claim_elements:
            try:
                self._process_claim(elem)
                count += 1
            except Exception:
                logger.exception(
                    "ScrubXmlExtractor: failed to parse claim element in session %d",
                    self.session_id,
                )

        self.db.flush()
        return count

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _process_claim(self, elem: ET.Element) -> None:
        claim_id = _text(elem, "ID") or _text(elem, "ClaimID") or ""
        encounter_type = _text(elem, "Encounter/Type")
        encounter_start = _parse_date(_text(elem, "Encounter/Start"))
        encounter_end = _parse_date(_text(elem, "Encounter/End"))
        member_id = _text(elem, "Patient/MemberID") or _text(elem, "MemberID") or ""
        emirates_id = _text(elem, "Patient/EmiratesID") or _text(elem, "EmiratesID")
        payer_id = _text(elem, "PayerID") or ""
        patient_id = _text(elem, "Encounter/PatientID")
        resubmission_key = _text(elem, "Resubmission/Type") or ""

        # Net amount: prefer <Net> under the claim root; fall back to summing activities
        net_amount: Optional[Decimal] = _decimal(_text(elem, "Net"))

        scrub_claim = ScrubClaim(
            session_id=self.session_id,
            facility_id=self.facility_id,
            claim_id=claim_id,
            encounter_type=encounter_type,
            encounter_start=encounter_start,
            encounter_end=encounter_end,
            member_id=member_id,
            emirates_id=emirates_id,
            payer_id=payer_id,
            receiver_id=getattr(self, "receiver_id", None) or payer_id,
            patient_id=patient_id,
            resubmission_key=resubmission_key,
            net_amount=net_amount,
        )
        self.db.add(scrub_claim)
        self.db.flush()  # populate scrub_claim.id before FK children

        # Diagnoses
        seq = 1
        for diag in elem.findall(".//Diagnosis"):
            dtype = _text(diag, "Type") or "Secondary"
            dcode = _text(diag, "Code") or ""
            if not dcode:
                continue
            self.db.add(ScrubClaimDiagnosis(
                scrub_claim_id=scrub_claim.id,
                session_id=self.session_id,
                diagnosis_type=dtype,
                diagnosis_code=dcode,
                sequence_no=seq,
            ))
            seq += 1

        # Activities
        seq = 1
        for act in elem.findall(".//Activity"):
            acode = _text(act, "Code") or ""
            atype = _text(act, "Type") or ""
            if not acode:
                continue
            qty = _decimal(_text(act, "Quantity")) or Decimal("1")
            net = _decimal(_text(act, "Net")) or Decimal("0")
            auth_id = _text(act, "PriorAuthorizationID")
            clinician = _text(act, "Clinician")
            act_start = _parse_date(_text(act, "Start"))
            self.db.add(ScrubClaimActivity(
                scrub_claim_id=scrub_claim.id,
                session_id=self.session_id,
                activity_type=atype,
                activity_code=acode,
                quantity=qty,
                net=net,
                authorization_id=auth_id if auth_id else None,
                clinician_id=clinician,
                activity_start=act_start,
                sequence_no=seq,
            ))
            seq += 1


class ScrubExcelExtractor:
    """
    Same staging as ScrubXmlExtractor, from an Excel sheet with one row per activity.
    Rows with the same Claim ID form one claim; diagnoses are comma-separated (first = principal).
    """

    COLUMNS = {
        "claim_id": ("claim id", "claimid", "invoice", "invoice no", "invoice#", "invoice number"),
        "patient_id": ("mrn", "file no", "patient id", "patientid"),
        "member_id": ("member id", "memberid", "card no", "card number"),
        "emirates_id": ("emirates id", "emiratesid"),
        "payer_id": ("payer id", "payerid", "payer"),
        "receiver_id": ("receiver id", "receiverid", "receiver", "insurance", "company"),
        "encounter_start": ("date", "encounter date", "visit date", "encounter start"),
        "clinician_id": ("clinician", "clinician id", "doctor licence", "doctor license"),
        "activity_type": ("activity type", "type"),
        "activity_code": ("activity code", "code", "cpt", "procedure code"),
        "quantity": ("quantity", "qty"),
        "net": ("net", "net amount"),
        "authorization_id": ("authorization", "authorization id", "approval no", "approval number", "prior authorization id"),
        "diagnoses": ("diagnosis", "diagnoses", "icd", "icd codes"),
    }

    def __init__(self, db: Session, session_id: int, facility_id: int) -> None:
        self.db = db
        self.session_id = session_id
        self.facility_id = facility_id

    def parse(self, xlsx_bytes: bytes) -> int:
        import openpyxl
        try:
            ws = openpyxl.load_workbook(BytesIO(xlsx_bytes), read_only=True, data_only=True).worksheets[0]
        except Exception as exc:
            raise ValueError(f"Invalid Excel file: {exc}") from exc
        rows = [r for r in ws.iter_rows(values_only=True) if any(c not in (None, "") for c in r)]
        if not rows:
            raise ValueError("Excel file is empty.")
        header = [str(c or "").strip().lower() for c in rows[0]]
        col = {}
        for field, names in self.COLUMNS.items():
            for i, h in enumerate(header):
                if h in names and field not in col:
                    col[field] = i
        missing = [f for f in ("claim_id", "activity_code") if f not in col]
        if missing:
            raise ValueError("Excel needs columns: Claim ID (or Invoice) and Activity code. Missing: " + ", ".join(missing))

        def val(r, f):
            i = col.get(f)
            v = r[i] if i is not None and i < len(r) else None
            return None if v is None or str(v).strip() == "" else v

        claims: dict = {}
        for r in rows[1:]:
            cid = val(r, "claim_id")
            if cid is None:
                continue
            claims.setdefault(str(cid).strip(), []).append(r)

        for cid, lines in claims.items():
            first = lines[0]
            d = val(first, "encounter_start")
            enc = d.date() if hasattr(d, "date") else _parse_date(str(d)) if d else None
            payer = str(val(first, "payer_id") or val(first, "receiver_id") or "").strip()
            sc = ScrubClaim(
                session_id=self.session_id, facility_id=self.facility_id, claim_id=cid,
                encounter_type=None, encounter_start=enc, encounter_end=enc,
                member_id=str(val(first, "member_id") or ""), emirates_id=val(first, "emirates_id"),
                payer_id=payer, receiver_id=str(val(first, "receiver_id") or payer).strip() or None,
                patient_id=str(val(first, "patient_id") or "").strip() or None,
                net_amount=sum((_decimal(str(val(r, "net") or 0)) or Decimal("0")) for r in lines),
            )
            self.db.add(sc)
            self.db.flush()
            seen_dx = []
            for r in lines:
                for dx in str(val(r, "diagnoses") or "").replace(";", ",").split(","):
                    dx = dx.strip().upper()
                    if dx and dx not in seen_dx:
                        seen_dx.append(dx)
            for n, dx in enumerate(seen_dx, 1):
                self.db.add(ScrubClaimDiagnosis(scrub_claim_id=sc.id, session_id=self.session_id,
                                                diagnosis_type="Principal" if n == 1 else "Secondary",
                                                diagnosis_code=dx, sequence_no=n))
            for n, r in enumerate(lines, 1):
                code = val(r, "activity_code")
                if code is None:
                    continue
                self.db.add(ScrubClaimActivity(
                    scrub_claim_id=sc.id, session_id=self.session_id,
                    activity_type=str(val(r, "activity_type") or ""), activity_code=str(code).strip(),
                    quantity=_decimal(str(val(r, "quantity") or 1)) or Decimal("1"),
                    net=_decimal(str(val(r, "net") or 0)) or Decimal("0"),
                    authorization_id=str(val(r, "authorization_id")).strip() if val(r, "authorization_id") else None,
                    clinician_id=str(val(r, "clinician_id") or "").strip() or None,
                    activity_start=enc, sequence_no=n,
                ))
        self.db.flush()
        return len(claims)


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------

def _strip_ns(element: ET.Element) -> None:
    """Remove namespace prefixes from all tags in the element tree in-place."""
    if "}" in element.tag:
        element.tag = element.tag.split("}", 1)[1]
    for child in element:
        _strip_ns(child)


def _text(elem: ET.Element, path: str) -> Optional[str]:
    node = elem.find(path)
    if node is None or node.text is None:
        return None
    val = node.text.strip()
    return val if val else None


def _parse_date(raw: Optional[str]) -> Optional[date]:
    if not raw:
        return None
    # Accept DHA format (DD/MM/YYYY HH:MM), ISO-8601 date (YYYY-MM-DD) and datetime (YYYY-MM-DDTHH:MM:SS)
    raw = raw.strip()
    try:
        if "/" in raw[:10]:
            dd, mm, yyyy = raw[:10].split("/")
            return date(int(yyyy), int(mm), int(dd))
        return date.fromisoformat(raw[:10])
    except (ValueError, TypeError):
        return None


def _decimal(raw: Optional[str]) -> Optional[Decimal]:
    if not raw:
        return None
    try:
        return Decimal(raw)
    except InvalidOperation:
        return None
