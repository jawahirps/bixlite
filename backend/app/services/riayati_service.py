"""
Riayati (MOH Post Office) TMB API integration.

TMB API Spec v2.3  —  Base URL: https://tmbapi.riayati.ae:8083/api/
Note: 'o-tmbapi' in the spec is the operator/payer URL; providers use 'tmbapi' (no prefix).

Auth (HTTP request headers):
  username  →  facility MOH license number  (e.g. "8580" for Nafeesa)
  password  →  API key UUID from po.riayati.ae portal

Data format: JSON (not XML like DHA eClaimLink).

Provider-facing flow:
  1. GET /Claim/Search   — list transaction descriptors in date window
  2. GET /Claim/View     — download full JSON body of each transaction
  3. POST /Claim/SetDownloaded — mark as downloaded (clears from GetNew)

direction parameter:
  0 = received  →  RemittanceAdvice  (payer → provider responses)
  1 = sent      →  ClaimSubmission   (provider → payer claims)

downloaded parameter:
  0 = not yet downloaded, 1 = already downloaded, 2 = all (use this)

NAFEESA MEDICAL CENTER (facility license 8580):
  Status: "Inactive facility-license" — Riayati team must reactivate after
  API key regeneration. Email: RiayatiPO@mohap.gov.ae
  Once reactivated, all fetches will succeed.

This module contains:
  RiayatiInactiveFacilityError  — raised when facility is not yet reactivated
  RiayatiClient                 — thin HTTP wrapper
  RiayatiJsonParser             — JSON → Claim/Remittance DB rows
  RiayatiSyncService            — orchestrates fetch + stage + parse + FetchLog

Activity type mapping (Riayati int → our VARCHAR enum):
  3=CPT  4=HCPCS  5=Drug  6=Dental  8=Service(Code)  9=Service(DRG)
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

import calendar

import requests
import urllib3
from sqlalchemy.orm import Session

from ..models import (
    Claim, ClaimActivity, ClaimDiagnosis, ClaimObservation,
    Facility, FetchLog, RawXmlStore,
    Remittance, RemittanceActivityDetail,
)

# Suppress InsecureRequestWarning for self-signed cert on tmbapi.riayati.ae
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

log = logging.getLogger(__name__)

RIAYATI_BASE_URL = "https://tmbapi.riayati.ae:8083/api"

DIRECTION_RECEIVED = 0   # payer → provider  (remittance advice)
DIRECTION_SENT     = 1   # provider → payer  (claim submission)

# String the server returns when facility is recognised but not yet reactivated
_INACTIVE_MARKER = "Inactive facility-license"

# Riayati activity type int → our VARCHAR enum
_ACT_TYPE_MAP: Dict[int, str] = {
    3:  "CPT",
    4:  "HCPCS",
    5:  "Drug",
    6:  "Dental",
    8:  "Service",
    9:  "Service",   # DRG
    10: "Service",
}

# Riayati date formats (server returns dd/MM/yyyy HH:mm or dd/MM/yyyy)
_DT_FORMATS = ["%d/%m/%Y %H:%M", "%d/%m/%Y"]


# ---------------------------------------------------------------------------
# Custom exception
# ---------------------------------------------------------------------------
class RiayatiInactiveFacilityError(Exception):
    """
    Raised when the Riayati server returns 'Inactive facility-license'.

    This means credentials are correct but the facility has not been
    reactivated by the Riayati team after an API key regeneration.
    Action: email RiayatiPO@mohap.gov.ae to request reactivation.
    """


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _parse_dt(val: Optional[str]) -> Optional[datetime]:
    if not val:
        return None
    for fmt in _DT_FORMATS:
        try:
            return datetime.strptime(val.strip(), fmt)
        except ValueError:
            continue
    return None


def _parse_date(val: Optional[str]) -> Optional[date]:
    dt = _parse_dt(val)
    return dt.date() if dt else None


def _dec(val, default: str = "0") -> Decimal:
    try:
        return Decimal(str(val)) if val is not None else Decimal(default)
    except InvalidOperation:
        return Decimal(default)


def _dec_or_none(val) -> Optional[Decimal]:
    try:
        return Decimal(str(val)) if val is not None else None
    except InvalidOperation:
        return None


def _norm_activity_type(raw) -> str:
    try:
        return _ACT_TYPE_MAP.get(int(raw), "Service")
    except (TypeError, ValueError):
        return "Service"


def _norm_diagnosis_type(raw: Optional[str]) -> str:
    if not raw:
        return "Secondary"
    r = str(raw).strip().lower()
    if r == "principal":
        return "Primary"
    if r == "admitting":
        return "Admitting"
    return "Secondary"


def _next_month_start(d: date) -> date:
    """Return the first day of the month after d's month."""
    if d.month == 12:
        return date(d.year + 1, 1, 1)
    return date(d.year, d.month + 1, 1)


