"""
Ghaf App — Analytics & Financial Insights endpoints.

Two responsibilities:
  1. /summary — lightweight KPI numbers computed directly from the app DB,
     used for the in-app dashboard cards (fast, no Metabase dependency).
  2. /embed/{dashboard_key} — issues a short-lived signed Metabase
     embedding URL for the heavy reporting (payer breakdowns, collection
     charts, AR aging, denials) rendered inside the React
     "Analytics & Financial Insights" tab via <iframe>.
"""
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import and_, func, text
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Claim, ClaimActivity, ClinicalProfile, Facility, LoginLog, RawXmlStore, Remittance, User
from ..schemas import DashboardSummary, MetabaseEmbedResponse
from ..security import resolve_facility, check_facility, allowed_facility_ids, is_clinic_user, get_current_user
from ..services.metabase import build_embed_url, list_dashboard_keys

router = APIRouter(prefix="/api/analytics", tags=["analytics"])


# ---------------------------------------------------------------------------
# Shared filters for the Submissions / Resubmissions / Remittance / Denials /
# Clinicians / Operations / Insurance / Department report tabs.
# ---------------------------------------------------------------------------
@dataclass
class AnalyticsFilters:
    facility_id: Optional[int] = None
    facility_ids: Optional[str] = None   # comma-separated IDs for multi-select
    receiver_id: Optional[str] = None
    payer_id: Optional[str] = None
    encounter_type: Optional[str] = None
    department: Optional[str] = None
    date_from: Optional[date] = None
    date_to: Optional[date] = None


