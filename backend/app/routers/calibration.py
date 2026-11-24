"""
calibration.py — Layer 3: Feedback loop between RA denial outcomes and scrub rule weights.

Joins:
  scrub_xml_findings.raw_xml_id
    → claims.raw_xml_id        (get claim_id, facility_id)
    → remittances              (denial_amount, paid_amount)
    → remittance_activity_details (denial_code, denial_description)

Exposes:
  GET /api/calibration/rule-stats?facility_id=X
      Per-rule precision: how often a triggered rule correlates with an actual denial.

  GET /api/calibration/denial-queue?facility_id=X&limit=50
      Claims that were denied AND had scrub findings — the resubmission candidate list.

  GET /api/calibration/denial-codes?facility_id=X
      Top denial codes from RA, grouped by frequency and average denial amount.
"""
from __future__ import annotations

import functools
import time
from typing import List, Optional

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import User
from ..security import get_current_user

_CACHE: dict = {}
TTL = 900   # 15 min: these read every scrub finding and RA of the facility (5–9 s each)


# Final-denied definition (same as services/denial_patterns.py): latest submission per claim; RA rows de-duplicated
# (the same RA can be stored twice); paid = least(greatest(sum paid, 0), net); denied = net - paid; the 21-day
# window runs from the LATEST RA per claim.
FINAL_CTE = """
    fd_c AS (SELECT DISTINCT ON (facility_id, claim_id) id, facility_id, claim_id, raw_xml_id,
                    COALESCE(net_amount, 0) AS net, gross_amount
             FROM claims WHERE facility_id = :fid
             ORDER BY facility_id, claim_id, submission_date DESC NULLS LAST, id DESC),
    fd_r AS (SELECT DISTINCT ON (r.facility_id, r.claim_id, r.ra_file_id, r.net_amount, r.paid_amount, r.denial_amount)
                    r.id, r.facility_id, r.claim_id, r.remittance_date, COALESCE(r.paid_amount, 0) AS paid
             FROM remittances r WHERE r.facility_id = :fid
             ORDER BY r.facility_id, r.claim_id, r.ra_file_id, r.net_amount, r.paid_amount, r.denial_amount, r.id),
    fd_g AS (SELECT facility_id, claim_id, sum(paid) AS paid, max(remittance_date) AS last_ra, array_agg(id) AS ra_ids
             FROM fd_r GROUP BY 1, 2),
    fd AS (SELECT c.facility_id, c.claim_id, c.raw_xml_id, c.net, c.gross_amount, g.last_ra, g.ra_ids,
                  CASE WHEN g.claim_id IS NOT NULL THEN LEAST(GREATEST(g.paid, 0), c.net) END         AS paid_amount,
                  CASE WHEN g.claim_id IS NOT NULL THEN c.net - LEAST(GREATEST(g.paid, 0), c.net) END AS denial_amount
           FROM fd_c c LEFT JOIN fd_g g ON g.facility_id = c.facility_id AND g.claim_id = c.claim_id)
"""


def _cached(fn):
    """Cache a facility-level read for TTL seconds, keyed on its query arguments."""
    @functools.wraps(fn)
    def wrap(*args, db: Session, _: User, **kw):
        key = (fn.__name__, args, tuple(sorted(kw.items())))
        hit = _CACHE.get(key)
        if hit and time.time() - hit[0] < TTL:
            return hit[1]
        out = fn(*args, db=db, _=_, **kw)
        _CACHE[key] = (time.time(), out)
        return out
    return wrap


router = APIRouter(prefix="/api/calibration", tags=["calibration"])


# ---------------------------------------------------------------------------
# Rule precision stats
# ---------------------------------------------------------------------------
@router.post("/refresh")
def refresh_cache(facility_id: int, _: User = Depends(get_current_user)):
    """Drop this facility's cached reads so the next load is fresh (the page's Refresh button)."""
    for k in [k for k in _CACHE if dict(k[2]).get("facility_id") == facility_id]:
        _CACHE.pop(k, None)
    return {"ok": True}


