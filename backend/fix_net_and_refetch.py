#!/usr/bin/env python3
"""
fix_net_and_refetch.py
======================
1. Reset ClaimSubmission raw_xml_store records (Jul/Aug 2026 batches that
   produced zero-net claims) back to "pending".
2. Re-run DHPOXmlParser.run_pending() so all reset records are re-parsed
   using the fixed net_amount derivation (activity sum fallback).
3. Fetch June 2026 claims from DHPO live for all credentialled facilities.
4. Print a summary of before/after zero-net claim counts.

Run from the backend directory with the app venv activated:
    cd /Users/lisnasaleena/my_project/ghaf-app/backend
    .venv/bin/python fix_net_and_refetch.py
"""
from __future__ import annotations

import sys
import os
from datetime import date
from decimal import Decimal

# ── Path setup ─────────────────────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

DB_URL = os.environ["DATABASE_URL"]
engine = create_engine(DB_URL, pool_pre_ping=True)
Session = sessionmaker(bind=engine)


def count_zero_net(db) -> int:
    row = db.execute(text("""
        SELECT COUNT(*) FROM claims
        WHERE (net_amount IS NULL OR net_amount = 0)
          AND resubmission_key = ''
    """)).scalar()
    return row or 0


def step1_reset_affected_submission_files(db) -> int:
    """
    Reset ClaimSubmission raw_xml_store records to 'pending' where any of
    the claims they produced have net_amount = 0.
    Scope: only ClaimSubmission files (not RemittanceAdvice).
    """
    result = db.execute(text("""
        UPDATE raw_xml_store
        SET    parse_status = 'pending',
               parse_error  = NULL,
               records_parsed = NULL
        WHERE  xml_type     = 'ClaimSubmission'
          AND  parse_status IN ('parsed', 'quarantined')
          AND  id IN (
              SELECT DISTINCT c.raw_xml_id
              FROM   claims c
              WHERE  (c.net_amount IS NULL OR c.net_amount = 0)
                AND  c.raw_xml_id IS NOT NULL
                AND  c.resubmission_key = ''
          )
    """))
    db.commit()
    return result.rowcount


def step2_reparse(db, facility_id: int | None = None) -> dict:
    """Re-run parser on all pending ClaimSubmission records."""
    from app.services.dhpo_parser import DHPOXmlParser
    parser = DHPOXmlParser(db)
    summary = parser.run_pending(facility_id=facility_id)
    return {
        "pending":      summary.pending_records,
        "parsed":       summary.parsed_records,
        "quarantined":  summary.quarantined_records,
        "total_claims": summary.records_parsed_total,
    }


def step3_fetch_june2026(db) -> list[dict]:
    """
    Fetch June 2026 ClaimSubmission + RemittanceAdvice from DHPO for every
    facility that has eClaimLink credentials.
    Note: GHAF-001 / demo_user is intentionally excluded.
    """
    from app.services.fetch_service import DHASyncService
    from app.models import Facility

    date_from = date(2026, 6, 1)
    date_to   = date.today()

    facilities = (
        db.query(Facility)
        .filter(
            Facility.eclaimlink_username.isnot(None),
            Facility.eclaimlink_username != "",
            Facility.eclaimlink_username != "demo_user",   # CRITICAL: never fetch GHAF-001
        )
        .order_by(Facility.id)
        .all()
    )

    results = []
    for fac in facilities:
        print(f"\n  → Fetching facility {fac.facility_code} ({fac.facility_name}) …")
        try:
            svc = DHASyncService(session=db)
            log = svc.run_sync_pipeline(
                facility_id=fac.id,
                date_from=date_from,
                date_to=date_to,
            )
            results.append({
                "facility":       fac.facility_code,
                "status":         log.status,
                "fetched":        log.records_fetched,
                "parsed":         log.records_parsed,
                "failed":         log.records_failed,
                "http_errors":    log.http_errors,
            })
            print(f"     status={log.status}  fetched={log.records_fetched}"
                  f"  parsed={log.records_parsed}  failed={log.records_failed}")
        except Exception as exc:
            print(f"     ERROR: {exc}")
            results.append({"facility": fac.facility_code, "status": "error", "error": str(exc)})

    return results


def main():
    db = Session()
    try:
        # ── Baseline ──────────────────────────────────────────────────────────
        zero_before = count_zero_net(db)
        print(f"[START] Zero-net initial submission claims: {zero_before:,}")

        # ── Step 1: Reset affected raw_xml_store records ─────────────────────
        print("\n[STEP 1] Resetting ClaimSubmission records that produced zero-net claims …")
        reset_count = step1_reset_affected_submission_files(db)
        print(f"  Reset {reset_count} raw_xml_store record(s) to 'pending'.")

        if reset_count == 0:
            print("  (Nothing to reset — zero-net claims may have no raw_xml_id, "
                  "or all submissions already pending.)")

        # ── Step 2: Re-parse with the fixed parser ────────────────────────────
        print("\n[STEP 2] Re-parsing with fixed net_amount derivation …")
        parse_summary = step2_reparse(db)
        print(f"  Parser summary: {parse_summary}")

        zero_after_reparse = count_zero_net(db)
        print(f"\n  Zero-net after re-parse: {zero_after_reparse:,}  "
              f"(fixed: {zero_before - zero_after_reparse:,})")

        # ── Step 3: Fetch June 2026 from DHPO ────────────────────────────────
        print("\n[STEP 3] Fetching June 2026 claims from DHPO for all facilities …")
        fetch_results = step3_fetch_june2026(db)

        # ── Final count ───────────────────────────────────────────────────────
        zero_final = count_zero_net(db)
        print(f"\n[DONE]")
        print(f"  Zero-net before:  {zero_before:,}")
        print(f"  Zero-net after:   {zero_final:,}")
        print(f"  Claims fixed:     {zero_before - zero_final:,}")
        print(f"\nFetch results per facility:")
        for r in fetch_results:
            print(f"  {r}")

        print("\nNext step: re-generate the ClaimSummary report from the Ghaf App UI.")

    finally:
        db.close()


if __name__ == "__main__":
    main()
