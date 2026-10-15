"""
Ghaf App — DHA/DHPO XML Parser Engine
Handles: Memory-optimized streaming parse of ClaimSubmission and
RemittanceAdvice XML payloads (DHA Shafafiya / eClaimLink format) that have
been staged into `raw_xml_store` by the upload endpoint or an external
eClaimLink fetch job.

This is invoked by the "Trigger DHA XML Sync" action in the React app
(POST /api/dha-sync/trigger), which calls `run_pending(...)` below.
"""
from __future__ import annotations

import logging
from datetime import datetime, date
from decimal import Decimal, InvalidOperation
from io import BytesIO
from typing import Optional

import re

from lxml import etree
from sqlalchemy.orm import Session
from sqlalchemy import select

from ..models import RawXmlStore, Claim, ClaimDiagnosis, ClaimActivity, ClaimObservation, Remittance, RemittanceActivityDetail, FetchLog

log = logging.getLogger(__name__)


def _strip_namespaces(xml_str: str) -> str:
    """Remove XML namespace declarations and prefixes so findtext() works without ns maps."""
    # Remove xmlns declarations (xmlns="..." and xmlns:prefix="...")
    s = re.sub(r'\s+xmlns(?::\w+)?="[^"]*"', '', xml_str)
    # Remove namespace-prefixed attributes: xsi:noNamespaceSchemaLocation="..." → removed
    s = re.sub(r'''\s+\w+:\w+=(?:"[^"]*"|'[^']*')''', '', s)
    # Remove namespace prefixes from tags: <ns1:Claim> → <Claim>, </ns1:Claim> → </Claim>
    s = re.sub(r'<(/?)\w+:(\w+)', r'<\1\2', s)
    return s


def _detect_root_tag(xml_str: str) -> str:
    """Return the root element tag name (namespace-stripped) or empty string."""
    m = re.search(r'<\s*(?:\w+:)?(\w+)', xml_str[:500])
    return m.group(1) if m else ""


# DHA XML uses numeric codes for activity type; map to the string values the DB expects
_ACTIVITY_TYPE_MAP = {
    "1": "CPT", "2": "HCPCS", "3": "Dental", "4": "Drug", "5": "Service",
    "CPT": "CPT", "HCPCS": "HCPCS", "Dental": "Dental", "Drug": "Drug", "Service": "Service",
}
_ALLOWED_ACTIVITY_TYPES = frozenset(_ACTIVITY_TYPE_MAP.values())


def _normalize_activity_type(raw: Optional[str]) -> str:
    if not raw:
        return "Service"
    mapped = _ACTIVITY_TYPE_MAP.get(raw.strip(), raw.strip())
    return mapped if mapped in _ALLOWED_ACTIVITY_TYPES else "Service"


# DHA XML uses "Principal" for primary diagnosis; DB constraint allows Primary/Admitting/Secondary
_DIAG_TYPE_MAP = {
    "Principal": "Primary",
    "principal": "Primary",
    "primary": "Primary",
    "Primary": "Primary",
    "Admitting": "Admitting",
    "admitting": "Admitting",
    "Secondary": "Secondary",
    "secondary": "Secondary",
}
_ALLOWED_DIAG_TYPES = frozenset(["Primary", "Admitting", "Secondary"])


def _normalize_diag_type(raw: Optional[str]) -> str:
    if not raw:
        return "Primary"
    mapped = _DIAG_TYPE_MAP.get(raw.strip(), raw.strip())
    return mapped if mapped in _ALLOWED_DIAG_TYPES else "Secondary"


class SyncRunSummary:
    def __init__(self) -> None:
        self.pending_records = 0
        self.parsed_records = 0
        self.quarantined_records = 0
        self.records_parsed_total = 0  # individual claims/remits across all files


