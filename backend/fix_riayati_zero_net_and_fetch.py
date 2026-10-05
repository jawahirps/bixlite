#!/usr/bin/env python3
"""
fix_riayati_zero_net_and_fetch.py
==================================
1. Re-parse existing Riayati RawXmlStore records that produced zero-net
   initial submission claims, using the fixed riayati_service.py parser.
2. Run a fresh Riayati API fetch for all Riayati facilities for the last
   2 years (Sep 2024 → today) to pull any missing records.

Run from backend directory with venv activated in a NEW terminal window
(safe to run while fix_zero_net_parallel.py is still running):
    caffeinate -i .venv/bin/python fix_riayati_zero_net_and_fetch.py
"""
from __future__ import annotations

import sys, os, logging
sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

from datetime import date, datetime
from decimal import Decimal

logging.basicConfig(level=logging.WARNING)
log = logging.getLogger(__name__)

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

DB_URL  = os.environ["DATABASE_URL"]
engine  = create_engine(DB_URL, pool_pre_ping=True)
Session = sessionmaker(bind=engine)

# Fetch from Sep 7, 2024 (2-year DHPO window; Riayati has same limit)
FETCH_FROM = date(2024, 9, 7)
FETCH_TO   = date.today()


def main():
    db = Session()
    try:
        # ── STEP 1: Find Riayati raw_xml_store records with zero-net claims ──
        print("Finding Riayati ClaimSubmission records with zero-net initial claims …")
        rows = db.execute(text("""
            SELECT DISTINCT c.raw_xml_id, r.source_filename, r.file_size_bytes,
                            COUNT(c.id) AS zero_claim_count
            FROM   claims c
            JOIN   raw_xml_store r ON r.id = c.raw_xml_id
            WHERE  (c.net_amount IS NULL OR c.net_amount = 0)
              AND  c.resubmission_key = ''
              AND  c.raw_xml_id IS NOT NULL
              AND  r.xml_type IN ('RiayatiClaimSubmission', 'ClaimSubmission')
              AND  c.source_channel = 'API'
            GROUP BY c.raw_xml_id, r.source_filename, r.file_size_bytes
            ORDER BY r.file_size_bytes ASC
        """)).fetchall()

        target_ids = [r[0] for r in rows]
        print(f"Found {len(target_ids)} Riayati staged records with zero-net claims")
        if target_ids:
            total_zero = sum(r[3] for r in rows)
            print(f"  Covering {total_zero:,} zero-net claim rows")

            # Reset to pending
            db.execute(text("""
                UPDATE raw_xml_store
                SET    parse_status   = 'pending',
                       parse_error    = NULL,
                       records_parsed = NULL
                WHERE  id = ANY(:ids)
            """), {"ids": target_ids})
            db.commit()
            print(f"  Reset {len(target_ids)} records to pending\n")

            # Re-parse with fixed parser via run_pending (handles both
            # RiayatiClaimSubmission and RiayatiRemittanceAdvice types)
            from app.services.riayati_service import RiayatiJsonParser

            print(f"  Re-parsing {len(target_ids)} pending Riayati records …")
            parser = RiayatiJsonParser(db)
            parsed_ok = parser.run_pending()
            print(f"  Re-parsed {parsed_ok} records")

            zero_after_reparse = db.execute(text("""
                SELECT COUNT(*) FROM claims
                WHERE (net_amount IS NULL OR net_amount = 0)
                  AND resubmission_key = ''
                  AND source_channel = 'API'
            """)).scalar()
            print(f"Riayati zero-net claims remaining after re-parse: {zero_after_reparse:,}")
        else:
            print("  No existing staged Riayati records with zero-net — skip re-parse step.\n")

        # ── STEP 2: Fresh fetch from Riayati API ─────────────────────────────
        print(f"\nFetching Riayati records {FETCH_FROM} → {FETCH_TO} for all Riayati facilities …")

        riayati_facilities = db.execute(text("""
            SELECT id, facility_code, facility_name, riayati_username
            FROM   facilities
            WHERE  is_active = true
              AND  riayati_username IS NOT NULL
              AND  riayati_username != ''
              AND  facility_code NOT IN ('GHAF-001')
            ORDER  BY id
        """)).fetchall()

        print(f"Found {len(riayati_facilities)} Riayati facilities:")
        for f in riayati_facilities:
            print(f"  [{f.id}] {f.facility_code}  {f.facility_name}  (user={f.riayati_username})")

        if not riayati_facilities:
            print("No Riayati facilities found — check credentials in DB.")
            return

        from app.services.riayati_service import RiayatiSyncService, RiayatiInactiveFacilityError

        for fac in riayati_facilities:
            print(f"\n── {fac.facility_code} ({fac.facility_name}) ──")
            # Fetch in 3-month windows to avoid timeout on large ranges
            from_d = FETCH_FROM
            while from_d <= FETCH_TO:
                import calendar
                # End of this month
                last_day = calendar.monthrange(from_d.year, from_d.month)[1]
                to_d = min(date(from_d.year, from_d.month, last_day), FETCH_TO)
                print(f"  Window {from_d} → {to_d} …", end="  ", flush=True)
                try:
                    with Session() as win_db:
                        svc = RiayatiSyncService(win_db)
                        fl  = svc.run_sync_pipeline(fac.id, from_d, to_d)
                        fl_status  = fl.status
                        fl_fetched = fl.records_fetched or 0
                        fl_parsed  = fl.records_parsed or 0
                        fl_failed  = fl.records_failed or 0
                    sym = "✅" if fl_status == "success" else ("⚠️" if fl_status == "partial" else "❌")
                    print(f"{sym} fetched={fl_fetched}  parsed={fl_parsed}  failed={fl_failed}")
                except RiayatiInactiveFacilityError as exc:
                    print(f"⚠️  INACTIVE: {exc}")
                    print(f"  → Email RiayatiPO@mohap.gov.ae to reactivate {fac.facility_name}")
                    break  # no point continuing this facility
                except Exception as exc:
                    print(f"❌ ERROR: {exc}")
                # Advance to next month
                if from_d.month == 12:
                    from_d = date(from_d.year + 1, 1, 1)
                else:
                    from_d = date(from_d.year, from_d.month + 1, 1)

        # ── Final zero-net count ──────────────────────────────────────────────
        zero_final = db.execute(text("""
            SELECT COUNT(*) FROM claims
            WHERE (net_amount IS NULL OR net_amount = 0)
              AND resubmission_key = ''
              AND source_channel = 'API'
        """)).scalar()
        print(f"\n{'='*60}")
        print(f"Riayati zero-net initial submission claims remaining: {zero_final:,}")
        print(f"{'='*60}")
        print("\nNext: regenerate ClaimSummary report from the Ghaf App UI.")

    finally:
        db.close()


if __name__ == "__main__":
    main()
