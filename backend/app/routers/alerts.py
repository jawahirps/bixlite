"""
Facility alerts: one list of business problems per facility.

Defaults are learned from each facility's own history (not one fixed number for all)
and anchored on DHA eClaimLink Standards Notice SN 03/2019 (Claim Cycle Standardisation):
  * Submission by the 15th of the month after the service month (3.7.1)
  * First Remittance Advice + payment within 45 days of submission (3.9.1)
  * Resubmission RA within 30 days (3.9.2); resubmit within 21 days of RA (3.8.1)

Alert types
  denial_spike   denial rate (denied / net, first RA) over the last 30 days is well above
                 the facility's own previous 3 months (baseline + max(5 pts, 2 x monthly SD))
  payment_rate   share of claims that got their first RA within 45 days (SN 3.9.1), and
                 paid / net, both against the facility's own baseline
  overdue        claims with no RA more than 45 days after submission (payer breach of SN 3.9.1)
                 above the facility's normal share
  submission_gap no first submission for longer than the facility's own longest normal gap
                 (learned over 120 days) + 3 days
  sn_deadline    service month not yet submitted at the usual volume near / after the 15th
  credentials    DHPO / Riayati fetch failing with 401 / login errors
"""
import statistics
import time
from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import User
from ..security import get_current_user

router = APIRouter(prefix="/api/alerts", tags=["alerts"])

import threading

_CACHE: dict = {}
_LOCK = threading.Lock()
_TTL = 3600


def _sd(xs):
    return statistics.pstdev(xs) if len(xs) > 1 else 0.0


def _month_start(d: date, back: int = 0) -> date:
    y, m = d.year, d.month - back
    while m <= 0:
        m += 12
        y -= 1
    return date(y, m, 1)


