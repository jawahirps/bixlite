"""
Verify portal.py fix: count by remittance_date (new filter) vs old submission_date filter.
"""
import sys
sys.path.insert(0, '/Users/lisnasaleena/my_project/ghaf-app/backend')

from app.config import get_settings
from sqlalchemy import create_engine, text

e = create_engine(get_settings().DATABASE_URL)

with e.connect() as c:
    # Old filter: rx.submission_date (June RA file date)
    r1 = c.execute(text("""
        SELECT COUNT(DISTINCT r.id)
        FROM remittances r
        JOIN raw_xml_store rx ON rx.id = r.raw_xml_id
        WHERE r.facility_id = 15
          AND rx.submission_date BETWEEN '2026-06-01' AND '2026-06-30'
    """))
    old_count = r1.scalar()

    # New filter: remittance_date (settlement date — per user requirement)
    r2 = c.execute(text("""
        SELECT COUNT(DISTINCT r.id)
        FROM remittances r
        WHERE r.facility_id = 15
          AND r.remittance_date BETWEEN '2026-06-01' AND '2026-06-30'
    """))
    new_count = r2.scalar()

    # Breakdown: what's changing?
    r3 = c.execute(text("""
        SELECT COUNT(DISTINCT r.id)
        FROM remittances r
        JOIN raw_xml_store rx ON rx.id = r.raw_xml_id
        WHERE r.facility_id = 15
          AND rx.submission_date BETWEEN '2026-06-01' AND '2026-06-30'
          AND r.remittance_date NOT BETWEEN '2026-06-01' AND '2026-06-30'
    """))
    removed = r3.scalar()

    r4 = c.execute(text("""
        SELECT COUNT(DISTINCT r.id)
        FROM remittances r
        WHERE r.facility_id = 15
          AND r.remittance_date BETWEEN '2026-06-01' AND '2026-06-30'
          AND (r.raw_xml_id IS NULL
               OR NOT EXISTS (
                   SELECT 1 FROM raw_xml_store rx
                   WHERE rx.id = r.raw_xml_id
                     AND rx.submission_date BETWEEN '2026-06-01' AND '2026-06-30'
               ))
    """))
    added = r4.scalar()

    # Total paid under new filter
    r5 = c.execute(text("""
        SELECT COALESCE(SUM(r.paid_amount), 0)
        FROM remittances r
        WHERE r.facility_id = 15
          AND r.remittance_date BETWEEN '2026-06-01' AND '2026-06-30'
    """))
    total_paid = r5.scalar()

    print("=" * 55)
    print("PORTAL DATE FILTER FIX — VERIFICATION")
    print("=" * 55)
    print(f"OLD filter (RA file date June):    {old_count:,} claims")
    print(f"NEW filter (settlement date June): {new_count:,} claims")
    print()
    print(f"  Removed (in old, not new): {removed:,} claims")
    print(f"     (were in June RA files but settled outside June)")
    print(f"  Added (in new, not old):   {added:,} claims")
    print(f"     (settled in June but RA file date is outside June)")
    print()
    print(f"System RA report (June 2026):     1,641 claims")
    print(f"Residual gap (new filter):        {1641 - new_count:,} claims")
    print(f"  → These need additional RA file backfill")
    print()
    print(f"Total paid (settlement date June): AED {float(total_paid):,.2f}")
    print("=" * 55)
    print()
    print("Fix applied to portal.py:")
    print("  - Date filter: ra_xml.submission_date → remittances.remittance_date")
    print("  - 'Remittance Date' column: now shows settlement date")
    print("  - KPI totals: same fix applied")
    print("  - ORDER BY: now remittance_date DESC")
