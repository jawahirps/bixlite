#!/usr/bin/env python3
"""
fix_zero_net_targeted.py
========================
Targeted fix: processes ONLY the ClaimSubmission raw_xml_store records
that directly produced zero-net initial submission claims.
Skips the general pending backlog entirely.

Run from backend directory with venv activated:
    .venv/bin/python fix_zero_net_targeted.py
"""
from __future__ import annotations

import sys, os
sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

from decimal import Decimal
from io import BytesIO
import logging

logging.basicConfig(level=logging.WARNING)  # suppress INFO noise

from sqlalchemy import create_engine, text, select
from sqlalchemy.orm import sessionmaker

DB_URL = os.environ["DATABASE_URL"]
engine = create_engine(DB_URL, pool_pre_ping=True)
Session = sessionmaker(bind=engine)


def main():
    db = Session()
    try:
        # ── Find the exact raw_xml_store IDs that produced zero-net claims ──
        print("Finding raw_xml_store records with zero-net initial submission claims …")
        rows = db.execute(text("""
            SELECT DISTINCT c.raw_xml_id, r.source_filename, r.file_size_bytes,
                            COUNT(c.id) AS zero_claim_count
            FROM   claims c
            JOIN   raw_xml_store r ON r.id = c.raw_xml_id
            WHERE  (c.net_amount IS NULL OR c.net_amount = 0)
              AND  c.resubmission_key = ''
              AND  c.raw_xml_id IS NOT NULL
              AND  r.xml_type = 'ClaimSubmission'
            GROUP BY c.raw_xml_id, r.source_filename, r.file_size_bytes
            ORDER BY c.raw_xml_id ASC
        """)).fetchall()

        target_ids = [r[0] for r in rows]
        print(f"Found {len(target_ids)} ClaimSubmission files with zero-net claims:")
        total_zero = sum(r[3] for r in rows)
        print(f"  Covering {total_zero:,} zero-net claim rows")
        print(f"  File sizes: min={min(r[2] for r in rows):,}B  "
              f"max={max(r[2] for r in rows):,}B  "
              f"total={sum(r[2] for r in rows)/1_048_576:.1f}MB")

        if not target_ids:
            print("Nothing to fix — all zero-net claims have no raw_xml_id (externally imported).")
            return

        # ── Reset only these files to pending ────────────────────────────────
        print(f"\nResetting {len(target_ids)} files to pending …")
        db.execute(text("""
            UPDATE raw_xml_store
            SET    parse_status   = 'pending',
                   parse_error    = NULL,
                   records_parsed = NULL
            WHERE  id = ANY(:ids)
        """), {"ids": target_ids})
        db.commit()
        print("  Done.")

        # ── Import parser and process ONLY these files ────────────────────────
        from app.models import RawXmlStore
        from app.services.dhpo_parser import DHPOXmlParser

        print(f"\nParsing {len(target_ids)} files with fixed net_amount derivation …")
        parsed_ok = 0
        quarantined = 0

        for i, raw_id in enumerate(target_ids, 1):
            record = db.get(RawXmlStore, raw_id)
            if not record:
                print(f"  [{i}/{len(target_ids)}] ID={raw_id}: NOT FOUND — skipping")
                continue

            size_kb = (record.file_size_bytes or 0) // 1024
            print(f"  [{i}/{len(target_ids)}] ID={raw_id}  {size_kb}KB  {record.source_filename[:60]}", end="  ", flush=True)

            try:
                parser = DHPOXmlParser(db)
                from datetime import datetime
                record.parsed_at = datetime.utcnow()
                count = parser._process_claims_submission(record)
                record.parse_status    = "parsed"
                record.records_parsed  = count
                db.commit()
                print(f"✓ {count} claims")
                parsed_ok += 1
            except Exception as exc:
                db.rollback()
                record.parse_status = "quarantined"
                record.parse_error  = str(exc)[:500]
                db.commit()
                print(f"✗ ERROR: {exc}")
                quarantined += 1

        # ── Final count ───────────────────────────────────────────────────────
        zero_after = db.execute(text("""
            SELECT COUNT(*) FROM claims
            WHERE (net_amount IS NULL OR net_amount = 0)
              AND resubmission_key = ''
        """)).scalar()

        print(f"\n{'='*60}")
        print(f"Files processed: {parsed_ok} ok, {quarantined} quarantined")
        print(f"Zero-net initial submission claims remaining: {zero_after:,}")
        print(f"{'='*60}")
        print("\nNext step: generate a new ClaimSummary report from the Ghaf App UI.")

    finally:
        db.close()


if __name__ == "__main__":
    main()
