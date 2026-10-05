"""
Migration: add settlement_date column to remittances table.

  settlement_date = per-claim DateSettlement from DHA RA XML
  remittance_date = RA header TransactionDate (the RA issue date)

Run once:
  cd ~/my_project/ghaf-app/backend
  .venv/bin/python3 scripts/add_settlement_date.py
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import engine
from sqlalchemy import text

def main():
    with engine.begin() as conn:
        # Check if column already exists
        result = conn.execute(text("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'remittances' AND column_name = 'settlement_date'
        """))
        if result.fetchone():
            print("✅ settlement_date column already exists — nothing to do.")
            return

        print("Adding settlement_date column to remittances...")
        conn.execute(text("""
            ALTER TABLE remittances
            ADD COLUMN settlement_date DATE NULL
        """))
        print("✅ Column added.")

        # Backfill: for existing NAS records where remittance_date was set
        # from DateSettlement (not TransactionDate), we can't recover the
        # TransactionDate from the DB alone — a re-parse would be needed.
        # For now settlement_date stays NULL for existing rows; new parses
        # will populate it going forward.
        print("ℹ️  Existing rows: settlement_date = NULL (will be populated on next re-parse).")
        print("Done.")

if __name__ == "__main__":
    main()
