#!/usr/bin/env python3
"""
Ghaf App — Nightly Fetch for ALL Facilities
============================================
Fetches the last LOOKBACK_DAYS of ClaimSubmission + RemittanceAdvice from
DHA eClaimLink and Riayati (MOH) for every active non-demo facility.

Files already in raw_xml_store are skipped (dedup by filename, done inside
fetch_service.py).  Run nightly — new data appears within a day even when
DHA posts files with a slight delay.

Cron / launchd usage:
    caffeinate -i /path/to/.venv/bin/python3.12 scripts/nightly_fetch_all.py

Options:
    --days N      look-back window (default: 7)
    --dry-run     list facilities only, no fetching
    --log FILE    append log to FILE (default: /tmp/ghaf_nightly_fetch.log)
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, timedelta, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.services.fetch_service import DHASyncService
from app.services.riayati_service import RiayatiSyncService, RiayatiInactiveFacilityError

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
LOOKBACK_DAYS = 7          # catch files posted up to a week late
LOG_FILE_DEFAULT = "/tmp/ghaf_nightly_fetch.log"

# Facilities / credentials to skip
SKIP_CODES        = {"GHAF-001"}
SKIP_DHA_USERS    = {"", "N/A", "demo_user"}


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def setup_logging(log_file: str) -> None:
    fmt = "%(asctime)s [%(levelname)s] %(name)s — %(message)s"
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    try:
        handlers.append(logging.FileHandler(log_file, mode="a"))
    except OSError as e:
        print(f"WARNING: cannot open log file {log_file}: {e}", file=sys.stderr)
    logging.basicConfig(level=logging.WARNING, format=fmt, handlers=handlers)


log = logging.getLogger("nightly_fetch")
log.setLevel(logging.INFO)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description="Ghaf nightly fetch — all facilities")
    parser.add_argument("--days",    type=int, default=LOOKBACK_DAYS,
                        help=f"Look-back window in days (default: {LOOKBACK_DAYS})")
    parser.add_argument("--dry-run", action="store_true",
                        help="List facilities without fetching")
    parser.add_argument("--log",     default=LOG_FILE_DEFAULT,
                        help=f"Log file path (default: {LOG_FILE_DEFAULT})")
    args = parser.parse_args()

    setup_logging(args.log)

    date_to   = date.today()
    date_from = date_to - timedelta(days=args.days - 1)

    log.info("=" * 65)
    log.info("Nightly fetch started  %s", datetime.now().isoformat(timespec="seconds"))
    log.info("Date range: %s → %s  (look-back %d days)", date_from, date_to, args.days)
    log.info("=" * 65)

    settings = get_settings()
    engine   = create_engine(settings.DATABASE_URL)

    # Load active facilities (excluding demo)
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT id,
                   facility_code,
                   facility_name,
                   COALESCE(eclaimlink_username, '') AS dha_user,
                   (riayati_username IS NOT NULL
                    OR EXISTS (SELECT 1 FROM facility_riayati_accounts a
                               WHERE a.facility_id = facilities.id AND a.is_active)) AS has_riayati
            FROM   facilities
            WHERE  is_active = true
              AND  facility_code NOT IN :skip_codes
            ORDER  BY id
        """), {"skip_codes": tuple(SKIP_CODES)}).fetchall()

    log.info("Active (non-demo) facilities: %d", len(rows))

    if args.dry_run:
        for r in rows:
            dha_tag = "DHA" if r.dha_user not in SKIP_DHA_USERS else "—"
            ria_tag = "Riayati" if r.has_riayati else "—"
            log.info("  [DRY RUN] %s  %s  (%s / %s)",
                     r.facility_code, r.facility_name, dha_tag, ria_tag)
        log.info("Dry run complete — nothing fetched.")
        return

    total_fetched = 0
    total_parsed  = 0
    failures: list[str] = []

    for r in rows:
        fid      = r.id
        fcode    = r.facility_code
        fname    = r.facility_name
        dha_user = r.dha_user
        has_ria  = r.has_riayati

        do_dha = dha_user not in SKIP_DHA_USERS
        do_ria = bool(has_ria)

        if not do_dha and not do_ria:
            log.info("── %s (%s) — no gateway credentials, skipping", fcode, fname)
            continue

        log.info("── %s  %s ──────────────────────────────", fcode, fname)

        # ── DHA eClaimLink ───────────────────────────────────────────────────
        if do_dha:
            try:
                with Session(engine) as db:
                    svc = DHASyncService(session=db)
                    fl  = svc.run_sync_pipeline(fid, date_from, date_to)
                    status  = fl.status
                    fetched = fl.records_fetched or 0
                    parsed  = fl.records_parsed  or 0
                    failed  = fl.records_failed  or 0
                sym = "✅" if status == "success" else ("⚠️" if status == "partial" else "❌")
                log.info("  %s DHA  status=%-8s  fetched=%d  parsed=%d  failed=%d",
                         sym, status, fetched, parsed, failed)
                total_fetched += fetched
                total_parsed  += parsed
            except Exception as exc:
                log.error("  ❌ DHA  FAILED: %s", str(exc)[:200])
                failures.append(f"[{fcode}] DHA: {str(exc)[:120]}")

        # ── Riayati (MOH) ────────────────────────────────────────────────────
        if do_ria:
            try:
                with Session(engine) as db:
                    svc = RiayatiSyncService(db)
                    fl  = svc.run_sync_pipeline(fid, date_from, date_to)
                    status  = fl.status
                    fetched = fl.records_fetched or 0
                    parsed  = fl.records_parsed  or 0
                    failed  = fl.records_failed  or 0
                sym = "✅" if status == "success" else ("⚠️" if status == "partial" else "❌")
                log.info("  %s Riayati  status=%-8s  fetched=%d  parsed=%d  failed=%d",
                         sym, status, fetched, parsed, failed)
                total_fetched += fetched
                total_parsed  += parsed
            except RiayatiInactiveFacilityError as exc:
                log.warning("  ⚠️  Riayati inactive: %s", exc)
            except Exception as exc:
                log.error("  ❌ Riayati  FAILED: %s", str(exc)[:200])
                failures.append(f"[{fcode}] Riayati: {str(exc)[:120]}")

    log.info("=" * 65)
    log.info("Nightly fetch COMPLETE  %s", datetime.now().isoformat(timespec="seconds"))
    log.info("  Total fetched : %d", total_fetched)
    log.info("  Total parsed  : %d", total_parsed)
    log.info("  Failures      : %d", len(failures))
    for err in failures:
        log.error("  ❌ %s", err)
    log.info("=" * 65)

    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
