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
            act_start = _parse_date(_text(act, "Start"))
            self.db.add(ScrubClaimActivity(
                scrub_claim_id=scrub_claim.id,
                session_id=self.session_id,
                activity_type=atype,
                activity_code=acode,
                quantity=qty,
                net=net,
                authorization_id=auth_id if auth_id else None,
                activity_start=act_start,
                sequence_no=seq,
            ))
            seq += 1


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
    # Accept ISO-8601 date (YYYY-MM-DD) and datetime (YYYY-MM-DDTHH:MM:SS)
    try:
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
