#!/usr/bin/env python3.12
"""
add_marina_facility.py
Inserts two Marina Medical Center facility records:
  MARINA-DHA  — eClaimLink / DHA patients
  MARINA-SHA  — Riayati / SHA patients
Passwords are entered via getpass — never appear in logs or this file.
Run from backend/ with the venv active:
  python3.12 scripts/add_marina_facility.py
"""
import sys, getpass
sys.path.insert(0, ".")

from app.database import SessionLocal
from app.models import Facility

db = SessionLocal()

print("=" * 58)
print(" Add Marina Medical Center LLC — DHA + SHA")
print("=" * 58)
print()

# ── Record 1: MARINA-DHA (eClaimLink) ─────────────────────
print(">>> Record 1: MARINA-DHA  (eClaimLink / DHA patients)")
print("    eclaimlink_username : MARINA MC")
print("    license_id          : MOH-F-1000070")
dha_pwd = getpass.getpass("    Enter eClaimLink password: ")
if not dha_pwd.strip():
    print("ERROR: eClaimLink password cannot be empty.")
    sys.exit(1)

# ── Record 2: MARINA-SHA (Riayati) ────────────────────────
print()
print(">>> Record 2: MARINA-SHA  (Riayati / SHA patients)")
riayati_user = input("    Enter Riayati username (facility ID): ").strip()
riayati_pwd  = getpass.getpass("    Enter Riayati API key (password): ")
if not riayati_user or not riayati_pwd.strip():
    print("ERROR: Riayati credentials cannot be empty.")
    sys.exit(1)

print()

# ── Check for existing records ────────────────────────────
existing_dha = db.query(Facility).filter_by(facility_code="MARINA-DHA").first()
existing_sha = db.query(Facility).filter_by(facility_code="MARINA-SHA").first()

if existing_dha:
    print(f"⚠  MARINA-DHA already exists (id={existing_dha.id}) — skipping")
else:
    fac_dha = Facility(
        facility_code      = "MARINA-DHA",
        facility_name      = "Marina Medical Center LLC (DHA)",
        license_id         = "MOH-F-1000070",
        eclaimlink_username= "MARINA MC",
        credential_key     = dha_pwd.strip(),
        dhpo_provider_id   = None,
        riayati_username   = None,
        riayati_password   = None,
        is_active          = True,
    )
    db.add(fac_dha)
    db.flush()
    print(f"✓  MARINA-DHA inserted — id={fac_dha.id}")

if existing_sha:
    print(f"⚠  MARINA-SHA already exists (id={existing_sha.id}) — skipping")
else:
    fac_sha = Facility(
        facility_code      = "MARINA-SHA",
        facility_name      = "Marina Medical Center LLC (SHA)",
        license_id         = "MOH-F-1000070",
        eclaimlink_username= "N/A",          # skipped in DHA fetches
        credential_key     = "N/A",
        dhpo_provider_id   = None,
        riayati_username   = riayati_user,
        riayati_password   = riayati_pwd.strip(),
        is_active          = True,
    )
    db.add(fac_sha)
    db.flush()
    print(f"✓  MARINA-SHA inserted — id={fac_sha.id}")

db.commit()
print()
print("Note the IDs above — you'll need them for the bulk fetch scripts.")
db.close()