def _compute(db: Session, today: date) -> dict:  # today can be set in the past to back-test
    facs = {r.id: r.facility_name for r in db.execute(text(
        "SELECT id, facility_name FROM facilities WHERE is_active AND facility_code <> 'GHAF-001'"))}
    if not facs:
        return {"items": [], "baselines": []}
    ids = list(facs)
    alerts, base = [], {f: {"facility_id": f, "facility_name": n} for f, n in facs.items()}

    def add(fid, kind, severity, title, detail, link, value=None, threshold=None):
        alerts.append({"facility_id": fid, "facility_name": facs[fid], "type": kind,
                       "severity": severity, "title": title, "detail": detail, "link": link,
                       "value": value, "threshold": threshold})

    # ---- 1. Denial spike (by RA date, first RA per claim) -----------------------------
    m0 = _month_start(today)           # current month start
    start = _month_start(today, 3)
    rows = db.execute(text("""
        WITH r AS (
          SELECT DISTINCT ON (facility_id, claim_id) facility_id, remittance_date d, net_amount net,
                 paid_amount paid
          FROM remittances
          WHERE remittance_date >= :start AND facility_id = ANY(:ids) AND net_amount > 0
          ORDER BY facility_id, claim_id, remittance_date, id)
        SELECT facility_id,
               CASE WHEN d >= :recent THEN 'recent' ELSE to_char(d, 'YYYY-MM') END bucket,
               sum(net) net, sum(greatest(net - coalesce(paid, 0), 0)) denied, count(*) n
        FROM r GROUP BY 1, 2
    """), {"start": start, "ids": ids, "recent": today - timedelta(days=30)}).all()
    by = {}
    for r in rows:
        by.setdefault(r.facility_id, {})[r.bucket] = (float(r.denied) / float(r.net) * 100, int(r.n))
    for fid, b in by.items():
        hist = [v[0] for k, v in b.items() if k != "recent" and v[1] >= 30
                and k < m0.strftime("%Y-%m")]
        if len(hist) < 2 or "recent" not in b or b["recent"][1] < 30:
            continue
        mean = statistics.mean(hist)
        thr = mean + max(5.0, 2 * _sd(hist))
        cur = b["recent"][0]
        base[fid].update(denial_rate_baseline=round(mean, 1), denial_rate_threshold=round(thr, 1),
                         denial_rate_last30=round(cur, 1))
        if cur > thr:
            add(fid, "denial_spike", "high" if cur > thr + 5 else "medium",
                f"Denial rate {cur:.1f}% in the last 30 days",
                f"Usual {mean:.1f}% (previous 3 months); alert above {thr:.1f}%.",
                "/denial-trends", round(cur, 1), round(thr, 1))

    # ---- 1b. Early warning: denial code spike per receiver (TPA) x code, week by week ---------
    # Last 7 RA days vs the 8 weeks before. Catches a payer rule change in its first week
    # (e.g. NAS / TPA004 MNEC-003, week of 5 Oct 2026: 2,448 lines vs ~900/week, all facilities).
    wk_end = today - timedelta(days=1)
    wk_start = wk_end - timedelta(days=6)
    rows = db.execute(text("""
        WITH r AS (
          SELECT id, facility_id, claim_id, remittance_date FROM remittances
          WHERE remittance_date BETWEEN :b0 AND :e AND facility_id = ANY(:ids)),
        c AS (
          SELECT DISTINCT ON (c.facility_id, c.claim_id) c.facility_id, c.claim_id,
                 coalesce(nullif(c.receiver_id, ''), c.payer_id) rec
          FROM claims c JOIN (SELECT DISTINCT facility_id, claim_id FROM r) k
            ON k.facility_id = c.facility_id AND k.claim_id = c.claim_id
          ORDER BY c.facility_id, c.claim_id, c.submission_date)
        SELECT c.rec, d.denial_code code, r.facility_id,
               CASE WHEN r.remittance_date >= :w0 THEN -1
                    ELSE ((:w0 - r.remittance_date) - 1) / 7 END wk,
               count(*) n, sum(greatest(coalesce(d.net, 0) - coalesce(d.paid_amount, 0), 0)) amt
        FROM remittance_activity_details d
        JOIN r ON r.id = d.remittance_id_fk
        JOIN c ON c.facility_id = r.facility_id AND c.claim_id = r.claim_id
        WHERE coalesce(d.denial_code, '') <> ''
        GROUP BY 1, 2, 3, 4
    """), {"b0": wk_start - timedelta(days=56), "e": wk_end, "w0": wk_start, "ids": ids}).all()
    from ..services.payer_names import payer_name
    agg = {}   # (rec, code) -> {"cur": n, "amt": x, "hist": [8], "facs": {fid: n}, "base_facs": set}
    for r in rows:
        a = agg.setdefault((r.rec, r.code), {"cur": 0, "amt": 0.0, "hist": [0] * 8, "facs": {}, "fac_hist": {}})
        if r.wk == -1:
            a["cur"] += r.n; a["amt"] += float(r.amt or 0)
            a["facs"][r.facility_id] = a["facs"].get(r.facility_id, 0) + r.n
        elif 0 <= r.wk < 8:
            a["hist"][r.wk] += r.n
            fh = a["fac_hist"].setdefault(r.facility_id, [0] * 8)
            fh[r.wk] += r.n
    rec_facs = {}
    for (rec, _), a in agg.items():
        rec_facs.setdefault(rec, set()).update(a["facs"]); rec_facs[rec].update(a["fac_hist"])
    for (rec, code), a in agg.items():
        mean, sd = statistics.mean(a["hist"]), _sd(a["hist"])
        cur = a["cur"]
        new_code = mean < 2 and cur >= 30
        spike = cur >= 50 and cur >= 2 * mean and cur >= mean + 3 * sd
        if not (spike or new_code) or a["amt"] < 2000:
            continue
        hit = [f for f, n in a["facs"].items()
               if n >= max(10, 2 * statistics.mean(a["fac_hist"].get(f, [0] * 8)))]
        wide = len(hit) >= max(2, 0.5 * len(rec_facs.get(rec, ())))
        rname = payer_name(rec) or rec
        ratio = f"{cur / mean:.1f}x usual" if mean >= 2 else "new this week"
        title = f"{rname}: {code} denials {cur:,} lines in 7 days ({ratio})"
        detail = (f"Usual {mean:,.0f}/week over the previous 8 weeks; AED {a['amt']:,.0f} denied this week. "
                  + (f"Seen across {len(hit)} facilities — likely a payer-side rule or adjudication change: "
                     "raise with the TPA." if wide else
                     "Limited to this facility — check coding / documentation and the scrubber rule."))
        targets = hit if (hit and not wide) else [None]
        for fid in targets:
            t_ = title
            if fid:
                fm = statistics.mean(a["fac_hist"].get(fid, [0] * 8))
                fn = a["facs"][fid]
                t_ = (f"{rname}: {code} denials {fn:,} lines in 7 days ("
                      + (f"{fn / fm:.1f}x usual" if fm >= 2 else "new this week") + ")")
            alerts.append({"facility_id": fid,
                           "facility_name": facs[fid] if fid else f"All facilities ({len(hit)})",
                           "facilities": hit, "type": "denial_code_spike",
                           "severity": "high" if (wide or cur >= 3 * max(mean, 1)) else "medium",
                           "title": t_, "detail": detail,
                           "link": f"/denial-trends?payer={rec}", "value": cur, "threshold": round(2 * mean)})

    # ---- 1c. Denial code above its usual level this month (per facility x TPA x code) --------
    # Month-to-date vs the same code's average of the 3 full months before, pro-rated by days.
    # Catches the slower build-up the weekly check can miss.
    if today.day >= 5:
        rows = db.execute(text("""
            WITH r AS (
              SELECT id, facility_id, claim_id, remittance_date FROM remittances
              WHERE remittance_date >= :s AND remittance_date < :t AND facility_id = ANY(:ids)),
            c AS (
              SELECT DISTINCT ON (c.facility_id, c.claim_id) c.facility_id, c.claim_id,
                     coalesce(nullif(c.receiver_id, ''), c.payer_id) rec
              FROM claims c JOIN (SELECT DISTINCT facility_id, claim_id FROM r) k
                ON k.facility_id = c.facility_id AND k.claim_id = c.claim_id
              ORDER BY c.facility_id, c.claim_id, c.submission_date)
            SELECT r.facility_id, c.rec, d.denial_code code, (r.remittance_date >= :m0) cur,
                   count(*) n, sum(greatest(coalesce(d.net, 0) - coalesce(d.paid_amount, 0), 0)) amt
            FROM remittance_activity_details d
            JOIN r ON r.id = d.remittance_id_fk
            JOIN c ON c.facility_id = r.facility_id AND c.claim_id = r.claim_id
            WHERE coalesce(d.denial_code, '') <> ''
            GROUP BY 1, 2, 3, 4
        """), {"s": _month_start(today, 3), "t": today, "m0": m0, "ids": ids}).all()
        from ..services.payer_names import payer_name as _pn
        from ..services.denial_codes import denial_description as _dd
        cur, base3 = {}, {}
        for r in rows:
            k = (r.facility_id, r.rec, r.code)
            if r.cur:
                cur[k] = (r.n, float(r.amt or 0))
            else:
                base3[k] = base3.get(k, 0) + r.n
        frac = (today - m0).days / 30.4
        weekly_hit = {(a["facility_id"], a["title"].split(":")[1].split()[0] if ":" in a["title"] else "")
                      for a in alerts if a["type"] == "denial_code_spike"}
        cands = []
        for (fid, rec, code), (n, amt) in cur.items():
            usual = base3.get((fid, rec, code), 0) / 3 * frac
            if n < 50 or amt < 3000 or usual < 5 or n < 2 * usual:
                continue
            if (fid, code) in weekly_hit:
                continue   # already reported by the weekly check
            cands.append((amt, fid, rec, code, n, usual))
        cands.sort(reverse=True)
        MAX_ABOVE = 10
        for amt, fid, rec, code, n, usual in cands[:MAX_ABOVE]:
            pct = round(100 * (n - usual) / usual)
            add(fid, "denial_above_usual", "high" if n >= 2.5 * usual else "medium",
                f"{_pn(rec) or rec}: {code} {n:,} lines this month (+{pct}% vs usual)",
                f"{_dd(code) or ''}. Usual ≈ {usual:,.0f} lines by this date (3-month average); "
                f"AED {amt:,.0f} denied so far. Open 'Why?' on Denial trends for the TPA reasons.",
                f"/denial-trends?payer={rec}&month={today:%Y-%m}", n, round(2 * usual))
        if len(cands) > MAX_ABOVE:
            alerts.append({"facility_id": None, "facility_name": "All facilities", "facilities": [],
                           "type": "denial_above_usual", "severity": "medium",
                           "title": f"{len(cands) - MAX_ABOVE} more denial codes above usual this month",
                           "detail": "Smaller amounts, not listed. See 'Above-usual first' on Denial trends.",
                           "link": f"/denial-trends?month={today:%Y-%m}", "value": None, "threshold": None})

    # ---- 2. Payment rate + overdue (SN 3.9.1: RA within 45 days) ------------------------
    rows = db.execute(text("""
        WITH c AS (
          SELECT facility_id, claim_id, min(submission_date) sub, max(net_amount) net
          FROM claims
          WHERE submission_date BETWEEN :s AND :e AND resubmission_type IS NULL
            AND facility_id = ANY(:ids)
          GROUP BY 1, 2),
        r AS (
          SELECT DISTINCT ON (facility_id, claim_id) facility_id, claim_id, remittance_date, net_amount, paid_amount
          FROM remittances WHERE remittance_date >= :s AND facility_id = ANY(:ids)
          ORDER BY facility_id, claim_id, remittance_date, id)
        SELECT c.facility_id, to_char(c.sub, 'YYYY-MM') m, count(*) n,
               count(*) FILTER (WHERE r.remittance_date - c.sub <= 45) ra45,
               count(*) FILTER (WHERE r.claim_id IS NULL) no_ra,
               coalesce(sum(c.net) FILTER (WHERE r.claim_id IS NULL), 0) no_ra_amt,
               sum(r.paid_amount) paid, sum(r.net_amount) FILTER (WHERE r.claim_id IS NOT NULL) rnet
        FROM c LEFT JOIN r USING (facility_id, claim_id)
        GROUP BY 1, 2
    """), {"s": _month_start(today, 7),
      # only complete submission months whose every claim is past the 45-day SN window
      "e": _month_start(today - timedelta(days=46)) - timedelta(days=1) if (today - timedelta(days=46)).day < 28 else today - timedelta(days=46),
      "ids": ids}).all()
    pm = {}
    for r in rows:
        pm.setdefault(r.facility_id, []).append(r)
    for fid, ms in pm.items():
        ms = sorted([m for m in ms if m.n >= 30], key=lambda m: m.m)
        if len(ms) < 3:
            continue
        *hist, cur = ms
        hist = hist[-4:]
        ra = [m.ra45 / m.n * 100 for m in hist]
        paid = [float(m.paid or 0) / float(m.rnet) * 100 for m in hist if m.rnet]
        cur_ra = cur.ra45 / cur.n * 100
        cur_paid = float(cur.paid or 0) / float(cur.rnet) * 100 if cur.rnet else None
        ra_thr = statistics.mean(ra) - max(8.0, 2 * _sd(ra))
        base[fid].update(ra45_baseline=round(statistics.mean(ra), 1), ra45_threshold=round(ra_thr, 1),
                         ra45_latest=round(cur_ra, 1), ra45_latest_month=cur.m)
        if cur_ra < ra_thr:
            add(fid, "payment_rate", "medium",
                f"Only {cur_ra:.0f}% of {cur.m} claims remitted within 45 days",
                f"SN 03/2019 3.9.1 requires RA + payment within 45 days. Usual {statistics.mean(ra):.0f}%; "
                f"alert below {ra_thr:.0f}%. Payer may owe a 0.03%/day delay fee.",
                "/turnaround", round(cur_ra, 1), round(ra_thr, 1))
        if paid and cur_paid is not None:
            p_thr = statistics.mean(paid) - max(5.0, 2 * _sd(paid))
            base[fid].update(paid_pct_baseline=round(statistics.mean(paid), 1),
                             paid_pct_threshold=round(p_thr, 1), paid_pct_latest=round(cur_paid, 1))
            if cur_paid < p_thr:
                add(fid, "payment_rate", "high" if cur_paid < p_thr - 5 else "medium",
                    f"Paid {cur_paid:.0f}% of claimed for {cur.m} claims",
                    f"Usual {statistics.mean(paid):.0f}% at first RA; alert below {p_thr:.0f}%.",
                    "/denial-trends", round(cur_paid, 1), round(p_thr, 1))
        # Overdue: claims with no RA at all, >45 days after submission, as % of the month.
        od = [m.no_ra / m.n * 100 for m in hist]
        od_thr = max(10.0, statistics.mean(od) + 2 * _sd(od))
        tot_n = sum(m.no_ra for m in ms[-3:])
        tot_amt = float(sum(m.no_ra_amt for m in ms[-3:]))
        share = tot_n / max(1, sum(m.n for m in ms[-3:])) * 100
        base[fid].update(overdue_pct_threshold=round(od_thr, 1), overdue_pct_latest=round(share, 1),
                         overdue_claims=tot_n, overdue_amount=round(tot_amt, 2))
        if share > od_thr and tot_amt >= 10000:
            add(fid, "overdue", "high" if share > 2 * od_thr else "medium",
                f"{tot_n:,} claims ({share:.0f}%) with no RA after 45 days",
                f"AED {tot_amt:,.0f} outstanding beyond SN 03/2019 3.9.1 (last 3 submission months). "
                f"Alert above {od_thr:.0f}%.",
                "/turnaround", round(share, 1), round(od_thr, 1))

    # ---- 4. Submission cadence + SN 3.7.1 deadline ------------------------------------
    rows = db.execute(text("""
        SELECT DISTINCT facility_id, submission_date d FROM claims
        WHERE submission_date BETWEEN :s AND :t AND resubmission_type IS NULL AND facility_id = ANY(:ids)
        ORDER BY 1, 2
    """), {"s": today - timedelta(days=150), "t": today, "ids": ids}).all()
    days = {}
    for r in rows:
        days.setdefault(r.facility_id, []).append(r.d)
    for fid, ds in days.items():
        gaps = [(b - a).days for a, b in zip(ds, ds[1:]) if a >= today - timedelta(days=120)]
        if len(gaps) < 3:
            continue
        allowed = max(gaps) + 3
        since = (today - ds[-1]).days
        pattern = "monthly batch" if statistics.median(gaps) >= 20 else (
            "daily" if statistics.median(gaps) <= 1.5 else f"every ~{statistics.median(gaps):.0f} days")
        base[fid].update(submission_pattern=pattern, submission_gap_allowed=allowed,
                         last_submission=ds[-1].isoformat(), days_since_submission=since)
        if since > allowed:
            add(fid, "submission_gap", "high",
                f"No new claims submitted for {since} days",
                f"Usual pattern: {pattern}; longest normal gap {max(gaps)} days (alert after {allowed}). "
                f"Check the facility ERP / eClaimLink access.",
                "/jobs", since, allowed)

    svc = _month_start(today, 1)  # service month due by the 15th of this month
    rows = db.execute(text("""
        SELECT facility_id, date_trunc('month', encounter_start)::date sm, count(*) n
        FROM claims
        WHERE encounter_start >= :s AND encounter_start < :e AND resubmission_type IS NULL
          AND submission_date <= :t AND facility_id = ANY(:ids)
          AND submission_date <= (date_trunc('month', encounter_start) + interval '1 month' + make_interval(days => :d))::date
        GROUP BY 1, 2
    """), {"s": _month_start(today, 4), "e": m0, "t": today, "ids": ids,
           "d": max(0, today.day - 1)}).all()
    vol = {}
    for r in rows:
        vol.setdefault(r.facility_id, {})[r.sm] = r.n
    if today.day >= 10:
        for fid in ids:
            v = vol.get(fid, {})
            prev = [v.get(_month_start(today, k), 0) for k in (2, 3, 4)]
            prev = [p for p in prev if p > 0]
            if len(prev) < 2:
                continue
            # prior months measured at the same day-of-month cut-off, so like is compared with like
            expected = statistics.mean(prev)
            got = v.get(svc, 0)
            base[fid].update(sn_month=svc.strftime("%Y-%m"), sn_submitted=got, sn_expected=round(expected))
            if got < 0.7 * expected:
                late = today.day > 15
                add(fid, "sn_deadline", "high" if late else "medium",
                    f"{svc:%b %Y} services: {got:,} of ~{expected:,.0f} usual claims submitted"
                    + (" — past the 15th" if late else ""),
                    "SN 03/2019 3.7.1: submit by the 15th of the next month; late claims carry a "
                    "0.03%/day delay fee on the net amount." if late else
                    f"Deadline {svc.year if svc.month < 12 else svc.year + 1}-"
                    f"{(svc.month % 12) + 1:02d}-15 (SN 03/2019 3.7.1).",
                    "/jobs", got, round(0.7 * expected))

    # ---- 7. Credentials (DHPO / Riayati) ---------------------------------------------
    rows = db.execute(text("""
        SELECT DISTINCT ON (facility_id, src) facility_id, src, status, started_at, err
        FROM (SELECT facility_id, CASE WHEN fetch_type = 'Riayati' THEN 'Riayati' ELSE 'DHPO' END src,
                     status, started_at, error_detail::text err
              FROM fetch_log WHERE started_at > now() - interval '7 days' AND status <> 'running'
                AND facility_id = ANY(:ids)) x
        ORDER BY facility_id, src, started_at DESC
    """), {"ids": ids}).all()
    for r in rows:
        e = (r.err or "").lower()
        if r.status != "success" and any(k in e for k in ("401", "unauthor", "login", "password", "credential",
                                                          "-2", "invalid user")):
            add(r.facility_id, "credentials", "high",
                f"{r.src} login rejected",
                f"Last {r.src} fetch ({r.started_at:%d %b %H:%M}) was refused (401 / login). "
                "Check the account in Facility settings.",
                "/facility-settings")

    # every link opens the target page on the alert's own facility ("" = all facilities)
    for a in alerts:
        sep = "&" if "?" in a["link"] else "?"
        a["link"] = f'{a["link"]}{sep}facility_id={a["facility_id"] or ""}'
        if a["type"] == "denial_code_spike":   # the spike is this week: open on the current month
            a["link"] += f"&month={today:%Y-%m}"
    order = {"high": 0, "medium": 1}
    alerts.sort(key=lambda a: (order.get(a["severity"], 2), a["type"] != "denial_code_spike", a["facility_name"]))
    return {"items": alerts, "baselines": sorted(base.values(), key=lambda b: b["facility_name"])}


