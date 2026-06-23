"""
Recovery: restore initial submission rows that were deleted when resubmissions
were parsed (parser used to DELETE the existing claim before inserting the
resubmission, losing the initial net_amount and submission_date).

For every claim currently stored as a resubmission (resubmission_key != ''),
this script searches ALL raw_xml_store ClaimSubmission records for a <Claim>
element with the same ID that has NO <Resubmission> child — that is the
initial submission. It inserts it as a new row with resubmission_key=''.

Run AFTER migrate_resubmission_key.py:
    cd ~/my_project/ghaf-app/backend
    .venv/bin/python3 scripts/recover_initial_submissions.py
"""
import sys
import os
import re
from io import BytesIO
from datetime import datetime, date
from decimal import Decimal, InvalidOperation
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lxml import etree
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

DATABASE_URL = "postgresql+psycopg2://ghaf@/ghaf_app?host=/tmp"
engine = create_engine(DATABASE_URL, pool_pre_ping=True)

# ── helpers ────────────────────────────────────────────────────────────────────
NS_RE   = re.compile(r'\s+xmlns(?::\w+)?="[^"]*"')
NS_PFX  = re.compile(r'<(/?)\w+:(\w+)')
NS_ATTR = re.compile(r'''\s+\w+:\w+=(?:"[^"]*"|'[^']*')''')

def _strip_ns(xml_str: str) -> str:
    s = NS_RE.sub('', xml_str)
    s = NS_ATTR.sub('', s)
    return NS_PFX.sub(r'<\1\2', s)

def _parse_date(val: str | None) -> date | None:
    if not val:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%d/%m/%Y %H:%M:%S",
                "%d/%m/%Y %H:%M", "%d/%m/%Y"):
        try:
            return datetime.strptime(val.strip(), fmt).date()
        except ValueError:
            continue
    return None

def _parse_dec(val: str | None, default: str = "0") -> Decimal:
    try:
        return Decimal(val.strip()) if val else Decimal(default)
    except (InvalidOperation, AttributeError):
        return Decimal(default)

_ACTIVITY_TYPE_MAP = {
    "1": "CPT", "2": "HCPCS", "3": "Dental", "4": "Drug", "5": "Service",
    "CPT": "CPT", "HCPCS": "HCPCS", "Dental": "Dental",
    "Drug": "Drug", "Service": "Service",
}
_DIAG_TYPE_MAP = {
    "Principal": "Primary", "primary": "Primary", "Primary": "Primary",
    "Admitting": "Admitting", "admitting": "Admitting",
    "Secondary": "Secondary", "secondary": "Secondary",
}

# ── load resubmission claim_ids we need to recover ─────────────────────────────
with engine.connect() as conn:
    resub_rows = conn.execute(text("""
        SELECT facility_id, claim_id
        FROM claims
        WHERE resubmission_key != ''
        ORDER BY facility_id, claim_id
    """)).fetchall()

# Group by facility_id
needed: dict[int, set[str]] = defaultdict(set)
for fac_id, cid in resub_rows:
    needed[fac_id].add(cid)

total_needed = sum(len(v) for v in needed.values())
print(f"Resubmission claims needing initial row recovery: {total_needed}")

if total_needed == 0:
    print("Nothing to do.")
    sys.exit(0)

# ── already recovered? ─────────────────────────────────────────────────────────
with engine.connect() as conn:
    already = conn.execute(text("""
        SELECT facility_id, claim_id
        FROM claims
        WHERE resubmission_key = ''
          AND (facility_id, claim_id) IN (
              SELECT facility_id, claim_id FROM claims WHERE resubmission_key != ''
          )
    """)).fetchall()
already_set = set(already)
print(f"Already have initial rows for: {len(already_set)}")

# ── scan raw_xml_store for initial submission elements ─────────────────────────
BATCH = 20   # raw_xml_store records per chunk (XMLs can be large)
inserted = 0
skipped  = 0

with engine.connect() as conn:
    # Get all ClaimSubmission raw_xml_store IDs per facility
    xml_records = conn.execute(text("""
        SELECT id, facility_id, submission_date
        FROM raw_xml_store
        WHERE xml_type = 'ClaimSubmission'
          AND parse_status = 'parsed'
        ORDER BY facility_id, id
    """)).fetchall()

print(f"Scanning {len(xml_records)} ClaimSubmission XML records...")

