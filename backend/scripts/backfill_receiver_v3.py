"""
backfill_receiver_v3.py  —  Fix receiver_id by reading Header/ReceiverID from raw XML.

ROOT CAUSE:
  ReceiverID is in <Header>, not inside each <Claim> (per DHA eClaimLink XSD schema).
  Newer XML files include ReceiverID at claim level too, so the parser got those right.
  Older files only put it in Header → parser stored NULL for those claims.

  This script:
    1. Finds DISTINCT raw_xml_ids of claims with NULL receiver_id
       (optionally filtered by encounter_start month and/or facility)
    2. Loads each raw XML and reads Header/ReceiverID
    3. Updates claims.receiver_id where it is still NULL

Usage:
    cd ~/my_project/ghaf-app/backend

    # Dry run — show counts without updating
    .venv/bin/python3 scripts/backfill_receiver_v3.py --dry-run

    # Fix May 2026 encounter data (filtered by encounter_start)
    .venv/bin/python3 scripts/backfill_receiver_v3.py --month 2026-05

    # Fix a specific facility (all time)
    .venv/bin/python3 scripts/backfill_receiver_v3.py --facility 7

    # Fix everything (run overnight with caffeinate)
    .venv/bin/python3 scripts/backfill_receiver_v3.py
"""
from __future__ import annotations

import argparse
import re
import sys
import calendar
from datetime import date

from lxml import etree
from sqlalchemy import create_engine, text

DATABASE_URL = "postgresql+psycopg2://ghaf@/ghaf_app?host=/tmp"
engine = create_engine(DATABASE_URL, pool_pre_ping=True)

# ── namespace stripping (same as dhpo_parser) ─────────────────────────────────
NS_RE   = re.compile(r'\s+xmlns(?::\w+)?="[^"]*"')
NS_PFX  = re.compile(r'<(/?)\w+:(\w+)')
NS_ATTR = re.compile(r'''\s+\w+:\w+=(?:"[^"]*"|'[^']*')''')

def _strip_ns(xml_str: str) -> str:
    s = NS_RE.sub('', xml_str)
    s = NS_ATTR.sub('', s)
    s = NS_PFX.sub(r'<\1\2', s)
    return s


def _header_receiver_id(xml_content) -> str | None:
    """Extract Header/ReceiverID from a ClaimSubmission XML file."""
    if not xml_content:
        return None
    xml_str = (
        xml_content if isinstance(xml_content, str)
        else xml_content.tobytes().decode("utf-8", errors="replace")
    )
    try:
        clean = _strip_ns(xml_str)
        root = etree.fromstring(clean.encode("utf-8"))
        rid = (
            root.findtext("Header/ReceiverID")
            or root.findtext("Header/Receiver/ID")
            or ""
        ).strip()
        # Ignore facility DHA-F- license codes — not a valid receiver
        if rid and not rid.upper().startswith("DHA-F-"):
            return rid
    except Exception as e:
        print(f"  [WARN] XML parse error: {e}")
    return None


