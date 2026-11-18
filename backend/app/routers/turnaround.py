"""
turnaround.py — Payer turnaround & overdue claims.

  GET /api/turnaround/summary?facility_id=&months=6&days=45      per facility + payer: submission -> first RA days
  GET /api/turnaround/overdue?facility_id=&payer=&days=45&months=12&limit=500   claims with no RA after N days

One row per claim (facility_id, claim_id). Submission date = FIRST submission (resubmissions don't reset the clock for
turnaround); amount / payer = latest submission. Remitted = at least one remittances row for the claim; turnaround =
first remittance_date on/after the first in-window submission minus that submission (a claim whose only RAs
predate it — an older claim resubmitted in the window — counts as remitted but has no turnaround). Overdue = no RA at all and current_date - latest submission_date > days. GHAF-001 (demo) is excluded.
Cached for an hour per parameter set — data only changes with the nightly fetch.
"""
from __future__ import annotations

import time
from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..services.payer_names import payer_name
from ..database import get_db
from ..models import User
from ..security import get_current_user

router = APIRouter(prefix="/api/turnaround", tags=["turnaround"])
TTL = 3600
_cache: dict = {}

# Claims submitted in [d0, today]; one row per claim with first/last submission and first RA date.
_CLAIMS_CTE = """
WITH c AS (
  SELECT facility_id, claim_id, min(submission_date) first_sub, max(submission_date) last_sub,
         (array_agg(payer_id ORDER BY submission_date DESC, id DESC))[1] payer_id,
         (array_agg(coalesce(nullif(receiver_id, ''), payer_id) ORDER BY submission_date DESC, id DESC))[1] receiver_id,
         (array_agg(nullif(receiver_name, '') ORDER BY submission_date DESC, id DESC))[1] receiver_name,
         (array_agg(coalesce(nullif(payer_name, ''), payer_id) ORDER BY submission_date DESC, id DESC))[1] payer_name,
         (array_agg(coalesce(net_amount, 0) ORDER BY submission_date DESC, id DESC))[1] net,
         (array_agg(member_id ORDER BY submission_date DESC, id DESC))[1] member_id,
         min(encounter_start) encounter_start
  FROM claims
  WHERE submission_date >= :d0 AND (CAST(:f AS int) IS NULL OR facility_id = :f)
    AND facility_id NOT IN (SELECT id FROM facilities WHERE facility_code = 'GHAF-001')
  GROUP BY 1, 2),
r AS (
  SELECT c.facility_id, c.claim_id, min(r.remittance_date) FILTER (WHERE r.remittance_date >= c.first_sub) first_ra
  FROM c JOIN remittances r ON r.facility_id = c.facility_id AND r.claim_id = c.claim_id
   AND r.remittance_date >= CAST(:d0 AS date) - 730
  GROUP BY 1, 2),
x AS (
  SELECT c.*, r.claim_id IS NOT NULL remitted, r.first_ra
  FROM c LEFT JOIN r USING (facility_id, claim_id))
"""

_SUMMARY_SQL = _CLAIMS_CTE + """
, y AS (SELECT x.*, first_ra - first_sub tat,
               current_date - last_sub age FROM x)
SELECT y.facility_id, f.facility_name, y.receiver_id, max(y.receiver_name) receiver_name,
       y.payer_id, max(y.payer_name) payer_name,
       count(*) claims,
       count(*) FILTER (WHERE remitted) remitted,
       avg(tat) avg_days,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY tat) median_days,
       count(*) FILTER (WHERE tat <= 30) w30, count(*) FILTER (WHERE tat <= 45) w45,
       count(*) FILTER (WHERE tat <= 60) w60, count(tat) tat_n,
       count(*) FILTER (WHERE NOT remitted) outstanding_n,
       coalesce(sum(net) FILTER (WHERE NOT remitted), 0) outstanding_amt,
       count(*) FILTER (WHERE NOT remitted AND age > :days) overdue_n,
       coalesce(sum(net) FILTER (WHERE NOT remitted AND age > :days), 0) overdue_amt
FROM y JOIN facilities f ON f.id = y.facility_id
GROUP BY y.facility_id, f.facility_name, y.receiver_id, y.payer_id
ORDER BY overdue_amt DESC, claims DESC
"""

