"""
Checks Marina Medical Center Riayati credentials in the DB
and attempts a live connection test to the Riayati TMB API.

Run from backend/ dir:
    .venv/bin/python3 check_marina_riayati.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv(".env")

from app.database import SessionLocal
from app.models import Facility
from app.services.riayati_service import RiayatiClient, RiayatiInactiveFacilityError
from datetime import date

db = SessionLocal()
facs = (
    db.query(Facility)
    .filter(Facility.facility_code.like("MARINA%"))
    .order_by(Facility.id)
    .all()
)

riayati_facs = [f for f in facs if f.riayati_username]
print(f"Found {len(riayati_facs)} Marina Riayati-enabled facility/facilities.\n")

for f in riayati_facs:
    print(f"{'='*60}")
    print(f"Facility : {f.facility_name}")
    print(f"Code     : {f.facility_code}  (DB id={f.id})")
    print(f"Username : {f.riayati_username!r}")
    pw = (f.riayati_password or "").strip()
    if pw:
        print(f"Password : SET ({len(pw)} chars)")
    else:
        print(f"Password : *** MISSING / EMPTY — cannot connect ***")
        print()
        continue

    # Attempt a small date-window search to test connectivity
    print("\nTesting live connection to tmbapi.riayati.ae…")
    try:
        client = RiayatiClient(f.riayati_username, pw, timeout=15)
        today = date.today()
        # One-day window to minimise data returned
        entities = client.search(today, today, direction=0, downloaded=2)
        print(f"  Connection OK — {len(entities)} record(s) returned for today")
    except RiayatiInactiveFacilityError as exc:
        print(f"  INACTIVE: credentials accepted but facility not reactivated.")
        print(f"  Action  : Email RiayatiPO@mohap.gov.ae to reactivate license {f.license_id!r}")
        print(f"  Detail  : {exc}")
    except Exception as exc:
        print(f"  ERROR   : {type(exc).__name__}: {exc}")
    print()

db.close()
