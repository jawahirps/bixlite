#!/usr/bin/env python3
"""
Generate a public portal token for a client.

Usage:
    python scripts/generate_portal_token.py \
        --client "Al Noor Hospital" \
        --facilities 1 2 3 \
        --days 365

    # Never-expire token:
    python scripts/generate_portal_token.py \
        --client "Al Noor Hospital" \
        --facilities 1

    # List existing tokens:
    python scripts/generate_portal_token.py --list

    # Revoke a token:
    python scripts/generate_portal_token.py --revoke <token>
"""
import argparse
import json
import secrets
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from app.models import PortalToken, Facility

DB_URL = "postgresql+psycopg2://ghaf@/ghaf_app?host=/tmp"

# ── Change this to your public domain ─────────────────────────────────────────
BASE_URL = "https://reports.ghafservices.com"
PORTAL_PATH = "/portal/remittance"
# ──────────────────────────────────────────────────────────────────────────────


def make_token() -> str:
    return secrets.token_urlsafe(32)  # 43-char URL-safe string


def main():
    parser = argparse.ArgumentParser(description="Manage Ghaf portal tokens")
    parser.add_argument("--client",     help="Client / company name")
    parser.add_argument("--facilities", nargs="+", type=int, help="Facility IDs to grant access")
    parser.add_argument("--days",       type=int, default=None, help="Token validity in days (default: never expires)")
    parser.add_argument("--list",       action="store_true", help="List all active tokens")
    parser.add_argument("--revoke",     metavar="TOKEN", help="Revoke a token")
    args = parser.parse_args()

    engine = create_engine(DB_URL)

    with Session(engine) as db:

        # ── List ────────────────────────────────────────────────────────────
        if args.list:
            tokens = db.query(PortalToken).order_by(PortalToken.created_at.desc()).all()
            if not tokens:
                print("No portal tokens found.")
                return
            print(f"\n{'ID':>4}  {'Active':6}  {'Client':<30}  {'Expires':12}  {'Last Used':<20}  Token")
            print("-" * 120)
            for t in tokens:
                exp = t.expires_at.isoformat() if t.expires_at else "never"
                lu  = t.last_used_at.strftime("%Y-%m-%d %H:%M") if t.last_used_at else "—"
                status = "✅" if t.is_active else "❌"
                print(f"{t.id:>4}  {status:6}  {t.client_name:<30}  {exp:12}  {lu:<20}  {t.token[:20]}…")
            return

        # ── Revoke ──────────────────────────────────────────────────────────
        if args.revoke:
            pt = db.query(PortalToken).filter(PortalToken.token == args.revoke).first()
            if not pt:
                print(f"ERROR: Token not found: {args.revoke}")
                sys.exit(1)
            pt.is_active = False
            db.commit()
            print(f"✅ Revoked token for client: {pt.client_name}")
            return

        # ── Generate ────────────────────────────────────────────────────────
        if not args.client or not args.facilities:
            parser.error("--client and --facilities are required to generate a token")

        # Validate facility IDs
        facs = db.query(Facility).filter(Facility.id.in_(args.facilities)).all()
        found_ids = {f.id for f in facs}
        missing = set(args.facilities) - found_ids
        if missing:
            print(f"ERROR: Facility IDs not found: {sorted(missing)}")
            sys.exit(1)

        expires_at = date.today() + timedelta(days=args.days) if args.days else None

        token = make_token()
        pt = PortalToken(
            token=token,
            client_name=args.client,
            facility_ids=json.dumps(args.facilities),
            expires_at=expires_at,
            is_active=True,
        )
        db.add(pt)
        db.commit()
        db.refresh(pt)

        portal_url = f"{BASE_URL}{PORTAL_PATH}?token={token}"

        print("\n" + "=" * 70)
        print("  ✅  Portal Token Created")
        print("=" * 70)
        print(f"  Client     : {args.client}")
        print(f"  Facilities : {args.facilities} — {[f.facility_name for f in facs]}")
        print(f"  Expires    : {expires_at.isoformat() if expires_at else 'Never'}")
        print(f"  Token      : {token}")
        print(f"\n  📎 Share this link with the client:")
        print(f"  {portal_url}")
        print("=" * 70 + "\n")


if __name__ == "__main__":
    main()
