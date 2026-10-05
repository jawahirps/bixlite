#!/usr/bin/env python3
"""
Migration: add indexes on claims table needed by ClaimSummaryV2.

Fixed version — closes the check connection BEFORE running CREATE INDEX CONCURRENTLY
to avoid the deadlock where the open transaction blocks the CONCURRENTLY wait.

Run once:
    cd ~/my_project/ghaf-app/backend
    source .venv/bin/activate
    set -a; source .env; set +a
    python3.12 add_claims_indexes.py
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

INDEXES = [
    (
        "idx_claims_fac_claim_id",
        "claims(facility_id, claim_id)",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_claims_fac_claim_id ON claims(facility_id, claim_id)",
    ),
    (
        "idx_claims_fac_submission_date",
        "claims(facility_id, submission_date)",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_claims_fac_submission_date ON claims(facility_id, submission_date)",
    ),
    (
        "idx_claims_fac_resub_key",
        "claims(facility_id, resubmission_key)",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_claims_fac_resub_key ON claims(facility_id, resubmission_key)",
    ),
]

# ── Step 1: drop any INVALID indexes left by a previous interrupted CONCURRENTLY ──
print("Checking for invalid indexes to clean up...")
with engine.connect() as conn:
    invalid_names = conn.execute(text("""
        SELECT i.relname
        FROM pg_index ix
        JOIN pg_class i ON i.oid = ix.indexrelid
        JOIN pg_class t ON t.oid = ix.indrelid
        WHERE t.relname = 'claims'
          AND ix.indisvalid = false
          AND i.relname IN ('idx_claims_fac_claim_id',
                            'idx_claims_fac_submission_date',
                            'idx_claims_fac_resub_key')
    """)).fetchall()
    invalid_list = [r[0] for r in invalid_names]

if invalid_list:
    print(f"Found INVALID indexes: {invalid_list}. Dropping them...")
    raw = engine.raw_connection()
    raw.set_isolation_level(0)
    cur = raw.cursor()
    for name in invalid_list:
        print(f"  DROP INDEX CONCURRENTLY {name}...")
        cur.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
        print(f"  Dropped {name}.")
    cur.close()
    raw.close()
else:
    print("No invalid indexes found.")

# ── Step 2: check which indexes already exist (valid) ──
to_create = []
with engine.connect() as conn:
    for idx_name, desc, sql in INDEXES:
        exists = conn.execute(text("""
            SELECT 1 FROM pg_indexes
            WHERE tablename = 'claims' AND indexname = :name
        """), {"name": idx_name}).fetchone()
        if exists:
            print(f"✅  Index '{idx_name}' already exists — skipping.")
        else:
            to_create.append((idx_name, desc, sql))
# conn is now CLOSED — no open transaction to block CONCURRENTLY

# ── Step 3: create missing indexes on separate AUTOCOMMIT connections ──
for idx_name, desc, sql in to_create:
    print(f"Creating index '{idx_name}' on {desc} ...")
    print(f"   (CONCURRENTLY — won't lock the table; may take 2-5 min for large tables)")
    raw = engine.raw_connection()
    raw.set_isolation_level(0)  # AUTOCOMMIT — required for CONCURRENTLY
    cur = raw.cursor()
    cur.execute(sql)
    cur.close()
    raw.close()
    print(f"✅  '{idx_name}' created.")

print("\nDone. ClaimSummaryV2 reports will now generate in seconds.")
