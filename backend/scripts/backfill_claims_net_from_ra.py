"""
backfill_claims_net_from_ra.py
──────────────────────────────
Corrects claims.net_amount for initial submissions where the ClaimSubmission
XML <Net> field was mis-populated by the billing software (e.g. PRCE-001
tariff-adjustment claims where the XML stores only the adjustment delta).

The RemittanceAdvice <Net> is the authoritative value — the payer echoes back
the correct submitted net.  This script updates claims.net_amount from the
matching remittance record so the ClaimSummary report can always use
claims.net_amount directly (simple, stable, no RA-dependency at report time).

Only initial submissions are touched (resubmission_key = '').
Only rows where the two values differ are updated (idempotent).
Only rows where a positive RA net exists are updated (no RA = no change).

Usage:
    python3.12 scripts/backfill_claims_net_from_ra.py               # all facilities
    python3.12 scripts/backfill_claims_net_from_ra.py --facility 15 # single facility
    python3.12 scripts/backfill_claims_net_from_ra.py --dry-run     # preview only
"""
import argparse
import logging
import sys
import os

# ── allow running from repo root or scripts/ ───────────────────────────────
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import text
from app.database import SessionLocal

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


def run(facility_id: int | None = None, dry_run: bool = False) -> None:
    db = SessionLocal()
    try:
        # ── Build facility filter ──────────────────────────────────────────
        fac_filter = "AND c.facility_id = :fid" if facility_id else ""
        params = {"fid": facility_id} if facility_id else {}

        # ── Preview: how many rows differ ─────────────────────────────────
        preview_sql = text(f"""
            SELECT COUNT(*) AS cnt,
                   COALESCE(SUM(ABS(CAST(r_net AS numeric) - CAST(c.net_amount AS numeric))), 0) AS total_delta
            FROM claims c
            JOIN LATERAL (
                SELECT net_amount AS r_net
                FROM   remittances
                WHERE  facility_id = c.facility_id
                AND    claim_id    = c.claim_id
                AND    net_amount  > 0
                ORDER  BY remittance_date ASC
                LIMIT  1
            ) r ON TRUE
            WHERE c.resubmission_key = ''
            AND   c.net_amount IS DISTINCT FROM r.r_net
            {fac_filter}
        """)
        result = db.execute(preview_sql, params).fetchone()
        affected = result.cnt
        total_delta = float(result.total_delta)

        fac_label = f"facility {facility_id}" if facility_id else "ALL facilities"
        log.info(f"{'[DRY RUN] ' if dry_run else ''}Scope: {fac_label}")
        log.info(f"  Claims to update : {affected:,}")
        log.info(f"  Total AED delta  : {total_delta:,.2f}")

        if affected == 0:
            log.info("  Nothing to do — claims.net_amount already matches RA.")
            return

        if dry_run:
            log.info("  Dry run — no changes written.")
            return

        # ── Apply update ───────────────────────────────────────────────────
        update_sql = text(f"""
            UPDATE claims c
            SET    net_amount = r.r_net,
                   updated_at = NOW()
            FROM (
                SELECT DISTINCT ON (facility_id, claim_id)
                       facility_id,
                       claim_id,
                       net_amount AS r_net
                FROM   remittances
                WHERE  net_amount > 0
                {("AND facility_id = :fid" if facility_id else "")}
                ORDER  BY facility_id, claim_id, remittance_date ASC
            ) r
            WHERE  c.facility_id      = r.facility_id
            AND    c.claim_id         = r.claim_id
            AND    c.resubmission_key = ''
            AND    c.net_amount IS DISTINCT FROM r.r_net
            {fac_filter}
        """)
        db.execute(update_sql, params)
        db.commit()
        log.info(f"  ✅  Updated {affected:,} claim(s). claims.net_amount now matches RA net.")

    except Exception as exc:
        db.rollback()
        log.error(f"Error: {exc}")
        raise
    finally:
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill claims.net_amount from RA net")
    parser.add_argument("--facility", type=int, default=None, help="Limit to one facility ID")
    parser.add_argument("--dry-run", action="store_true", help="Preview only, no DB writes")
    args = parser.parse_args()

    run(facility_id=args.facility, dry_run=args.dry_run)
