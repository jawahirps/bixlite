"""Scrub ClaimSubmission files that have no scrub findings yet (catch-up + nightly safety net).

Scope: files submitted in the last N months (default 12) plus any file holding a claim with an RA in the last 21 days.
Usage (from backend/):  python scripts/scrub_catchup.py [--months 12] [--limit 0] [--dry-run]
"""
import argparse, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sqlalchemy import text
from app.database import SessionLocal
from app.services.scrub_xml_service import ScrubXmlService

SQL = """
SELECT x.id, x.facility_id FROM raw_xml_store x
WHERE x.xml_type = 'ClaimSubmission' AND x.parse_status = 'parsed'
  AND NOT EXISTS (SELECT 1 FROM scrub_xml_findings f WHERE f.raw_xml_id = x.id)
  AND (x.submission_date >= now() - make_interval(months => :m)
       OR x.id IN (SELECT c.raw_xml_id FROM claims c JOIN remittances r ON r.facility_id = c.facility_id AND r.claim_id = c.claim_id
                   WHERE r.remittance_date >= current_date - 21 AND c.raw_xml_id IS NOT NULL))
ORDER BY x.submission_date DESC NULLS LAST, x.id DESC
"""

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--months", type=int, default=12)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    db = SessionLocal()
    rows = db.execute(text(SQL), {"m": a.months}).all()
    if a.limit:
        rows = rows[:a.limit]
    print(f"{len(rows)} unscrubbed files in scope", flush=True)
    if a.dry_run:
        return
    svc, ok, bad, t0 = ScrubXmlService(db), 0, 0, time.time()
    for i, (rid, fid) in enumerate(rows, 1):
        try:
            xml = db.execute(text("SELECT xml_content FROM raw_xml_store WHERE id = :i"), {"i": rid}).scalar() or ""
            svc.run_for_raw(raw_xml_id=rid, xml_bytes=xml.encode("utf-8"), facility_id=fid)
            db.commit(); ok += 1
        except Exception as e:
            db.rollback(); bad += 1
            print(f"failed raw_xml_id={rid}: {type(e).__name__}: {str(e)[:120]}", flush=True)
        if i % 200 == 0:
            print(f"{i}/{len(rows)} done ({ok} ok, {bad} failed, {time.time()-t0:.0f}s)", flush=True)
    print(f"finished: {ok} scrubbed, {bad} failed, {time.time()-t0:.0f}s", flush=True)

if __name__ == "__main__":
    main()
