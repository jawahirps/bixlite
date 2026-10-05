"""
When was raw_xml_id=98679 parsed? Was it before or after the 18:13 report?
Also: how many claims in the June portal report RIGHT NOW?
"""
import sys
sys.path.insert(0, '/Users/lisnasaleena/my_project/ghaf-app/backend')

from app.config import get_settings
from sqlalchemy import create_engine, text

e = create_engine(get_settings().DATABASE_URL)

with e.connect() as c:
    # 1. When was the June RA file parsed?
    r = c.execute(text("""
        SELECT id, source_filename, submission_date, parse_status, records_parsed, fetched_at, parsed_at
        FROM raw_xml_store
        WHERE id = 98679
    """))
    row = r.fetchone()
    if row:
        print("raw_xml_id=98679:")
        print(f"  file:         {row[1]}")
        print(f"  sub_date:     {row[2]}")
        print(f"  status:       {row[3]}")
        print(f"  records:      {row[4]}")
        print(f"  fetched_at:   {row[5]}")
        print(f"  parsed_at:    {row[6]}")
    print()

    # 2. How many remittances currently point to raw_xml_id=98679 for facility 15?
    r2 = c.execute(text("""
        SELECT COUNT(*) FROM remittances WHERE raw_xml_id = 98679 AND facility_id = 15
    """))
    print(f"Remittances pointing to raw_xml_id=98679 (facility 15): {r2.scalar()}")
    print()

    # 3. Current portal count for June 2026 (as of NOW)
    r3 = c.execute(text("""
        SELECT COUNT(DISTINCT r.id)
        FROM remittances r
        JOIN raw_xml_store rx ON rx.id = r.raw_xml_id
        WHERE r.facility_id = 15
          AND rx.submission_date BETWEEN '2026-06-01' AND '2026-06-30'
    """))
    print(f"Portal count NOW (rx.submission_date June 2026, facility 15): {r3.scalar()}")
    print()

    # 4. Show the remittances for fac 15 in June by sub_date
    r4 = c.execute(text("""
        SELECT rx.submission_date, COUNT(DISTINCT r.id) as cnt
        FROM remittances r
        JOIN raw_xml_store rx ON rx.id = r.raw_xml_id
        WHERE r.facility_id = 15
          AND rx.submission_date BETWEEN '2026-06-01' AND '2026-06-30'
        GROUP BY rx.submission_date
        ORDER BY rx.submission_date
    """))
    rows4 = r4.fetchall()
    print("Remittances for facility 15 in June 2026, by raw_xml sub_date:")
    total = 0
    for row in rows4:
        print(f"  {row[0]}: {row[1]}")
        total += row[1]
    print(f"  TOTAL: {total}")
    print()

    # 5. What's in the system RA report but not June portal?
    # Claims where raw_xml points to June BUT remittance_date is NOT June
    r5 = c.execute(text("""
        SELECT r.remittance_date, COUNT(*) as cnt
        FROM remittances r
        JOIN raw_xml_store rx ON rx.id = r.raw_xml_id
        WHERE r.facility_id = 15
          AND rx.submission_date BETWEEN '2026-06-01' AND '2026-06-30'
        GROUP BY r.remittance_date
        ORDER BY r.remittance_date
    """))
    rows5 = r5.fetchall()
    print("For June portal claims: distribution of remittance_date (settlement date):")
    for row in rows5:
        print(f"  rem_date={row[0]}: {row[1]} claims")
