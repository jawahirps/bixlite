#!/usr/bin/env python3
"""
fix_zero_net_parallel.py
========================
Same logic as fix_zero_net_targeted.py but runs 4 workers in parallel.
Finds all ClaimSubmission files still pending with zero-net initial submission
claims, then processes them concurrently (each worker has its own DB session).

Run from backend directory with venv activated:
    caffeinate -i .venv/bin/python fix_zero_net_parallel.py

Kill the old fix_zero_net_targeted.py before running this.
"""
from __future__ import annotations

import sys, os, threading
sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

logging.basicConfig(level=logging.WARNING)

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

DB_URL   = os.environ["DATABASE_URL"]
engine   = create_engine(DB_URL, pool_pre_ping=True, pool_size=8, max_overflow=4)
Session  = sessionmaker(bind=engine)

WORKERS  = 4          # parallel workers — safe because each file has distinct claims
print_lock = threading.Lock()


def safe_print(*args, **kwargs):
    with print_lock:
        print(*args, **kwargs)


def process_file(task):
    """Runs in a worker thread. Each call gets its own session."""
    i, total, raw_id = task
    db = Session()
    try:
        from app.models import RawXmlStore
        from app.services.dhpo_parser import DHPOXmlParser

        record = db.get(RawXmlStore, raw_id)
        if not record:
            safe_print(f"  [{i}/{total}] ID={raw_id}: NOT FOUND — skipping")
            return raw_id, "missing", 0

        size_kb = (record.file_size_bytes or 0) // 1024
        safe_print(f"  [{i}/{total}] ID={raw_id}  {size_kb}KB  {record.source_filename[:55]}", end="  ", flush=True)

        parser = DHPOXmlParser(db)
        record.parsed_at = datetime.utcnow()
        count = parser._process_claims_submission(record)
        record.parse_status   = "parsed"
        record.records_parsed = count
        db.commit()
        safe_print(f"✓ {count} claims")
        return raw_id, "ok", count

    except Exception as exc:
        db.rollback()
        try:
            from app.models import RawXmlStore
            rec = db.get(RawXmlStore, raw_id)
            if rec:
                rec.parse_status = "quarantined"
                rec.parse_error  = str(exc)[:500]
                db.commit()
        except Exception:
            db.rollback()
        safe_print(f"✗ ERROR: {exc}")
        return raw_id, "error", 0

    finally:
        db.close()


def main():
    db = Session()
    try:
        # ── Find remaining pending files with zero-net claims ─────────────────
        print("Finding remaining ClaimSubmission files with zero-net initial claims …")
        rows = db.execute(text("""
            SELECT DISTINCT c.raw_xml_id, r.source_filename, r.file_size_bytes,
                            COUNT(c.id) AS zero_claim_count
            FROM   claims c
            JOIN   raw_xml_store r ON r.id = c.raw_xml_id
            WHERE  (c.net_amount IS NULL OR c.net_amount = 0)
              AND  c.resubmission_key = ''
              AND  c.raw_xml_id IS NOT NULL
              AND  r.xml_type = 'ClaimSubmission'
              AND  r.parse_status IN ('pending', 'parsed', 'quarantined')
            GROUP BY c.raw_xml_id, r.source_filename, r.file_size_bytes
            ORDER BY r.file_size_bytes ASC   -- smallest first for faster early wins
        """)).fetchall()

        target_ids = [r[0] for r in rows]
        total_zero = sum(r[3] for r in rows)
        print(f"Found {len(target_ids)} files covering {total_zero:,} zero-net claim rows")

        if not target_ids:
            print("Nothing to fix.")
            return

        # ── Reset all to pending ──────────────────────────────────────────────
        print(f"Resetting {len(target_ids)} files to pending …")
        db.execute(text("""
            UPDATE raw_xml_store
            SET    parse_status   = 'pending',
                   parse_error    = NULL,
                   records_parsed = NULL
            WHERE  id = ANY(:ids)
        """), {"ids": target_ids})
        db.commit()
        print("  Done.\n")
    finally:
        db.close()

    # ── Process in parallel ───────────────────────────────────────────────────
    total = len(target_ids)
    tasks = [(i + 1, total, rid) for i, rid in enumerate(target_ids)]

    parsed_ok   = 0
    quarantined = 0
    errors      = []

    print(f"Processing {total} files with {WORKERS} parallel workers …\n")
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(process_file, t): t for t in tasks}
        for fut in as_completed(futures):
            raw_id, status, count = fut.result()
            if status == "ok":
                parsed_ok += 1
            elif status == "error":
                quarantined += 1
                errors.append(raw_id)

    # ── Final summary ─────────────────────────────────────────────────────────
    check_db = Session()
    try:
        zero_after = check_db.execute(text("""
            SELECT COUNT(*) FROM claims
            WHERE (net_amount IS NULL OR net_amount = 0)
              AND resubmission_key = ''
        """)).scalar()
    finally:
        check_db.close()

    print(f"\n{'='*60}")
    print(f"Files processed : {parsed_ok} ok, {quarantined} quarantined")
    if errors:
        print(f"Quarantined IDs : {errors}")
    print(f"Zero-net initial submission claims remaining: {zero_after:,}")
    print(f"{'='*60}")
    print("\nNext: generate a new ClaimSummary report from the Ghaf App UI.")


if __name__ == "__main__":
    main()
