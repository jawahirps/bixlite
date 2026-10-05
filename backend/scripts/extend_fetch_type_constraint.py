"""
Migration: Extend fetch_logs.fetch_type CHECK constraint to include 'Riayati'.

Run once:
  cd ~/my_project/ghaf-app/backend
  .venv/bin/python3 scripts/extend_fetch_type_constraint.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import engine
from sqlalchemy import text

def main():
    with engine.begin() as conn:
        existing = conn.execute(text(
            "SELECT constraint_name FROM information_schema.table_constraints "
            "WHERE table_name='fetch_log' AND constraint_type='CHECK' "
            "AND constraint_name='ck_fetch_type'"
        )).fetchone()

        if existing:
            print("Dropping old ck_fetch_type constraint...")
            conn.execute(text("ALTER TABLE fetch_log DROP CONSTRAINT ck_fetch_type"))

        print("Adding extended ck_fetch_type (includes Riayati)...")
        conn.execute(text(
            "ALTER TABLE fetch_log ADD CONSTRAINT ck_fetch_type CHECK ("
            "fetch_type IN ('ClaimSubmission','RemittanceAdvice','Backfill','Riayati'))"
        ))
        print("  ✅ Done.")

    print("\nFetch logs now accept fetch_type='Riayati'.")

if __name__ == "__main__":
    main()
