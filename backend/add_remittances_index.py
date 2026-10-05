#!/usr/bin/env python3
"""
Migration: add composite index on remittances(facility_id, claim_id, id)

This index covers the three CTEs in ClaimCycleV2 that filter/order by
facility_id, claim_id, and id — eliminating full-table scans and making
report generation for large facilities (Alnoor, Apollo etc.) ~10-50x faster.

Run once:
    cd ~/my_project/ghaf-app/backend
    source .venv/bin/activate
    set -a; source .env; set +a
    python3.12 add_remittances_index.py
"""
import os, sys

DB_ENV = os.path.expanduser("~/my_project/ghaf-app/backend/.env")
db_url = None
if os.path.exists(DB_ENV):
    for line in open(DB_ENV):
        if line.startswith("DATABASE_URL="):
            db_url = line.strip().split("=", 1)[1]
            break
db_url = db_url or os.environ.get("DATABASE_URL")
if not db_url:
    sys.exit("❌  DATABASE_URL not found in .env")

from sqlalchemy import create_engine, text
engine = create_engine(db_url)

INDEX_NAME = "idx_remittances_fac_claim_id"

with engine.connect() as conn:
    # Check if index already exists
    exists = conn.execute(text("""
        SELECT 1 FROM pg_indexes
        WHERE tablename = 'remittances'
          AND indexname  = :name
    """), {"name": INDEX_NAME}).fetchone()

    if exists:
        print(f"✅  Index '{INDEX_NAME}' already exists — nothing to do.")
    else:
        print(f"Creating index '{INDEX_NAME}' on remittances(facility_id, claim_id, id)...")
        print("   (CONCURRENTLY — won't lock the table; may take 1-2 min for large tables)")
        # CONCURRENTLY cannot run inside a transaction; use raw connection
        raw = engine.raw_connection()
        raw.set_isolation_level(0)  # AUTOCOMMIT
        cur = raw.cursor()
        cur.execute(f"""
            CREATE INDEX CONCURRENTLY IF NOT EXISTS {INDEX_NAME}
            ON remittances(facility_id, claim_id, id)
        """)
        cur.close()
        raw.close()
        print(f"✅  Index created successfully.")
        print("   ClaimCycleV2 reports will now generate much faster.")
