"""
Find root cause: what submission_date does raw_xml_store hold for missing claims' RA files?
"""
import sys
sys.path.insert(0, '/Users/lisnasaleena/my_project/ghaf-app/backend')

from app.config import get_settings
from sqlalchemy import create_engine, text

e = create_engine(get_settings().DATABASE_URL)

missing_ids = [
    'OPC-R-MH26-028123','OPC-R-MH26-028165','OPC-R-MH26-028166',
    'OPC-R-MH26-028832','OPC-R-MH26-028834','OPC-R-MH26-028836',
    'OPC-R-MH26-028837','OPC-R-MH26-028838','OPC-R-MH26-028850',
]

with e.connect() as c:
    # 1. Missing claims: remittance_date + which raw_xml they point to
    r = c.execute(text("""
        SELECT r.claim_id, r.remittance_date, r.raw_xml_id, r.ra_file_id,
               rx.source_filename, rx.submission_date AS rx_sub_date
        FROM remittances r
        LEFT JOIN raw_xml_store rx ON rx.id = r.raw_xml_id
        WHERE r.claim_id = ANY(:ids) AND r.facility_id = 15
        ORDER BY r.claim_id
    """), {'ids': missing_ids})
    rows = r.fetchall()
    print(f"Missing claims — remittance → raw_xml ({len(rows)} rows):")
    for row in rows:
        print(f"  claim={row[0]} | rem_date={row[1]} | raw_xml_id={row[2]} | ra_file_id={row[3]}")
        print(f"           source_filename={row[4]} | rx.submission_date={row[5]}")

    print()

    # 2. What raw_xml_store entries exist for June 2026 RA files for facility 15?
    r2 = c.execute(text("""
        SELECT id, source_filename, submission_date, xml_type
        FROM raw_xml_store
        WHERE facility_id = 15
          AND xml_type = 'RemittanceAdvice'
          AND (source_filename LIKE '%Jun%' OR submission_date >= '2026-06-01')
        ORDER BY submission_date DESC
        LIMIT 20
    """))
    rows2 = r2.fetchall()
    print(f"raw_xml_store RA entries for facility 15 in June 2026 ({len(rows2)} rows):")
    for row in rows2:
        print(f"  id={row[0]} | file={row[1]} | sub_date={row[2]} | type={row[3]}")

    print()

    # 3. Broader: what submission_dates exist for facility 15 RA files?
    r3 = c.execute(text("""
        SELECT submission_date, COUNT(*) as files, MIN(source_filename), MAX(source_filename)
        FROM raw_xml_store
        WHERE facility_id = 15 AND xml_type = 'RemittanceAdvice'
          AND submission_date >= '2026-04-01'
        GROUP BY submission_date
        ORDER BY submission_date
    """))
    rows3 = r3.fetchall()
    print("raw_xml_store RA files by submission_date (facility 15, Apr-Jun 2026):")
    for row in rows3:
        print(f"  sub_date={row[0]} | files={row[1]} | first={row[2]} | last={row[3]}")

    print()

    # 4. Check if any missing claims have NULL raw_xml_id
    r4 = c.execute(text("""
        SELECT COUNT(*) FROM remittances
        WHERE claim_id = ANY(:ids) AND facility_id = 15 AND raw_xml_id IS NULL
    """), {'ids': missing_ids})
    null_count = r4.scalar()
    print(f"Missing claims with NULL raw_xml_id: {null_count}")

    # 5. What does the portal query actually return for June 2026?
    r5 = c.execute(text("""
        SELECT COUNT(DISTINCT r.id)
        FROM remittances r
        JOIN raw_xml_store rx ON rx.id = r.raw_xml_id
        WHERE r.facility_id = 15
          AND rx.submission_date BETWEEN '2026-06-01' AND '2026-06-30'
    """))
    june_count = r5.scalar()
    print(f"\nClaims the portal query returns for June 2026 (filter on rx.submission_date): {june_count}")

    r6 = c.execute(text("""
        SELECT COUNT(DISTINCT r.id)
        FROM remittances r
        WHERE r.facility_id = 15
          AND r.remittance_date BETWEEN '2026-06-01' AND '2026-06-30'
    """))
    june_count2 = r6.scalar()
    print(f"Claims with remittance_date in June 2026: {june_count2}")
