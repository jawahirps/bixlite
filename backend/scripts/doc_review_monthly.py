"""
doc_review_monthly.py — run the Documentation review for every facility for one treatment month.

Scheduled on the server on the 16th (the month's submissions close on the 15th), for the previous month:
    python -m scripts.doc_review_monthly                 # previous month, all facilities
    python -m scripts.doc_review_monthly --month 2026-10
    python -m scripts.doc_review_monthly --from 2026-06  # catch-up: every month from June to last month

Facilities whose claim files carry no case-sheet text (80%+ of visits without one) are skipped and listed.
A facility-month that already has a completed run is skipped: a re-run deletes the findings and any review
notes entered on them. Use --force only on purpose. Nothing is sent to facilities or payers.
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text  # noqa: E402

from app.database import SessionLocal  # noqa: E402
from app.models import Facility  # noqa: E402
from app.services import doc_review as dr  # noqa: E402


def prev_month(d: date) -> str:
    y, m = (d.year, d.month - 1) if d.month > 1 else (d.year - 1, 12)
    return f"{y}-{m:02d}"


def months_from(start: str, end: str):
    y, m = map(int, start.split("-"))
    while f"{y}-{m:02d}" <= end:
        yield f"{y}-{m:02d}"
        y, m = (y, m + 1) if m < 12 else (y + 1, 1)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--month")
    ap.add_argument("--from", dest="start")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    last = prev_month(date.today())
    months = list(months_from(a.start, last)) if a.start else [a.month or last]

    db = SessionLocal()
    facs = [(f.id, f.facility_name) for f in db.query(Facility).filter(Facility.is_active.is_(True))
            if f.facility_code != "GHAF-001"]
    failed = 0
    for month in months:
        for fid, name in facs:
            done = (db.query(dr.DocReviewRun).filter_by(facility_id=fid, month=month, status="Completed")
                    .filter(dr.DocReviewRun.claims_checked > 0).first())
            if done and not a.force:
                print(f"{month} {name}: already done ({done.progress}) - skipped")
                continue
            r = dr.DocReviewRun(facility_id=fid, month=month, user_id=None, status="Queued")
            db.add(r); db.commit(); db.refresh(r)
            dr.run(r.id)                      # synchronous; uses its own session
            db.expire_all()
            r = db.get(dr.DocReviewRun, r.id)
            if r.status != "Completed":
                failed += 1
                print(f"{month} {name}: FAILED {r.error}")
            elif not r.claims_checked or r.claims_no_note >= 0.8 * r.claims_checked:
                # no case-sheet text in this facility's claim files: every Dx would read as "not documented",
                # which is noise, not findings. Remove the run and its findings so a later run can retry.
                db.execute(text("DELETE FROM doc_findings WHERE run_id = :r"), {"r": r.id})
                db.delete(r); db.commit()
                print(f"{month} {name}: skipped - claim files carry no case sheets "
                      f"({r.claims_no_note} of {r.claims_checked} visits without one)")
            else:
                print(f"{month} {name}: {r.progress} ({r.claims_checked} checked, {r.claims_no_note} without case sheet)")
    db.close()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
