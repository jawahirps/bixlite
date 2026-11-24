"""
dashboard.py — Claim cycle status for the Dashboard.

  GET /api/dashboard/cycle?facility_id=&date_from=&date_to=   claims by encounter date, where each one is in the cycle

One row per claim (latest submission). Paid = all RAs for the claim (identical RA loads counted once), capped at the
claim net; Denied = net not paid once an RA exists; Awaiting RA = net of claims with no RA yet. So net = paid +
denied + awaiting. Results are cached for an hour per (facility, dates) — the data only changes with the nightly fetch.
"""
from __future__ import annotations

import threading
import time
from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import User
from ..security import get_current_user

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])
TTL = 3600
_cache: dict = {}

_SQL = """
WITH c AS (
  SELECT DISTINCT ON (facility_id, claim_id) facility_id, claim_id, encounter_start, submission_date,
         coalesce(net_amount, 0) net, coalesce(resubmission_count, 0) > 0 OR resubmission_type IS NOT NULL resub,
         -- SN 03/2019 3.9.4: emergency (encounter type with Emergency Room) RA due in 7 days, else 45 (3.9.1)
         CASE WHEN coalesce((encounter_type IN ('2', '4', '6') OR (encounter_type ILIKE '%emergency%' AND encounter_type NOT ILIKE '%no emergency%')), false) THEN 7 ELSE 45 END sn_days
  FROM claims
  WHERE encounter_start >= :d0 AND encounter_start < :d1 AND (CAST(:f AS int) IS NULL OR facility_id = :f)
    AND facility_id NOT IN (SELECT id FROM facilities WHERE facility_code = 'GHAF-001')
  ORDER BY facility_id, claim_id, submission_date DESC NULLS LAST, id DESC),
ra AS (
  SELECT DISTINCT ON (r.facility_id, r.claim_id, r.ra_file_id, r.net_amount, r.paid_amount, r.denial_amount)
         r.facility_id, r.claim_id, coalesce(r.paid_amount, 0) paid
  FROM remittances r JOIN c USING (facility_id, claim_id)),
p AS (SELECT facility_id, claim_id, sum(paid) paid FROM ra GROUP BY 1, 2),
x AS (
  SELECT to_char(c.encounter_start, 'YYYY-MM') m, c.net, c.resub, p.claim_id IS NOT NULL remitted,
         least(greatest(coalesce(p.paid, 0), 0), c.net) paid,
         CASE WHEN p.claim_id IS NULL THEN current_date - c.submission_date::date END age, c.sn_days
  FROM c LEFT JOIN p USING (facility_id, claim_id))
SELECT m, count(*) n, sum(net) net, count(*) FILTER (WHERE resub) resub,
       count(*) FILTER (WHERE remitted AND paid >= net - 0.5) full_n,
       count(*) FILTER (WHERE remitted AND paid > 0.5 AND paid < net - 0.5) part_n,
       count(*) FILTER (WHERE remitted AND paid <= 0.5 AND net > 0.5) den_n,
       count(*) FILTER (WHERE NOT remitted) await_n,
       count(*) FILTER (WHERE NOT remitted AND age > sn_days) overdue_n,
       sum(paid) paid, sum(net - paid) FILTER (WHERE remitted) denied,
       sum(net) FILTER (WHERE NOT remitted) awaiting, sum(net) FILTER (WHERE NOT remitted AND age > sn_days) overdue
FROM x GROUP BY 1 ORDER BY 1
"""

COUNTS = ("n", "resub", "full_n", "part_n", "den_n", "await_n", "overdue_n")
AMOUNTS = ("net", "paid", "denied", "awaiting", "overdue")


@router.get("/cycle")
def claim_cycle(facility_id: Optional[int] = None, date_from: Optional[date] = None, date_to: Optional[date] = None,
                db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    if user.role != "admin" and user.facility_id is not None:
        if facility_id not in (None, user.facility_id):
            raise HTTPException(403, "Not your facility")
        facility_id = user.facility_id
    d1 = date_to or date.today()
    d0 = date_from or (d1.replace(day=1) - timedelta(days=335)).replace(day=1)
    if d0 > d1:
        raise HTTPException(400, "date_from is after date_to")
    key = (facility_id, d0, d1)
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < TTL:
        return hit[1]
    if hit:   # stale: answer now, recompute in the background (All facilities takes ~9 s)
        if key not in _busy:
            threading.Thread(target=_refresh, args=(facility_id, d0, d1), daemon=True).start()
        return hit[1]
    return _build(db, facility_id, d0, d1)


_busy: set = set()


def _refresh(facility_id, d0, d1):
    from ..database import SessionLocal
    key = (facility_id, d0, d1)
    if key in _busy:
        return
    _busy.add(key)
    db = SessionLocal()
    try:
        _build(db, facility_id, d0, d1)
    except Exception:
        pass
    finally:
        db.close()
        _busy.discard(key)


def warm():
    """Pre-compute the default All-facilities view at startup so the first Dashboard open is instant."""
    d1 = date.today()
    y, m = divmod(d1.year * 12 + d1.month - 1 - 11, 12)   # same as the page's "Last 12 months" preset
    d0 = date(y, m + 1, 1)
    threading.Thread(target=_refresh, args=(None, d0, d1), daemon=True).start()


def _build(db, facility_id, d0, d1):
    key = (facility_id, d0, d1)
    rows = db.execute(text(_SQL), {"f": facility_id, "d0": d0, "d1": d1 + timedelta(days=1)}).mappings().all()
    months = [{"month": r["m"], **{k: int(r[k] or 0) for k in COUNTS}, **{k: round(float(r[k] or 0), 2) for k in AMOUNTS}}
              for r in rows]
    total = {k: sum(m[k] for m in months) for k in COUNTS + AMOUNTS}
    out = {"facility_id": facility_id, "date_from": d0.isoformat(), "date_to": d1.isoformat(),
           "total": total, "months": months, "as_of": time.strftime("%Y-%m-%d %H:%M")}
    _cache[key] = (time.time(), out)
    if len(_cache) > 200:
        _cache.pop(min(_cache, key=lambda k: _cache[k][0]))
    return out