for chunk_start in range(0, len(xml_records), BATCH):
    chunk = xml_records[chunk_start:chunk_start + BATCH]
    xml_ids = [r[0] for r in chunk]
    fac_map  = {r[0]: (r[1], r[2]) for r in chunk}  # id → (facility_id, sub_date)

    with engine.connect() as conn:
        xml_rows = conn.execute(text("""
            SELECT id, xml_content FROM raw_xml_store WHERE id = ANY(:ids)
        """), {"ids": xml_ids}).fetchall()

    for xml_id, xml_content in xml_rows:
        facility_id, sub_date = fac_map[xml_id]
        fac_needed = needed.get(facility_id, set())
        if not fac_needed:
            continue

        if not xml_content:
            continue
        xml_str = (xml_content if isinstance(xml_content, str)
                   else xml_content.tobytes().decode("utf-8", errors="replace"))
        clean = _strip_ns(xml_str)

        try:
            context = etree.iterparse(
                BytesIO(clean.encode("utf-8")),
                events=("end",), tag="Claim", recover=True,
            )
            for _ev, elem in context:
                claim_id = (elem.findtext("ID") or "").strip()

                # Only process claim_ids we need and that are NOT already recovered
                if claim_id not in fac_needed:
                    elem.clear(); continue
                if (facility_id, claim_id) in already_set:
                    elem.clear(); continue

                # Skip if this element IS a resubmission
                if elem.find("Resubmission") is not None:
                    elem.clear(); continue

                # This is the initial submission element — insert it
                encounter_id = elem.findtext("Encounter/ID") or f"ACT-{claim_id}"
                rec_id = (elem.findtext("ReceiverID") or "").strip()
                if rec_id.upper().startswith("DHA-F-"):
                    rec_id = None

                row_sub_date = _parse_date(elem.findtext("Encounter/Start")) or sub_date or date.today()

                with engine.begin() as txn:
                    txn.execute(text("""
                        INSERT INTO claims (
                            facility_id, raw_xml_id, source_channel, claim_id,
                            encounter_id, encounter_type,
                            encounter_start, encounter_end,
                            member_id, emirates_id, emirates_id_type,
                            payer_id, payer_name, clinician_id,
                            receiver_id,
                            gross_amount, net_amount, patient_share_total,
                            submission_date, claim_status,
                            resubmission_type, resubmission_key,
                            created_at, updated_at
                        ) VALUES (
                            :facility_id, :raw_xml_id, 'API', :claim_id,
                            :encounter_id, :encounter_type,
                            :encounter_start, :encounter_end,
                            :member_id, :emirates_id, :emirates_id_type,
                            :payer_id, NULL, :clinician_id,
                            :receiver_id,
                            :gross_amount, :net_amount, :patient_share_total,
                            :submission_date, 'submitted',
                            NULL, '',
                            now(), now()
                        )
                        ON CONFLICT (facility_id, claim_id, resubmission_key) DO NOTHING
                    """), {
                        "facility_id":      facility_id,
                        "raw_xml_id":       xml_id,
                        "claim_id":         claim_id,
                        "encounter_id":     encounter_id,
                        "encounter_type":   elem.findtext("Encounter/Type"),
                        "encounter_start":  _parse_date(elem.findtext("Encounter/Start")),
                        "encounter_end":    _parse_date(elem.findtext("Encounter/End")),
                        "member_id":        elem.findtext("Patient/MemberID") or elem.findtext("MemberID") or "UNKNOWN",
                        "emirates_id":      elem.findtext("Patient/EmiratesID"),
                        "emirates_id_type": None,
                        "payer_id":         elem.findtext("PayerID") or "UNKNOWN",
                        "clinician_id":     (elem.findtext("ClinicianID") or
                                             elem.findtext("Activity/Clinician") or "UNKNOWN"),
                        "receiver_id":      rec_id or None,
                        "gross_amount":     _parse_dec(elem.findtext("Gross")),
                        "net_amount":       _parse_dec(elem.findtext("Net")),
                        "patient_share_total": _parse_dec(elem.findtext("PatientShare")),
                        "submission_date":  row_sub_date,
                    })
                inserted += 1
                already_set.add((facility_id, claim_id))  # mark as recovered
                elem.clear()
        except Exception as e:
            print(f"  [WARN] xml_id={xml_id}: {e}")

    done = min(chunk_start + BATCH, len(xml_records))
    if done % 200 == 0 or done == len(xml_records):
        print(f"  Scanned {done}/{len(xml_records)} XML records — recovered {inserted} initial submissions")

print(f"\nDone. Inserted {inserted} initial submission rows.")
remaining = total_needed - len(already_set)
if remaining > 0:
    print(f"  {remaining} claims could not be recovered (initial XML not found in raw_xml_store).")
