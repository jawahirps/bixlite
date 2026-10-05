from app.config import get_settings
from sqlalchemy import create_engine, text

e = create_engine(get_settings().DATABASE_URL)

# The 49 claim IDs from the missing RA file RA_2026Jun23101854
missing_ids = [
    'OPC-R-MH26-028123','OPC-R-MH26-028165','OPC-R-MH26-028166','OPC-R-MH26-028832',
    'OPC-R-MH26-028834','OPC-R-MH26-028836','OPC-R-MH26-028837','OPC-R-MH26-028838',
    'OPC-R-MH26-028850','OPC-R-MH26-028882','OPC-R-MH26-028887','OPC-R-MH26-028888',
    'OPC-R-MH26-028940','OPC-R-MH26-028944','OPC-R-MH26-028954','OPC-R-MH26-028959',
    'OPC-R-MH26-028960','OPC-R-MH26-028961','OPC-R-MH26-028963','OPC-R-MH26-028978',
]

with e.connect() as c:
    # Check encounter dates for these claims
    r = c.execute(text("""
        SELECT cl.claim_id, cl.encounter_start, cl.encounter_end, r.remittance_date, r.paid_amount
        FROM claims cl
        LEFT JOIN remittances r ON cl.claim_id = r.claim_id AND cl.facility_id = r.facility_id
        WHERE cl.claim_id = ANY(:ids) AND cl.facility_id = 15
        LIMIT 20
    """), {'ids': missing_ids})
    rows = r.fetchall()
    print(f"Sample of missing claims with encounter dates ({len(rows)} rows):")
    for row in rows:
        print(f"  claim={row[0]} | enc_start={row[1]} | enc_end={row[2]} | ra_date={row[3]} | paid={row[4]}")

    # What date range does the app report use?
    # Check what the app report query filter looks like - look at the report for June 2026
    r2 = c.execute(text("""
        SELECT MIN(encounter_start), MAX(encounter_start), COUNT(*)
        FROM claims WHERE facility_id=15
    """))
    row2 = r2.fetchone()
    print(f"\nAll claims for New Lotus: enc_start range {row2[0]} to {row2[1]}, total={row2[2]}")

    r3 = c.execute(text("""
        SELECT MIN(remittance_date), MAX(remittance_date), COUNT(*)
        FROM remittances WHERE facility_id=15
    """))
    row3 = r3.fetchone()
    print(f"All remittances for New Lotus: ra_date range {row3[0]} to {row3[1]}, total={row3[2]}")
