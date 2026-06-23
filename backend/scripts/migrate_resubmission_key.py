"""
Migration: support separate rows for initial submission + resubmission per claim.

Changes:
  1. Adds resubmission_key column ('' = initial, resubmission_type value = resub)
  2. Drops old unique constraint (facility_id, claim_id)
  3. Adds new unique constraint (facility_id, claim_id, resubmission_key)

Run ONCE before recover_initial_submissions.py:
    cd ~/my_project/ghaf-app/backend
    .venv/bin/python3 scripts/migrate_resubmission_key.py
"""
from sqlalchemy import create_engine, text

DATABASE_URL = "postgresql+psycopg2://ghaf@/ghaf_app?host=/tmp"
engine = create_engine(DATABASE_URL, pool_pre_ping=True)

with engine.begin() as conn:
    # 1. Add column
    conn.execute(text("""
        ALTER TABLE claims
        ADD COLUMN IF NOT EXISTS resubmission_key VARCHAR(50) NOT NULL DEFAULT ''
    """))
    print("Column resubmission_key added.")

    # 2. Populate from existing resubmission_type
    conn.execute(text("""
        UPDATE claims
        SET resubmission_key = COALESCE(resubmission_type, '')
        WHERE resubmission_key = ''
    """))
    updated = conn.execute(text(
        "SELECT COUNT(*) FROM claims WHERE resubmission_key != ''"
    )).scalar()
    print(f"Populated resubmission_key for {updated} resubmission rows.")

    # 3. Drop old unique constraint
    conn.execute(text("""
        ALTER TABLE claims
        DROP CONSTRAINT IF EXISTS uq_claims_facility_claim_id
    """))
    print("Old unique constraint dropped.")

    # 4. Add new unique constraint
    conn.execute(text("""
        ALTER TABLE claims
        ADD CONSTRAINT uq_claims_facility_claim_resub
        UNIQUE (facility_id, claim_id, resubmission_key)
    """))
    print("New unique constraint (facility_id, claim_id, resubmission_key) added.")

    total = conn.execute(text("SELECT COUNT(*) FROM claims")).scalar()
    resub = conn.execute(text("SELECT COUNT(*) FROM claims WHERE resubmission_key != ''")).scalar()
    print(f"\nTotal claims: {total}  (initial: {total-resub}, resubmission: {resub})")
    print("\nMigration complete.")
