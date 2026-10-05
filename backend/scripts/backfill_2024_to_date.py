#!/usr/bin/env python3
"""
backfill_2024_to_date.py — Full historical fetch from 2024-01-01 → today
=========================================================================
Fetches ALL ClaimSubmission + RemittanceAdvice files from DHA eClaimLink
for every active non-demo facility, starting from 2024-01-01.

Why this is needed:
  The initial fetch only went back to May 2026.  RemittanceAdvice files
  received in June 2026 contain claims originally submitted in Mar/Apr 2026
  (and earlier).  Because those ClaimSubmission files were never fetched, the
  `claims` table has no record of them, so the remittance report JOIN fails.

  Fetching from 2024-01-01 ensures the `claims` table is fully populated for
  all remittances we have stored.

Notes:
  • Already-stored files are skipped via source_filename dedup — safe to re-run.
  • ClaimSubmission > 24 months automatically uses the Archive endpoint.
  • Date window is split into monthly chunks to avoid hitting the DHPO 500-result cap.

Usage:
    python3.12 scripts/backfill_2024_to_date.py
    python3.12 scripts/backfill_2024_to_date.py --facility 15   # New Lotus only
    python3.12 scripts/backfill_2024_to_date.py --dry-run
    python3.12 scripts/backfill_2024_to_date.py --from 2025-01-01  # custom start
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, timedelta
from pathlib import Path
try:
    from dateutil.relativedelta import relativedelta
    _HAS_DATEUTIL = True
except ImportError:
    _HAS_DATEUTIL = False

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.services.fetch_service import DHASyncService

SKIP_CODES     = {"GHAF-001"}
SKIP_DHA_USERS = {"", "N/A", "demo_user"}
DEFAULT_START  = date(2024, 1, 1)

log = logging.getLogger("backfill")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)


def _add_one_month(d: date) -> date:
    """Add one calendar month to d."""
    if _HAS_DATEUTIL:
        from dateutil.relativedelta import relativedelta
        return d + relativedelta(months=1)
    # Fallback: advance month manually
    month = d.month + 1
    year  = d.year + (month - 1) // 12
    month = ((month - 1) % 12) + 1
    return d.replace(year=year, month=month, day=1)


def month_windows(date_from: date, date_to: date):
    """Yield (start, end) monthly chunks from date_from to date_to."""
    cur = date_from.replace(day=1)
    while cur <= date_to:
        next_month = _add_one_month(cur)
        chunk_end  = min(next_month - timedelta(days=1), date_to)
        yield cur, chunk_end
        cur = next_month


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--from",          dest="date_from", default=str(DEFAULT_START),
                        help=f"Start date YYYY-MM-DD (default: {DEFAULT_START})")
    parser.add_argument("--facility",      type=int,  help="Single facility ID (default: all)")
    parser.add_argument("--skip-facility", type=int,  action="append", dest="skip_facility_ids",
                        metavar="ID",      help="Skip facility by DB id (repeatable)")
    parser.add_argument("--dry-run",       action="store_true")
    args = parser.parse_args()

    date_from = date.fromisoformat(args.date_from)
    date_to   = date.today()

    log.info("=" * 65)
    log.info("Historical backfill  %s → %s", date_from, date_to)
    log.info("=" * 65)

    settings = get_settings()
    engine   = create_engine(settings.DATABASE_URL)

    # Load facilities
    with engine.connect() as conn:
        if args.facility:
            rows = conn.execute(text(
                "SELECT id, facility_code, facility_name, "
                "COALESCE(eclaimlink_username,'') AS dha_user "
                "FROM facilities WHERE id=:fid AND is_active=true"
            ), {"fid": args.facility}).fetchall()
        else:
            rows = conn.execute(text("""
                SELECT id, facility_code, facility_name,
                       COALESCE(eclaimlink_username,'') AS dha_user
                FROM   facilities
                WHERE  is_active = true
                  AND  facility_code NOT IN :skip
                ORDER  BY id
            """), {"skip": tuple(SKIP_CODES)}).fetchall()

    # Filter out facilities without DHA credentials + any user-specified skips
    skip_ids = set(args.skip_facility_ids or [])
    dha_rows = [r for r in rows
                if r.dha_user not in SKIP_DHA_USERS
                and r.id not in skip_ids]
    if skip_ids:
        log.info("Skipping facility IDs (--skip-facility): %s", sorted(skip_ids))
    log.info("Facilities with DHA credentials: %d", len(dha_rows))

    total_fetched = 0
    total_parsed  = 0
    failures: list[str] = []

    for fac in dha_rows:
        fid   = fac.id
        fcode = fac.facility_code
        fname = fac.facility_name
        log.info("")
        log.info("══ %s  %s ══════════════════════════════════════", fcode, fname)

        windows = list(month_windows(date_from, date_to))
        log.info("  %d monthly windows: %s → %s", len(windows), windows[0][0], windows[-1][1])

        if args.dry_run:
            log.info("  [DRY RUN] Would fetch %d windows", len(windows))
            continue

        fac_fetched = 0
        fac_parsed  = 0

        for (w_from, w_to) in windows:
            try:
                with Session(engine) as db:
                    svc = DHASyncService(session=db)
                    fl  = svc.run_sync_pipeline(fid, w_from, w_to)
                    f   = fl.records_fetched or 0
                    p   = fl.records_parsed  or 0
                    fac_fetched += f
                    fac_parsed  += p
                    sym = "✅" if fl.status == "success" else ("⚠️" if fl.status == "partial" else "❌")
                    if f > 0 or p > 0:
                        log.info("  %s %s→%s  fetched=%d  parsed=%d  status=%s",
                                 sym, w_from, w_to, f, p, fl.status)
                    else:
                        log.info("  %s %s→%s  (nothing new)", sym, w_from, w_to)
            except Exception as exc:
                log.error("  ❌ %s→%s  FAILED: %s", w_from, w_to, str(exc)[:200])
                failures.append(f"[{fcode}] {w_from}→{w_to}: {str(exc)[:100]}")

        log.info("  ── %s total: fetched=%d  parsed=%d", fcode, fac_fetched, fac_parsed)
        total_fetched += fac_fetched
        total_parsed  += fac_parsed

    log.info("")
    log.info("=" * 65)
    log.info("Backfill COMPLETE")
    log.info("  Total fetched : %d", total_fetched)
    log.info("  Total parsed  : %d", total_parsed)
    log.info("  Failures      : %d", len(failures))
    for f in failures:
        log.error("  ❌ %s", f)
    log.info("=" * 65)

    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
