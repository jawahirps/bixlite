from app.config import get_settings
from sqlalchemy import create_engine, text

e = create_engine(get_settings().DATABASE_URL)
ids = ['OPC-R-MH26-028123','OPC-R-MH26-028165','OPC-R-MH26-028832',
       'OPC-R-MH26-028834','OPC-R-MH26-028836','OPC-R-MH26-028940',
       'OPC-R-MH26-029002','OPC-R-MH26-028465','OPC-R-MH26-028487']

with e.connect() as c:
    fac = c.execute(text("SELECT id, facility_code, facility_name FROM facilities WHERE facility_code LIKE '%0046977%' OR facility_name ILIKE '%lotus%'")).fetchall()
    print('New Lotus facility:', fac)
    fid = fac[0][0] if fac else None

    r = c.execute(text("SELECT claim_id, facility_id FROM claims WHERE claim_id = ANY(:ids)"), {'ids': ids})
    rows = r.fetchall()
    print(f'\nIn claims table: {len(rows)}/{len(ids)}')
    for row in rows: print(' ', row)

    r2 = c.execute(text("SELECT claim_id, facility_id, paid_amount FROM remittances WHERE claim_id = ANY(:ids)"), {'ids': ids})
    rows2 = r2.fetchall()
    print(f'\nIn remittances table: {len(rows2)}/{len(ids)}')
    for row in rows2: print(' ', row)

    if fid:
        r3 = c.execute(text("SELECT COUNT(*) FROM claims WHERE facility_id=:fid AND claim_id LIKE 'OPC-R-MH26%'"), {'fid': fid})
        print(f'\nTotal OPC-R-MH26 claims for New Lotus (fid={fid}):', r3.scalar())
        r4 = c.execute(text("SELECT COUNT(*) FROM remittances WHERE facility_id=:fid AND claim_id LIKE 'OPC-R-MH26%'"), {'fid': fid})
        print(f'Total OPC-R-MH26 remittances for New Lotus (fid={fid}):', r4.scalar())
