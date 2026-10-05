"""
Migration: Create scrub_xml_findings table for Strawberry XML scrubber findings.

Run once:
  cd ~/my_project/ghaf-app/backend
  .venv/bin/python3.12 scripts/add_scrub_xml_findings.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import engine
from sqlalchemy import text


def main():
    with engine.begin() as conn:
        exists = conn.execute(text(
            "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
            "WHERE table_name = 'scrub_xml_findings')"
        )).scalar()

        if exists:
            print("✅ scrub_xml_findings already exists — skipping.")
            return

        print("Creating scrub_xml_findings table...")
        conn.execute(text("""
            CREATE TABLE scrub_xml_findings (
                id              BIGSERIAL PRIMARY KEY,
                session_id      BIGINT NOT NULL
                                REFERENCES scrub_sessions(id) ON DELETE CASCADE,
                session_status  VARCHAR(10)  NOT NULL
                                CHECK (session_status IN ('BLOCK','WARN','PASS')),
                risk_level      VARCHAR(10),
                risk_score      INTEGER,
                severity        VARCHAR(10)  NOT NULL
                                CHECK (severity IN ('BLOCK','WARN','PASS')),
                rule_id         VARCHAR(60)  NOT NULL,
                location        VARCHAR(200),
                claim_id        VARCHAR(100),
                activity_id     VARCHAR(100),
                message         TEXT         NOT NULL,
                source          TEXT,
                profile         VARCHAR(30)  NOT NULL DEFAULT 'UAE_COMMON',
                scrubbed_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW()
            )
        """))
        conn.execute(text("CREATE INDEX idx_scrub_xml_session  ON scrub_xml_findings(session_id)"))
        conn.execute(text("CREATE INDEX idx_scrub_xml_severity ON scrub_xml_findings(severity)"))
        conn.execute(text("CREATE INDEX idx_scrub_xml_rule_id  ON scrub_xml_findings(rule_id)"))
        print("  ✅ Table + indexes created.")

    print("\nDone — scrub_xml_findings is ready.")


if __name__ == "__main__":
    main()
