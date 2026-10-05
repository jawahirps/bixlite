"""
Check: RA files with NULL submission_date for facility 15,
and claims in those files.
"""
import sys
sys.path.insert(0, '/Users/lisnasaleena/my_project/ghaf-app/backend')

from app.config import get_settings
from sqlalchemy import create_engine, text

e = create_engine(get_settings().DATABASE_URL)

with e.connect() as c:
    # 1. raw_xml_id=98679 details (when was it fetched and parsed?)
    r = c.execute(text("""
        SELECT id, source_filename, submission_date, parse_status,
               records_parsed, fetched_at, parsed_at
        FROM raw_xml_store WHERE id = 98679
    """))
    row = r.fetchone()
    if row:
        print(f"raw_xml_id=98679:")
        print(f"  file:       {row[1]}")
        print(f"  sub_date:   {row[2]}")
        print(f"  status:     {row[3]}")
        print(f"  records:    {row[4]}")
        print(f"  fetched_at: {row[5]}")
        print(f"  parsed_at:  {row[6]}")
    print()

    # 2. How many RA files for facility 15 have NULL submission_date?
    r2 = c.execute(text("""
        SELECT COUNT(*) FROM raw_xml_store
        WHERE facility_id=15 AND xml_type='RemittanceAdvice' AND submission_date IS NULL
    """))
    null_count = r2.scalar()
    print(f"RA files with NULL submission_date for facility 15: {null_count}")

    # 3. How many remittances point to NULL-submission_date RA files?
    r3 = c.execute(text("""
        SELECT COUNT(DISTINCT r.id)
        FROM remittances r
        JOIN raw_xml_store rx ON rx.id = r.raw_xml_id
        WHERE r.facility_id = 15
          AND rx.xml_type = 'RemittanceAdvice'
          AND rx.submission_date IS NULL
    """))
    print(f"Remittances (facility 15) pointing to NULL-sub_date RA files: {r3.scalar()}")
    print()

    # 4. Sample of RA files with NULL submission_date for facility 15
    r4 = c.execute(text("""
        SELECT rx.id, rx.source_filename, rx.fetched_at, rx.parse_status,
               COUNT(rem.id) as claim_count
        FROM raw_xml_store rx
        LEFT JOIN remittances rem ON rem.raw_xml_id = rx.id AND rem.facility_id = 15
        WHERE rx.facility_id = 15
          AND rx.xml_type = 'RemittanceAdvice'
          AND rx.submission_date IS NULL
        GROUP BY rx.id, rx.source_filename, rx.fetched_at, rx.parse_status
        ORDER BY rx.fetched_at DESC
        LIMIT 20
    """))
    rows4 = r4.fetchall()
    print(f"Sample NULL-sub_date RA files for facility 15 ({len(rows4)} shown):")
    for row in rows4:
        print(f"  id={row[0]} | fetched={row[2]} | status={row[3]} | claims={row[4]}")
        print(f"    file: {row[1]}")
    print()

    # 5. Specifically: are there NULL-sub_date RA files fetched in June 2026?
    r5 = c.execute(text("""
        SELECT rx.id, rx.source_filename, rx.fetched_at, COUNT(rem.id) as claim_count
        FROM raw_xml_store rx
        LEFT JOIN remittances rem ON rem.raw_xml_id = rx.id AND rem.facility_id = 15
        WHERE rx.facility_id = 15
          AND rx.xml_type = 'RemittanceAdvice'
          AND rx.submission_date IS NULL
          AND rx.fetched_at >= '2026-06-01'
        GROUP BY rx.id, rx.source_filename, rx.fetched_at
        ORDER BY rx.fetched_at
        LIMIT 30
    """))
    rows5 = r5.fetchall()
    print(f"NULL-sub_date RA files fetched in June+ 2026 for facility 15 ({len(rows5)} rows):")
    for row in rows5:
        print(f"  id={row[0]} | fetched={row[2]} | claims={row[3]}")
        print(f"    file: {row[1]}")
