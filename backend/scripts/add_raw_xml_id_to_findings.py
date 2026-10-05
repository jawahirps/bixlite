"""
Migration: Layer 2 auto-scrub support.

Changes to scrub_xml_findings:
  1. Make session_id nullable (was NOT NULL)
  2. Add raw_xml_id column (FK → raw_xml_store.id, nullable)
  3. Add CHECK: (session_id IS NOT NULL OR raw_xml_id IS NOT NULL)
  4. Add index on raw_xml_id

Run once:
  cd ~/my_project/ghaf-app/backend
  .venv/bin/python3.12 scripts/add_raw_xml_id_to_findings.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import engine
from sqlalchemy import text


def col_exists(conn, table, col):
    return conn.execute(text(
        "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
        "WHERE table_name=:t AND column_name=:c)"
    ), {"t": table, "c": col}).scalar()


def constraint_exists(conn, table, name):
    return conn.execute(text(
        "SELECT EXISTS (SELECT 1 FROM information_schema.table_constraints "
        "WHERE table_name=:t AND constraint_name=:n)"
    ), {"t": table, "n": name}).scalar()


def index_exists(conn, name):
    return conn.execute(text(
        "SELECT EXISTS (SELECT 1 FROM pg_indexes WHERE indexname=:n)"
    ), {"n": name}).scalar()


def main():
    with engine.begin() as conn:
        print("=== Layer 2 migration: scrub_xml_findings ===\n")

        # 1. Make session_id nullable
        nullable = conn.execute(text(
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_name='scrub_xml_findings' AND column_name='session_id'"
        )).scalar()
        if nullable == "NO":
            print("1. Making session_id nullable...")
            conn.execute(text(
                "ALTER TABLE scrub_xml_findings ALTER COLUMN session_id DROP NOT NULL"
            ))
            print("   ✅ session_id is now nullable")
        else:
            print("1. session_id already nullable — skipping")

        # 2. Add raw_xml_id column
        if not col_exists(conn, "scrub_xml_findings", "raw_xml_id"):
            print("2. Adding raw_xml_id column...")
            conn.execute(text(
                "ALTER TABLE scrub_xml_findings "
                "ADD COLUMN raw_xml_id BIGINT REFERENCES raw_xml_store(id) ON DELETE CASCADE"
            ))
            print("   ✅ raw_xml_id column added")
        else:
            print("2. raw_xml_id already exists — skipping")

        # 3. Add CHECK constraint
        if not constraint_exists(conn, "scrub_xml_findings", "ck_scrub_xml_source"):
            print("3. Adding CHECK constraint (session_id OR raw_xml_id)...")
            conn.execute(text(
                "ALTER TABLE scrub_xml_findings "
                "ADD CONSTRAINT ck_scrub_xml_source "
                "CHECK (session_id IS NOT NULL OR raw_xml_id IS NOT NULL)"
            ))
            print("   ✅ constraint added")
        else:
            print("3. ck_scrub_xml_source constraint already exists — skipping")

        # 4. Add index on raw_xml_id
        if not index_exists(conn, "idx_scrub_xml_raw_xml"):
            print("4. Creating index on raw_xml_id...")
            conn.execute(text(
                "CREATE INDEX idx_scrub_xml_raw_xml ON scrub_xml_findings(raw_xml_id)"
            ))
            print("   ✅ index created")
        else:
            print("4. idx_scrub_xml_raw_xml already exists — skipping")

    print("\n✅ Layer 2 migration complete.")


if __name__ == "__main__":
    main()
