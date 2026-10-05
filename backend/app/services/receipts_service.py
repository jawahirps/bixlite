"""
Receipts Reconciliation Service
================================
Reads RemittanceAdvice data (DHPO + Riayati) for a given date range,
groups by (facility, date, payer, channel), and writes DailyReceipt rows.

Date convention: filters by raw_xml_store.submission_date (RA posting date /
eClaimLink TransactionDate) — same as existing Remittance Activity reports.

Channel detection:
  raw_xml_store.xml_type = 'RemittanceAdvice'        → channel = 'DHPO'
  raw_xml_store.xml_type = 'RiayatiRemittanceAdvice' → channel = 'Riayati'
"""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from typing import Optional

from sqlalchemy import func, literal, null, select, text
from sqlalchemy.orm import Session

from app.models import (
    DailyReceipt,
    Facility,
    RawXmlStore,
    ReceiptPayerMapping,
    Remittance,
)

log = logging.getLogger(__name__)

_ZERO = Decimal("0.00")

# ── channel tag mapping ──────────────────────────────────────────────────────
_XML_TYPE_TO_CHANNEL = {
    "RemittanceAdvice": "DHPO",
    "RiayatiRemittanceAdvice": "Riayati",
}


class ReceiptsService:
    """
    Reconciliation + daily-receipt generation service.

    Typical usage (called after every nightly fetch):
        svc = ReceiptsService(db)
        rows = svc.reconcile_date(facility_id=1, target_date=date.today())
    """

    def __init__(self, db: Session):
        self.db = db

    # ── public API ──────────────────────────────────────────────────────────

    def reconcile_date(
        self,
        facility_id: int,
        target_date: date,
        upsert: bool = True,
    ) -> list[DailyReceipt]:
        """
        Build DailyReceipt rows for one facility + one date.
        Returns the list of rows written/updated.
        """
        log.info("Reconciling fac=%d date=%s", facility_id, target_date)
        buckets = self._aggregate_ra(facility_id, target_date, target_date)
        return self._upsert_receipts(facility_id, buckets, upsert=upsert)

    def reconcile_range(
        self,
        facility_id: int,
        date_from: date,
        date_to: date,
        upsert: bool = True,
    ) -> list[DailyReceipt]:
        """
        Build DailyReceipt rows for one facility across a date range.
        Iterates day-by-day so each receipt_date stays granular.
        """
        all_rows: list[DailyReceipt] = []
        d = date_from
        while d <= date_to:
            rows = self.reconcile_date(facility_id, d, upsert=upsert)
            all_rows.extend(rows)
            d += timedelta(days=1)
        return all_rows

    def reconcile_all_facilities(
        self,
        target_date: date,
        upsert: bool = True,
    ) -> dict[int, list[DailyReceipt]]:
        """
        Reconcile all active facilities for a given date.
        Called by the nightly fetch trigger.
        """
        facilities = (
            self.db.query(Facility)
            .filter(Facility.is_active.is_(True))
            .all()
        )
        result: dict[int, list[DailyReceipt]] = {}
        for fac in facilities:
            try:
                rows = self.reconcile_date(fac.id, target_date, upsert=upsert)
                result[fac.id] = rows
                log.info("fac=%d %s: %d receipt rows", fac.id, fac.facility_code, len(rows))
            except Exception as exc:
                log.error("fac=%d reconcile failed: %s", fac.id, exc, exc_info=True)
                result[fac.id] = []
        return result

    # ── export helpers ──────────────────────────────────────────────────────

    def get_daily_receipts(
        self,
        facility_id: int,
        date_from: date,
        date_to: date,
        channel: Optional[str] = None,
        payer_id: Optional[str] = None,
    ) -> list[DailyReceipt]:
        """Query daily_receipts for a facility + date range with optional filters."""
        q = (
            self.db.query(DailyReceipt)
            .filter(
                DailyReceipt.facility_id == facility_id,
                DailyReceipt.receipt_date >= date_from,
                DailyReceipt.receipt_date <= date_to,
            )
            .order_by(DailyReceipt.receipt_date, DailyReceipt.channel, DailyReceipt.payer_id)
        )
        if channel:
            q = q.filter(DailyReceipt.channel == channel)
        if payer_id:
            q = q.filter(DailyReceipt.payer_id == payer_id)
        return q.all()

    def get_unmatched(self, facility_id: int, date_from: date, date_to: date) -> list[DailyReceipt]:
        """Return rows where received > 0 but no matching submission claim found (unmatched)."""
        return (
            self.db.query(DailyReceipt)
            .filter(
                DailyReceipt.facility_id == facility_id,
                DailyReceipt.receipt_date >= date_from,
                DailyReceipt.receipt_date <= date_to,
                DailyReceipt.status == "unmatched",
            )
            .order_by(DailyReceipt.receipt_date)
            .all()
        )

    # ── internals ───────────────────────────────────────────────────────────

    def _aggregate_ra(
        self,
        facility_id: int,
        date_from: date,
        date_to: date,
    ) -> dict[tuple, dict]:
        """
        Pull all remittances whose RA file was posted in [date_from, date_to]
        (filtered via raw_xml_store.submission_date), then aggregate by
        (receipt_date, payer_id, channel).

        Returns a dict keyed by (receipt_date, payer_id, channel) with:
          received_aed, denied_aed, patient_share_aed, expected_aed,
          claim_count, payment_refs (set), payer_name
        """
        # Join remittances → raw_xml_store to get channel + RA posting date
        rows = (
            self.db.query(
                RawXmlStore.submission_date.label("ra_date"),
                RawXmlStore.xml_type.label("xml_type"),
                RawXmlStore.source_filename.label("source_filename"),
                Remittance.payer_id,
                null().label("payer_name"),
                Remittance.paid_amount,
                Remittance.denial_amount,
                Remittance.patient_share_total,
                Remittance.net_amount,
                Remittance.ra_file_id,
                Remittance.claim_id,
            )
            .join(RawXmlStore, Remittance.raw_xml_id == RawXmlStore.id)
            .filter(
                Remittance.facility_id == facility_id,
                Remittance.raw_xml_id.isnot(None),
                RawXmlStore.submission_date >= date_from,
                RawXmlStore.submission_date <= date_to,
                RawXmlStore.xml_type.in_(list(_XML_TYPE_TO_CHANNEL.keys())),
            )
            .all()
        )

        # Also pull remittances without raw_xml_id (EXCEL_HISTORICAL) — tagged as DHPO
        rows_no_raw = (
            self.db.query(
                Remittance.remittance_date.label("ra_date"),
                Remittance.payer_id,
                Remittance.paid_amount,
                Remittance.denial_amount,
                Remittance.patient_share_total,
                Remittance.net_amount,
                Remittance.ra_file_id,
                Remittance.claim_id,
            )
            .filter(
                Remittance.facility_id == facility_id,
                Remittance.raw_xml_id.is_(None),
                Remittance.remittance_date >= date_from,
                Remittance.remittance_date <= date_to,
                Remittance.source_channel == "EXCEL_HISTORICAL",
            )
            .all()
        )

        buckets: dict[tuple, dict] = defaultdict(lambda: {
            "received_aed": _ZERO,
            "denied_aed": _ZERO,
            "patient_share_aed": _ZERO,
            "expected_aed": _ZERO,
            "claim_count": 0,
            "payment_refs": set(),
            "payer_name": None,
        })

        for r in rows:
            channel = _XML_TYPE_TO_CHANNEL.get(r.xml_type, "DHPO")
            key = (r.ra_date, r.payer_id, channel)
            b = buckets[key]
            b["received_aed"] += Decimal(str(r.paid_amount or 0))
            b["denied_aed"] += Decimal(str(r.denial_amount or 0))
            b["patient_share_aed"] += Decimal(str(r.patient_share_total or 0))
            b["expected_aed"] += Decimal(str(r.net_amount or 0))
            b["claim_count"] += 1
            if r.ra_file_id:
                b["payment_refs"].add(r.ra_file_id)
            elif r.source_filename:
                b["payment_refs"].add(r.source_filename)
            if not b["payer_name"] and hasattr(r, "payer_name") and r.payer_name:
                b["payer_name"] = r.payer_name

        for r in rows_no_raw:
            key = (r.ra_date, r.payer_id, "DHPO")
            b = buckets[key]
            b["received_aed"] += Decimal(str(r.paid_amount or 0))
            b["denied_aed"] += Decimal(str(r.denial_amount or 0))
            b["patient_share_aed"] += Decimal(str(r.patient_share_total or 0))
            b["expected_aed"] += Decimal(str(r.net_amount or 0))
            b["claim_count"] += 1
            if r.ra_file_id:
                b["payment_refs"].add(r.ra_file_id)

        return dict(buckets)

    def _status_for(self, b: dict) -> str:
        """Derive receipt status from aggregated bucket."""
        received = b["received_aed"]
        denied = b["denied_aed"]
        expected = b["expected_aed"]

        if received == _ZERO and denied == _ZERO:
            return "unmatched"
        if received == _ZERO and denied > _ZERO:
            return "denied"
        if denied > _ZERO:
            return "partial"
        return "reconciled"

    def _get_mapping(self, facility_id: int, payer_id: str, channel: str) -> Optional[ReceiptPayerMapping]:
        """Look up GL mapping: exact match first, then 'ALL' channel fallback."""
        exact = (
            self.db.query(ReceiptPayerMapping)
            .filter_by(facility_id=facility_id, payer_id=payer_id, channel=channel, is_active=True)
            .first()
        )
        if exact:
            return exact
        return (
            self.db.query(ReceiptPayerMapping)
            .filter_by(facility_id=facility_id, payer_id=payer_id, channel="ALL", is_active=True)
            .first()
        )

    def _upsert_receipts(
        self,
        facility_id: int,
        buckets: dict[tuple, dict],
        upsert: bool,
    ) -> list[DailyReceipt]:
        written: list[DailyReceipt] = []
        for (receipt_date, payer_id, channel), b in buckets.items():
            if receipt_date is None:
                continue

            mapping = self._get_mapping(facility_id, payer_id, channel)
            gl_code = mapping.gl_account_code if mapping else None

            existing = (
                self.db.query(DailyReceipt)
                .filter_by(
                    facility_id=facility_id,
                    receipt_date=receipt_date,
                    payer_id=payer_id,
                    channel=channel,
                )
                .first()
            )

            pay_refs_json = json.dumps(sorted(b["payment_refs"])) if b["payment_refs"] else None
            status = self._status_for(b)

            if existing and upsert:
                existing.expected_aed = b["expected_aed"]
                existing.received_aed = b["received_aed"]
                existing.denied_aed = b["denied_aed"]
                existing.patient_share_aed = b["patient_share_aed"]
                existing.claim_count = b["claim_count"]
                existing.payment_references = pay_refs_json
                existing.status = status
                existing.gl_account_code = gl_code
                if b["payer_name"]:
                    existing.payer_name = b["payer_name"]
                written.append(existing)
            elif not existing:
                row = DailyReceipt(
                    facility_id=facility_id,
                    receipt_date=receipt_date,
                    payer_id=payer_id,
                    payer_name=b["payer_name"],
                    channel=channel,
                    expected_aed=b["expected_aed"],
                    received_aed=b["received_aed"],
                    denied_aed=b["denied_aed"],
                    patient_share_aed=b["patient_share_aed"],
                    claim_count=b["claim_count"],
                    payment_references=pay_refs_json,
                    status=status,
                    gl_account_code=gl_code,
                )
                self.db.add(row)
                written.append(row)

        self.db.commit()
        log.info("fac=%d: upserted %d daily_receipt rows", facility_id, len(written))
        return written