class DHPOXmlParser:
    """Parses staged raw XML payloads into normalized claim/remittance rows."""

    def __init__(self, db: Session):
        self.db = db

    # ------------------------------------------------------------------
    # Entry point used by the DHA Sync trigger endpoint
    # ------------------------------------------------------------------
    def run_pending(
        self,
        facility_id: Optional[int] = None,
        raw_xml_ids: Optional[list] = None,   # restrict to specific IDs (used by date-filtered reparse)
    ) -> SyncRunSummary:
        summary = SyncRunSummary()

        stmt = select(RawXmlStore).where(RawXmlStore.parse_status == "pending")
        if facility_id is not None:
            stmt = stmt.where(RawXmlStore.facility_id == facility_id)
        if raw_xml_ids is not None:
            stmt = stmt.where(RawXmlStore.id.in_(raw_xml_ids))
        stmt = stmt.order_by(RawXmlStore.id.asc())

        records = self.db.scalars(stmt).all()
        summary.pending_records = len(records)

        if not records:
            log.info("No pending XML records found in raw store.")
            return summary

        log.info("Found %d pending XML records to parse.", len(records))

        for record in records:
            try:
                record.parsed_at = datetime.utcnow()

                if record.xml_type == "ClaimSubmission":
                    parsed_count = self._process_claims_submission(record)
                elif record.xml_type == "RemittanceAdvice":
                    parsed_count = self._process_remittance_advice(record)
                else:
                    raise ValueError(f"Unknown XML type: {record.xml_type}")

                record.parse_status = "parsed"
                record.records_parsed = parsed_count
                self.db.commit()

                summary.parsed_records += 1
                summary.records_parsed_total += parsed_count
                log.info(
                    "Successfully processed raw_xml_id=%s (%s). Records: %d",
                    record.id, record.xml_type, parsed_count,
                )

            except Exception as e:
                self.db.rollback()
                log.error("Failed parsing raw_xml_id=%s. Sent to quarantine. Error: %s", record.id, e, exc_info=True)
                record.parse_status = "quarantined"
                record.parse_error = str(e)
                self.db.commit()
                summary.quarantined_records += 1

        return summary

    # ------------------------------------------------------------------
    # ClaimSubmission  (also handles ClaimActivity payer-response files)
    # ------------------------------------------------------------------
    def _process_claims_submission(self, record: RawXmlStore) -> int:
        """Parses ClaimSubmission and ClaimActivity XML into normalized Claim rows.

        ClaimActivity files (OP-AC-CR-* naming) are payer acknowledgements of
        submitted claims.  They contain <Claim> elements but NO <Encounter> child.
        We detect the root tag and adjust parsing accordingly.
        """
        # Strip namespaces so findtext() works without namespace maps
        clean_xml = _strip_namespaces(record.xml_content or "")
        root_tag = _detect_root_tag(clean_xml)
        is_activity = root_tag in ("ClaimActivity", "ClaimResponse", "ClaimAcknowledgement")
        if is_activity:
            log.info("raw_xml_id=%s detected as %s (activity/response format)", record.id, root_tag)

        # ReceiverID is a Header-level field per DHA eClaimLink schema:
        #   <Claim.Submission><Header><ReceiverID>INS044</ReceiverID>...
        # Parse the full tree once to extract it before iterating Claims.
        try:
            _root = etree.fromstring(clean_xml.encode("utf-8"))
            header_receiver_id = (
                _root.findtext("Header/ReceiverID")
                or _root.findtext("Header/Receiver/ID")
                or ""
            ).strip() or None
        except Exception:
            header_receiver_id = None
        if header_receiver_id:
            log.debug("raw_xml_id=%s Header/ReceiverID=%s", record.id, header_receiver_id)

        context = etree.iterparse(
            BytesIO(clean_xml.encode("utf-8")), events=("end",), tag="Claim"
        )
        claim_count = 0

        for _event, elem in context:
            claim_id = elem.findtext("ID")
            if not claim_id:
                elem.clear()
                continue

            encounter_id = elem.findtext("Encounter/ID")
            # ClaimActivity files have no <Encounter>: synthesise an encounter_id
            if not encounter_id:
                encounter_id = f"ACT-{claim_id}"

            # Receiver: prefer Header-level ReceiverID (correct per schema),
            # fall back to any claim-level tag for older/non-standard files.
            receiver_id = (
                header_receiver_id
                or elem.findtext("ReceiverID")
                or elem.findtext("Receiver/ID")
            )

            db_claim = Claim(
                facility_id=record.facility_id,
                raw_xml_id=record.id,
                source_channel="FILE",  # DHA eClaimLink — distinguishes from Riayati API
                claim_id=claim_id,
                encounter_id=encounter_id,
                encounter_type=elem.findtext("Encounter/Type"),
                encounter_start=self._parse_dt(elem.findtext("Encounter/Start")),
                encounter_end=self._parse_optional_dt(elem.findtext("Encounter/End")),
                member_id=elem.findtext("Patient/MemberID") or elem.findtext("MemberID") or "UNKNOWN",
                patient_id=elem.findtext("Encounter/PatientID") or elem.findtext("PatientID") or None,
                emirates_id=elem.findtext("Patient/EmiratesID") or elem.findtext("EmiratesIDNumber"),
                payer_id=elem.findtext("PayerID") or "UNKNOWN",
                receiver_id=receiver_id,
                clinician_id=(
                    elem.findtext("ClinicianID")
                    or elem.findtext("Activity/Clinician")
                    or "UNKNOWN"
                ),
                gross_amount=self._parse_decimal(elem.findtext("Gross")),
                net_amount=self._parse_decimal(elem.findtext("Net")),
                patient_share_total=self._parse_decimal(elem.findtext("PatientShare")),
                submission_date=record.submission_date or datetime.utcnow().date(),
                claim_status="submitted",
                resubmission_type=elem.findtext("Resubmission/Type"),
                resubmission_key=elem.findtext("Resubmission/Type") or "",
                resubmission_comment=elem.findtext("Resubmission/Comment"),
            )

            # 1. Nested Diagnoses
            for seq, diag in enumerate(elem.findall("Diagnosis"), start=1):
                db_claim.diagnoses.append(
                    ClaimDiagnosis(
                        facility_id=record.facility_id,
                        diagnosis_type=_normalize_diag_type(diag.findtext("Type")),
                        diagnosis_code=diag.findtext("Code") or "UNKNOWN",
                        diagnosis_description=diag.findtext("Description"),
                        sequence_no=seq,
                    )
                )

            # 2. Nested Activities (+ sub-nested Observations)
            for seq, act in enumerate(elem.findall("Activity"), start=1):
                db_act = ClaimActivity(
                    facility_id=record.facility_id,
                    activity_id=act.findtext("ID"),
                    activity_type=_normalize_activity_type(act.findtext("Type")),
                    activity_code=act.findtext("Code") or "UNKNOWN",
                    activity_description=act.findtext("Description"),
                    non_std_code=act.findtext("NonStandardCode"),
                    quantity=self._parse_decimal(act.findtext("Quantity"), default="1.0"),
                    gross=self._parse_decimal(act.findtext("Gross")),
                    net=self._parse_decimal(act.findtext("Net")),
                    patient_share=self._parse_decimal(act.findtext("PatientShare")),
                    activity_start=self._parse_dt(act.findtext("Start")).date(),
                    authorization_id=act.findtext("PriorAuthorizationID"),
                    clinician_id=act.findtext("Clinician"),
                    sequence_no=seq,
                )

                for obs in act.findall("Observation"):
                    db_act.observations.append(
                        ClaimObservation(
                            observation_type=obs.findtext("Type") or "Other",
                            observation_code=obs.findtext("Code"),
                            observation_value=obs.findtext("Value"),
                            value_type=obs.findtext("ValueType"),
                        )
                    )
                db_claim.activities.append(db_act)

            # 3. Fallback: when the claim-level <Net> / <Gross> is missing or zero,
            #    derive the amounts by summing across all parsed <Activity> children.
            #    This handles ClaimSubmission XMLs that omit claim-level totals and
            #    store financial data only at the activity line level.
            #
            #    IMPORTANT: only apply to INITIAL SUBMISSIONS (resubmission_key == "").
            #    Resubmission rows can legitimately carry zero net (e.g. correction-only
            #    resubmissions with no additional amount due), so we must not overwrite them.
            is_initial_submission = not db_claim.resubmission_key  # '' or None → initial
            if is_initial_submission:
                if not db_claim.net_amount or db_claim.net_amount == Decimal("0"):
                    act_net_sum = sum(
                        (act.net or Decimal("0")) for act in db_claim.activities
                    )
                    if act_net_sum:
                        db_claim.net_amount = act_net_sum
                        log.debug(
                            "claim_id=%s: net_amount derived from activity sum: %s",
                            claim_id, act_net_sum,
                        )

                if not db_claim.gross_amount or db_claim.gross_amount == Decimal("0"):
                    act_gross_sum = sum(
                        (act.gross or Decimal("0")) for act in db_claim.activities
                    )
                    if act_gross_sum:
                        db_claim.gross_amount = act_gross_sum

            # Upsert: match on (facility_id, claim_id, resubmission_key) so that
            # initial submissions and resubmissions are stored as SEPARATE rows.
            # Only replace if the SAME submission level is re-synced (true duplicate).
            resub_key = db_claim.resubmission_key  # '' for initial, type for resub
            existing = self.db.scalar(
                select(Claim).where(
                    Claim.facility_id    == record.facility_id,
                    Claim.claim_id       == claim_id,
                    Claim.resubmission_key == resub_key,
                )
            )
            if existing:
                self.db.delete(existing)
                self.db.flush()

            self.db.add(db_claim)
            claim_count += 1

            elem.clear()
            while elem.getprevious() is not None:
                del elem.getparent()[0]

        self.db.flush()
        return claim_count

    # ------------------------------------------------------------------
    # RemittanceAdvice
    # ------------------------------------------------------------------
    @staticmethod
    def _extract_ra_transaction_date(clean_xml: str) -> Optional[date]:
        """Extract TransactionDate from the RemittanceAdvice root element.

        The root element carries the date on which the TPA sent the RA:
          <RemittanceAdvice TransactionDate="02/06/2026" ...>
        This is the authoritative remittance date for all claims in the file.
        """
        fmts = (
            "%d/%m/%Y %H:%M:%S",
            "%d/%m/%Y %I:%M:%S %p",
            "%d/%m/%Y %H:%M",
            "%d/%m/%Y",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d",
        )

        def _parse(raw: str) -> Optional[date]:
            for fmt in fmts:
                try:
                    return datetime.strptime(raw.strip(), fmt).date()
                except ValueError:
                    continue
            return None

        try:
            ctx = etree.iterparse(BytesIO(clean_xml.encode("utf-8")), events=("start", "end"))
            root_seen = False
            for ev, elem in ctx:
                if ev == "start" and not root_seen:
                    # Root attribute form: <RemittanceAdvice TransactionDate="...">
                    root_seen = True
                    raw = (elem.get("TransactionDate")
                           or elem.get("transactionDate")
                           or elem.get("Transaction_Date")
                           or "")
                    if raw and _parse(raw):
                        return _parse(raw)
                elif ev == "end" and elem.tag == "TransactionDate":
                    # eClaimLink standard: <Header><TransactionDate>15/10/2026 13:01</TransactionDate>
                    if elem.text and _parse(elem.text):
                        return _parse(elem.text)
                elif ev == "start" and elem.tag == "Claim":
                    break  # header is over
        except Exception:
            pass
        return None

    def _process_remittance_advice(self, record: RawXmlStore) -> int:
        """Parses RemittanceAdvice payment elements and links them to baseline claims.

        Handles two RA formats:
          • DHA standard: payment/denial fields at <Claim> level
            (<Gross>, <Net>, <PaymentAmount>, <DenialCode>, <DenialAmount> …)
          • NAS / TPA004: all financial detail is nested inside <Activity> children;
            the <Claim> element only carries <ID>, <IDPayer>, <DateSettlement>,
            <Comments>.  This method aggregates activity-level data up to the
            claim level and stores per-activity breakdowns in
            RemittanceActivityDetail.
        """
        clean_xml = _strip_namespaces(record.xml_content or "")

        # Prefer TransactionDate from root element; fall back to staged submission_date
        ra_tx_date = self._extract_ra_transaction_date(clean_xml)
        if ra_tx_date:
            log.info("raw_xml_id=%s RA TransactionDate from XML: %s", record.id, ra_tx_date)
        else:
            log.warning(
                "raw_xml_id=%s could not parse RA TransactionDate from XML root; "
                "falling back to record.submission_date=%s",
                record.id, record.submission_date,
            )

        context = etree.iterparse(BytesIO(clean_xml.encode("utf-8")), events=("end",), tag="Claim")
        remit_count = 0

        for _event, elem in context:
            claim_id = elem.findtext("ID")
            if not claim_id:
                elem.clear()
                continue

            # ------------------------------------------------------------------
            # 1. Try claim-level financial fields (DHA standard format)
            # ------------------------------------------------------------------
            gross         = self._parse_decimal(elem.findtext("Gross"), allow_none=True)
            net           = self._parse_decimal(elem.findtext("Net"), allow_none=True)
            paid          = self._parse_decimal(
                                elem.findtext("PaymentAmount") or elem.findtext("Payment/Amount"),
                                default="0")
            patient_share = self._parse_decimal(elem.findtext("PatientShare"), default="0")
            discount      = self._parse_decimal(elem.findtext("Discount"), default="0")
            denial_amount = self._parse_decimal(
                                elem.findtext("DenialAmount") or elem.findtext("Denial/Amount"),
                                default="0")
            denial_code   = elem.findtext("DenialCode") or elem.findtext("Denial/Code")
            denial_desc   = elem.findtext("DenialDescription") or elem.findtext("Denial/Description")
            status_ra     = elem.findtext("Status") or elem.findtext("ResubmissionType")
            comments      = (elem.findtext("Comments") or "").strip()

            # DateSettlement (NAS format uses this instead of TransactionDate)
            date_settlement = self._parse_optional_dt(
                elem.findtext("DateSettlement") or ""
            )
            if date_settlement:
                date_settlement = date_settlement.date()

            # ------------------------------------------------------------------
            # 2. Aggregate from <Activity> children if claim-level data is absent
            #    (NAS / TPA004 format — denial and payment live inside activities)
            # ------------------------------------------------------------------
            activities = elem.findall("Activity")
            act_detail_rows: list[dict] = []

            if activities:
                agg_paid    = Decimal("0")
                agg_net     = Decimal("0")
                agg_gross   = Decimal("0")
                agg_ps      = Decimal("0")
                act_dcodes: list[str] = []

                for act in activities:
                    a_paid  = self._parse_decimal(act.findtext("PaymentAmount"), default="0") or Decimal("0")
                    a_net   = self._parse_decimal(act.findtext("Net"),  allow_none=True) or Decimal("0")
                    a_gross = self._parse_decimal(act.findtext("List"), allow_none=True) or Decimal("0")
                    a_ps    = self._parse_decimal(act.findtext("PatientShare"), default="0") or Decimal("0")
                    a_dc    = act.findtext("DenialCode")

                    agg_paid  += a_paid
                    agg_net   += a_net
                    agg_gross += a_gross
                    agg_ps    += a_ps
                    if a_dc and a_dc not in act_dcodes:
                        act_dcodes.append(a_dc)

                    act_detail_rows.append({
                        "activity_id":        act.findtext("ID"),
                        "activity_code":      act.findtext("Code"),
                        "activity_type":      _normalize_activity_type(act.findtext("Type")),
                        "gross":              a_gross,
                        "net":                a_net,
                        "paid_amount":        a_paid,
                        "patient_share":      a_ps,
                        "denial_code":        a_dc,
                        "denial_description": (act.findtext("DenialDescription")
                                               or act.findtext("Denial/Description")
                                               or act.findtext("Comments")
                                               or ""),
                        "denial_type":        (act.findtext("DenialType")
                                               or act.findtext("Denial/Type")
                                               or ""),
                    })

                # Fill claim-level fields from aggregated activity data when missing
                if gross is None:
                    gross = agg_gross if agg_gross else None
                if net is None:
                    net = agg_net if agg_net else None
                if paid == Decimal("0") and agg_paid:
                    paid = agg_paid
                if patient_share == Decimal("0"):
                    patient_share = agg_ps
                if denial_code is None and act_dcodes:
                    denial_code = " | ".join(act_dcodes)
                if not denial_desc and comments:
                    denial_desc = comments
                # Denial amount = total net submitted minus what was paid
                if denial_amount == Decimal("0") and net and paid is not None:
                    denial_amount = net - paid

            # Enforce column length limits
            if denial_code and len(denial_code) > 50:
                denial_code = denial_code[:50]
            if denial_desc and len(denial_desc) > 500:
                denial_desc = denial_desc[:500]

            # ------------------------------------------------------------------
            # 3. Resolve remittance date — prefer DateSettlement (claim-level),
            #    then RA header TransactionDate, then staged submission_date
            # ------------------------------------------------------------------
            remittance_date = (
                date_settlement
                or ra_tx_date
                or record.submission_date
                or datetime.utcnow().date()
            )

            # ------------------------------------------------------------------
            # 4. Upsert inside no_autoflush to prevent premature flushes.
            #    Delete by raw_xml_id+claim_id (catches re-parses of same file)
            #    AND by facility+claim+date (clears old records that would
            #    violate the uq_remittance_claim_date unique constraint).
            # ------------------------------------------------------------------
            with self.db.no_autoflush:
                existing = self.db.scalar(
                    select(Remittance).where(
                        Remittance.facility_id == record.facility_id,
                        Remittance.raw_xml_id  == record.id,
                        Remittance.claim_id    == claim_id,
                    )
                )
                if existing:
                    self.db.delete(existing)
                    self.db.flush()

                # Also clear any old record that shares the unique constraint key
                # (different raw_xml_id but same facility/claim/date)
                existing2 = self.db.scalar(
                    select(Remittance).where(
                        Remittance.facility_id    == record.facility_id,
                        Remittance.claim_id       == claim_id,
                        Remittance.remittance_date == remittance_date,
                    )
                )
                if existing2:
                    self.db.delete(existing2)
                    self.db.flush()

            db_remit = Remittance(
                facility_id=record.facility_id,
                raw_xml_id=record.id,
                source_channel="FILE",  # DHA eClaimLink — distinguishes from Riayati API
                claim_id=claim_id,
                payer_id=elem.findtext("PayerID") or elem.findtext("SenderID") or "UNKNOWN",
                remittance_date=remittance_date,
                ra_file_id=elem.findtext("IDPayer"),
                claim_id_payer=elem.findtext("IDPayer"),
                gross_amount=gross,
                net_amount=net,
                paid_amount=paid or Decimal("0"),
                patient_share_total=patient_share or Decimal("0"),
                discount_amount=discount or Decimal("0"),
                denial_amount=denial_amount or Decimal("0"),
                denial_code=denial_code,
                denial_description=denial_desc or None,
                # TPA free-text claim-level comments (RA.Claim/Comments) stored separately
                # from denial_description so both are available in reports.
                tpa_comments=comments or None,
                claim_status_ra=status_ra,
            )

            # 5. Attach per-activity breakdowns.
            #    For NAS/TPA004 format: act_detail_rows has one entry per <Activity>.
            #    For DHA standard format (payment at claim level, no <Activity> children):
            #    act_detail_rows is empty — synthesise a single claim-level row so the
            #    report query (which starts from RemittanceActivityDetail) can see this claim.
            if not act_detail_rows:
                act_detail_rows = [{
                    "activity_id":        None,
                    "activity_code":      None,
                    "activity_type":      None,
                    "gross":              gross,
                    "net":                net,
                    "paid_amount":        paid or Decimal("0"),
                    "patient_share":      patient_share or Decimal("0"),
                    "denial_code":        denial_code,
                    "denial_description": denial_desc or None,
                    "denial_type":        None,
                }]

            for ad in act_detail_rows:
                db_remit.activity_details.append(
                    RemittanceActivityDetail(
                        facility_id=record.facility_id,
                        activity_id=ad["activity_id"],
                        activity_code=ad["activity_code"],
                        activity_type=ad["activity_type"],
                        gross=ad["gross"],
                        net=ad["net"],
                        paid_amount=ad["paid_amount"],
                        patient_share=ad["patient_share"],
                        denial_code=ad["denial_code"],
                        denial_description=ad.get("denial_description") or None,
                        denial_type=ad.get("denial_type") or None,
                    )
                )

            self.db.add(db_remit)
            remit_count += 1

            # ── Fetch-time net sync ────────────────────────────────────────
            # Correct claims.net_amount from the RA net when the RA net is
            # larger than the stored value.  This handles PRCE-001 cases where
            # the ClaimSubmission XML stores only the tariff-adjustment delta
            # (e.g. 0.50) instead of the full submitted amount (e.g. 40.28).
            # Using "only update if RA net > claim net" ensures that a later
            # small-delta adjustment RA never overwrites the already-correct
            # larger value that a previous RA established.
            if net and net > 0:
                db_claim = self.db.scalar(
                    select(Claim).where(
                        Claim.facility_id      == record.facility_id,
                        Claim.claim_id         == claim_id,
                        Claim.resubmission_key == "",   # initial submissions only
                    )
                )
                if db_claim and (db_claim.net_amount is None or net > db_claim.net_amount):
                    db_claim.net_amount = net

            elem.clear()
            while elem.getprevious() is not None:
                del elem.getparent()[0]

        self.db.flush()
        return remit_count

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_dt(dt_str: Optional[str]) -> datetime:
        if not dt_str:
            return datetime.utcnow()
        for fmt in ("%d/%m/%Y %H:%M", "%d/%m/%Y"):
            try:
                return datetime.strptime(dt_str, fmt)
            except ValueError:
                continue
        try:
            return datetime.fromisoformat(dt_str)
        except ValueError:
            return datetime.utcnow()

    @classmethod
    def _parse_optional_dt(cls, dt_str: Optional[str]) -> Optional[datetime]:
        if not dt_str:
            return None
        return cls._parse_dt(dt_str)

    @staticmethod
    def _parse_decimal(value: Optional[str], default: str = "0.00", allow_none: bool = False) -> Optional[Decimal]:
        if value is None or value == "":
            return None if allow_none else Decimal(default)
        try:
            return Decimal(value)
        except InvalidOperation:
            return None if allow_none else Decimal(default)
