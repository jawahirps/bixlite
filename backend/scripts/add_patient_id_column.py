"""
Migration: add patient_id column to claims table.

  patient_id = Encounter/PatientID from DHA claim XML
               (the patient's facility registration / DHA patient number)

Run once:
  cd ~/my_project/ghaf-app/backend
  .venv/bin/python3 scripts/add_patient_id_column.py
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import engine
from sqlalchemy import text

def main():
    with engine.begin() as conn:
        result = conn.execute(text("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'claims' AND column_name = 'patient_id'
        """))
        if result.fetchone():
            print("✅ patient_id column already exists — nothing to do.")
            return

        print("Adding patient_id column to claims...")
        conn.execute(text("""
            ALTER TABLE claims
            ADD COLUMN patient_id VARCHAR(100) NULL
        """))
        print("✅ Column added.")
        print("ℹ️  Existing rows: patient_id = NULL (populated on next re-parse/re-fetch).")
        print("Done.")

if __name__ == "__main__":
    main()
