"""
turnaround.py — Payer turnaround & overdue claims.

  GET /api/turnaround/summary?facility_id=&months=6&days=45      per facility + payer: submission -> first RA days
  GET /api/turnaround/overdue?facility_id=&payer=&days=45&months=12&limit=500   claims with no RA after N days

One row per claim (facility_id, claim_id). Submission date = FIRST submission (resubmissions don't reset the clock for
turnaround); amount / payer = latest submission. Remitted = at least one remittances row for the claim; turnaround =
first remittance_date on/after the first in-window submission minus that submission (a claim whose only RAs
predate it — an older claim resubmitted in the window — counts as remitted but has no turnaround). Overdue = no RA at all and current_date - latest submission_date > days. GHAF-001 (demo) is excluded.

SN 03/2019 (eClaimLink Claim Cycle Standardisation):
  3.9.1 RA within 45 days of submission; 3.9.4 emergency claims within 7 days; 3.9.2 resubmission RA within 30 days.
Emergency = eClaimLink encounter type with an Emergency Room: codes 2 / 4 / 6 ("No Bed + Emergency Room",
"Inpatient Bed + Emergency Room", "Daycase Bed + Emergency Room") or the text form containing "Emergency"
but not "No Emergency". Emergency claims are overdue after min(days, 7).
Resubmission RA = each resubmission row (resubmission_key <> '') -> first RA dated on/after its submission;
on time when <= 30 days; compliance % is over resubmissions that got an RA or are already past 30 days.
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
         min(encounter_start) encounter_start,
         bool_or(coalesce((encounter_type IN ('2', '4', '6') OR (encounter_type ILIKE '%emergency%' AND encounter_type NOT ILIKE '%no emergency%')), false)) emergency
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
  SELECT c.*, r.claim_id IS NOT NULL remitted, r.first_ra,
         CASE WHEN c.emergency THEN least(:days, 7) ELSE :days END od_days,
         CASE WHEN c.emergency THEN 7 ELSE 45 END sn_days
  FROM c LEFT JOIN r USING (facility_id, claim_id))
"""

EMERGENCY_SQL = """(encounter_type IN ('2', '4', '6') OR (encounter_type ILIKE '%emergency%' AND encounter_type NOT ILIKE '%no emergency%'))"""

_SUMMARY_SQL = _CLAIMS_CTE + """
, y AS (SELECT x.*, first_ra - first_sub tat,
               current_date - last_sub age FROM x)
SELECT y.facility_id, f.facility_name, y.receiver_id, max(y.receiver_name) receiver_name,
       y.payer_id, max(y.payer_name) payer_name,
       count(*) claims,
       count(*) FILTER (WHERE remitted) remitted,
       avg(tat) avg_days,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY tat) median_days,
       count(*) FILTER (WHERE tat <= sn_days) wsn, count(*) FILTER (WHERE tat <= 45) w45,
       count(*) FILTER (WHERE tat <= 60) w60, count(tat) tat_n,
       count(*) FILTER (WHERE NOT remitted) outstanding_n,
       coalesce(sum(net) FILTER (WHERE NOT remitted), 0) outstanding_amt,
       count(*) FILTER (WHERE NOT remitted AND age > od_days) overdue_n,
       coalesce(sum(net) FILTER (WHERE NOT remitted AND age > od_days), 0) overdue_amt,
       count(*) FILTER (WHERE emergency) em_n, count(tat) FILTER (WHERE emergency) em_tat_n,
       count(*) FILTER (WHERE emergency AND tat <= 7) em_w7,
       count(*) FILTER (WHERE emergency AND (tat > 7 OR (NOT remitted AND age > 7))) em_breach
FROM y JOIN facilities f ON f.id = y.facility_id
GROUP BY y.facility_id, f.facility_name, y.receiver_id, y.payer_id
ORDER BY overdue_amt DESC, claims DESC
"""

