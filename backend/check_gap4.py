"""
Check parse_status of June RA files for facility 15,
and what raw_xml_id the missing claims currently point to.
"""
import sys
sys.path.insert(0, '/Users/lisnasaleena/my_project/ghaf-app/backend')

from app.config import get_settings
from sqlalchemy import create_engine, text

e = create_engine(get_settings().DATABASE_URL)

missing_ids = [
    'OPC-R-MH26-028123','OPC-R-MH26-028165','OPC-R-MH26-028166',
    'OPC-R-MH26-028832','OPC-R-MH26-028834','OPC-R-MH26-028836',
]

with e.connect() as c:
    # 1. Parse status of ALL RA files for facility 15, June 2026
    r = c.execute(text("""
        SELECT id, source_filename, submission_date, parse_status, records_parsed, parsed_at
        FROM raw_xml_store
        WHERE facility_id = 15
          AND xml_type = 'RemittanceAdvice'
          AND submission_date >= '2026-06-01'
        ORDER BY submission_date, id
    """))
    rows = r.fetchall()
    print(f"RA files for facility 15, June 2026 ({len(rows)} rows):")
    for row in rows:
        print(f"  id={row[0]} | sub={row[2]} | status={row[3]} | recs={row[4]} | parsed_at={row[5]}")
        print(f"    file: {row[1]}")
    print()

    # 2. What raw_xml_id do the missing claims point to, and what is that file's status?
    r2 = c.execute(text("""
        SELECT r.claim_id, r.remittance_date, r.raw_xml_id,
               rx.submission_date AS rx_sub, rx.parse_status AS rx_status,
               rx.source_filename
        FROM remittances r
        LEFT JOIN raw_xml_store rx ON rx.id = r.raw_xml_id
        WHERE r.claim_id = ANY(:ids) AND r.facility_id = 15
        ORDER BY r.claim_id
    """), {'ids': missing_ids})
    rows2 = r2.fetchall()
    print(f"Missing claims current raw_xml link ({len(rows2)} rows):")
    for row in rows2:
        print(f"  {row[0]} | rem_date={row[1]} | raw_xml_id={row[2]} | rx.sub_date={row[3]} | rx.status={row[4]}")
        print(f"    file: {row[5]}")
    print()

    # 3. Count: how many RA files for fac 15 are still pending parse?
    r3 = c.execute(text("""
        SELECT parse_status, COUNT(*) as cnt
        FROM raw_xml_store
        WHERE facility_id = 15 AND xml_type = 'RemittanceAdvice'
        GROUP BY parse_status
        ORDER BY parse_status
    """))
    rows3 = r3.fetchall()
    print("RA parse_status counts for facility 15:")
    for row in rows3:
        print(f"  {row[0]}: {row[1]}")
    print()

    # 4. Check all facilities: how many pending RA files in total?
    r4 = c.execute(text("""
        SELECT f.facility_name, COUNT(*) as pending_ra
        FROM raw_xml_store rx
        JOIN facilities f ON f.id = rx.facility_id
        WHERE rx.xml_type = 'RemittanceAdvice' AND rx.parse_status = 'pending'
        GROUP BY f.facility_name
        ORDER BY pending_ra DESC
    """))
    rows4 = r4.fetchall()
    print("Pending RA files by facility:")
    for row in rows4:
        print(f"  {row[0]}: {row[1]}")