_OVERDUE_SQL = _CLAIMS_CTE + """
SELECT x.facility_id, f.facility_name, x.claim_id, x.receiver_id, x.receiver_name, x.payer_id, x.payer_name, x.member_id,
       x.encounter_start::date encounter_date, x.first_sub, x.last_sub, x.net,
       current_date - x.last_sub days_outstanding
FROM x JOIN facilities f ON f.id = x.facility_id
WHERE NOT x.remitted AND current_date - x.last_sub > :days
  AND (CAST(:payer AS text) IS NULL OR x.payer_id = :payer)
  AND (CAST(:receiver AS text) IS NULL OR x.receiver_id = :receiver)
ORDER BY days_outstanding DESC, x.net DESC
LIMIT :limit
"""


def _scope(user: User, facility_id: Optional[int]) -> Optional[int]:
    if user.role != "admin" and user.facility_id is not None:
        if facility_id not in (None, user.facility_id):
            raise HTTPException(403, "Not your facility")
        return user.facility_id
    return facility_id


def _cached(key, fn):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < TTL:
        return hit[1]
    out = fn()
    _cache[key] = (time.time(), out)
    if len(_cache) > 200:
        _cache.pop(min(_cache, key=lambda k: _cache[k][0]))
    return out


def _pct(n, d):
    return round(100.0 * n / d, 1) if d else None


_SUM_KEYS = ("claims", "remitted", "outstanding_count", "outstanding_amount", "overdue_count", "overdue_amount")


def _row(r):
    tn = int(r["tat_n"] or 0)
    return {
        "facility_id": r["facility_id"], "facility_name": r["facility_name"],
        "receiver_id": r["receiver_id"], "payer_id": r["payer_id"],
        "payer_name": payer_name(r["payer_id"], r["payer_name"]),
        "claims": int(r["claims"]), "remitted": int(r["remitted"]),
        "remitted_pct": _pct(int(r["remitted"]), int(r["claims"])),
        "avg_days": round(float(r["avg_days"]), 1) if r["avg_days"] is not None else None,
        "median_days": round(float(r["median_days"]), 1) if r["median_days"] is not None else None,
        "within_30_pct": _pct(int(r["w30"]), tn), "within_45_pct": _pct(int(r["w45"]), tn),
        "within_60_pct": _pct(int(r["w60"]), tn),
        "_tat_sum": float(r["avg_days"] or 0) * tn, "_tn": tn,
        "_w": (int(r["w30"]), int(r["w45"]), int(r["w60"])),
        "outstanding_count": int(r["outstanding_n"]), "outstanding_amount": round(float(r["outstanding_amt"]), 2),
        "overdue_count": int(r["overdue_n"]), "overdue_amount": round(float(r["overdue_amt"]), 2),
    }


