"""
Migration: Add Riayati (MOH Post Office) credentials to facilities table
        + extend raw_xml_store xml_type constraint to include Riayati types.

Run once:
  cd ~/my_project/ghaf-app/backend
  .venv/bin/python3 scripts/add_riayati_credentials.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import engine
from sqlalchemy import text

def main():
    with engine.begin() as conn:
        # ── 1. Add riayati_username ──────────────────────────────────────
        row = conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='facilities' AND column_name='riayati_username'"
        )).fetchone()
        if row:
            print("✅ riayati_username already exists — skipping column adds.")
        else:
            print("Adding riayati_username, riayati_password to facilities...")
            conn.execute(text(
                "ALTER TABLE facilities "
                "ADD COLUMN riayati_username VARCHAR(100) NULL, "
                "ADD COLUMN riayati_password VARCHAR(100) NULL"
            ))
            print("  ✅ Columns added.")

        # ── 2. Extend raw_xml_store xml_type CHECK ───────────────────────
        # Drop old constraint, recreate with Riayati types included.
        existing = conn.execute(text(
            "SELECT constraint_name FROM information_schema.table_constraints "
            "WHERE table_name='raw_xml_store' AND constraint_type='CHECK' "
            "AND constraint_name='ck_raw_xml_type'"
        )).fetchone()
        if existing:
            print("Dropping old ck_raw_xml_type constraint...")
            conn.execute(text(
                "ALTER TABLE raw_xml_store DROP CONSTRAINT ck_raw_xml_type"
            ))

        print("Adding extended ck_raw_xml_type constraint (includes Riayati types)...")
        conn.execute(text(
            "ALTER TABLE raw_xml_store ADD CONSTRAINT ck_raw_xml_type CHECK ("
            "xml_type IN ("
            "  'ClaimSubmission', 'RemittanceAdvice',"
            "  'RiayatiClaimSubmission', 'RiayatiRemittanceAdvice'"
            "))"
        ))
        print("  ✅ Constraint updated.")

    print("\nDone. Run add_riayati_creds_noor.command on Desktop to set credentials.")

if __name__ == "__main__":
    main()