def run(facility_id: int | None, month: str | None, dry_run: bool) -> None:
    # Build WHERE clauses for the claims query (with alias c)
    select_filters = [
        "(c.receiver_id IS NULL OR c.receiver_id = '')",
        "c.raw_xml_id IS NOT NULL",
    ]
    # Separate UPDATE filters (no alias)
    update_filters = ["(receiver_id IS NULL OR receiver_id = '')"]
    params: dict = {}

    if facility_id:
        select_filters.append("c.facility_id = :fac_id")
        update_filters.append("facility_id = :fac_id")
        params["fac_id"] = facility_id

    if month:
        try:
            y, m = int(month[:4]), int(month[5:7])
            month_start = date(y, m, 1)
            # First day of next month
            next_m = m % 12 + 1
            next_y = y + (1 if m == 12 else 0)
            month_end_excl = date(next_y, next_m, 1)
            select_filters.append("c.encounter_start >= :es AND c.encounter_start < :ee")
            update_filters.append("encounter_start >= :es AND encounter_start < :ee")
            params["es"] = month_start
            params["ee"] = month_end_excl
        except Exception:
            print(f"ERROR: Invalid --month '{month}'. Use YYYY-MM format.")
            sys.exit(1)

    select_where = " AND ".join(select_filters)
    update_where = " AND ".join(update_filters)

    print("=" * 65)
    print(f"  Receiver ID Backfill v3  (Header/ReceiverID)")
    print(f"  Facility       : {facility_id or 'ALL'}")
    print(f"  Encounter month: {month or 'ALL'}")
    print(f"  Mode           : {'DRY RUN' if dry_run else 'LIVE'}")
    print("=" * 65)

    with engine.connect() as conn:
        null_count = conn.execute(text(
            f"SELECT COUNT(*) FROM claims c WHERE {select_where}"
        ), params).scalar()
        print(f"  NULL receiver claims : {null_count:,}")

        xml_id_rows = conn.execute(text(
            f"SELECT DISTINCT c.raw_xml_id FROM claims c WHERE {select_where}"
        ), params).fetchall()

    xml_ids = [r[0] for r in xml_id_rows]
    print(f"  Distinct XML files   : {len(xml_ids):,}")

    if not xml_ids:
        print("  Nothing to do.")
        return

    CHUNK = 100
    updated = 0
    skipped_no_rid = 0

    for chunk_start in range(0, len(xml_ids), CHUNK):
        chunk = xml_ids[chunk_start:chunk_start + CHUNK]

        with engine.connect() as conn:
            xml_data = conn.execute(text(
                "SELECT id, xml_type, xml_content FROM raw_xml_store WHERE id = ANY(:ids)"
            ), {"ids": chunk}).fetchall()

        xml_map = {r[0]: (r[1], r[2]) for r in xml_data}

        with engine.begin() as txn:
            for xml_id in chunk:
                xml_type, xml_content = xml_map.get(xml_id, (None, None))

                # Only ClaimSubmission files have Header/ReceiverID = payer
                # RemittanceAdvice files have Header/ReceiverID = facility (wrong)
                if xml_type and xml_type != 'ClaimSubmission':
                    skipped_no_rid += 1
                    continue

                rid = _header_receiver_id(xml_content)
                if not rid:
                    skipped_no_rid += 1
                    continue

                if dry_run:
                    cnt = txn.execute(text(
                        f"SELECT COUNT(*) FROM claims WHERE raw_xml_id=:xid AND {update_where}"
                    ), {**params, "xid": xml_id}).scalar() or 0
                    updated += cnt
                else:
                    result = txn.execute(text(
                        f"UPDATE claims SET receiver_id=:rid WHERE raw_xml_id=:xid AND {update_where}"
                    ), {**params, "rid": rid, "xid": xml_id})
                    updated += result.rowcount

        done = min(chunk_start + CHUNK, len(xml_ids))
        print(
            f"  {done:>6}/{len(xml_ids)} XML files | "
            f"claims {'would update' if dry_run else 'updated'}: {updated:,} | "
            f"skipped (non-sub or no-rid): {skipped_no_rid}",
            end="\r", flush=True
        )

    print()
    print("=" * 65)
    if dry_run:
        print(f"  DRY RUN — would update {updated:,} NULL-receiver claims")
        print(f"  Skipped {skipped_no_rid} XML files (RemittanceAdvice or no Header/ReceiverID)")
    else:
        print(f"  Done.  Updated {updated:,} claims.")
        print(f"  Skipped {skipped_no_rid} XML files (RemittanceAdvice or no Header/ReceiverID)")
    print("=" * 65)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill receiver_id from Header/ReceiverID")
    parser.add_argument("--facility", type=int, help="Limit to one facility DB id")
    parser.add_argument("--month",    type=str, help="Limit to encounter month YYYY-MM (filters by encounter_start)")
    parser.add_argument("--dry-run",  action="store_true", help="Show counts without updating")
    args = parser.parse_args()
    run(args.facility, args.month, args.dry_run)