_OVERDUE_SQL = _CLAIMS_CTE + """
SELECT x.facility_id, f.facility_name, x.claim_id, x.receiver_id, x.receiver_name, x.payer_id, x.payer_name, x.member_id,
       x.encounter_start::date encounter_date, x.first_sub, x.last_sub, x.net,
       current_date - x.last_sub days_outstanding, x.emergency
FROM x JOIN facilities f ON f.id = x.facility_id
WHERE NOT x.remitted AND current_date - x.last_sub > x.od_days
  AND (CAST(:payer AS text) IS NULL OR x.payer_id = :payer)
  AND (CAST(:receiver AS text) IS NULL OR x.receiver_id = :receiver)
ORDER BY days_outstanding DESC, x.net DESC
LIMIT :limit
"""

# SN 3.9.2: each resubmission -> first RA on/after it; compliant within 30 days.
_RESUB_SQL = """
WITH rs AS (
  SELECT facility_id, claim_id, submission_date sub, payer_id,
         coalesce(nullif(receiver_id, ''), payer_id) receiver_id
  FROM claims
  WHERE coalesce(resubmission_key, '') <> '' AND submission_date >= :d0
    AND (CAST(:f AS int) IS NULL OR facility_id = :f)
    AND facility_id NOT IN (SELECT id FROM facilities WHERE facility_code = 'GHAF-001')),
y AS (
  SELECT rs.*, (SELECT min(r.remittance_date) FROM remittances r
                WHERE r.facility_id = rs.facility_id AND r.claim_id = rs.claim_id
                  AND r.remittance_date >= rs.sub) ra
  FROM rs)
SELECT facility_id, receiver_id, payer_id, count(*) rs_n, count(ra) rs_ra,
       count(*) FILTER (WHERE ra - sub <= 30) rs_w30,
       count(*) FILTER (WHERE ra IS NOT NULL OR current_date - sub > 30) rs_due,
       count(*) FILTER (WHERE ra IS NULL AND current_date - sub > 30) rs_overdue,
       coalesce(sum(ra - sub), 0) rs_tat_sum
FROM y GROUP BY 1, 2, 3
"""
_RS_KEYS = ("rs_n", "rs_ra", "rs_w30", "rs_due", "rs_overdue", "rs_tat_sum")
_EM_KEYS = ("em_n", "em_tat_n", "em_w7", "em_breach")


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


_SUM_KEYS = ("claims", "remitted", "outstanding_count", "outstanding_amount", "overdue_count", "overdue_amount",
             "_wsn", "_tn2") + _EM_KEYS + _RS_KEYS


def _derive(d):
    """Percentages from summed counts (payer, TPA or total level)."""
    d["sn_on_time_pct"] = _pct(d["_wsn"], d["_tn2"])
    d["emergency_claims"] = d["em_n"]
    d["emergency_within_7_pct"] = _pct(d["em_w7"], d["em_tat_n"])
    d["emergency_breached"] = d["em_breach"]
    d["resubmissions"] = d["rs_n"]
    d["resub_remitted"] = d["rs_ra"]
    d["resub_within_30_pct"] = _pct(d["rs_w30"], d["rs_due"])
    d["resub_overdue"] = d["rs_overdue"]
    d["resub_avg_days"] = round(d["rs_tat_sum"] / d["rs_ra"], 1) if d["rs_ra"] else None
    return d


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
        "within_45_pct": _pct(int(r["w45"]), tn), "within_60_pct": _pct(int(r["w60"]), tn),
        "_tat_sum": float(r["avg_days"] or 0) * tn, "_tn": tn,
        "_w": (int(r["w45"]), int(r["w60"])), "_wsn": int(r["wsn"]), "_tn2": tn,
        **{k: int(r[k]) for k in _EM_KEYS}, **{k: 0 for k in _RS_KEYS},
        "outstanding_count": int(r["outstanding_n"]), "outstanding_amount": round(float(r["outstanding_amt"]), 2),
        "overdue_count": int(r["overdue_n"]), "overdue_amount": round(float(r["overdue_amt"]), 2),
    }