def _group_tpa(rows):
    """Receiver (TPA / insurer the claim is sent to) -> payers under it."""
    tpas = {}
    for r in rows:
        p = _row(r)
        t = tpas.setdefault(p["receiver_id"] or "?", {
            "receiver_id": p["receiver_id"],
            "receiver_name": payer_name(p["receiver_id"], r["receiver_name"]),
            "payers": [], "_tat_sum": 0.0, "_tn": 0, "_w": [0, 0, 0], **{k: 0 for k in _SUM_KEYS}})
        t["payers"].append(p)
        for k in _SUM_KEYS:
            t[k] += p[k]
        t["_tat_sum"] += p["_tat_sum"]; t["_tn"] += p["_tn"]
        t["_w"] = [a + b for a, b in zip(t["_w"], p["_w"])]
    out = []
    for t in tpas.values():
        tn = t.pop("_tn"); ts = t.pop("_tat_sum"); w = t.pop("_w")
        t["remitted_pct"] = _pct(t["remitted"], t["claims"])
        t["avg_days"] = round(ts / tn, 1) if tn else None
        t["within_30_pct"], t["within_45_pct"], t["within_60_pct"] = (_pct(x, tn) for x in w)
        t["outstanding_amount"] = round(t["outstanding_amount"], 2)
        t["overdue_amount"] = round(t["overdue_amount"], 2)
        t["is_direct"] = len(t["payers"]) == 1 and t["payers"][0]["payer_id"] == t["receiver_id"]
        for p in t["payers"]:
            for k in ("_tat_sum", "_tn", "_w"):
                p.pop(k, None)
        t["payers"].sort(key=lambda p: (-p["overdue_amount"], -p["claims"]))
        out.append(t)
    out.sort(key=lambda t: (-t["overdue_amount"], -t["claims"]))
    return out


