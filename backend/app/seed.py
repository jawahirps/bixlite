"""
Ghaf App — first-run seed data.

Creates a default facility if the database is empty, and a first admin user
only when SEED_ADMIN_EMAIL and SEED_ADMIN_PASSWORD are set in the environment
(nothing is created otherwise; credentials never live in code or logs).

Also seeds the initial DHA Exclusion 1 & 2 code reference list if the
exclusion_codes table is empty.
"""
import logging
import os

from sqlalchemy.orm import Session

from .models import ExclusionCode, Facility, User
from .security import hash_password

log = logging.getLogger(__name__)



def run_seed(db: Session) -> None:
    facility = db.query(Facility).first()
    if facility is None:
        facility = Facility(
            facility_code="GHAF-001",
            facility_name="Ghaf App Demo Clinic",
            license_id="DHA-LIC-0001",
            eclaimlink_username="demo_user",
            credential_key="demo_credential",
            dhpo_provider_id="PR0001",
            is_active=True,
        )
        db.add(facility)
        db.commit()
        db.refresh(facility)
        log.info("Seeded default facility %s", facility.facility_code)

    admin_email = os.getenv("SEED_ADMIN_EMAIL", "").strip()
    admin_password = os.getenv("SEED_ADMIN_PASSWORD", "")
    if admin_email and admin_password:
        admin = db.query(User).filter(User.email == admin_email).first()
        if admin is None:
            admin = User(
                facility_id=facility.id,
                email=admin_email,
                full_name="Administrator",
                hashed_password=hash_password(admin_password),
                role="admin",
                is_active=True,
            )
            db.add(admin)
            db.commit()
            log.warning("Seeded admin user %s from SEED_ADMIN_* env vars.", admin_email)

    _seed_exclusion_codes(db)


# ---------------------------------------------------------------------------
# DHA Exclusion Code seed
# ---------------------------------------------------------------------------

# Starter list — operators should add to this via the Scrubber UI.
# Format: (exclusion_type, code_type, code, description)
_EXCLUSION_SEED: list[tuple[int, str, str, str]] = [
    # ---------- Exclude 1 (absolute — ERROR) ----------
    # Cosmetic / elective procedures not covered by DHA
    (1, "activity", "15820", "Blepharoplasty — cosmetic (Exclude 1)"),
    (1, "activity", "15823", "Blepharoplasty upper eyelid — cosmetic (Exclude 1)"),
    (1, "activity", "17380", "Electrolysis epilation (Exclude 1)"),
    (1, "activity", "19318", "Reduction mammaplasty (Exclude 1)"),
    (1, "activity", "30400", "Rhinoplasty — cosmetic (Exclude 1)"),

    # Dental exclusions (basic scaling/cleaning not covered)
    (1, "activity", "D1110", "Prophylaxis — adult dental cleaning (Exclude 1)"),
    (1, "activity", "D1120", "Prophylaxis — child dental cleaning (Exclude 1)"),

    # IVF / fertility — not routinely covered
    (1, "activity", "58970", "Follicle puncture for oocyte retrieval (Exclude 1)"),
    (1, "activity", "58974", "Embryo transfer into uterus (Exclude 1)"),

    # ---------- Exclude 2 (conditional — WARN) ----------
    # Requires prior auth / specific plan inclusion
    (2, "activity", "27447", "Total knee arthroplasty (Exclude 2 — prior auth required)"),
    (2, "activity", "27130", "Total hip arthroplasty (Exclude 2 — prior auth required)"),
    (2, "activity", "43239", "Upper GI endoscopy with biopsy (Exclude 2 — auth required)"),
    (2, "activity", "45378", "Colonoscopy diagnostic (Exclude 2 — auth required)"),
    (2, "activity", "70553", "MRI brain w/ and w/o contrast (Exclude 2 — auth required)"),
    (2, "activity", "72148", "MRI lumbar spine without contrast (Exclude 2 — auth required)"),

    # Diagnosis codes that flag conditional review
    (2, "diagnosis", "Z41.1",  "Cosmetic surgery encounter (Exclude 2 — review required)"),
    (2, "diagnosis", "Z41.8",  "Encounter for other proc for purposes other than remedying health state (Exclude 2)"),
]


def _seed_exclusion_codes(db: Session) -> None:
    existing = db.query(ExclusionCode).first()
    if existing is not None:
        return  # Already seeded — do not overwrite

    count = 0
    for etype, ctype, code, desc in _EXCLUSION_SEED:
        db.add(ExclusionCode(
            exclusion_type=etype,
            code_type=ctype,
            code=code,
            description=desc,
            is_active=True,
        ))
        count += 1

    db.commit()
    log.info("Seeded %d DHA exclusion codes.", count)