def _group_tpa(rows, rs_rows=()):
    """Receiver (TPA / insurer the claim is sent to) -> payers under it."""
    tpas = {}
    prow = {}
    for r in rows:
        prow[(r["facility_id"], r["receiver_id"], r["payer_id"])] = _row(r)
    for q in rs_rows:  # resubmission RA counts, attached to the matching payer row (or a resub-only row)
        key = (q["facility_id"], q["receiver_id"], q["payer_id"])
        p = prow.get(key)
        if p is None:
            p = prow[key] = _row({"facility_id": q["facility_id"], "facility_name": None,
                                  "receiver_id": q["receiver_id"], "payer_id": q["payer_id"], "payer_name": None,
                                  "claims": 0, "remitted": 0, "avg_days": None,
                                  "median_days": None, "w45": 0, "w60": 0, "wsn": 0, "tat_n": 0,
                                  "outstanding_n": 0, "outstanding_amt": 0, "overdue_n": 0, "overdue_amt": 0,
                                  **{k: 0 for k in _EM_KEYS}})
        for k in _RS_KEYS:
            p[k] += int(q[k])
    names = {(r["facility_id"], r["receiver_id"], r["payer_id"]): r["receiver_name"] for r in rows}
    for key, p in prow.items():
        r = {"receiver_name": names.get(key)}
        t = tpas.setdefault(p["receiver_id"] or "?", {
            "receiver_id": p["receiver_id"],
            "receiver_name": payer_name(p["receiver_id"], r["receiver_name"]),
            "payers": [], "_tat_sum": 0.0, "_tn": 0, "_w": [0, 0], **{k: 0 for k in _SUM_KEYS}})
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
        t["within_45_pct"], t["within_60_pct"] = (_pct(x, tn) for x in w)
        _derive(t)
        t["outstanding_amount"] = round(t["outstanding_amount"], 2)
        t["overdue_amount"] = round(t["overdue_amount"], 2)
        t["is_direct"] = len(t["payers"]) == 1 and t["payers"][0]["payer_id"] == t["receiver_id"]
        for p in t["payers"]:
            _derive(p)
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
        rs_rows = db.execute(text(_RESUB_SQL), {"f": facility_id, "d0": d0}).mappings().all()
        tpas = _group_tpa(rows, rs_rows)
        tot = _derive({k: sum(t[k] for t in tpas) for k in _SUM_KEYS})
        tot["outstanding_amount"] = round(tot["outstanding_amount"], 2)
        tot["overdue_amount"] = round(tot["overdue_amount"], 2)
        for d in [tot] + tpas + [p for t in tpas for p in t["payers"]]:
            for k in ("_wsn", "_tn2") + _EM_KEYS + _RS_KEYS:
                d.pop(k, None)
        return {"facility_id": facility_id, "submitted_from": d0.isoformat(), "months": months, "days": days,
                "emergency_days": min(days, 7), "sn_rules": {"ra_days": 45, "emergency_ra_days": 7, "resub_ra_days": 30},
                "total": tot, "tpas": tpas, "as_of": time.strftime("%Y-%m-%d %H:%M")}

    return _cached(("s3", facility_id, d0, days), run)


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
        "emergency": bool(r["emergency"]),
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

    return _cached(("o3", facility_id, receiver, payer, days, d0, limit), run)


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

    sn_note = ("SN 03/2019 3.9.1: RA + payment due within 45 days of submission; "
               "3.9.4: emergency claims (encounter type with Emergency Room) within 7 days")
    rows = []
    for c in claims:
        due = date.fromisoformat(c["submission_date"]) + timedelta(days=7 if c["emergency"] else 45)
        rows.append([c["facility_name"], c["receiver_id"], c["receiver_name"], c["payer_id"], c["payer_name"],
                     c["claim_id"], c["member_id"], c["encounter_date"], c["first_submission"],
                     c["submission_date"], due.isoformat(), max(0, (date.today() - due).days),
                     c["net_amount"], c["days_outstanding"], "Yes" if c["emergency"] else ""])
    sheet(wb.create_sheet("Overdue claims"),
          ["Facility", "Receiver (TPA) ID", "Receiver (TPA)", "Payer ID", "Payer", "Claim ID", "Member ID",
           "Encounter date", "First submission", "Latest submission", "SN RA due date", "Days past SN due",
           "Net amount (AED)", "Days outstanding", "Emergency"], rows, money_cols=(13,))
    info = wb.create_sheet("About")
    for line in [f"Overdue = no remittance advice and more than {days} days ({min(days, 7)} for emergency claims) "
                 "since the latest submission.",
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
