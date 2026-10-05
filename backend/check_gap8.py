"""
check_gap8.py — Count ACTIVITY ROWS (not claims) for June 2026, facility 15.

Both our app report and the client system report are activity-level.
verify_fix.py counted CLAIMS (1,232). This counts ACTIVITY LINES.
"""
import sys
sys.path.insert(0, '/Users/lisnasaleena/my_project/ghaf-app/backend')

from app.config import get_settings
from sqlalchemy import create_engine, text

e = create_engine(get_settings().DATABASE_URL)

with e.connect() as c:
    print("=" * 60)
    print("ACTIVITY-LEVEL COUNT FOR JUNE 2026 — FACILITY 15")
    print("=" * 60)

    # Total activity rows for June 2026 (matches what portal export produces)
    r = c.execute(text("""
        SELECT COUNT(*) AS activity_rows
        FROM remittance_activity_details rad
        JOIN remittances rem ON rem.id = rad.remittance_id_fk
        WHERE rem.facility_id = 15
          AND rem.remittance_date BETWEEN '2026-06-01' AND '2026-06-30'
    """))
    activity_count = r.scalar()
    print(f"\nActivity rows (June 2026, settlement date filter): {activity_count:,}")
    print(f"Unique claims  (June 2026, settlement date filter): 1,232")
    print(f"Avg activities per claim: {activity_count/1232:.1f}")
    print(f"\nClient system report (June 2026):                   1,641")
    print(f"Gap (activity rows):  {1641 - activity_count:,}")

    # Also count using OLD filter (RA file date June) for comparison
    r2 = c.execute(text("""
        SELECT COUNT(*) AS activity_rows
        FROM remittance_activity_details rad
        JOIN remittances rem ON rem.id = rad.remittance_id_fk
        JOIN raw_xml_store rx ON rx.id = rem.raw_xml_id
        WHERE rem.facility_id = 15
          AND rx.submission_date BETWEEN '2026-06-01' AND '2026-06-30'
    """))
    old_activity_count = r2.scalar()
    print(f"\nOLD filter (RA file date June) activity rows:       {old_activity_count:,}")

    # Breakdown by activity type
    r3 = c.execute(text("""
        SELECT
            rad.activity_type,
            COUNT(*) AS rows
        FROM remittance_activity_details rad
        JOIN remittances rem ON rem.id = rad.remittance_id_fk
        WHERE rem.facility_id = 15
          AND rem.remittance_date BETWEEN '2026-06-01' AND '2026-06-30'
        GROUP BY 1
        ORDER BY 2 DESC
        LIMIT 10
    """))
    print(f"\nActivity breakdown by type:")
    for row in r3.fetchall():
        print(f"  {(row.activity_type or 'NULL'):<20} {row.rows:>6}")

    print("=" * 60)