def _get(db: Session, refresh: bool = False):
    """One computation at a time; concurrent callers wait for it instead of repeating it."""
    today = date.today()
    hit = _CACHE.get(today)
    if not refresh and hit and time.time() - hit[0] <= _TTL:
        return hit
    with _LOCK:
        hit = _CACHE.get(today)
        if refresh or not hit or time.time() - hit[0] > _TTL:
            hit = (time.time(), _compute(db, today))
            _CACHE.clear()
            _CACHE[today] = hit
    return hit


def warm():
    """Compute alerts in the background at startup so the first page view is instant."""
    from ..database import SessionLocal

    def run():
        db = SessionLocal()
        try:
            _get(db)
        except Exception:
            pass
        finally:
            db.close()
    threading.Thread(target=run, daemon=True).start()


@router.get("")
def list_alerts(
    refresh: bool = Query(False),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    hit = _get(db, refresh)
    data = hit[1]
    items, baselines = data["items"], data["baselines"]
    if user.role != "admin":
        if user.facility_id is None:
            raise HTTPException(403, "No facility assigned")
        items = [a for a in items if a["facility_id"] == user.facility_id
                 or (a["facility_id"] is None and user.facility_id in a.get("facilities", []))]
        baselines = [b for b in baselines if b["facility_id"] == user.facility_id]
    return {"generated_at": datetime.fromtimestamp(hit[0]).astimezone(), "count": len(items),
            "high": sum(1 for a in items if a["severity"] == "high"),
            "items": items, "baselines": baselines,
            "source": "Defaults learned per facility; timelines per DHA SN 03/2019"}
