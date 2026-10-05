"""
check_gap7.py — Diagnose the 409-claim residual gap for facility 15 (New Lotus).

Questions:
1. What is the date range of ALL RA files we have for facility 15?
2. How many RA files per month (to spot missing months)?
3. Are there remittances with NULL remittance_date for facility 15?
4. How are the 1,232 June-settled claims distributed across RA file dates?
5. What months' RA files contain those June-settled claims?
"""
import sys
sys.path.insert(0, '/Users/lisnasaleena/my_project/ghaf-app/backend')

from app.config import get_settings
from sqlalchemy import create_engine, text

e = create_engine(get_settings().DATABASE_URL)

with e.connect() as c:
    print("=" * 65)
    print("FACILITY 15 (NEW LOTUS) — RA FILE COVERAGE DIAGNOSTIC")
    print("=" * 65)

    # 1. Date range of RA files for facility 15
    r = c.execute(text("""
        SELECT
            MIN(submission_date) AS earliest,
            MAX(submission_date) AS latest,
            COUNT(*) AS total_ra_files,
            COUNT(*) FILTER (WHERE parse_status='parsed') AS parsed,
            COUNT(*) FILTER (WHERE parse_status='pending') AS pending,
            COUNT(*) FILTER (WHERE parse_status='failed') AS failed
        FROM raw_xml_store
        WHERE facility_id = 15 AND xml_type = 'RemittanceAdvice'
    """))
    row = r.fetchone()
    print(f"\n1. RA FILE DATE RANGE FOR FACILITY 15")
    print(f"   Earliest RA file: {row.earliest}")
    print(f"   Latest RA file:   {row.latest}")
    print(f"   Total RA files:   {row.total_ra_files}")
    print(f"   Parsed:           {row.parsed}")
    print(f"   Pending:          {row.pending}")
    print(f"   Failed:           {row.failed}")

    # 2. Monthly RA file count (last 12 months)
    r = c.execute(text("""
        SELECT
            TO_CHAR(submission_date, 'YYYY-MM') AS month,
            COUNT(*) AS file_count,
            SUM(COALESCE(records_parsed, 0)) AS records_parsed
        FROM raw_xml_store
        WHERE facility_id = 15
          AND xml_type = 'RemittanceAdvice'
          AND submission_date >= '2025-07-01'
        GROUP BY 1
        ORDER BY 1
    """))
    print(f"\n2. MONTHLY RA FILE COUNT (last 12 months)")
    print(f"   {'Month':<12} {'Files':>6} {'Records':>10}")
    print(f"   {'-'*12} {'-'*6} {'-'*10}")
    for row in r.fetchall():
        print(f"   {row.month:<12} {row.file_count:>6} {row.records_parsed:>10}")

    # 3. Remittances with NULL remittance_date for facility 15
    r = c.execute(text("""
        SELECT COUNT(*) AS null_date_count
        FROM remittances
        WHERE facility_id = 15 AND remittance_date IS NULL
    """))
    null_count = r.scalar()
    print(f"\n3. REMITTANCES WITH NULL remittance_date: {null_count:,}")

    # 4. Distribution of June-settled claims by RA file month
    r = c.execute(text("""
        SELECT
            TO_CHAR(rx.submission_date, 'YYYY-MM') AS ra_file_month,
            COUNT(DISTINCT rem.id) AS claim_count
        FROM remittances rem
        LEFT JOIN raw_xml_store rx ON rx.id = rem.raw_xml_id
        WHERE rem.facility_id = 15
          AND rem.remittance_date BETWEEN '2026-06-01' AND '2026-06-30'
        GROUP BY 1
        ORDER BY 1
    """))
    print(f"\n4. JUNE-SETTLED CLAIMS BY RA FILE DATE")
    print(f"   (Where are the 1,232 claims' RA files dated?)")
    print(f"   {'RA File Month':<15} {'Claims':>8}")
    print(f"   {'-'*15} {'-'*8}")
    total = 0
    for row in r.fetchall():
        month = row.ra_file_month or 'NULL (no RA file)'
        print(f"   {month:<15} {row.claim_count:>8}")
        total += row.claim_count
    print(f"   {'TOTAL':<15} {total:>8}")

    # 5. Check if there are LARGE RA files (records_parsed > 0) we might have missed
    r = c.execute(text("""
        SELECT
            submission_date,
            source_filename,
            records_parsed,
            parse_status
        FROM raw_xml_store
        WHERE facility_id = 15
          AND xml_type = 'RemittanceAdvice'
          AND submission_date BETWEEN '2026-05-01' AND '2026-07-10'
        ORDER BY submission_date DESC, records_parsed DESC
        LIMIT 30
    """))
    print(f"\n5. RECENT RA FILES (May-Jul 2026) for facility 15")
    print(f"   {'Date':<12} {'Records':>8} {'Status':<10} {'Filename'}")
    print(f"   {'-'*12} {'-'*8} {'-'*10} {'-'*50}")
    for row in r.fetchall():
        fname = (row.source_filename or '')[:60]
        print(f"   {str(row.submission_date):<12} {(row.records_parsed or 0):>8} {row.parse_status:<10} {fname}")

    # 6. How many total remittances for facility 15 in DB?
    r = c.execute(text("""
        SELECT COUNT(*) FROM remittances WHERE facility_id = 15
    """))
    print(f"\n6. TOTAL REMITTANCES IN DB FOR FACILITY 15: {r.scalar():,}")

print()
