#!/usr/bin/env python3
"""
reparse_all_remittances.py — Re-parse ALL RemittanceAdvice records for ALL facilities
======================================================================================
After patching dhpo_parser.py to generate synthetic RemittanceActivityDetail rows
for DHA standard-format RA files (those without <Activity> children), previously
parsed RA files that were in the old format won't have their claims visible in the
remittance activity report.

This script resets parse_status → 'pending' for ALL RemittanceAdvice records and
re-runs the parser for every active non-demo facility.

Run from backend/:
    python3.12 scripts/reparse_all_remittances.py
    python3.12 scripts/reparse_all_remittances.py --dry-run    # preview only
    python3.12 scripts/reparse_all_remittances.py --facility 15  # single facility
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.services.dhpo_parser import DHPOXmlParser

SKIP_CODES     = {"GHAF-001"}
SKIP_DHA_USERS = {"", "N/A", "demo_user"}

log = logging.getLogger("reparse_all")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run",    action="store_true", help="Show what would be done, no writes")
    parser.add_argument("--facility",   type=int,            help="Reparse single facility by ID")
    args = parser.parse_args()

    settings = get_settings()
    engine   = create_engine(settings.DATABASE_URL)

    log.info("=" * 65)
    log.info("Reparse ALL RemittanceAdvice records  %s",
             datetime.now().isoformat(timespec="seconds"))
    log.info("=" * 65)

    with engine.connect() as conn:
        if args.facility:
            fac_rows = conn.execute(text(
                "SELECT id, facility_code, facility_name FROM facilities WHERE id=:fid"
            ), {"fid": args.facility}).fetchall()
        else:
            fac_rows = conn.execute(text("""
                SELECT id, facility_code, facility_name
                FROM   facilities
                WHERE  is_active = true
                  AND  facility_code NOT IN :skip
                ORDER  BY id
            """), {"skip": tuple(SKIP_CODES)}).fetchall()

    log.info("Facilities to reparse: %d", len(fac_rows))

    total_reset  = 0
    total_parsed = 0
    total_quaran = 0
    failures: list[str] = []

    for fac in fac_rows:
        fid   = fac.id
        fcode = fac.facility_code
        fname = fac.facility_name

        # Count RemittanceAdvice records for this facility
        with engine.connect() as conn:
            cnt = conn.execute(text(
                "SELECT COUNT(*) FROM raw_xml_store "
                "WHERE facility_id=:fid AND xml_type='RemittanceAdvice'"
            ), {"fid": fid}).scalar()

        if cnt == 0:
            log.info("  %s (%s) — no RemittanceAdvice records, skipping", fcode, fname)
            continue

        log.info("── %s  %s  (%d RA records) ──────────────────────────────", fcode, fname, cnt)

        if args.dry_run:
            log.info("  [DRY RUN] Would reset %d records → pending and reparse", cnt)
            total_reset += cnt
            continue

        try:
            with Session(engine) as db:
                # Reset all RemittanceAdvice records to 'pending'
                result = db.execute(text(
                    "UPDATE raw_xml_store SET parse_status='pending' "
                    "WHERE facility_id=:fid AND xml_type='RemittanceAdvice'"
                ), {"fid": fid})
                reset_count = result.rowcount
                db.commit()
                log.info("  Reset %d records → pending", reset_count)
                total_reset += reset_count

                # Re-run parser (picks up all pending, including ClaimSubmission if any were reset)
                p       = DHPOXmlParser(db)
                summary = p.run_pending(facility_id=fid)
                db.commit()

                parsed  = summary.records_parsed_total
                quaran  = summary.quarantined_records
                total_parsed += parsed
                total_quaran += quaran

                sym = "✅" if quaran == 0 else "⚠️"
                log.info("  %s Parsed=%d  Quarantined=%d", sym, parsed, quaran)

        except Exception as exc:
            log.error("  ❌ FAILED for %s: %s", fcode, str(exc)[:200])
            failures.append(f"[{fcode}] {str(exc)[:120]}")

    log.info("=" * 65)
    log.info("Reparse complete  %s", datetime.now().isoformat(timespec="seconds"))
    log.info("  Records reset:   %d", total_reset)
    log.info("  Records parsed:  %d", total_parsed)
    log.info("  Quarantined:     %d", total_quaran)
    log.info("  Failures:        %d", len(failures))
    for f in failures:
        log.error("  ❌ %s", f)
    log.info("=" * 65)

    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