def get_analytics_filters(
    facility_id: Optional[int] = None,
    facility_ids: Optional[str] = None,
    receiver_id: Optional[str] = None,
    payer_id: Optional[str] = None,
    encounter_type: Optional[str] = None,
    department: Optional[str] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> AnalyticsFilters:
    allowed = allowed_facility_ids(db, user)
    if allowed is not None:   # RBAC: never "all of BIX Lite" for non-admins
        ids = [int(x) for x in (facility_ids or "").split(",") if x.strip().isdigit()]
        for fid in ids + ([facility_id] if facility_id else []):
            check_facility(db, user, fid)
        if not facility_id and not ids:
            facility_ids = ",".join(str(x) for x in sorted(allowed)) or "-1"
    return AnalyticsFilters(
        facility_id=facility_id,
        facility_ids=facility_ids,
        receiver_id=receiver_id,
        payer_id=payer_id,
        encounter_type=encounter_type,
        department=department,
        date_from=date_from,
        date_to=date_to,
    )


def _apply_claim_filters(query, f: AnalyticsFilters):
    # facility_ids (multi-select) takes priority over facility_id (single)
    if f.facility_ids:
        ids = [int(x) for x in f.facility_ids.split(",") if x.strip().isdigit()]
        if ids:
            query = query.filter(Claim.facility_id.in_(ids))
    elif f.facility_id:
        query = query.filter(Claim.facility_id == f.facility_id)
    if f.receiver_id:
        query = query.filter(Claim.receiver_id == f.receiver_id)
    if f.payer_id:
        query = query.filter(Claim.payer_id == f.payer_id)
    if f.encounter_type:
        query = query.filter(Claim.encounter_type == f.encounter_type)
    if f.department:
        query = query.filter(Claim.department == f.department)
    # by encounter date, like the Dashboard's claim cycle; date_to includes the whole day
    if f.date_from:
        query = query.filter(Claim.encounter_start >= f.date_from)
    if f.date_to:
        query = query.filter(Claim.encounter_start < f.date_to + timedelta(days=1))
    return query


def _remits_with_claims(db: Session, f: AnalyticsFilters):
    """Remittance rows joined to their parent Claim, with filters applied."""
    query = db.query(Remittance, Claim).join(
        Claim,
        and_(Claim.facility_id == Remittance.facility_id, Claim.claim_id == Remittance.claim_id),
    )
    if f.facility_ids:
        ids = [int(x) for x in f.facility_ids.split(",") if x.strip().isdigit()]
        if ids:
            query = query.filter(Remittance.facility_id.in_(ids))
    elif f.facility_id:
        query = query.filter(Remittance.facility_id == f.facility_id)
    if f.receiver_id:
        query = query.filter(Claim.receiver_id == f.receiver_id)
    if f.payer_id:
        query = query.filter(Claim.payer_id == f.payer_id)  # claim-side payer, same as the claim queries
    if f.encounter_type:
        query = query.filter(Claim.encounter_type == f.encounter_type)
    if f.department:
        query = query.filter(Claim.department == f.department)
    # by encounter date, like the Dashboard's claim cycle; date_to includes the whole day
    if f.date_from:
        query = query.filter(Claim.encounter_start >= f.date_from)
    if f.date_to:
        query = query.filter(Claim.encounter_start < f.date_to + timedelta(days=1))
    return query


def _money(value) -> float:
    return float(value or 0)


def _month_label(d) -> str:
    return d.strftime("%Y-%m") if d else "Unknown"


@router.get("/summary", response_model=DashboardSummary)
def dashboard_summary(
    facility_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # Admins see all facilities unless they explicitly pass facility_id.
    # Non-admin users are always scoped to their own facility.
    facility_scope = resolve_facility(db, current_user, facility_id)  # Super Admin: None = all facilities

    claims_q = db.query(Claim)
    remits_q = db.query(Remittance)
    raw_q = db.query(RawXmlStore)
    profiles_q = db.query(ClinicalProfile)

    if facility_scope is not None:
        claims_q = claims_q.filter(Claim.facility_id == facility_scope)
        remits_q = remits_q.filter(Remittance.facility_id == facility_scope)
        raw_q = raw_q.filter(RawXmlStore.facility_id == facility_scope)
        profiles_q = profiles_q.filter(ClinicalProfile.facility_id == facility_scope)

    total_claims = claims_q.count()
    # Money per the claim-cycle definition: one row per claim (latest submission), de-duplicated RAs,
    # paid capped at net, denied = net - paid only for claims with an RA.
    cyc = _cycle_rows(db, AnalyticsFilters(facility_id=facility_scope), "claims")
    total_net = Decimal(str(round(sum(float(r["net"]) for r in cyc), 2)))
    total_paid = Decimal(str(round(sum(float(r["paid"]) for r in cyc), 2)))
    total_denied = Decimal(str(round(sum(float(r["denied"]) for r in cyc), 2)))

    pending_raw = raw_q.filter(RawXmlStore.parse_status == "pending").count()
    quarantined_raw = raw_q.filter(RawXmlStore.parse_status == "quarantined").count()
    active_profiles = profiles_q.filter(ClinicalProfile.is_active.is_(True)).count()

    since = datetime.utcnow() - timedelta(hours=24)
    recent_logins = (
        db.query(LoginLog)
        .filter(LoginLog.success.is_(True))
        .filter(LoginLog.created_at >= since)
        .count()
    )

    return DashboardSummary(
        total_claims=total_claims,
        total_net_amount=total_net,
        total_paid_amount=total_paid,
        total_denial_amount=total_denied,
        pending_raw_xml=pending_raw,
        quarantined_raw_xml=quarantined_raw,
        active_clinical_profiles=active_profiles,
        recent_logins_24h=recent_logins,
    )


@router.get("/dashboards", response_model=list[str])
def dashboards(_: User = Depends(get_current_user)):
    """Available Metabase dashboard keys for the Analytics & Financial Insights tab."""
    return list_dashboard_keys()


@router.get("/embed/{dashboard_key}", response_model=MetabaseEmbedResponse)
def embed_url(
    dashboard_key: str,
    facility_id: Optional[int] = None,
    current_user: User = Depends(get_current_user),
):
    """
    Returns a signed Metabase iframe URL for the requested dashboard.

    Row-level security: the facility scope (current user's facility, or an
    explicit facility_id for admins) is passed as a Metabase dashboard
    parameter named `facility_id`, which should be wired to a SQL filter
    on the underlying questions.
    """
    facility_scope = resolve_facility(None, current_user, facility_id)

    params = {}
    if facility_scope is not None:
        params["facility_id"] = facility_scope

    try:
        iframe_url, expires_in = build_embed_url(dashboard_key, params=params)
    except KeyError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Unknown dashboard: {dashboard_key}")
    except RuntimeError as e:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(e))

    return MetabaseEmbedResponse(dashboard_key=dashboard_key, iframe_url=iframe_url, expires_in=expires_in)


# ---------------------------------------------------------------------------
# Filter options (Facility / Receiver / Payer / Encounter type / Department)
# ---------------------------------------------------------------------------
_FILTER_CACHE: dict = {}
_REPORT_CACHE: dict = {}


def _cached_report(fn):
    """Cache a report for an hour per filter set: all-facility runs scan every claim (7–22 s)."""
    import functools

    @functools.wraps(fn)
    def wrap(f: AnalyticsFilters, db: Session, _: User):
        key = (fn.__name__, repr(f))
        hit = _REPORT_CACHE.get(key)
        if hit and time.time() - hit[0] < 3600:
            return hit[1]
        out = fn(f=f, db=db, _=_)
        _REPORT_CACHE[key] = (time.time(), out)
        if len(_REPORT_CACHE) > 300:
            _REPORT_CACHE.pop(min(_REPORT_CACHE, key=lambda k: _REPORT_CACHE[k][0]))
        return out
    return wrap


@router.get("/filters")
def analytics_filter_options(
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    facilities = [
        {"id": fac.id, "code": fac.facility_code, "name": fac.facility_name}
        for fac in db.query(Facility).filter(Facility.is_active.is_(True)).order_by(Facility.facility_name).all()
        if allowed_facility_ids(db, _) is None or fac.id in allowed_facility_ids(db, _)
    ]
    # receiver/payer/encounter type/department lists scan every claim (~11 s) and only change with the nightly
    # fetch, so they are cached for an hour; facilities stay live
    hit = _FILTER_CACHE.get("lists")
    if hit and time.time() - hit[0] < 3600:
        return {"facilities": facilities, **hit[1]}

    receivers = (
        db.query(Claim.receiver_id, Claim.receiver_name)
        .filter(Claim.receiver_id.isnot(None))
        .distinct()
        .order_by(Claim.receiver_name)
        .all()
    )
    payers = (
        db.query(Claim.payer_id, Claim.payer_name)
        .filter(Claim.payer_id.isnot(None))
        .distinct()
        .order_by(Claim.payer_name)
        .all()
    )
    encounter_types = [
        row[0]
        for row in db.query(Claim.encounter_type)
        .filter(Claim.encounter_type.isnot(None))
        .distinct()
        .order_by(Claim.encounter_type)
        .all()
    ]
    departments = [
        row[0]
        for row in db.query(Claim.department)
        .filter(Claim.department.isnot(None))
        .distinct()
        .order_by(Claim.department)
        .all()
    ]

    lists = {
        "receivers": [{"id": r, "name": n} for r, n in receivers],
        "payers": [{"id": p, "name": n} for p, n in payers],
        "encounter_types": encounter_types,
        "departments": departments,
    }
    _FILTER_CACHE["lists"] = (time.time(), lists)
    return {"facilities": facilities, **lists}


# ---------------------------------------------------------------------------
# Claim-cycle money (same definition as the Dashboard's claim cycle, dashboard._SQL):
# one row per claim = its latest submission; paid = all RA payments for the claim with identical RA loads counted
# once, capped at the claim net; denied = net - paid only once the claim has an RA (otherwise it is awaiting).
# Grouped paid/denied therefore add up to the Dashboard's Paid / Denied cards for the same filters.
# ---------------------------------------------------------------------------
_CYCLE_CACHE: dict = {}


def _cycle_where(f: AnalyticsFilters):
    conds, params = [], {}
    ids = [int(x) for x in (f.facility_ids or "").split(",") if x.strip().isdigit()]
    if ids:
        conds.append("facility_id = ANY(:fids)"); params["fids"] = ids
    elif f.facility_id:
        conds.append("facility_id = :fid"); params["fid"] = f.facility_id
    for col in ("receiver_id", "payer_id", "encounter_type", "department"):
        if getattr(f, col):
            conds.append(f"{col} = :{col}"); params[col] = getattr(f, col)
    if f.date_from:
        conds.append("encounter_start >= :d0"); params["d0"] = f.date_from
    if f.date_to:
        conds.append("encounter_start < :d1"); params["d1"] = f.date_to + timedelta(days=1)
    conds.append("facility_id NOT IN (SELECT id FROM facilities WHERE facility_code = 'GHAF-001')")
    return " AND ".join(conds), params


_CYCLE_CTE = """
WITH c AS (
  SELECT DISTINCT ON (facility_id, claim_id) id, facility_id, claim_id, coalesce(net_amount, 0) net,
         to_char(submission_date, 'YYYY-MM') sub_m, to_char(encounter_start, 'YYYY-MM') enc_m,
         payer_name, receiver_name, clinician_name, department
  FROM claims WHERE {where}
  ORDER BY facility_id, claim_id, submission_date DESC NULLS LAST, id DESC),
ra AS (
  SELECT DISTINCT ON (r.facility_id, r.claim_id, r.ra_file_id, r.net_amount, r.paid_amount, r.denial_amount)
         r.id, r.facility_id, r.claim_id, coalesce(r.paid_amount, 0) paid, r.remittance_date,
         nullif(r.denial_code, '') dcode, r.denial_description ddesc
  FROM remittances r JOIN c USING (facility_id, claim_id)
  ORDER BY r.facility_id, r.claim_id, r.ra_file_id, r.net_amount, r.paid_amount, r.denial_amount, r.id DESC),
p AS (
  SELECT facility_id, claim_id, sum(paid) paid,
         (array_agg(dcode ORDER BY remittance_date DESC, id DESC) FILTER (WHERE dcode IS NOT NULL))[1] dcode,
         (array_agg(ddesc ORDER BY remittance_date DESC, id DESC) FILTER (WHERE dcode IS NOT NULL))[1] ddesc
  FROM ra GROUP BY 1, 2),
cc AS (
  SELECT c.*, p.claim_id IS NOT NULL remitted, least(greatest(coalesce(p.paid, 0), 0), c.net) paid,
         CASE WHEN p.claim_id IS NOT NULL THEN c.net - least(greatest(coalesce(p.paid, 0), 0), c.net) ELSE 0 END denied,
         p.dcode, p.ddesc
  FROM c LEFT JOIN p USING (facility_id, claim_id))
"""

_CLAIMS_SQL = _CYCLE_CTE + "SELECT * FROM cc"

# Remittance tab: claim-cycle rows plus, per claim, the latest RA month, the discount on the de-duplicated RAs and the
# turnaround (latest RA date - latest submission date, in days).
_REMIT_SQL = _CYCLE_CTE + """,
rx AS (
  SELECT ra.facility_id, ra.claim_id, max(ra.remittance_date) last_ra, sum(coalesce(r.discount_amount, 0)) discount
  FROM ra JOIN remittances r ON r.id = ra.id GROUP BY 1, 2)
SELECT cc.*, rx.discount, to_char(rx.last_ra, 'YYYY-MM') ra_m,
       (rx.last_ra::date - cl.submission_date::date) tat
FROM cc JOIN claims cl ON cl.id = cc.id
LEFT JOIN rx ON rx.facility_id = cc.facility_id AND rx.claim_id = cc.claim_id
"""

# Service lines of each claim's latest submission. Denied per line = line net minus that line's payments across the
# de-duplicated RAs, coded with the line's latest denial code; then each claim's lines are scaled so they add up to
# the claim's denied (as in portal.portal_dashboard). Paid is spread the same way over each line's payments. Claims
# with no unpaid/paid line detail fall back to line net shares; claims without lines get one 'Unknown' line.
_LINES_SQL = _CYCLE_CTE + """,
ap AS (
  SELECT ra.facility_id, ra.claim_id, ad.activity_id, sum(coalesce(ad.paid_amount, 0)) paid,
         (array_agg(nullif(ad.denial_code, '') ORDER BY ra.remittance_date DESC, ra.id DESC)
            FILTER (WHERE nullif(ad.denial_code, '') IS NOT NULL))[1] dcode,
         (array_agg(ad.denial_description ORDER BY ra.remittance_date DESC, ra.id DESC)
            FILTER (WHERE nullif(ad.denial_code, '') IS NOT NULL))[1] ddesc
  FROM ra JOIN remittance_activity_details ad ON ad.remittance_id_fk = ra.id
  GROUP BY 1, 2, 3),
l AS (
  SELECT cc.id cid, cc.paid cpaid, cc.denied cden, ca.activity_code code, ca.activity_description descr,
         coalesce(ca.net, 0) net,
         greatest(coalesce(ca.net, 0) - coalesce(ap.paid, 0), 0) un,
         least(greatest(coalesce(ap.paid, 0), 0), greatest(coalesce(ca.net, 0), 0)) pr,
         coalesce(ap.dcode, cc.dcode) dcode, CASE WHEN ap.dcode IS NOT NULL THEN ap.ddesc ELSE cc.ddesc END ddesc
  FROM cc JOIN claim_activities ca ON ca.claim_id_fk = cc.id
  LEFT JOIN ap ON ap.facility_id = cc.facility_id AND ap.claim_id = cc.claim_id AND ap.activity_id = ca.activity_id),
w AS (
  SELECT l.*, sum(un) OVER k su, sum(pr) OVER k sp, sum(greatest(net, 0)) OVER k sn, count(*) OVER k n
  FROM l WINDOW k AS (PARTITION BY cid))
SELECT code, descr, net, dcode, ddesc,
       CASE WHEN cden = 0 THEN 0 WHEN su > 0.005 THEN un * cden / su
            WHEN sn > 0.005 THEN greatest(net, 0) * cden / sn ELSE cden / n END denied,
       CASE WHEN cpaid = 0 THEN 0 WHEN sp > 0.005 THEN pr * cpaid / sp
            WHEN sn > 0.005 THEN greatest(net, 0) * cpaid / sn ELSE cpaid / n END paid
FROM w
UNION ALL
SELECT 'Unknown', NULL, cc.net, cc.dcode, cc.ddesc, cc.denied, cc.paid
FROM cc WHERE NOT EXISTS (SELECT 1 FROM claim_activities ca WHERE ca.claim_id_fk = cc.id)
"""


def _cycle_rows(db: Session, f: AnalyticsFilters, kind: str):
    """Per-claim ('claims') or per-service-line ('lines') rows for the filters, cached for an hour."""
    key = (kind, repr(f))
    hit = _CYCLE_CACHE.get(key)
    if hit and time.time() - hit[0] < 3600:
        return hit[1]
    where, params = _cycle_where(f)
    sql = (_CLAIMS_SQL if kind == "claims" else _LINES_SQL).format(where=where)
    rows = db.execute(text(sql), params).mappings().all()
    _CYCLE_CACHE[key] = (time.time(), rows)
    if len(_CYCLE_CACHE) > 60:
        _CYCLE_CACHE.pop(min(_CYCLE_CACHE, key=lambda k: _CYCLE_CACHE[k][0]))
    return rows


def _group(rows, keyfn):
    """Sum claimed/paid/denied and count claims per key."""
    out: dict = {}
    for r in rows:
        g = out.setdefault(keyfn(r), {"claimed": 0.0, "paid": 0.0, "denied": 0.0, "n": 0})
        g["claimed"] += float(r["net"]); g["paid"] += float(r["paid"]); g["denied"] += float(r["denied"]); g["n"] += 1
    return out


def _r2(x) -> float:
    return round(float(x or 0), 2)


# ---------------------------------------------------------------------------
# Submissions
# ---------------------------------------------------------------------------
@router.get("/submissions")
@_cached_report
def submissions_report(
    f: AnalyticsFilters = Depends(get_analytics_filters),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    claims = _cycle_rows(db, f, "claims")
    total_submitted_amount = sum(float(r["net"]) for r in claims)
    total_paid_amount = sum(float(r["paid"]) for r in claims)
    rejected_amount = sum(float(r["denied"]) for r in claims)
    not_remitted_amount = sum(float(r["net"]) for r in claims if not r["remitted"])

    monthly_trend = [
        {"month": m, "claimed_amount": _r2(g["claimed"]), "claim_count": g["n"],
         "paid_amount": _r2(g["paid"]), "rejected_amount": _r2(g["denied"])}
        for m, g in sorted(_group(claims, lambda r: r["sub_m"] or "Unknown").items())
    ]

    clin = sorted(_group(claims, lambda r: (r["clinician_name"], r["department"])).items(),
                  key=lambda kv: -kv[1]["claimed"])[:10]
    by_clinician = [
        {
            "clinician": c or "Unknown",
            "department": d or "Unknown",
            "claimed_amount": _r2(g["claimed"]),
            "pct_of_total": round(g["claimed"] / total_submitted_amount * 100, 2) if total_submitted_amount else 0,
        }
        for (c, d), g in clin
    ]

    svc: dict = {}
    for r in _cycle_rows(db, f, "lines"):
        g = svc.setdefault((r["code"], r["descr"]), [0.0, 0.0, 0.0])
        g[0] += float(r["net"]); g[1] += float(r["paid"]); g[2] += float(r["denied"])
    by_service = [
        {"code": code, "description": descr, "claimed_amount": _r2(g[0]), "paid_amount": _r2(g[1]),
         "denied_amount": _r2(g[2])}
        for (code, descr), g in sorted(svc.items(), key=lambda kv: -kv[1][0])
    ]

    return {
        "kpis": {
            "total_submitted_amount": _r2(total_submitted_amount),
            "total_submitted_claims": len(claims),
            "total_paid_amount": _r2(total_paid_amount),
            "rejected_amount": _r2(rejected_amount),
            "remitted_amount": _r2(total_paid_amount + rejected_amount),
            "not_remitted_amount": _r2(not_remitted_amount),
        },
        "monthly_trend": monthly_trend,
        "by_clinician": by_clinician,
        "by_service": by_service,
    }

# ---------------------------------------------------------------------------
# Resubmissions
# ---------------------------------------------------------------------------
@router.get("/resubmissions")
def resubmissions_report(
    f: AnalyticsFilters = Depends(get_analytics_filters),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    claims_q = _apply_claim_filters(db.query(Claim), f)
    resubmitted_q = claims_q.filter(Claim.resubmission_count > 0)

    total_claims = claims_q.count()
    total_resubmitted_claims = resubmitted_q.count()
    total_resubmissions = resubmitted_q.with_entities(func.coalesce(func.sum(Claim.resubmission_count), 0)).scalar() or 0
    resubmitted_amount = _money(resubmitted_q.with_entities(func.coalesce(func.sum(Claim.net_amount), 0)).scalar())
    resubmission_rate = round(total_resubmitted_claims / total_claims * 100, 2) if total_claims else 0

    monthly_rows = (
        resubmitted_q.with_entities(
            func.to_char(Claim.submission_date, "YYYY-MM").label("month"),
            func.count(Claim.id).label("claim_count"),
            func.coalesce(func.sum(Claim.net_amount), 0).label("amount"),
        )
        .group_by("month")
        .order_by("month")
        .all()
    )
    monthly_trend = [{"month": r.month, "claim_count": r.claim_count, "amount": _money(r.amount)} for r in monthly_rows]

    payer_rows = (
        resubmitted_q.with_entities(
            Claim.payer_name,
            func.count(Claim.id).label("claim_count"),
            func.coalesce(func.sum(Claim.net_amount), 0).label("amount"),
        )
        .group_by(Claim.payer_name)
        .order_by(func.count(Claim.id).desc())
        .limit(10)
        .all()
    )
    by_payer = [{"payer": r.payer_name or "Unknown", "claim_count": r.claim_count, "amount": _money(r.amount)} for r in payer_rows]

    reason_q = _remits_with_claims(db, f).filter(Claim.resubmission_count > 0).filter(Remittance.denial_code.isnot(None))
    reason_rows = (
        reason_q.with_entities(
            Remittance.denial_code,
            Remittance.denial_description,
            func.count(Remittance.id).label("count"),
        )
        .group_by(Remittance.denial_code, Remittance.denial_description)
        .order_by(func.count(Remittance.id).desc())
        .limit(10)
        .all()
    )
    by_reason = [{"code": r.denial_code, "description": r.denial_description, "count": r.count} for r in reason_rows]

    return {
        "kpis": {
            "total_resubmitted_claims": total_resubmitted_claims,
            "total_resubmissions": int(total_resubmissions),
            "resubmitted_amount": resubmitted_amount,
            "resubmission_rate_pct": resubmission_rate,
        },
        "monthly_trend": monthly_trend,
        "by_payer": by_payer,
        "by_reason": by_reason,
    }


# ---------------------------------------------------------------------------
# Remittance
# ---------------------------------------------------------------------------
@router.get("/remittance")
def remittance_report(
    f: AnalyticsFilters = Depends(get_analytics_filters),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    where, params = _cycle_where(f)
    rows = db.execute(text(_REMIT_SQL.format(where=where)), params).mappings().all()
    remitted = [r for r in rows if r["remitted"]]
    total_paid = round(sum(float(r["paid"]) for r in remitted), 2)
    total_denied = round(sum(float(r["denied"]) for r in remitted), 2)
    total_discount = round(sum(float(r["discount"] or 0) for r in remitted), 2)
    remitted_claims = len(remitted)
    tat = [r["tat"] for r in remitted if r["tat"] is not None]
    avg_turnaround_days = round(sum(tat) / len(tat), 1) if tat else 0

    months: dict = {}
    for r in remitted:
        m = months.setdefault(r["ra_m"] or "Unknown", {"paid": 0.0, "denied": 0.0, "discount": 0.0})
        m["paid"] += float(r["paid"]); m["denied"] += float(r["denied"]); m["discount"] += float(r["discount"] or 0)
    monthly_trend = [
        {"month": k, "paid_amount": round(v["paid"], 2), "denied_amount": round(v["denied"], 2),
         "discount_amount": round(v["discount"], 2)}
        for k, v in sorted(months.items())
    ]

    buckets = {"0-15": 0.0, "16-30": 0.0, "31-45": 0.0, "46-60": 0.0, "60+": 0.0}
    for r in remitted:
        amt = float(r["paid"])
        d = r["tat"] or 0
        if d <= 15:
            buckets["0-15"] += amt
        elif d <= 30:
            buckets["16-30"] += amt
        elif d <= 45:
            buckets["31-45"] += amt
        elif d <= 60:
            buckets["46-60"] += amt
        else:
            buckets["60+"] += amt
    aging_buckets = [{"bucket": k, "amount": round(v, 2)} for k, v in buckets.items()]

    payer_g: dict = {}
    for r in remitted:
        g = payer_g.setdefault(r["payer_name"] or "Unknown", [0.0, 0.0])
        g[0] += float(r["paid"]); g[1] += float(r["denied"])
    by_payer = [{"payer": k, "paid_amount": round(v[0], 2), "denied_amount": round(v[1], 2)}
                for k, v in sorted(payer_g.items(), key=lambda kv: -kv[1][0])[:10]]

    return {
        "kpis": {
            "total_paid_amount": total_paid,
            "total_denied_amount": total_denied,
            "total_discount_amount": total_discount,
            "remitted_claims": remitted_claims,
            "avg_turnaround_days": avg_turnaround_days,
        },
        "monthly_trend": monthly_trend,
        "aging_buckets": aging_buckets,
        "by_payer": by_payer,
    }


# ---------------------------------------------------------------------------
# Denials
# ---------------------------------------------------------------------------
@router.get("/denials")
@_cached_report
def denials_report(
    f: AnalyticsFilters = Depends(get_analytics_filters),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    claims = _cycle_rows(db, f, "claims")
    denied = [r for r in claims if float(r["denied"]) > 0.005]
    total_denial_amount = sum(float(r["denied"]) for r in denied)
    # Denial rate = claims with an unpaid balance after RA / all claims (one row per claim)
    denial_rate = round(len(denied) / len(claims) * 100, 2) if claims else 0

    monthly_trend = [{"month": m, "amount": _r2(g["denied"]), "count": g["n"]}
                     for m, g in sorted(_group(denied, lambda r: r["enc_m"] or "Unknown").items())]

    codes: dict = {}
    for r in _cycle_rows(db, f, "lines"):
        amt = float(r["denied"])
        if amt <= 0.005:
            continue
        g = codes.setdefault(r["dcode"] or "No code", {"amount": 0.0, "count": 0, "description": None})
        g["amount"] += amt; g["count"] += 1
        g["description"] = g["description"] or r["ddesc"]
    by_code = [{"code": k, "description": g["description"], "amount": _r2(g["amount"]), "count": g["count"]}
               for k, g in sorted(codes.items(), key=lambda kv: -kv[1]["amount"])]

    by_payer = [{"payer": k or "Unknown", "amount": _r2(g["denied"])}
                for k, g in sorted(_group(denied, lambda r: r["payer_name"]).items(), key=lambda kv: -kv[1]["denied"])]
    by_clinician = [{"clinician": k or "Unknown", "amount": _r2(g["denied"])}
                    for k, g in sorted(_group(denied, lambda r: r["clinician_name"]).items(),
                                       key=lambda kv: -kv[1]["denied"])]

    return {
        "kpis": {
            "total_denial_amount": _r2(total_denial_amount),
            "denied_claims": len(denied),
            "denial_rate_pct": denial_rate,
        },
        "monthly_trend": monthly_trend,
        "by_code": by_code,
        "by_payer": by_payer,
        "by_clinician": by_clinician,
    }

# ---------------------------------------------------------------------------
# Clinicians
# ---------------------------------------------------------------------------
@router.get("/clinicians")
@_cached_report
def clinicians_report(
    f: AnalyticsFilters = Depends(get_analytics_filters),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    claims = _cycle_rows(db, f, "claims")
    total_claimed = sum(float(r["net"]) for r in claims)
    rows = [
        {
            "clinician": c or "Unknown",
            "department": d or "Unknown",
            "claimed_amount": _r2(g["claimed"]),
            "paid_amount": _r2(g["paid"]),
            "denied_amount": _r2(g["denied"]),
            "claim_count": g["n"],
            "pct_of_total": round(g["claimed"] / total_claimed * 100, 2) if total_claimed else 0,
        }
        for (c, d), g in _group(claims, lambda r: (r["clinician_name"], r["department"])).items()
    ]
    rows.sort(key=lambda x: x["claimed_amount"], reverse=True)
    # all rows (the Dashboard shows the top 25 and says "Top 25 of N"), so the columns add up to the claim cycle
    return {"kpis": {"total_claimed_amount": _r2(total_claimed), "clinician_count": len(rows)}, "clinicians": rows}

# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------
@router.get("/operations")
def operations_report(
    f: AnalyticsFilters = Depends(get_analytics_filters),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    claims_q = _apply_claim_filters(db.query(Claim), f)
    total_claims = claims_q.count()

    encounter_rows = (
        claims_q.with_entities(
            Claim.encounter_type,
            func.count(Claim.id).label("claim_count"),
            func.coalesce(func.sum(Claim.net_amount), 0).label("amount"),
        )
        .group_by(Claim.encounter_type)
        .order_by(func.count(Claim.id).desc())
        .all()
    )
    by_encounter_type = [
        {"encounter_type": r.encounter_type or "Unknown", "claim_count": r.claim_count, "amount": _money(r.amount)}
        for r in encounter_rows
    ]

    status_rows = claims_q.with_entities(Claim.claim_status, func.count(Claim.id).label("count")).group_by(Claim.claim_status).all()
    by_status = [{"status": r.claim_status, "count": r.count} for r in status_rows]

    remits_q = _remits_with_claims(db, f)
    avg_turnaround = remits_q.with_entities(func.avg(Remittance.remittance_date - Claim.submission_date)).scalar()
    avg_turnaround_days = round(float(avg_turnaround), 1) if avg_turnaround is not None else 0

    monthly_rows = (
        claims_q.with_entities(
            func.to_char(Claim.submission_date, "YYYY-MM").label("month"),
            func.count(Claim.id).label("claim_count"),
        )
        .group_by("month")
        .order_by("month")
        .all()
    )
    monthly_trend = [{"month": r.month, "claim_count": r.claim_count} for r in monthly_rows]

    return {
        "kpis": {"total_claims": total_claims, "avg_turnaround_days": avg_turnaround_days},
        "by_encounter_type": by_encounter_type,
        "by_status": by_status,
        "monthly_trend": monthly_trend,
    }


# ---------------------------------------------------------------------------
# Insurance (Payer / Receiver breakdown)
# ---------------------------------------------------------------------------
@router.get("/insurance")
@_cached_report
def insurance_report(
    f: AnalyticsFilters = Depends(get_analytics_filters),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    claims = _cycle_rows(db, f, "claims")
    total_claimed = sum(float(r["net"]) for r in claims)

    def table(col, label):
        out = [
            {
                label: k or "Unknown",
                "claimed_amount": _r2(g["claimed"]),
                "paid_amount": _r2(g["paid"]),
                "denied_amount": _r2(g["denied"]),
                "claim_count": g["n"],
                "pct_of_total": round(g["claimed"] / total_claimed * 100, 2) if total_claimed else 0,
            }
            for k, g in _group(claims, lambda r: r[col]).items()
        ]
        out.sort(key=lambda x: x["claimed_amount"], reverse=True)
        return out

    return {"kpis": {"total_claimed_amount": _r2(total_claimed)}, "by_payer": table("payer_name", "payer"),
            "by_receiver": table("receiver_name", "receiver")}

# ---------------------------------------------------------------------------
# Department
# ---------------------------------------------------------------------------
@router.get("/department")
@_cached_report
def department_report(
    f: AnalyticsFilters = Depends(get_analytics_filters),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    claims = _cycle_rows(db, f, "claims")
    total_claimed = sum(float(r["net"]) for r in claims)
    by_department = [
        {
            "department": k or "Unknown",
            "claimed_amount": _r2(g["claimed"]),
            "paid_amount": _r2(g["paid"]),
            "denied_amount": _r2(g["denied"]),
            "claim_count": g["n"],
            "pct_of_total": round(g["claimed"] / total_claimed * 100, 2) if total_claimed else 0,
        }
        for k, g in _group(claims, lambda r: r["department"]).items()
    ]
    by_department.sort(key=lambda x: x["claimed_amount"], reverse=True)

    monthly_trend = [
        {"month": m, "department": d or "Unknown", "claimed_amount": _r2(g["claimed"])}
        for (m, d), g in sorted(_group(claims, lambda r: (r["sub_m"], r["department"])).items(),
                                key=lambda kv: (kv[0][0] or "", kv[0][1] or ""))
    ]

    return {"kpis": {"total_claimed_amount": _r2(total_claimed)}, "by_department": by_department, "monthly_trend": monthly_trend}