def _month_end(d: date) -> date:
    """Return the last day of d's month."""
    return date(d.year, d.month, calendar.monthrange(d.year, d.month)[1])


def _subtract_months(d: date, months: int) -> date:
    """Subtract `months` from d, returning the 1st of that month."""
    total = d.year * 12 + (d.month - 1) - months
    return date(total // 12, total % 12 + 1, 1)


def _month_windows(date_from: date, date_to: date) -> List[tuple]:
    """
    Split [date_from, date_to] into non-overlapping calendar-month windows.
    Riayati returns <=500 records per call; monthly windows keep results manageable.
    """
    windows = []
    cursor = date_from.replace(day=1)
    while cursor <= date_to:
        win_start = max(cursor, date_from)
        win_end   = min(_month_end(cursor), date_to)
        windows.append((win_start, win_end))
        cursor = _next_month_start(cursor)
    return windows


# ---------------------------------------------------------------------------
# REST client
# ---------------------------------------------------------------------------
class RiayatiClient:
    """
    Thin wrapper around the Riayati TMB REST API.

    Authentication: 'username' and 'password' as plain HTTP request headers.
      username = facility MOH license number (e.g. "8580")
      password = API key UUID from po.riayati.ae portal

    SSL: tmbapi.riayati.ae uses a self-signed/internal cert; verify=False.
    """

    def __init__(self, username: str, password: str, timeout: int = 90):
        self._s = requests.Session()
        self._s.verify = False   # self-signed cert on tmbapi.riayati.ae
        self._s.headers.update({
            "username":      username,
            "password":      password,
            "Content-Type":  "application/json",
            "Accept":        "application/json",
        })
        self._timeout = timeout

    def _check_inactive(self, resp: requests.Response) -> None:
        """Raise RiayatiInactiveFacilityError if server returns the inactive marker."""
        if _INACTIVE_MARKER in resp.text:
            raise RiayatiInactiveFacilityError(
                f"Riayati facility is inactive (credentials recognised but facility "
                f"not yet reactivated). Email RiayatiPO@mohap.gov.ae to request "
                f"reactivation. Server response: {resp.text[:300]}"
            )

    def search(
        self,
        from_date: date,
        to_date: date,
        direction: int = DIRECTION_RECEIVED,
        downloaded: int = 2,
        from_time: str = "00:00",
        to_time: str = "23:59",
    ) -> List[Dict]:
        """
        Return up to 500 transaction descriptors matching the date window.
        Use _search_with_splitting() to get everything past the 500 cap.

        direction: 0=received (remittance), 1=sent (claim submission)
        downloaded: 0=new only, 1=downloaded only, 2=all
        from_time / to_time: HH:MM on from_date / to_date (intraday splitting)
        """
        params = {
            "fromDate":   from_date.strftime("%d/%m/%Y ") + from_time,
            "toDate":     to_date.strftime("%d/%m/%Y ") + to_time,
            "direction":  direction,
            "downloaded": downloaded,
        }
        resp = self._s.get(
            f"{RIAYATI_BASE_URL}/Claim/Search",
            params=params,
            timeout=self._timeout,
        )
        self._check_inactive(resp)
        resp.raise_for_status()
        data = resp.json()
        if data.get("Success") is False:
            raise RuntimeError(f"Riayati Search failed: {data.get('Message') or data}")
        return data.get("Entities") or []

    def view(self, transaction_id: str, direction: int = DIRECTION_RECEIVED) -> Dict:
        """Download the full JSON content of one transaction."""
        resp = self._s.get(
            f"{RIAYATI_BASE_URL}/Claim/View",
            params={"id": transaction_id, "direction": direction},
            timeout=self._timeout,
        )
        self._check_inactive(resp)
        resp.raise_for_status()
        return resp.json()

    def set_downloaded(self, transaction_id: str) -> None:
        """Mark a transaction as downloaded (removes it from GetNew results)."""
        resp = self._s.post(
            f"{RIAYATI_BASE_URL}/Claim/SetDownloaded",
            json={"id": transaction_id},
            timeout=self._timeout,
        )
        resp.raise_for_status()


RIAYATI_SEARCH_CAP = 500   # Riayati Claim/Search returns at most this many rows


def _search_with_splitting(
    client: RiayatiClient,
    start: datetime,
    end: datetime,
    direction: int,
    seen_ids: set,
) -> List[Dict]:
    """
    Search [start, end] (minute precision). While Riayati returns the 500-row
    cap, bisect the window by time — whole days first, then hours/minutes —
    so busy months (e.g. Sabah Al Noor RHA, NAS Ajman SHA) are not silently
    truncated at 500. Entities are de-duplicated on ID via *seen_ids*.
    """
    entities = client.search(
        start.date(), end.date(), direction=direction, downloaded=2,
        from_time=start.strftime("%H:%M"), to_time=end.strftime("%H:%M"),
    )

    span_minutes = int((end - start).total_seconds() // 60)
    if len(entities) >= RIAYATI_SEARCH_CAP and span_minutes >= 1:
        mid = start + timedelta(minutes=span_minutes // 2)
        log.info(
            "Riayati Search hit cap (%d) for %s -> %s dir=%d — splitting at %s",
            len(entities), start, end, direction, mid,
        )
        return (
            _search_with_splitting(client, start, mid, direction, seen_ids)
            + _search_with_splitting(client, mid + timedelta(minutes=1), end,
                                     direction, seen_ids)
        )

    if len(entities) >= RIAYATI_SEARCH_CAP:
        log.warning(
            "Riayati one-minute window %s still hits cap (%d, dir=%d) — "
            "some transactions may be missing.", start, len(entities), direction,
        )

    new = []
    for ent in entities:
        tx_id = ent.get("ID")
        if tx_id and tx_id not in seen_ids:
            seen_ids.add(tx_id)
            new.append(ent)
    return new


# ---------------------------------------------------------------------------
# JSON parser
# ---------------------------------------------------------------------------
class RiayatiJsonParser:
    """Parses staged Riayati JSON payloads into normalised Claim/Remittance rows."""

    def __init__(self, db: Session):
        self.db = db
        self.quarantined = 0   # files that failed to parse in the last run_pending()

    def run_pending(self, facility_id: Optional[int] = None) -> int:
        """Parse all pending Riayati raw records. Returns count parsed
        (failures are counted in self.quarantined)."""
        self.quarantined = 0
        from sqlalchemy import select
        stmt = (
            select(RawXmlStore)
            .where(RawXmlStore.parse_status == "pending")
            .where(RawXmlStore.xml_type.in_(
                ["RiayatiClaimSubmission", "RiayatiRemittanceAdvice"]
            ))
        )
        if facility_id is not None:
            stmt = stmt.where(RawXmlStore.facility_id == facility_id)
        stmt = stmt.order_by(RawXmlStore.id.asc())

        records = self.db.scalars(stmt).all()
        parsed  = 0

        for rec in records:
            try:
                rec.parsed_at = datetime.utcnow()
                if rec.xml_type == "RiayatiClaimSubmission":
                    n = self._parse_claim_submission(rec)
                else:
                    n = self._parse_remittance_advice(rec)
                rec.parse_status   = "parsed"
                rec.records_parsed = n
                self.db.commit()
                parsed += 1
                log.info("Parsed Riayati raw_id=%s (%s) -> %d records", rec.id, rec.xml_type, n)
            except Exception as exc:
                self.db.rollback()
                log.error("Failed parsing Riayati raw_id=%s: %s", rec.id, exc, exc_info=True)
                rec.parse_status = "quarantined"
                rec.parse_error  = str(exc)
                self.db.commit()
                self.quarantined += 1

        return parsed

    # -- ClaimSubmission -------------------------------------------------------
    def _parse_claim_submission(self, record: RawXmlStore) -> int:
        body = json.loads(record.xml_content)
        # Riayati View API wraps payload under "Entity" key
        body       = body.get("Entity", body)
        submission = body.get("Submission") or body
        header     = submission.get("Header", {})

        payer_id    = str(header.get("PayerID", "") or "").strip() or "UNKNOWN"
        receiver_id = str(header.get("ReceiverID", "") or "").strip() or None
        tx_date_str = header.get("TransactionDate", "")
        tx_date     = _parse_date(tx_date_str)
        sub_date    = tx_date or record.submission_date or date.today()

        count = 0
        for claim_data in submission.get("Claim", []):
            enc  = claim_data.get("Encounter", {})
            diag = claim_data.get("Diagnosis", []) or []
            acts = claim_data.get("Activity",  []) or []

            claim_id = str(claim_data.get("ID", "")).strip()
            if not claim_id:
                continue

            enc_start_str = enc.get("Start", "")
            enc_start     = _parse_dt(enc_start_str)
            if not enc_start:
                enc_start = datetime.combine(sub_date, datetime.min.time())

            enc_end = _parse_dt(enc.get("End", ""))

            # Riayati has no separate encounter ID -- derive one
            encounter_id = f"R-{claim_id}"

            # Clinician from first activity that has one
            clinician_id = ""
            for a in acts:
                clin = str(a.get("Clinician", "") or "").strip()
                if clin:
                    clinician_id = clin
                    break

            # Resubmission block (same semantics as DHA <Resubmission>):
            # key '' = initial, else the Type text, e.g. 'internal complaint',
            # 'correction', 'reconciliation' — one row per (claim, key).
            resub = claim_data.get("Resubmission")
            resub = resub if isinstance(resub, dict) else {}
            resub_type    = str(resub.get("Type") or "").strip() or None
            resub_key     = resub_type or ""
            resub_comment = str(resub.get("Comment") or "").strip() or None

            existing = (
                self.db.query(Claim)
                .filter_by(facility_id=record.facility_id, claim_id=claim_id,
                           resubmission_key=resub_key)
                .first()
            )
            if existing:
                continue

            claim_row = Claim(
                facility_id         = record.facility_id,
                raw_xml_id          = record.id,
                source_channel      = "API",
                claim_id            = claim_id,
                claim_id_payer      = str(claim_data.get("IDPayer", "") or "").strip() or None,
                encounter_id        = encounter_id,
                encounter_type      = str(enc.get("Type", "") or "").strip() or None,
                encounter_start     = enc_start,
                encounter_end       = enc_end,
                start_type          = str(enc.get("StartType", "") or "").strip() or None,
                end_type            = str(enc.get("EndType",   "") or "").strip() or None,
                transfer_source     = str(enc.get("TransferSource", "") or "").strip() or None,
                member_id           = str(claim_data.get("MemberID", "") or "").strip() or "UNKNOWN",
                patient_id          = str(enc.get("PatientID", "") or "").strip() or None,
                emirates_id         = str(claim_data.get("NationalIDNumber", "") or "").strip() or None,
                payer_id            = payer_id,
                receiver_id         = receiver_id,
                clinician_id        = clinician_id or "UNKNOWN",
                gross_amount        = _dec(claim_data.get("Gross")),
                net_amount          = _dec(claim_data.get("Net")),
                patient_share_total = _dec(claim_data.get("PatientShare")),
                submission_date     = sub_date,
                claim_status        = "submitted",
                resubmission_count  = 1 if resub_key else 0,
                resubmission_type   = resub_type,
                resubmission_key    = resub_key,
                resubmission_comment= resub_comment,
            )
            self.db.add(claim_row)
            self.db.flush()

            for seq, d in enumerate(diag, 1):
                self.db.add(ClaimDiagnosis(
                    claim_id_fk    = claim_row.id,
                    facility_id    = record.facility_id,
                    diagnosis_type = _norm_diagnosis_type(d.get("Type")),
                    diagnosis_code = str(d.get("Code", "")).strip() or "UNKNOWN",
                    sequence_no    = seq,
                ))

            act_rows = []
            for seq, a in enumerate(acts, 1):
                act_start_dt = _parse_dt(a.get("Start", ""))
                act_start_d  = act_start_dt.date() if act_start_dt else sub_date

                act_row = ClaimActivity(
                    claim_id_fk      = claim_row.id,
                    facility_id      = record.facility_id,
                    activity_id      = str(a.get("ID", "") or "").strip() or None,
                    activity_type    = _norm_activity_type(a.get("Type")),
                    activity_code    = str(a.get("Code", "") or "").strip() or "UNKNOWN",
                    quantity         = _dec(a.get("Quantity"), "1"),
                    gross            = _dec(a.get("Gross")),
                    net              = _dec(a.get("Net")),
                    patient_share    = _dec(a.get("PatientShare")),
                    activity_start   = act_start_d,
                    authorization_id = str(a.get("PriorAuthorizationID", "") or "").strip() or None,
                    clinician_id     = str(a.get("Clinician", "") or "").strip() or None,
                    sequence_no      = seq,
                )
                self.db.add(act_row)
                self.db.flush()   # gives act_row.id before observation loop
                act_rows.append(act_row)

                for obs in (a.get("Observation") or []):
                    self.db.add(ClaimObservation(
                        activity_id_fk    = act_row.id,
                        claim_id_fk       = claim_row.id,
                        observation_type  = str(obs.get("Type",      "") or "").strip() or "Unknown",
                        observation_code  = str(obs.get("Code",      "") or "").strip() or None,
                        observation_value = str(obs.get("Value",     "") or "").strip() or None,
                        value_type        = str(obs.get("ValueType", "") or "").strip() or None,
                    ))

            # Fallback: derive claim-level net/gross from activity lines when
            # the claim-level "Net"/"Gross" JSON fields are missing or zero.
            # Only applied to initial submissions (resubmission_key == "").
            # Resubmission zero-net is legitimate — do not overwrite.
            if not claim_row.resubmission_key:
                if not claim_row.net_amount or claim_row.net_amount == Decimal("0"):
                    act_net_sum = sum((a.net or Decimal("0")) for a in act_rows)
                    if act_net_sum:
                        claim_row.net_amount = act_net_sum
                        log.debug("Riayati claim_id=%s: net_amount derived from activity sum: %s",
                                  claim_id, act_net_sum)

                if not claim_row.gross_amount or claim_row.gross_amount == Decimal("0"):
                    act_gross_sum = sum((a.gross or Decimal("0")) for a in act_rows)
                    if act_gross_sum:
                        claim_row.gross_amount = act_gross_sum

            count += 1

        return count

    # -- RemittanceAdvice ------------------------------------------------------
    def _parse_remittance_advice(self, record: RawXmlStore) -> int:
        body = json.loads(record.xml_content)
        # Riayati View API wraps payload under "Entity" key
        body       = body.get("Entity", body)
        remittance = body.get("Remittance") or body
        header     = remittance.get("Header", {})

        payer_id    = str(header.get("PayerID", "") or "").strip() or "UNKNOWN"
        tx_date_str = header.get("TransactionDate", "")
        tx_date     = _parse_date(tx_date_str)
        ra_file_id  = str(header.get("SenderID", "") or "").strip() or None

        count = 0
        for claim_data in remittance.get("Claim", []):
            claim_id = str(claim_data.get("ID", "")).strip()
            if not claim_id:
                continue

            settle_str  = claim_data.get("DateSettlement", "")
            # tx_date = header-level TransactionDate (the RA file's own date) — use as
            # primary so "filter by remittance transaction date 2026" works correctly.
            # DateSettlement is per-claim and can be from an earlier period.
            settle_date = tx_date or _parse_date(settle_str) or record.submission_date or date.today()
            acts        = claim_data.get("Activity", []) or []

            agg_gross = agg_net = agg_paid = agg_ps = Decimal("0")
            denial_codes = []
            act_rows     = []

            for a in acts:
                a_gross = _dec(a.get("Gross"))
                a_net   = _dec(a.get("Net"))
                a_paid  = _dec(a.get("PaymentAmount"))
                a_ps    = _dec(a.get("PatientShare"))
                a_dc    = str(a.get("DenialCode", "") or "").strip() or None
                agg_gross += a_gross
                agg_net   += a_net
                agg_paid  += a_paid
                agg_ps    += a_ps
                if a_dc and a_dc not in denial_codes:
                    denial_codes.append(a_dc)
                act_rows.append({
                    "activity_id":        str(a.get("ID", "") or "").strip() or None,
                    "activity_code":      str(a.get("Code", "") or "").strip() or None,
                    "activity_type":      _norm_activity_type(a.get("Type")),
                    "gross":              a_gross,
                    "net":                a_net,
                    "paid_amount":        a_paid,
                    "patient_share":      a_ps,
                    "denial_code":        a_dc,
                    "denial_description": str(a.get("DenialDescription", "") or "").strip() or None,
                    "denial_type":        str(a.get("DenialType",        "") or "").strip() or None,
                    "quantity_approved":  _dec_or_none(a.get("Quantity")),
                })

            denial_code = claim_data.get("DenialCode") or (denial_codes[0] if denial_codes else None)

            existing = (
                self.db.query(Remittance)
                .filter_by(facility_id=record.facility_id, claim_id=claim_id, remittance_date=settle_date)
                .first()
            )
            if existing:
                continue

            ra_row = Remittance(
                facility_id         = record.facility_id,
                raw_xml_id          = record.id,
                source_channel      = "API",
                claim_id            = claim_id,
                claim_id_payer      = str(claim_data.get("IDPayer", "") or "").strip() or None,
                payer_id            = payer_id,
                remittance_date     = settle_date,
                ra_file_id          = ra_file_id,
                gross_amount        = agg_gross or None,
                net_amount          = agg_net   or None,
                paid_amount         = agg_paid,
                patient_share_total = agg_ps,
                discount_amount     = Decimal("0"),
                denial_amount       = Decimal("0"),
                denial_code         = str(denial_code or "").strip() or None,
                claim_status_ra     = "paid" if agg_paid > 0 else "denied",
            )
            self.db.add(ra_row)
            self.db.flush()

            for ad in act_rows:
                self.db.add(RemittanceActivityDetail(
                    remittance_id_fk = ra_row.id,
                    facility_id      = record.facility_id,
                    **ad,
                ))

            count += 1

        return count


# ---------------------------------------------------------------------------
# Sync service
# ---------------------------------------------------------------------------
class RiayatiSyncService:
    """
    Orchestrates a Riayati fetch cycle for one facility + date window.

    Main entry points:
      run_sync_pipeline(facility_id, date_from, date_to)  -> FetchLog
      run_bulk_fetch(facility_id, months=24)              -> list[FetchLog]
      run_nightly_fetch(facility_id)                      -> FetchLog
    """

    def __init__(self, session: Session):
        self.db = session

    # -- Core pipeline ---------------------------------------------------------
    def run_sync_pipeline(
        self,
        facility_id: int,
        date_from: date,
        date_to: date,
    ) -> FetchLog:
        """
        Fetch + stage + parse all Riayati transactions for one date window.

        Returns a FetchLog with status:
          "success"  -- all records fetched and parsed
          "partial"  -- some records failed (see error_detail)
          "inactive" -- facility not yet reactivated by Riayati team
        """
        facility = self.db.get(Facility, facility_id)
        if not facility:
            raise ValueError(f"Facility {facility_id} not found")
        if not facility.riayati_username or not facility.riayati_password:
            raise ValueError(
                f"Facility {facility_id} ({facility.facility_name}) "
                "has no Riayati credentials configured."
            )

        fetch_log = FetchLog(
            facility_id     = facility_id,
            fetch_type      = "Riayati",
            date_from       = date_from,
            date_to         = date_to,
            status          = "running",
            records_fetched = 0,
            records_parsed  = 0,
            records_failed  = 0,
        )
        self.db.add(fetch_log)
        self.db.commit()

        client = RiayatiClient(facility.riayati_username, facility.riayati_password)
        parser = RiayatiJsonParser(self.db)
        errors = []

        # Fetch both directions: remittance (received) + claims (sent)
        for direction, xml_type in [
            (DIRECTION_RECEIVED, "RiayatiRemittanceAdvice"),
            (DIRECTION_SENT,     "RiayatiClaimSubmission"),
        ]:
            try:
                entities = _search_with_splitting(
                    client,
                    datetime.combine(date_from, datetime.min.time()),
                    datetime.combine(date_to, datetime.min.time()).replace(hour=23, minute=59),
                    direction,
                    seen_ids=set(),
                )
                log.info(
                    "Riayati facility=%d %s -> %d entities (%s -> %s)",
                    facility_id, xml_type, len(entities), date_from, date_to,
                )

                for ent in entities:
                    tx_id = ent.get("ID")
                    if not tx_id:
                        continue

                    # Skip if already staged
                    already = (
                        self.db.query(RawXmlStore)
                        .filter_by(facility_id=facility_id, source_filename=str(tx_id))
                        .first()
                    )
                    if already:
                        log.debug("Riayati tx %s already staged -- skip", tx_id)
                        continue

                    try:
                        view_resp = client.view(str(tx_id), direction=direction)
                        content   = json.dumps(view_resp)

                        tx_date_str = (
                            ent.get("TransactionDate")
                            or ent.get("CreationDate")
                            or ""
                        )
                        sub_date = _parse_date(tx_date_str)

                        raw = RawXmlStore(
                            facility_id     = facility_id,
                            xml_type        = xml_type,
                            source_filename = str(tx_id),
                            submission_date = sub_date,
                            xml_content     = content,
                            file_size_bytes = len(content.encode()),
                            parse_status    = "pending",
                        )
                        self.db.add(raw)
                        self.db.commit()
                        fetch_log.records_fetched += 1
                        self.db.commit()

                    except RiayatiInactiveFacilityError:
                        raise   # bubble up -- caught below
                    except Exception as exc:
                        log.error("Riayati View tx %s failed: %s", tx_id, exc)
                        errors.append(f"View {tx_id}: {exc}")
                        fetch_log.records_failed += 1
                        self.db.commit()

            except RiayatiInactiveFacilityError as exc:
                log.warning("Riayati facility=%d INACTIVE: %s", facility_id, exc)
                fetch_log.completed_at = datetime.utcnow()
                fetch_log.status       = "inactive"
                fetch_log.error_detail = {
                    "error": str(exc),
                    "action": (
                        "Email RiayatiPO@mohap.gov.ae to reactivate facility "
                        f"license {facility.license_id} after API key regeneration."
                    ),
                }
                self.db.commit()
                return fetch_log

            except Exception as exc:
                log.error("Riayati Search direction=%d failed: %s", direction, exc)
                errors.append(f"Search direction={direction}: {exc}")

        # Parse all pending Riayati records for this facility
        parsed = parser.run_pending(facility_id=facility_id)
        fetch_log.records_parsed = parsed
        if parser.quarantined:
            # Parse failures must not hide behind a 'success' status
            fetch_log.records_failed += parser.quarantined
            errors.append(f"{parser.quarantined} file(s) failed to parse (quarantined)")

        fetch_log.completed_at = datetime.utcnow()
        fetch_log.status       = "success" if not errors else "partial"
        if errors:
            fetch_log.error_detail = {"errors": errors}
        self.db.commit()

        return fetch_log

    # -- Bulk (historical) backfill --------------------------------------------
    def run_bulk_fetch(
        self,
        facility_id: int,
        months: int = 24,
    ) -> List[FetchLog]:
        """
        Fetch the last `months` months of data, one calendar month per window.

        Riayati caps Search at 500 records per call; monthly windows ensure
        we stay well under that limit for a typical facility.

        Returns a list of FetchLog rows, one per month window.
        """
        today     = date.today()
        date_to   = today
        date_from = _subtract_months(today, months - 1)

        windows = _month_windows(date_from, date_to)
        logs    = []

        log.info(
            "Riayati bulk fetch facility=%d -- %d month windows (%s -> %s)",
            facility_id, len(windows), date_from, date_to,
        )

        for win_start, win_end in windows:
            fetch_log = self.run_sync_pipeline(facility_id, win_start, win_end)
            logs.append(fetch_log)

            if fetch_log.status == "inactive":
                log.warning(
                    "Riayati bulk fetch stopped -- facility inactive. "
                    "Remaining windows skipped until reactivation."
                )
                break

            log.info(
                "Riayati bulk window %s -> %s: fetched=%d parsed=%d failed=%d status=%s",
                win_start, win_end,
                fetch_log.records_fetched,
                fetch_log.records_parsed,
                fetch_log.records_failed,
                fetch_log.status,
            )

        return logs

    # -- Nightly incremental fetch ---------------------------------------------
    def run_nightly_fetch(
        self,
        facility_id: int,
        lookback_days: int = 7,
    ) -> FetchLog:
        """
        Fetch the last `lookback_days` days (default 7 for safety overlap).

        Use this for the nightly cron job. A 7-day lookback catches any
        late-arriving remittance responses without re-fetching months of data.
        """
        today     = date.today()
        date_from = today - timedelta(days=lookback_days - 1)

        log.info(
            "Riayati nightly fetch facility=%d -- %s -> %s",
            facility_id, date_from, today,
        )
        return self.run_sync_pipeline(facility_id, date_from, today)
