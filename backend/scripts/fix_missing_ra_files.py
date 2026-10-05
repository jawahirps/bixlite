#!/usr/bin/env python3
"""
fix_missing_ra_files.py — Fix 54 missing RA files for New Lotus Medical Center
================================================================================
Gap analysis (2026-07-09) found 54 RA files present in DHA eClaimLink but
missing from our app's remittance report.  This script:

  STEP 1 — Diagnose: check which of the 54 files exist in raw_xml_store
  STEP 2 — Re-parse: reset parse_status='pending' for files already stored
  STEP 3 — Re-fetch: run a targeted fetch for the June 2026 date range to
            pick up files that were never downloaded into raw_xml_store

Run from backend/:
    python3.12 scripts/fix_missing_ra_files.py
    python3.12 scripts/fix_missing_ra_files.py --dry-run   # diagnose only
    python3.12 scripts/fix_missing_ra_files.py --step 1    # diagnose only
    python3.12 scripts/fix_missing_ra_files.py --step 2    # re-parse only
    python3.12 scripts/fix_missing_ra_files.py --step 3    # re-fetch only
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.services.fetch_service import DHASyncService
from app.services.dhpo_parser import DHPOXmlParser

# ─────────────────────────────────────────────────────────────────────────────
# The 54 RA files confirmed missing from our app (from gap analysis 2026-07-09)
# ─────────────────────────────────────────────────────────────────────────────
MISSING_RA_FILES = [
    "086BD83A-50D5-41DE-B716-1D8AC38F83E5.xml",
    "20260602_172441_TPA008_BANKREF_25-100006386150-1-15_AE000579.xml",
    "20260602_172553_TPA008_BANKREF_25-100009052936-1-15_AE000579.xml",
    "20260602_172651_TPA008_BANKREF_25-100009157112-1-15_AE000579.xml",
    "20260602_172712_TPA008_BANKREF_25-100009158064-1-15_AE000579.xml",
    "20260616_001331_TPA008_BANKREF_25-100006387360-1-15_AE000579.xml",
    "2F548F35-1C28-42CC-8181-EBEEAA710D48.xml",
    "3170EC2D-F1CD-4D5A-8932-5E9C6E7BCE2C.xml",
    "C68A1564-03A5-463A-B2CE-2D75EC7D07B0.xml",
    "CNMD926181790_80822555_1_1_DHA-F-0046977.xml",
    "CNMD926187614_80822555_1_1_DHA-F-0046977.xml",
    "CNMD926191086_80827205_1_1_DHA-F-0046977.xml",
    "DHA-F-0046977-33577229-27629000-24062026171507.XML",
    "DHA-F-0046977-33578419-27629990-03062026031538.XML",
    "DHA-F-0046977-33579436-27630876-24062026175511.XML",
    "DHA-F-0046977_201-132567_20260610_20_16_48.xml",
    "DHA-F-0046977_201-132568_20260610_20_16_48.xml",
    "DHA-F-0046977_201-133488_20260610_20_13_40.xml",
    "DHA-F-0046977_201-133489_20260610_20_16_47.xml",
    "DHA-F-0046977_201-138901_20260612_20_14_16.xml",
    "DHA-F-0046977_201-148744_20260616_20_16_01.xml",
    "DHA-F-0046977_202-16621_20260610_20_16_48.xml",
    "DHA-F-0046977_202-17086_20260612_20_14_16.xml",
    "DHA-F-0046977_202-17087_20260612_20_14_16.xml",
    "DHA-F-0046977_203-27032_20260610_20_16_48.xml",
    "DHA-F-0046977_203-27980_20260612_20_14_16.xml",
    "DHA-F-0046977_205-18737_20260610_20_13_40.xml",
    "DHA-F-0046977_208-52854_20260610_20_16_49.xml",
    "DHA-F-0046977_208-52855_20260610_20_16_47.xml",
    "DHA-F-0046977_208-52856_20260610_20_16_48.xml",
    "DHA-F-0046977_208-53105_20260610_20_13_40.xml",
    "DHA-F-0046977_211-20996_20260610_20_16_49.xml",
    "DHA-F-0046977_211-20997_20260610_20_16_48.xml",
    "JBM020626-025003_Direct_220626607.xml",
    "JBM020626-051635_Direct_220651371.xml",
    "JBM020626-065314_Direct_220669647.xml",
    "JBM020626-083943_Direct_220691092.xml",
    "JBM120626-114545_Direct_23291852.xml",
    "RA_2026Jun23101854_DHA-F-0046977_916337c80bd144ed88d90afc3da3a241.xml",
    "RA_2026Jun23103007_DHA-F-0046977_1bf7b6b29e1145bbba3e2db388c2ec12.xml",
    "RA_DHA_INS015_667767_04062026_190053.xml",
    "RA_GC03_1493918_03062026203548_NAS_To_DHA-F-0046977.xml",
    "RA_GC03_EPBCOP107094NPW4_05062026200228_Neuron_To_DHA-F-0046977.xml",
    "RA_GC03_EPBCOP118095JVFS_03062026211733_NAS_To_DHA-F-0046977.xml",
    "RA_GC03_FT261432J0T1_08062026203845_NAS_To_DHA-F-0046977.xml",
    "RA_GC03_PHUB660272177_04062026220316_NAS_To_DHA-F-0046977.xml",
    "RA_NOTAPPLICABLE_01062026203414_NAS_To_DHA-F-0046977.xml",
    "Ra_TPA036_DHA-F-0046977_202606010815_6a1d074e537121_19290933_3689525_validated.xml",
    "Ra_TPA036_DHA-F-0046977_202606021704_6a1ed4cb085f02_42060619_3695828_validated.xml",
    "Ra_TPA036_DHA-F-0046977_202606021744_6a1ede282c3651_21047270_3697221_validated.xml",
    "Ra_TPA036_DHA-F-0046977_202606031639_6a2020719d8377_20059877_3701592_validated.xml",
    "Ra_TPA036_DHA-F-0046977_202606031722_6a202a8158fc77_09293517_3702882_validated.xml",
    "Ra_TPA036_DHA-F-0046977_202606081023_6a265fcf8796b2_22463538_3708655_validated.xml",
    "ZERO_CNMDZ00337459_DHA-F-0046977_202664_349a9.xml",
]

# Date range covering all missing files (June 2026, with May buffer)
FETCH_DATE_FROM = date(2026, 5, 15)
FETCH_DATE_TO   = date(2026, 7, 9)

# ─────────────────────────────────────────────────────────────────────────────
log = logging.getLogger("fix_missing_ra")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Diagnose only, no writes")
    parser.add_argument("--step", type=int, choices=[1, 2, 3],
                        help="Run only one step (1=diagnose, 2=re-parse, 3=re-fetch)")
    args = parser.parse_args()

    do_all    = args.step is None
    do_step2  = do_all or args.step == 2
    do_step3  = do_all or args.step == 3
    dry_run   = args.dry_run

    settings = get_settings()
    engine   = create_engine(settings.DATABASE_URL)

    # ── Find New Lotus facility ───────────────────────────────────────────────
    with engine.connect() as conn:
        fac_row = conn.execute(text(
            "SELECT id, facility_code, facility_name, eclaimlink_username "
            "FROM facilities WHERE facility_name ILIKE '%lotus%' LIMIT 1"
        )).fetchone()

    if not fac_row:
        log.error("New Lotus Medical Center not found in facilities table.")
        sys.exit(1)

    fac_id   = fac_row.id
    fac_code = fac_row.facility_code
    fac_name = fac_row.facility_name
    log.info("Facility: id=%d  code=%s  name=%s  user=%s",
             fac_id, fac_code, fac_name, fac_row.eclaimlink_username)

    # ─────────────────────────────────────────────────────────────────────────
    # STEP 1 — Diagnose: which files are in raw_xml_store?
    # ─────────────────────────────────────────────────────────────────────────
    log.info("=" * 65)
    log.info("STEP 1 — Checking raw_xml_store for %d missing RA files", len(MISSING_RA_FILES))
    log.info("=" * 65)

    in_store:     list[tuple[str, int, str]] = []   # (filename, id, parse_status)
    not_in_store: list[str] = []

    with engine.connect() as conn:
        for fn in MISSING_RA_FILES:
            row = conn.execute(text(
                "SELECT id, parse_status, records_parsed "
                "FROM raw_xml_store WHERE facility_id=:fid AND source_filename=:fn LIMIT 1"
            ), {"fid": fac_id, "fn": fn}).fetchone()
            if row:
                in_store.append((fn, row.id, row.parse_status, row.records_parsed))
            else:
                # Also try case-insensitive match (some filenames have .XML uppercase)
                row2 = conn.execute(text(
                    "SELECT id, parse_status, records_parsed "
                    "FROM raw_xml_store WHERE facility_id=:fid AND LOWER(source_filename)=LOWER(:fn) LIMIT 1"
                ), {"fid": fac_id, "fn": fn}).fetchone()
                if row2:
                    in_store.append((fn, row2.id, row2.parse_status, row2.records_parsed))
                else:
                    not_in_store.append(fn)

    log.info("  Files already in raw_xml_store:   %d", len(in_store))
    for fn, rid, pstatus, parsed in in_store:
        log.info("    [STORED  parse_status=%-10s records=%s] %s", pstatus, parsed, fn)

    log.info("  Files NOT in raw_xml_store:       %d", len(not_in_store))
    for fn in not_in_store:
        log.info("    [MISSING] %s", fn)

    # ─────────────────────────────────────────────────────────────────────────
    # STEP 2 — Re-parse files already in raw_xml_store
    # ─────────────────────────────────────────────────────────────────────────
    if do_step2 and in_store:
        log.info("=" * 65)
        log.info("STEP 2 — Re-parsing %d files already in raw_xml_store", len(in_store))
        log.info("=" * 65)

        # Reset parse_status to 'pending' for all stored files
        ids_to_reset = [rid for _, rid, _, _ in in_store]
        if dry_run:
            log.info("  [DRY RUN] Would reset parse_status='pending' for ids: %s", ids_to_reset)
        else:
            with Session(engine) as db:
                for rid in ids_to_reset:
                    db.execute(text(
                        "UPDATE raw_xml_store SET parse_status='pending' WHERE id=:rid"
                    ), {"rid": rid})
                db.commit()
                log.info("  Reset parse_status='pending' for %d rows", len(ids_to_reset))

                # Run parser for this facility (picks up all 'pending' entries)
                parser = DHPOXmlParser(db)
                summary = parser.run_pending(facility_id=fac_id)
                db.commit()
                log.info(
                    "  Parser done — parsed=%d  quarantined=%d",
                    summary.records_parsed_total, summary.quarantined_records,
                )
    elif do_step2 and not in_store:
        log.info("STEP 2 — No stored files to re-parse, skipping.")

    # ─────────────────────────────────────────────────────────────────────────
    # STEP 3 — Re-fetch files NOT in raw_xml_store
    # ─────────────────────────────────────────────────────────────────────────
    if do_step3 and not_in_store:
        log.info("=" * 65)
        log.info("STEP 3 — Re-fetching %d files missing from raw_xml_store", len(not_in_store))
        log.info("  Date range: %s → %s", FETCH_DATE_FROM, FETCH_DATE_TO)
        log.info("=" * 65)

        if dry_run:
            log.info("  [DRY RUN] Would run DHASyncService.run_sync_pipeline for facility %d", fac_id)
            log.info("  [DRY RUN] Date range: %s → %s", FETCH_DATE_FROM, FETCH_DATE_TO)
            log.info("  [DRY RUN] SearchTransactions fetches both new (status=1) and "
                     "already-downloaded (status=2) files, so all missing files will be caught.")
        else:
            with Session(engine) as db:
                svc = DHASyncService(session=db)
                log.info("  Running DHASyncService.run_sync_pipeline ...")
                fl = svc.run_sync_pipeline(fac_id, FETCH_DATE_FROM, FETCH_DATE_TO)
                sym = "✅" if fl.status == "success" else ("⚠️" if fl.status == "partial" else "❌")
                log.info(
                    "  %s status=%-8s  fetched=%d  parsed=%d  failed=%d",
                    sym, fl.status,
                    fl.records_fetched or 0,
                    fl.records_parsed  or 0,
                    fl.records_failed  or 0,
                )
                if fl.status in ("success", "partial"):
                    log.info("  ✅ Re-fetch complete.")
                else:
                    log.error("  ❌ Fetch failed: %s", fl.error_detail)
                    sys.exit(1)

    elif do_step3 and not not_in_store:
        log.info("STEP 3 — All missing files were found in raw_xml_store. No re-fetch needed.")

    # ─────────────────────────────────────────────────────────────────────────
    # STEP 4 — Verify: count RA remittance records for New Lotus after fix
    # ─────────────────────────────────────────────────────────────────────────
    log.info("=" * 65)
    log.info("VERIFICATION — Checking remittance record counts after fix")
    log.info("=" * 65)

    with engine.connect() as conn:
        # Check raw_xml_store count
        total_raw = conn.execute(text(
            "SELECT COUNT(*), parse_status FROM raw_xml_store "
            "WHERE facility_id=:fid AND xml_type='RemittanceAdvice' "
            "GROUP BY parse_status ORDER BY parse_status"
        ), {"fid": fac_id}).fetchall()
        log.info("  raw_xml_store RemittanceAdvice rows by parse_status:")
        for r in total_raw:
            log.info("    parse_status=%-12s  count=%d", r.parse_status, r[0])

        # Check how many of the 54 target files are now in raw_xml_store
        now_in_store = 0
        for fn in MISSING_RA_FILES:
            row = conn.execute(text(
                "SELECT 1 FROM raw_xml_store WHERE facility_id=:fid AND LOWER(source_filename)=LOWER(:fn) LIMIT 1"
            ), {"fid": fac_id, "fn": fn}).fetchone()
            if row:
                now_in_store += 1
        log.info("  Target files now in raw_xml_store: %d / %d", now_in_store, len(MISSING_RA_FILES))

    log.info("=" * 65)
    log.info("Done.")
    log.info("Re-run the app report to verify the gap is closed.")


if __name__ == "__main__":
    main()