@router.get("/summary")
def turnaround_summary(facility_id: Optional[int] = None,
                       months: int = Query(6, ge=1, le=36),
                       days: int = Query(45, ge=1, le=730),
                       db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    facility_id = _scope(user, facility_id)
    d0 = date.today() - timedelta(days=round(months * 30.44))

    def run():
        rows = db.execute(text(_SUMMARY_SQL), {"f": facility_id, "d0": d0, "days": days}).mappings().all()
        tpas = _group_tpa(rows)
        tot = {k: sum(t[k] for t in tpas) for k in _SUM_KEYS}
        tot["outstanding_amount"] = round(tot["outstanding_amount"], 2)
        tot["overdue_amount"] = round(tot["overdue_amount"], 2)
        return {"facility_id": facility_id, "submitted_from": d0.isoformat(), "months": months, "days": days,
                "total": tot, "tpas": tpas, "as_of": time.strftime("%Y-%m-%d %H:%M")}

    return _cached(("s2", facility_id, d0, days), run)


def _overdue_rows(db, facility_id, receiver, payer, days, d0, limit):
    rows = db.execute(text(_OVERDUE_SQL), {"f": facility_id, "d0": d0, "days": days, "payer": payer,
                                           "receiver": receiver, "limit": limit}).mappings().all()
    return [{
        "facility_id": r["facility_id"], "facility_name": r["facility_name"], "claim_id": r["claim_id"],
        "receiver_id": r["receiver_id"], "receiver_name": payer_name(r["receiver_id"], r["receiver_name"]),
        "payer_id": r["payer_id"], "payer_name": payer_name(r["payer_id"], r["payer_name"]),
        "member_id": r["member_id"],
        "encounter_date": r["encounter_date"].isoformat() if r["encounter_date"] else None,
        "first_submission": r["first_sub"].isoformat(), "submission_date": r["last_sub"].isoformat(),
        "net_amount": round(float(r["net"]), 2), "days_outstanding": int(r["days_outstanding"]),
    } for r in rows]


@router.get("/overdue")
def turnaround_overdue(facility_id: Optional[int] = None, receiver: Optional[str] = None,
                       payer: Optional[str] = None,
                       days: int = Query(45, ge=1, le=730),
                       months: int = Query(12, ge=1, le=36),
                       limit: int = Query(500, ge=1, le=5000),
                       db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    facility_id = _scope(user, facility_id)
    receiver, payer = receiver or None, payer or None
    d0 = date.today() - timedelta(days=round(months * 30.44))

    def run():
        claims = _overdue_rows(db, facility_id, receiver, payer, days, d0, limit)
        return {"facility_id": facility_id, "receiver": receiver, "payer": payer, "days": days,
                "submitted_from": d0.isoformat(), "limit": limit, "truncated": len(claims) >= limit,
                "claims": claims}

    return _cached(("o2", facility_id, receiver, payer, days, d0, limit), run)


@router.get("/overdue.xlsx")
def turnaround_overdue_xlsx(facility_id: Optional[int] = None, receiver: Optional[str] = None,
                            payer: Optional[str] = None,
                            days: int = Query(45, ge=1, le=730),
                            months: int = Query(12, ge=1, le=36),
                            db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Excel follow-up list: Summary sheet (TPA -> payer) + one row per overdue claim."""
    import io
    from fastapi.responses import StreamingResponse
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter

    facility_id = _scope(user, facility_id)
    receiver, payer = receiver or None, payer or None
    d0 = date.today() - timedelta(days=round(months * 30.44))
    claims = _overdue_rows(db, facility_id, receiver, payer, days, d0, 100000)

    wb = Workbook()
    head = Font(bold=True, color="FFFFFF")
    fill = PatternFill("solid", fgColor="0B4F6C")

    def sheet(ws, header, rows, money_cols=()):
        ws.append(header)
        for c in ws[1]:
            c.font, c.fill = head, fill
        for r in rows:
            ws.append(r)
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions
        for i, h in enumerate(header, 1):
            width = max([len(str(h))] + [len(str(r[i - 1] or "")) for r in rows[:500]]) + 2
            ws.column_dimensions[get_column_letter(i)].width = min(width, 50)
            if i in money_cols:
                for cell in ws.iter_cols(min_col=i, max_col=i, min_row=2):
                    for c in cell:
                        c.number_format = "#,##0.00"

    summ = {}
    for c in claims:
        k = (c["facility_name"], c["receiver_id"], c["receiver_name"], c["payer_id"], c["payer_name"])
        n, a, o = summ.get(k, (0, 0.0, 0))
        summ[k] = (n + 1, a + c["net_amount"], max(o, c["days_outstanding"]))
    ws = wb.active
    ws.title = "Summary"
    sheet(ws, ["Facility", "Receiver (TPA) ID", "Receiver (TPA)", "Payer ID", "Payer", "Overdue claims",
               "Net amount (AED)", "Oldest (days)"],
          [list(k) + [v[0], round(v[1], 2), v[2]] for k, v in sorted(summ.items(), key=lambda x: -x[1][1])],
          money_cols=(7,))

    sn_note = "SN 03/2019 3.9.1: RA + payment due within 45 days of submission"
    rows = []
    for c in claims:
        due = date.fromisoformat(c["submission_date"]) + timedelta(days=45)
        rows.append([c["facility_name"], c["receiver_id"], c["receiver_name"], c["payer_id"], c["payer_name"],
                     c["claim_id"], c["member_id"], c["encounter_date"], c["first_submission"],
                     c["submission_date"], due.isoformat(), max(0, (date.today() - due).days),
                     c["net_amount"], c["days_outstanding"]])
    sheet(wb.create_sheet("Overdue claims"),
          ["Facility", "Receiver (TPA) ID", "Receiver (TPA)", "Payer ID", "Payer", "Claim ID", "Member ID",
           "Encounter date", "First submission", "Latest submission", "SN RA due date", "Days past SN due",
           "Net amount (AED)", "Days outstanding"], rows, money_cols=(13,))
    info = wb.create_sheet("About")
    for line in [f"Overdue = no remittance advice and more than {days} days since the latest submission.",
                 f"Claims submitted since {d0.isoformat()}. Generated {time.strftime('%Y-%m-%d %H:%M')}.",
                 sn_note, "Receiver = the TPA / insurer the claim was sent to; Payer = the insurer under it."]:
        info.append([line])
    info.column_dimensions["A"].width = 100

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    tag = "_".join(x for x in [receiver, payer] if x) or "all"
    name = f"overdue_claims_{tag}_{date.today():%Y%m%d}.xlsx"
    return StreamingResponse(buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})