@router.get("/rule-stats")
@_cached
def get_rule_stats(
    facility_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """
    For each scrub rule that has been triggered for this facility, compute:
    - total_triggers: how many times the rule fired
    - claims_affected: distinct claims touched
    - with_ra: claims that have a corresponding RA (outcome known)
    - denied: of those, how many were denied (denial_amount > 0)
    - paid: of those, how many were paid in full
    - precision_pct: denied / (denied + paid) * 100  [null if no RA yet]
    - total_denial_amount: sum of denial_amount across denied claims
    - suggested_severity: rule's current severity vs. suggested
    """
    rows = db.execute(
        text("""
        WITH """ + FINAL_CTE + """,
        scrub_claims AS (
            -- One row per (rule_id, claim) — deduplicate multiple findings per claim
            SELECT DISTINCT
                f.rule_id,
                f.severity                AS current_severity,
                c.claim_id,
                c.facility_id
            FROM scrub_xml_findings f
            JOIN claims c ON c.raw_xml_id = f.raw_xml_id AND c.claim_id = f.claim_id
            WHERE f.rule_id    != 'ALL_CLEAR'
              AND NOT (f.rule_id = 'OVERLAPPING_ACTIVITY' AND f.message ~ 'in claims (\\S+) and \\1;')
              AND f.raw_xml_id IS NOT NULL
              AND c.facility_id = :fid
        ),
        outcomes AS (
            SELECT
                sc.rule_id,
                sc.current_severity,
                sc.claim_id,
                r.denial_amount,
                r.paid_amount,
                r.gross_amount
            FROM scrub_claims sc
            LEFT JOIN fd r
                   ON r.claim_id    = sc.claim_id
                  AND r.facility_id = sc.facility_id
        )
        SELECT
            rule_id,
            current_severity,
            COUNT(*)                                                              AS total_triggers,
            COUNT(DISTINCT claim_id)                                              AS claims_affected,
            COUNT(*)        FILTER (WHERE denial_amount IS NOT NULL)              AS with_ra,
            COUNT(*)        FILTER (WHERE denial_amount > 0)                      AS denied,
            COUNT(*)        FILTER (WHERE denial_amount IS NOT NULL AND denial_amount = 0) AS paid,
            COUNT(*)        FILTER (WHERE denial_amount IS NULL)                  AS no_ra_yet,
            ROUND(
                COUNT(*) FILTER (WHERE denial_amount > 0)::numeric /
                NULLIF(COUNT(*) FILTER (WHERE denial_amount IS NOT NULL), 0) * 100
            , 1)                                                                  AS precision_pct,
            COALESCE(SUM(denial_amount) FILTER (WHERE denial_amount > 0), 0)     AS total_denial_amount
        FROM outcomes
        GROUP BY rule_id, current_severity
        ORDER BY denied DESC, precision_pct DESC NULLS LAST
        """),
        {"fid": facility_id},
    ).mappings().all()

    result = []
    for r in rows:
        prec = float(r["precision_pct"]) if r["precision_pct"] is not None else None
        # Suggest severity upgrade if precision >= 70% and denied >= 3
        suggested = None
        if prec is not None and r["denied"] >= 3:
            if prec >= 70 and r["current_severity"] == "WARN":
                suggested = "BLOCK"
            elif prec < 30 and r["current_severity"] == "BLOCK":
                suggested = "WARN"
        result.append({
            "rule_id":            r["rule_id"],
            "current_severity":   r["current_severity"],
            "total_triggers":     r["total_triggers"],
            "claims_affected":    r["claims_affected"],
            "with_ra":            r["with_ra"],
            "denied":             r["denied"],
            "paid":               r["paid"],
            "no_ra_yet":          r["no_ra_yet"],
            "precision_pct":      prec,
            "total_denial_amount": float(r["total_denial_amount"]),
            "suggested_severity": suggested,
        })
    return result


# ---------------------------------------------------------------------------
# Denial queue — resubmission candidates
# ---------------------------------------------------------------------------
def _build_recall_rows(db: Session, facility_id: int, limit: int = 500,
                        date_from=None, date_to=None):
    """Shared query for recall queue (denial-queue endpoint + Excel export)."""
    date_filter = ""
    params: dict = {"fid": facility_id, "lim": min(limit, 1000)}
    if date_from:
        date_filter += " AND r.remittance_date >= :date_from"
        params["date_from"] = date_from
    if date_to:
        date_filter += " AND r.remittance_date <= :date_to"
        params["date_to"] = date_to

    rows = db.execute(
        text(f"""
        SELECT
            c.claim_id,
            c.encounter_start::date                              AS encounter_date,
            c.encounter_type,
            c.payer_id,
            r.remittance_date,
            r.ra_file_id,
            MAX(rax.source_filename)                             AS ra_file_name,
            r.gross_amount,
            r.paid_amount,
            r.denial_amount,
            -- Scrub findings for this claim
            array_agg(DISTINCT f.rule_id)
                FILTER (WHERE f.rule_id != 'ALL_CLEAR')         AS scrub_rules,
            array_agg(DISTINCT f.severity)
                FILTER (WHERE f.severity != 'PASS')             AS scrub_severities,
            -- Denial codes from RA activity details
            array_agg(DISTINCT rad.denial_code)
                FILTER (WHERE rad.denial_code IS NOT NULL)       AS denial_codes,
            array_agg(DISTINCT rad.denial_description)
                FILTER (WHERE rad.denial_description IS NOT NULL) AS denial_descriptions,
            CASE
                WHEN 'BLOCK' = ANY(array_agg(DISTINCT f.severity)) AND r.denial_amount > 500
                     THEN 'CRITICAL'
                WHEN 'BLOCK' = ANY(array_agg(DISTINCT f.severity))
                     THEN 'HIGH'
                WHEN r.denial_amount > 500
                     THEN 'HIGH'
                ELSE 'MEDIUM'
            END                                                  AS priority,
            (SELECT COALESCE(min(x.submission_date) FILTER (WHERE x.resubmission_key = ''), min(x.submission_date))
               FROM claims x WHERE x.facility_id = :fid AND x.claim_id = c.claim_id) AS first_sub,
            (SELECT count(*) FROM claims x WHERE x.facility_id = :fid AND x.claim_id = c.claim_id
                    AND x.resubmission_key <> '')               AS resubs_used
        FROM claims c
        JOIN remittances r
             ON  r.claim_id    = c.claim_id
             AND r.facility_id = c.facility_id
        JOIN scrub_xml_findings f
             ON  f.raw_xml_id  = c.raw_xml_id
             AND f.claim_id    = c.claim_id
             AND f.raw_xml_id IS NOT NULL
             AND NOT (f.rule_id = 'OVERLAPPING_ACTIVITY' AND f.message ~ 'in claims (\\S+) and \\1;')
             AND f.rule_id    != 'ALL_CLEAR'
        LEFT JOIN remittance_activity_details rad
             ON  rad.remittance_id_fk = r.id
        LEFT JOIN raw_xml_store rax
             ON  rax.id = r.raw_xml_id
        WHERE r.denial_amount  > 0
          AND c.facility_id   = :fid
          {date_filter}
        GROUP BY
            c.claim_id, c.encounter_start, c.encounter_type,
            c.payer_id, r.remittance_date, r.ra_file_id,
            r.gross_amount, r.paid_amount, r.denial_amount
        ORDER BY r.remittance_date DESC, r.denial_amount DESC
        LIMIT :lim
        """),
        params,
    ).mappings().all()

    return [
        {
            "claim_id":            r["claim_id"],
            "encounter_date":      str(r["encounter_date"]) if r["encounter_date"] else None,
            "encounter_type":      r["encounter_type"],
            "payer_id":            r["payer_id"],
            "remittance_date":     str(r["remittance_date"]) if r["remittance_date"] else None,
            "ra_file_id":          r["ra_file_id"],
            "ra_file_name":        r["ra_file_name"] or r["ra_file_id"] or "",
            "gross_amount":        float(r["gross_amount"]) if r["gross_amount"] else 0,
            "paid_amount":         float(r["paid_amount"]) if r["paid_amount"] else 0,
            "denial_amount":       float(r["denial_amount"]) if r["denial_amount"] else 0,
            "scrub_rules":         r["scrub_rules"] or [],
            "scrub_severities":    r["scrub_severities"] or [],
            "denial_codes":        r["denial_codes"] or [],
            "denial_descriptions": r["denial_descriptions"] or [],
            "recall_type":         "DENIED",
            "recall_flag":         bool(
                "OVERLAPPING_ACTIVITY" in (r["scrub_rules"] or []) or
                any((c or "").startswith("DUPL") for c in (r["denial_codes"] or []))
            ),
            "priority":            r["priority"],
            **_sn_fields(r["remittance_date"], r["resubs_used"], r["first_sub"]),
        }
        for r in rows
    ]


def _sn_fields(ra_date, used, first_sub=None) -> dict:
    """SN 03/2019: resubmit within 21 days of the RA (3.8.1); at most 2 resubmissions incl. reconciliation (3.8.2);
    claim valid 24 months from the first original submission (3.10.5, shared helper resub_worklist._valid_until)."""
    from datetime import date as _d, timedelta as _td
    from ..services.resub_worklist import _valid_until
    used = int(used or 0)
    due = (ra_date + _td(days=21)) if ra_date else None
    left = (due - _d.today()).days if due else None
    fd = first_sub.date() if hasattr(first_sub, "date") else first_sub
    valid_until = _valid_until(fd, None) if fd else None
    if used >= 2:
        sn = "Limit reached"
    elif valid_until and _d.today() > valid_until:
        sn = "Claim validity ended"
    elif used == 1:
        sn = "Reconciliation only"
    elif left is not None and left < 0:
        sn = "21-day window closed"
    else:
        sn = f"{left} days left" if left is not None else ""
    return {"resubs_used": used, "resubmit_by": due.isoformat() if due else None, "days_left": left, "sn_status": sn,
            "valid_until": valid_until.isoformat() if valid_until else None}


def _build_proactive_recall_rows(
    db: Session,
    facility_id: int,
    denied_claim_ids: set,
    date_from=None,
    date_to=None,
):
    """
    Fetch claims flagged by OVERLAPPING_ACTIVITY (or future high-risk rules) that
    have NOT been denied yet — so they appear in the recall queue proactively
    before a formal RA denial triggers them.
    """
    date_filter = ""
    params: dict = {"fid": facility_id}
    if date_from:
        date_filter += " AND rx.submission_date >= :date_from"
        params["date_from"] = date_from
    if date_to:
        date_filter += " AND rx.submission_date <= :date_to"
        params["date_to"] = date_to

    rows = db.execute(
        text(f"""
        WITH latest_claim AS (
            SELECT DISTINCT ON (facility_id, claim_id)
                facility_id, claim_id, raw_xml_id,
                encounter_start::date AS encounter_date,
                encounter_type, payer_id
            FROM claims
            WHERE facility_id = :fid
            ORDER BY facility_id, claim_id, id DESC
        )
        SELECT
            lc.claim_id,
            lc.encounter_date,
            lc.encounter_type,
            lc.payer_id,
            rx.source_filename                                      AS ra_file_name,
            array_agg(DISTINCT f.rule_id)
                FILTER (WHERE f.rule_id != 'ALL_CLEAR')            AS scrub_rules,
            array_agg(DISTINCT f.severity)
                FILTER (WHERE f.severity != 'PASS')                AS scrub_severities
        FROM latest_claim lc
        JOIN scrub_xml_findings f ON f.raw_xml_id = lc.raw_xml_id AND f.claim_id = lc.claim_id
            AND f.rule_id IN ('OVERLAPPING_ACTIVITY')
            -- older findings paired a claim with itself (two of the same code in one claim) — not an overlap
            AND f.message !~ 'in claims (\\S+) and \\1;'
            AND f.severity IN ('BLOCK', 'WARN')
        LEFT JOIN raw_xml_store rx ON rx.id = lc.raw_xml_id
        WHERE lc.facility_id = :fid
          {date_filter}
        GROUP BY lc.claim_id, lc.encounter_date, lc.encounter_type,
                 lc.payer_id, rx.source_filename
        ORDER BY lc.encounter_date DESC
        LIMIT 300
        """),
        params,
    ).mappings().all()

    result = []
    for r in rows:
        if r["claim_id"] in denied_claim_ids:
            continue   # Already visible in the denied recall queue
        result.append({
            "claim_id":            r["claim_id"],
            "encounter_date":      str(r["encounter_date"]) if r["encounter_date"] else None,
            "encounter_type":      r["encounter_type"],
            "payer_id":            r["payer_id"],
            "remittance_date":     None,
            "ra_file_id":          None,
            "ra_file_name":        r["ra_file_name"] or "",
            "gross_amount":        0,
            "paid_amount":         0,
            "denial_amount":       0,
            "scrub_rules":         r["scrub_rules"] or [],
            "scrub_severities":    r["scrub_severities"] or [],
            "denial_codes":        [],
            "denial_descriptions": [],
            "recall_type":         "PROACTIVE",
            "recall_flag":         True,
            "priority":            "PROACTIVE",
        })
    return result


@router.get("/denial-queue")
def get_denial_queue(
    facility_id: int,
    limit: int = 200,
    date_from: Optional[str] = None,
    date_to:   Optional[str] = None,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """
    Claims denied AND flagged by scrubber — recall review queue.
    Only includes claims where a scrub rule was triggered (not all denied claims).
    Sorted by remittance_date DESC (newest RA first).
    """
    denied_rows   = _build_recall_rows(db, facility_id, limit, date_from, date_to)
    denied_ids    = {r["claim_id"] for r in denied_rows}
    proactive_rows = _build_proactive_recall_rows(db, facility_id, denied_ids, date_from, date_to)
    # Proactive items go after denied items (denied = already actioned)
    return denied_rows + proactive_rows


@router.get("/export-recall-excel")
def export_recall_excel(
    facility_id: int,
    date_from: Optional[str] = None,
    date_to:   Optional[str] = None,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """Download recall queue as Excel workbook."""
    import io
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter
    from fastapi.responses import StreamingResponse

    rows = _build_recall_rows(db, facility_id, 2000, date_from, date_to)

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Recall Queue"

    HDR_FILL = PatternFill("solid", fgColor="1E3A5F")
    HDR_FONT = Font(color="FFFFFF", bold=True, size=10)
    COLS = [
        ("Priority", 12), ("Claim ID", 22), ("Encounter Date", 16),
        ("Enc. Type", 12), ("Payer", 12), ("RA Date", 14), ("RA File", 35),
        ("Claimed (AED)", 14), ("Paid (AED)", 14), ("Denied (AED)", 14),
        ("Scrub Rules", 40), ("Denial Codes", 30), ("Denial Description", 60),
    ]
    for ci, (hdr, w) in enumerate(COLS, 1):
        cell = ws.cell(row=1, column=ci, value=hdr)
        cell.font = HDR_FONT
        cell.fill = HDR_FILL
        cell.alignment = Alignment(horizontal="center", vertical="center")
        ws.column_dimensions[get_column_letter(ci)].width = w

    PRI_FILLS = {
        "CRITICAL": PatternFill("solid", fgColor="FEE2E2"),
        "HIGH":     PatternFill("solid", fgColor="FFEDD5"),
        "MEDIUM":   PatternFill("solid", fgColor="FEF9C3"),
    }

    for rn, d in enumerate(rows, 2):
        vals = [
            d["priority"],
            d["claim_id"],
            d["encounter_date"] or "",
            d["encounter_type"] or "",
            d["payer_id"] or "",
            d["remittance_date"] or "",
            d.get("ra_file_name") or d["ra_file_id"] or "",
            d["gross_amount"],
            d["paid_amount"],
            d["denial_amount"],
            ", ".join(d["scrub_rules"]),
            ", ".join(d["denial_codes"]),
            " | ".join(d["denial_descriptions"]),
        ]
        fill = PRI_FILLS.get(d["priority"])
        for ci, val in enumerate(vals, 1):
            cell = ws.cell(row=rn, column=ci, value=val)
            if fill:
                cell.fill = fill
            if ci in (13,):
                cell.alignment = Alignment(wrap_text=True)

    buf = io.BytesIO()
    wb.save(buf); buf.seek(0)
    fname = f"recall_queue_{facility_id}.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


# ---------------------------------------------------------------------------
# File-wise denial summary — for resubmission planning (21-day window)
# ---------------------------------------------------------------------------
ORIG_CTE = """
    oc_all AS (SELECT id, claim_id, raw_xml_id, submission_date, COALESCE(net_amount, 0) AS net,
                      COALESCE(resubmission_key, '') <> '' AS is_resub
               FROM claims WHERE facility_id = :fid),
    oc_o AS (SELECT DISTINCT ON (claim_id) claim_id, raw_xml_id AS orig_xml, net AS orig_net, submission_date AS first_sub
             FROM oc_all ORDER BY claim_id, submission_date ASC NULLS LAST, id ASC),
    oc_l AS (SELECT DISTINCT ON (claim_id) claim_id, raw_xml_id AS last_xml, submission_date AS last_sub
             FROM oc_all ORDER BY claim_id, submission_date DESC NULLS LAST, id DESC),
    oc_u AS (SELECT claim_id, count(*) FILTER (WHERE is_resub) AS used FROM oc_all GROUP BY 1),
    oc_r AS (SELECT DISTINCT ON (r.claim_id, r.ra_file_id, r.net_amount, r.paid_amount, r.denial_amount)
                    r.id, r.claim_id, r.remittance_date, COALESCE(r.paid_amount, 0) AS paid
             FROM remittances r WHERE r.facility_id = :fid
             ORDER BY r.claim_id, r.ra_file_id, r.net_amount, r.paid_amount, r.denial_amount, r.id),
    oc_g AS (SELECT claim_id, sum(paid) AS paid, max(remittance_date) AS last_ra, array_agg(id) AS ra_ids
             FROM oc_r GROUP BY 1),
    oc AS (SELECT o.claim_id, o.orig_xml, o.orig_net, o.first_sub, l.last_xml, l.last_sub, u.used, g.last_ra, g.ra_ids,
                  LEAST(GREATEST(g.paid, 0), o.orig_net)              AS paid,
                  o.orig_net - LEAST(GREATEST(g.paid, 0), o.orig_net) AS denied,
                  (l.last_xml <> o.orig_xml AND l.last_sub::date > g.last_ra::date) AS awaiting
           FROM oc_o o JOIN oc_l l USING (claim_id) JOIN oc_u u USING (claim_id) JOIN oc_g g USING (claim_id))
"""


@router.get("/file-denial-summary")
@_cached
def get_file_denial_summary(
    facility_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """
    Denied claims grouped by their ORIGINAL ClaimSubmission file, so a file keeps its billed / paid / denied
    totals after some of its claims are resubmitted (amounts are never counted under the CR file too).
    Per claim (SN 03/2019): paid = all RA payments capped at the original net; denied = original net - paid;
    a claim resubmitted after its latest RA is "awaiting RA" and has no deadline of its own; otherwise
    resubmit by = latest RA + 21 days. The file deadline is the earliest such date among its actionable claims.
    """
    from datetime import date as date_type, timedelta

    rows = db.execute(
        text("""
        WITH """ + ORIG_CTE + """
        SELECT
            rx.id AS raw_xml_id, rx.source_filename, rx.submission_date,
            (SELECT count(*) FROM oc_o WHERE orig_xml = rx.id)                      AS total_claims,
            COUNT(*) FILTER (WHERE oc.denied > 0)                                    AS denied_claims,
            COALESCE(SUM(oc.orig_net), 0)                                            AS net_with_ra,
            COALESCE(SUM(oc.paid), 0)                                                AS paid_amount,
            COALESCE(SUM(oc.denied) FILTER (WHERE oc.denied > 0), 0)                 AS total_denial_amount,
            COALESCE(SUM(oc.denied) FILTER (WHERE oc.denied > 0 AND oc.awaiting), 0) AS awaiting_amount,
            COUNT(*) FILTER (WHERE oc.denied > 0 AND oc.awaiting)                    AS awaiting_claims,
            COUNT(*) FILTER (WHERE oc.used > 0)                                      AS resubmitted_claims,
            MIN(oc.last_ra) FILTER (WHERE oc.denied > 0 AND NOT oc.awaiting)         AS earliest_ra_date,
            MAX(oc.last_ra) FILTER (WHERE oc.denied > 0)                             AS latest_ra_date,
            (SELECT array_agg(DISTINCT f.rule_id) FROM scrub_xml_findings f
              WHERE f.raw_xml_id = rx.id AND f.rule_id != 'ALL_CLEAR')               AS scrub_rules,
            (SELECT array_agg(DISTINCT rad.denial_code) FROM oc x
               JOIN remittance_activity_details rad ON rad.remittance_id_fk = ANY(x.ra_ids)
              WHERE x.orig_xml = rx.id AND x.denied > 0 AND rad.denial_code IS NOT NULL) AS denial_codes,
            (SELECT json_agg(t ORDER BY t.resubmitted_on DESC) FROM (
                SELECT x.claim_id, x.orig_net AS net, x.paid, x.denied, x.used, x.awaiting,
                       x.last_sub::date AS resubmitted_on, lx.source_filename AS resub_file
                FROM oc x JOIN raw_xml_store lx ON lx.id = x.last_xml
                WHERE x.orig_xml = rx.id AND x.used > 0 LIMIT 100) t)                AS resubmissions
        FROM raw_xml_store rx
        JOIN oc ON oc.orig_xml = rx.id
        WHERE rx.facility_id = :fid
          AND rx.xml_type    = 'ClaimSubmission'
        GROUP BY rx.id, rx.source_filename, rx.submission_date
        HAVING COUNT(*) FILTER (WHERE oc.denied > 0) > 0
        ORDER BY earliest_ra_date DESC NULLS LAST, total_denial_amount DESC
        """),
        {"fid": facility_id},
    ).mappings().all()

    today = date_type.today()
    result = []
    for r in rows:
        earliest_ra = r["earliest_ra_date"]
        ra_d = earliest_ra.date() if hasattr(earliest_ra, "date") else earliest_ra
        resubmit_by = (ra_d + timedelta(days=21)) if ra_d else None
        days_remaining = (resubmit_by - today).days if resubmit_by else None
        if days_remaining is None:
            window_status = "awaiting_ra" if r["awaiting_claims"] else "no_ra"
        elif days_remaining < 0:
            window_status = "expired"
        elif days_remaining <= 5:
            window_status = "urgent"
        else:
            window_status = "open"
        result.append({
            "raw_xml_id":          r["raw_xml_id"],
            "source_filename":     r["source_filename"],
            "submission_date":     str(r["submission_date"]) if r["submission_date"] else None,
            "total_claims":        r["total_claims"],
            "denied_claims":       r["denied_claims"],
            "net_with_ra":         float(r["net_with_ra"]),
            "paid_amount":         float(r["paid_amount"]),
            "total_denial_amount": float(r["total_denial_amount"]),
            "awaiting_amount":     float(r["awaiting_amount"]),
            "awaiting_claims":     r["awaiting_claims"],
            "resubmitted_claims":  r["resubmitted_claims"],
            "earliest_ra_date":    str(ra_d) if ra_d else None,
            "latest_ra_date":      str(r["latest_ra_date"])[:10] if r["latest_ra_date"] else None,
            "resubmit_by":         resubmit_by.isoformat() if resubmit_by else None,
            "days_remaining":      days_remaining,
            "window_status":       window_status,
            "scrub_rules":         r["scrub_rules"] or [],
            "denial_codes":        [c for c in (r["denial_codes"] or []) if c],
            "resubmissions":       r["resubmissions"] or [],
        })
    return result


@router.get("/resubmission-files")
@_cached
def get_resubmission_files(
    facility_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """CR files for tracking only: which original files their claims come from and how many still await an RA.
    Amounts stay under the original file (file-denial-summary)."""
    rows = db.execute(
        text("""
        WITH """ + ORIG_CTE + """
        SELECT lx.id AS raw_xml_id, lx.source_filename, min(oc.last_sub)::date AS submission_date,
               count(*) AS claims, count(*) FILTER (WHERE oc.awaiting) AS awaiting_claims,
               array_agg(DISTINCT ox.source_filename) AS original_files
        FROM oc JOIN raw_xml_store lx ON lx.id = oc.last_xml JOIN raw_xml_store ox ON ox.id = oc.orig_xml
        WHERE oc.last_xml <> oc.orig_xml AND lx.facility_id = :fid
        GROUP BY lx.id, lx.source_filename
        ORDER BY submission_date DESC NULLS LAST
        LIMIT 200
        """),
        {"fid": facility_id},
    ).mappings().all()
    return [{**dict(r), "submission_date": str(r["submission_date"]) if r["submission_date"] else None} for r in rows]


# ---------------------------------------------------------------------------
# Top denial codes from RA
# ---------------------------------------------------------------------------
@router.get("/denial-codes")
@_cached
def get_denial_codes(
    facility_id: int,
    limit: int = 30,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """Top denial codes from RemittanceAdvice activity details for this facility."""
    rows = db.execute(
        text("""
        SELECT
            rad.denial_code,
            rad.denial_description,
            rad.denial_type,
            COUNT(*)                       AS occurrences,
            COUNT(DISTINCT r.claim_id)     AS claims_affected,
            SUM(rad.gross - rad.paid_amount) FILTER (WHERE rad.gross IS NOT NULL)
                                           AS total_denied_amount
        FROM remittance_activity_details rad
        JOIN remittances r ON r.id = rad.remittance_id_fk
        WHERE r.facility_id     = :fid
          AND rad.denial_code  IS NOT NULL
          AND rad.denial_code  != ''
        GROUP BY rad.denial_code, rad.denial_description, rad.denial_type
        ORDER BY occurrences DESC
        LIMIT :lim
        """),
        {"fid": facility_id, "lim": min(limit, 100)},
    ).mappings().all()

    return [
        {
            "denial_code":        r["denial_code"],
            "denial_description": r["denial_description"],
            "denial_type":        r["denial_type"],
            "occurrences":        r["occurrences"],
            "claims_affected":    r["claims_affected"],
            "total_denied_amount": float(r["total_denied_amount"]) if r["total_denied_amount"] else 0,
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Denial patterns — evidence-based "why" + "what to do" cards (services/denial_patterns.py)
# ---------------------------------------------------------------------------
def _stopped() -> set:
    import json
    from pathlib import Path
    f = Path(__file__).resolve().parents[2] / "data" / "stopped_facilities.json"
    try:
        return {int(k) for k in json.loads(f.read_text()) if k.isdigit()}
    except Exception:
        return set()


@router.get("/patterns")
def get_denial_patterns(
    facility_id: Optional[int] = None,
    refresh: bool = False,
    _: User = Depends(get_current_user),
):
    """Pattern cards for one facility (or all active, non-stopped facilities when facility_id is omitted).
    Hourly cache, refreshed in the background; {"warming": true} until the first computation finishes."""
    from ..database import SessionLocal
    from ..services import denial_patterns as dp
    from ..services.payer_names import payer_name

    data, busy, err = dp.get_data(SessionLocal, force=refresh)
    if data is None:
        return {"warming": True, "error": err, "cards": []}
    out = dp.build(data, facility_id, _stopped(), lambda p: payer_name(p) or p)
    out["refreshing"] = busy
    return out
