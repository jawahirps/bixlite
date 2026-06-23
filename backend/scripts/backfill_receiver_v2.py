"""
Improved receiver_id backfill — parses each <Claim> element individually
using lxml (same approach as dhpo_parser.py), so the ReceiverID is extracted
from the EXACT <Claim> matching that claim's ID rather than the first
<ReceiverID> found anywhere in the batch XML.

Targets:
  - receiver_id IS NULL or DHA-F-*  (never got a valid receiver)
  - receiver_id = payer_id           (fell back to payer — likely wrong for TPA claims)

Groups claims by raw_xml_id so each batch XML is parsed only once.

Run after backfill_receiver.py (this replaces / improves on it):
    cd ~/my_project/ghaf-app/backend
    .venv/bin/python3 scripts/backfill_receiver_v2.py
"""
import re
import sys
from io import BytesIO
from collections import defaultdict

from lxml import etree
from sqlalchemy import create_engine, text

DATABASE_URL = "postgresql+psycopg2://ghaf@/ghaf_app?host=/tmp"
engine = create_engine(DATABASE_URL, pool_pre_ping=True)

NS_RE   = re.compile(r'\s+xmlns(?::\w+)?="[^"]*"')
NS_PFX  = re.compile(r'<(/?)\w+:(\w+)')
NS_ATTR = re.compile(r'''\s+\w+:\w+=(?:"[^"]*"|'[^']*')''')

def _strip_ns(xml_str: str) -> str:
    s = NS_RE.sub('', xml_str)
    s = NS_ATTR.sub('', s)
    s = NS_PFX.sub(r'<\1\2', s)
    return s


def _extract_receivers_from_xml(xml_content) -> dict[str, str]:
    """
    Parse batch XML and return {claim_id: receiver_id} for every <Claim>
    that has a non-empty <ReceiverID> that is NOT a facility DHA-F- license.
    """
    if not xml_content:
        return {}
    xml_str = (
        xml_content if isinstance(xml_content, str)
        else xml_content.tobytes().decode("utf-8", errors="replace")
    )
    clean = _strip_ns(xml_str)
    result: dict[str, str] = {}
    try:
        context = etree.iterparse(
            BytesIO(clean.encode("utf-8")),
            events=("end",),
            tag="Claim",
            recover=True,
        )
        for _event, elem in context:
            claim_id    = (elem.findtext("ID") or "").strip()
            receiver_id = (elem.findtext("ReceiverID") or "").strip()
            if claim_id and receiver_id and not receiver_id.upper().startswith("DHA-F-"):
                result[claim_id] = receiver_id
            elem.clear()
    except Exception as e:
        print(f"  [WARN] XML parse error: {e}")
    return result


with engine.connect() as conn:
    # Count claims to process:
    # 1. receiver_id is NULL or DHA-F-
    # 2. receiver_id == payer_id (fell back — could be wrong TPA mapping)
    total = conn.execute(text("""
        SELECT COUNT(*)
        FROM claims c
        WHERE c.raw_xml_id IS NOT NULL
          AND (
              c.receiver_id IS NULL
              OR c.receiver_id = ''
              OR c.receiver_id ILIKE 'DHA-F-%%'
              OR c.receiver_id = c.payer_id
          )
    """)).scalar()
    print(f"Claims to re-check: {total}")

    # Load all claims to process, grouped by raw_xml_id
    # We process one batch XML at a time so we only parse each file once
    rows = conn.execute(text("""
        SELECT c.id, c.claim_id, c.payer_id, c.receiver_id, c.raw_xml_id
        FROM claims c
        WHERE c.raw_xml_id IS NOT NULL
          AND (
              c.receiver_id IS NULL
              OR c.receiver_id = ''
              OR c.receiver_id ILIKE 'DHA-F-%%'
              OR c.receiver_id = c.payer_id
          )
        ORDER BY c.raw_xml_id, c.id
    """)).fetchall()

print(f"Loaded {len(rows)} claim rows.")

# Group by raw_xml_id
by_xml: dict[int, list] = defaultdict(list)
for r in rows:
    by_xml[r[4]].append(r)   # r[4] = raw_xml_id

updated  = 0
skipped  = 0
xml_ids  = list(by_xml.keys())
total_xml = len(xml_ids)

CHUNK = 50   # parse 50 XML batches at a time

for chunk_start in range(0, total_xml, CHUNK):
    chunk_ids = xml_ids[chunk_start:chunk_start + CHUNK]

    with engine.connect() as conn:
        xml_rows = conn.execute(text("""
            SELECT id, xml_content
            FROM raw_xml_store
            WHERE id = ANY(:ids)
        """), {"ids": chunk_ids}).fetchall()

    xml_map = {r[0]: r[1] for r in xml_rows}

    with engine.begin() as txn:
        for xml_id in chunk_ids:
            xml_content = xml_map.get(xml_id)
            receivers   = _extract_receivers_from_xml(xml_content)

            for (pk, claim_id, payer_id, old_receiver, _raw_xml_id) in by_xml[xml_id]:
                new_receiver = receivers.get(claim_id)
                if not new_receiver:
                    skipped += 1
                    continue

                # Only update if it changes the value and is genuinely different from payer
                # (if XML says receiver == payer it might still be correct for direct claims)
                if new_receiver == (old_receiver or ""):
                    skipped += 1
                    continue

                txn.execute(text(
                    "UPDATE claims SET receiver_id = :rid WHERE id = :id"
                ), {"rid": new_receiver, "id": pk})
                updated += 1

    done = min(chunk_start + CHUNK, total_xml)
    print(f"  XML batches {done}/{total_xml} — updated so far: {updated}, skipped: {skipped}")

print(f"\nDone.")
print(f"  Updated : {updated}")
print(f"  Skipped : {skipped}  (XML had no valid ReceiverID for those claims)")
print(f"\nRun the backend and regenerate reports to verify.")
