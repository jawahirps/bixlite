"""
resub_worklist.py — Daily resubmission worklist built from RA denials.

Every denied activity whose resubmission window is still open becomes a work
item with: owner (Ghaf team or Facility), action, what to ask the facility for,
documents, a draft resubmission comment, deadline, and an expected recovery
based on how often this receiver has paid on resubmission for this denial code
at this facility.

Used by:
  • routers/resub.py              — the Resubmission Worklist screen
  • report-scripts/resub_worklist.py — nightly Excel files (Ghaf team + facility)
"""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..models import ResubPlaybook, ResubTracking

WINDOW_DAYS = 21
URGENT_DAYS = 5
# Only send the facility items worth their time: P1/P2 and a payer that
# actually pays on resubmission. Everything else stays with the Ghaf team.
FACILITY_MIN_RECOVERY_PCT = 20
LOW_VALUE_ACTION = ("Low chance of recovery (payer rarely pays this denial on resubmission). "
                    "Not sent to the facility — close as write-off unless the claim shows a clear case.")
LOOKBACK_DAYS = 400

STATUSES = [
    "New", "In progress", "Sent to facility", "Facility replied", "Ready to resubmit",
    "Resubmitted", "Not resubmitting (write-off)", "Collect from patient",
]
CLOSED_BY_TEAM = ("Resubmitted", "Not resubmitting (write-off)", "Collect from patient")

# (pattern, category, owner, facility_role, action, facility_request, documents, draft_comment)
DEFAULT_PLAYBOOK = [
    ("^MNEC-00[345]", "Medical necessity", "Facility", "Doctor",
     "Resubmit with the treating doctor's clinical justification once received.",
     "Treating doctor to write a short clinical justification: presenting symptoms, findings, "
     "and why this service was needed at this visit.",
     "Consultation notes, vitals, investigation results",
     "The service was medically indicated based on the presenting complaints and clinical findings "
     "documented in the attached notes. Kindly review and settle."),
    ("^MNEC-006", "Consultation visit rules", "Ghaf", None,
     "Check the patient's previous visits: if the diagnosis or clinician differs, resubmit explaining this; "
     "if it is a true follow-up within the free period, close as not resubmitting.",
     None, "Previous and current visit details",
     "This visit was for a new complaint / different diagnosis from the previous consultation, as documented. "
     "Kindly review and settle."),
    ("^MNEC|^PRCE-011", "Medical necessity", "Facility", "Doctor",
     "Resubmit with the doctor's justification for the service / frequency.",
     "Treating doctor to justify why the service (or its frequency) was clinically required.",
     "Clinical notes, previous results showing need",
     "The service was clinically required for the patient's condition as documented in the attached notes. "
     "Kindly review and settle."),
    ("^AUTH-00[45]", "Prior approval", "Facility", "Front office",
     "Resubmit quoting the valid approval number once the facility confirms it.",
     "Front office to share the valid approval number, or request an extension / retro-approval from the payer.",
     "Approval letter / e-authorization reference",
     "The service was rendered under a valid prior approval (reference attached). Kindly review and settle."),
    ("^AUTH|^CLAI-012", "Prior approval", "Facility", "Front office",
     "Resubmit with the prior approval reference once received.",
     "Front office to share the prior approval; if none was taken, request retro-approval with the clinical reason.",
     "Approval reference, clinical notes",
     "Prior approval reference is attached for this pre-approved service. Kindly review and settle."),
    ("^PRCE", "Contract & tariff", "Ghaf", None,
     "Check the contracted price for this payer; correct the net amount or resubmit quoting the contract.",
     None, "Contract / tariff reference",
     "The service is billed as per the agreed contract tariff. Kindly review and settle."),
    ("^CODE", "Coding", "Ghaf", None,
     "Correct the ICD / CPT coding (principal diagnosis, sequela codes, code pairs) and resubmit.",
     None, "Corrected coding",
     "Coding has been corrected as per the clinical documentation. Kindly review and settle."),
    ("^TIME|^DOCU", "Documents", "Facility", "Front office",
     "Resubmit with the documents once received from the facility.",
     "Send the requested documents (notes, reports, invoices) for this claim.",
     "Requested documents",
     "The requested documents are attached. Kindly review and settle."),
    ("^COPY|^CLAI-014", "Documents", "Facility", "Doctor",
     "Resubmit with the clinical indication / report once received.",
     "Doctor to provide the clinical indication or report for this activity.",
     "Clinical indication, report",
     "The clinical indication for this service is documented in the attached report. Kindly review and settle."),
    ("^DUPL", "Duplicate", "Ghaf", None,
     "Check if truly duplicated. If not, resubmit explaining the separate service; if duplicate, close as write-off.",
     None, "Proof of separate service",
     "This is a separate service rendered on the same day, not a duplicate, as documented. Kindly review and settle."),
    ("^BENX|^NCOV|^ELIG", "Coverage / eligibility", "Facility", "Front office",
     "If the facility confirms eligibility, resubmit with proof; otherwise mark Collect from patient.",
     "Front office to verify eligibility / benefit on the payer portal and share proof, "
     "or confirm the amount will be collected from the patient.",
     "Eligibility check screenshot, card copy",
     "The patient was eligible and the service is covered under the policy (eligibility proof attached). "
     "Kindly review and settle."),
    (".*", "Other", "Ghaf", None,
     "Review the denial reason and decide: correct and resubmit, or close as write-off.",
     None, "As required by the denial",
     "Kindly review the attached details and settle."),
]


def ensure_playbook(db: Session) -> List[ResubPlaybook]:
    rows = db.query(ResubPlaybook).order_by(ResubPlaybook.sort_order, ResubPlaybook.id).all()
    if rows:
        return rows
    for i, (pat, cat, owner, role, action, req, docs, comment) in enumerate(DEFAULT_PLAYBOOK):
        db.add(ResubPlaybook(sort_order=(i + 1) * 10, pattern=pat, category=cat, owner=owner,
                             facility_role=role, action=action, facility_request=req,
                             documents=docs, draft_comment=comment, updated_by="system"))
    db.commit()
    return db.query(ResubPlaybook).order_by(ResubPlaybook.sort_order, ResubPlaybook.id).all()


def _matcher(playbook: List[ResubPlaybook]):
    compiled = []
    for p in playbook:
        if not p.is_active:
            continue
        try:
            compiled.append((re.compile(p.pattern, re.I), p))
        except re.error:
            continue

    def match(code: str) -> Optional[ResubPlaybook]:
        for rx, p in compiled:
            if rx.search(code):
                return p
        return None
    return match


def norm_code(c: Optional[str]) -> str:
    return (c or "").split("|")[0].split(",")[0].strip().upper() or "—"


# ── Payer recovery model ──────────────────────────────────────────────────────
_HIST_SQL = text("""
WITH den AS (
  SELECT r.facility_id, r.claim_id, r.remittance_date AS d1, d.activity_id,
         upper(trim(split_part(split_part(COALESCE(NULLIF(d.denial_code,''), r.denial_code),'|',1),',',1))) AS code,
         GREATEST(COALESCE(d.net,0) - COALESCE(d.paid_amount,0),0) AS denied
  FROM remittances r JOIN remittance_activity_details d ON d.remittance_id_fk = r.id
  WHERE r.facility_id = :f AND r.remittance_date >= :since
    AND COALESCE(d.net,0) - COALESCE(d.paid_amount,0) > 0.009
), later AS (
  SELECT den.*, (SELECT SUM(COALESCE(d2.paid_amount,0)) FROM remittances r2
                 JOIN remittance_activity_details d2 ON d2.remittance_id_fk = r2.id
                 WHERE r2.facility_id = den.facility_id AND r2.claim_id = den.claim_id
                   AND r2.remittance_date > den.d1 AND d2.activity_id = den.activity_id) AS paid_later
  FROM den
  WHERE EXISTS (SELECT 1 FROM claims x WHERE x.facility_id = den.facility_id AND x.claim_id = den.claim_id
                AND x.resubmission_key <> '' AND x.submission_date::date >= den.d1)
)
SELECT c.receiver_id, l.code, COUNT(*) AS n,
       SUM(l.denied) AS denied, SUM(LEAST(COALESCE(l.paid_later,0), l.denied)) AS recovered
FROM later l
LEFT JOIN LATERAL (SELECT receiver_id FROM claims c WHERE c.facility_id = l.facility_id AND c.claim_id = l.claim_id
                   ORDER BY c.id LIMIT 1) c ON true
GROUP BY 1, 2
""")

_model_cache: Dict[tuple, tuple] = {}


def recovery_model(db: Session, fid: int):
    key = (fid, date.today())
    if key in _model_cache:
        return _model_cache[key]
    rows = db.execute(_HIST_SQL, {"f": fid, "since": date.today() - timedelta(days=LOOKBACK_DAYS)}).fetchall()
    by_pair, by_code, tot = {}, defaultdict(lambda: [0, 0.0, 0.0]), [0, 0.0, 0.0]
    for r in rows:
        den, rec = float(r.denied or 0), float(r.recovered or 0)
        by_pair[(r.receiver_id, r.code)] = (r.n, den, rec)
        bc = by_code[r.code]; bc[0] += r.n; bc[1] += den; bc[2] += rec
        tot[0] += r.n; tot[1] += den; tot[2] += rec
    overall = tot[2] / tot[1] if tot[1] else 0.35

    def rate(receiver, code):
        # small samples are blended toward the code-level, then facility-level rate
        base = overall
        bc = by_code.get(code)
        if bc and bc[1]:
            base = (bc[2] + overall * 200) / (bc[1] + 200)
        p = by_pair.get((receiver, code))
        if p and p[1]:
            return (p[2] + base * 150) / (p[1] + 150)
        return base

    _model_cache.clear()
    _model_cache[key] = (rate, overall)
    return rate, overall


# ── Worklist ─────────────────────────────────────────────────────────────────
_OPEN_SQL = text("""
WITH den AS (
  SELECT r.id AS rid, r.facility_id, r.claim_id, r.payer_id, r.remittance_date AS ra_date,
         r.created_at AS received_at, r.tpa_comments,
         d.activity_id, d.activity_code,
         COALESCE(NULLIF(d.denial_code,''), r.denial_code) AS denial_code,
         COALESCE(NULLIF(d.denial_description,''), r.denial_description) AS denial_desc,
         GREATEST(COALESCE(d.net,0) - COALESCE(d.paid_amount,0), 0) AS denied
  FROM remittances r
  JOIN remittance_activity_details d ON d.remittance_id_fk = r.id
  WHERE r.facility_id = :f AND r.remittance_date >= :since
    AND COALESCE(d.net,0) - COALESCE(d.paid_amount,0) > 0.009
)
SELECT den.*,
       c.encounter_start::date AS treatment_date, c.receiver_id, c.receiver_name, c.payer_name,
       c.clinician_name, c.member_id, c.payer_id AS claim_payer_id,
       (SELECT a.authorization_id FROM claim_activities a
         WHERE a.claim_id_fk = c.id AND a.activity_id = den.activity_id LIMIT 1) AS authorization_id,
       EXISTS (SELECT 1 FROM claims x WHERE x.facility_id = den.facility_id AND x.claim_id = den.claim_id
               AND x.resubmission_key <> '' AND x.submission_date::date >= den.ra_date) AS resub_after,
       EXISTS (SELECT 1 FROM claims x WHERE x.facility_id = den.facility_id AND x.claim_id = den.claim_id
               AND x.resubmission_key <> '' AND x.submission_date::date < den.ra_date) AS resub_before
FROM den
LEFT JOIN LATERAL (SELECT * FROM claims c WHERE c.facility_id = den.facility_id AND c.claim_id = den.claim_id
                   AND c.resubmission_key = '' ORDER BY c.id LIMIT 1) c ON true
""")


_names_cache: Dict[date, tuple] = {}


def party_names(db: Session):
    """Payer / receiver id -> most common name seen on any claim."""
    if date.today() in _names_cache:
        return _names_cache[date.today()]
    def lookup(col):
        return {r[0]: r[1] for r in db.execute(text(f"""
            SELECT DISTINCT ON (upper({col}_id)) upper({col}_id), {col}_name
            FROM (SELECT {col}_id, {col}_name, count(*) n FROM claims
                  WHERE nullif({col}_name,'') IS NOT NULL GROUP BY 1, 2) t
            ORDER BY upper({col}_id), n DESC"""))}
    _names_cache.clear()
    _names_cache[date.today()] = (lookup("payer"), lookup("receiver"))
    return _names_cache[date.today()]


def tracking_key(claim_id, activity_id, ra_date) -> str:
    return f"{claim_id}|{activity_id}|{ra_date}"


def build_worklist(db: Session, fid: int, window: int = WINDOW_DAYS) -> dict:
    """Returns {'items': [...], 'overall_rate': float}. Items carry a 'bucket':
    open | closed_resubmitted | closed_team | expired."""
    today = date.today()
    match = _matcher(ensure_playbook(db))
    rate, overall = recovery_model(db, fid)
    payer_names, receiver_names = party_names(db)

    tracking = {tracking_key(t.claim_id, t.activity_id, t.ra_date): t
                for t in db.query(ResubTracking).filter(ResubTracking.facility_id == fid,
                                                        ResubTracking.ra_date >= today - timedelta(days=window + 8))}
    rows = db.execute(_OPEN_SQL, {"f": fid, "since": today - timedelta(days=window + 7)}).fetchall()
    new_cut = datetime.now(timezone.utc) - timedelta(hours=30)

    items = []
    for r in rows:
        code = norm_code(r.denial_code)
        pb = match(code)
        deadline = r.ra_date + timedelta(days=window)
        days_left = (deadline - today).days
        p = rate(r.receiver_id, code)
        denied = float(r.denied or 0)
        k = tracking_key(r.claim_id, r.activity_id, r.ra_date)
        t = tracking.get(k)
        status = t.status if t else "New"
        reconciliation = bool(r.resub_before)

        if r.resub_after:
            bucket, status = "closed_resubmitted", "Resubmitted"
        elif status in CLOSED_BY_TEAM:
            bucket = "closed_team"
        elif days_left < 0:
            bucket = "expired"
        else:
            bucket = "open"
        if bucket == "expired" and days_left < -7:
            continue

        owner = (t.owner_override if t and t.owner_override else (pb.owner if pb else "Ghaf"))
        exp = round(denied * p, 2)
        if bucket == "open":
            priority = ("P1" if days_left <= URGENT_DAYS and exp >= 20 else
                        "P2" if exp >= 100 or days_left <= URGENT_DAYS else "P3")
        else:
            priority = None
        playbook_owner = pb.owner if pb else "Ghaf"
        low_value = False
        if not (t and t.owner_override) and playbook_owner == "Facility":
            if reconciliation:
                owner = "Ghaf"   # reconciliation / B2B settlement is negotiated by Ghaf with the payer
            elif priority not in ("P1", "P2") or p * 100 < FACILITY_MIN_RECOVERY_PCT:
                owner, low_value = "Ghaf", True
        items.append({
            "key": k, "claim_id": r.claim_id, "activity_id": r.activity_id, "ra_date": r.ra_date.isoformat(),
            "received_at": r.received_at.isoformat() if r.received_at else None,
            "is_new": bool(r.received_at and r.received_at >= new_cut and t is None),
            "treatment_date": r.treatment_date.isoformat() if r.treatment_date else None,
            "deadline": deadline.isoformat(), "days_left": days_left, "priority": priority,
            "receiver_id": r.receiver_id,
            "receiver": " – ".join(x for x in (r.receiver_id, r.receiver_name
                                              or receiver_names.get((r.receiver_id or "").upper())) if x),
            "payer_id": r.claim_payer_id,
            "payer": " – ".join(x for x in (r.claim_payer_id, r.payer_name
                                           or payer_names.get((r.claim_payer_id or "").upper())) if x) or None,
            "authorization_id": r.authorization_id, "clinician": r.clinician_name, "member_id": r.member_id,
            "activity_code": r.activity_code, "denial_code": code,
            "denial_reason": (r.denial_desc or r.tpa_comments or "")[:400],
            "denied": round(denied, 2), "recovery_pct": round(p * 100), "expected_recovery": exp,
            "category": pb.category if pb else "Other", "owner": owner,
            "facility_role": pb.facility_role if pb else None,
            "route": "Reconciliation" if reconciliation else "Resubmission",
            "action": ("Second denial after resubmission — request reconciliation / B2B settlement with the payer."
                       if reconciliation else LOW_VALUE_ACTION if low_value else (pb.action if pb else "")),
            "facility_request": pb.facility_request if (pb and owner == "Facility") else None,
            "documents": pb.documents if pb else None,
            "draft_comment": pb.draft_comment if pb else None,
            "status": status, "bucket": bucket, "low_value": low_value,
            "assigned_to": t.assigned_to if t else None, "remarks": t.remarks if t else None,
            "facility_reply": t.facility_reply if t else None,
            "updated_by": t.updated_by if t else None,
            "updated_at": t.updated_at.isoformat() if t and t.updated_at else None,
        })

    order = {"P1": 0, "P2": 1, "P3": 2, None: 3}
    items.sort(key=lambda x: (order[x["priority"]], x["days_left"], -x["expected_recovery"]))
    return {"items": items, "overall_rate": round(overall * 100), "window_days": window}


def summarize(items: List[dict]) -> dict:
    def agg(rs):
        return {"count": len(rs), "denied": round(sum(x["denied"] for x in rs), 2),
                "expected": round(sum(x["expected_recovery"] for x in rs), 2)}
    open_ = [x for x in items if x["bucket"] == "open"]
    return {
        "new": agg([x for x in open_ if x["is_new"]]),
        "open": agg(open_),
        "urgent": agg([x for x in open_ if x["days_left"] <= URGENT_DAYS]),
        "ghaf": agg([x for x in open_ if x["owner"] == "Ghaf"]),
        "facility": agg([x for x in open_ if x["owner"] == "Facility"]),
        "resubmitted_7d": agg([x for x in items if x["bucket"] == "closed_resubmitted"]),
        "expired_7d": agg([x for x in items if x["bucket"] == "expired"]),
    }


def save_tracking(db: Session, fid: int, updates: List[dict], user_email: str) -> int:
    n = 0
    for u in updates:
        claim_id, activity_id, ra_date = u["claim_id"], u["activity_id"], date.fromisoformat(u["ra_date"])
        t = (db.query(ResubTracking)
             .filter_by(facility_id=fid, claim_id=claim_id, activity_id=activity_id, ra_date=ra_date).first())
        if not t:
            t = ResubTracking(facility_id=fid, claim_id=claim_id, activity_id=activity_id, ra_date=ra_date)
            db.add(t)
        for f in ("status", "owner_override", "assigned_to", "remarks", "facility_reply"):
            if f in u:
                v = u[f]
                if f == "status" and v not in STATUSES:
                    raise ValueError(f"Unknown status: {v}")
                if f == "owner_override" and v not in (None, "", "Ghaf", "Facility"):
                    raise ValueError(f"Unknown owner: {v}")
                setattr(t, f, v or None if f != "status" else v)
        t.updated_by = user_email
        n += 1
    db.commit()
    return n


# ── Excel export (Ghaf team / facility) ───────────────────────────────────────
def to_workbook(facility_name: str, data: dict, audience: str = "ghaf"):
    """audience='ghaf'     → full worklist for the Ghaf resubmission team
       audience='facility' → only items needing facility input, phrased as requests"""
    import openpyxl
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    TEAL = PatternFill("solid", fgColor="1E6B75"); GREEN = PatternFill("solid", fgColor="8BC34A")
    URG = PatternFill("solid", fgColor="FDE2C4"); NEWF = PatternFill("solid", fgColor="E3F1F3")
    DONE = PatternFill("solid", fgColor="E8F5E9"); GREY = PatternFill("solid", fgColor="EEEEEE")
    THIN = Border(bottom=Side(style="thin", color="DDDDDD"))
    WB = Font(bold=True, color="FFFFFF")
    WIDE = {"Denial Reason", "Action", "What We Need From You", "Documents to Attach",
            "Draft Resubmission Comment", "Remarks", "Facility Reply"}

    items = data["items"]
    open_ = [x for x in items if x["bucket"] == "open"]
    wb = openpyxl.Workbook()

    if audience == "facility":
        cols = [("Priority", "priority"), ("Days Left", "days_left"), ("Reply By", "reply_by"),
                ("Claim ID", "claim_id"), ("Activity ID", "activity_id"), ("Treatment Date", "treatment_date"),
                ("Member ID", "member_id"), ("Clinician", "clinician"), ("Receiver", "receiver"), ("Payer", "payer"),
                ("Auth ID", "authorization_id"),
                ("Activity Code", "activity_code"), ("Denial Code", "denial_code"), ("Denial Reason", "denial_reason"),
                ("Denied (AED)", "denied"), ("Who", "facility_role"), ("What We Need From You", "facility_request"),
                ("Documents to Attach", "documents"), ("Facility Reply", "facility_reply")]
        rows = [x | {"reply_by": (date.fromisoformat(x["deadline"]) - timedelta(days=3)).isoformat()}
                for x in open_ if x["owner"] == "Facility" and x["status"] not in ("Facility replied", "Ready to resubmit")]
        sheets = [("Needs Your Input", rows, lambda x: URG if x["days_left"] <= URGENT_DAYS else None)]
        team_from = len(cols) - 1
        intro = (f"{facility_name} — claims needing your input for resubmission. "
                 f"Please reply by the 'Reply By' date so the Ghaf team can resubmit before the payer deadline.")
    else:
        cols = [("Priority", "priority"), ("Days Left", "days_left"), ("Deadline", "deadline"),
                ("Owner", "owner"), ("Claim ID", "claim_id"), ("Activity ID", "activity_id"), ("RA Date", "ra_date"),
                ("Treatment Date", "treatment_date"), ("Receiver", "receiver"), ("Payer", "payer"),
                ("Auth ID", "authorization_id"),
                ("Clinician", "clinician"), ("Member ID", "member_id"), ("Activity Code", "activity_code"),
                ("Denial Code", "denial_code"), ("Denial Reason", "denial_reason"), ("Denied (AED)", "denied"),
                ("Payer Pays on Resub %", "recovery_pct"), ("Expected Recovery (AED)", "expected_recovery"),
                ("Category", "category"), ("Route", "route"), ("Action", "action"),
                ("What We Need From Facility", "facility_request"), ("Documents to Attach", "documents"),
                ("Draft Resubmission Comment", "draft_comment"),
                ("Status", "status"), ("Assigned To", "assigned_to"), ("Remarks", "remarks"),
                ("Facility Reply", "facility_reply")]
        team_from = len(cols) - 4
        closed = [x for x in items if x["bucket"] != "open"]
        sheets = [
            ("New Today", [x for x in open_ if x["is_new"]], lambda x: URG if x["days_left"] <= URGENT_DAYS else NEWF),
            ("Action List", open_, lambda x: URG if x["days_left"] <= URGENT_DAYS else (NEWF if x["is_new"] else None)),
            ("Needs Facility", [x for x in open_ if x["owner"] == "Facility"],
             lambda x: URG if x["days_left"] <= URGENT_DAYS else None),
            ("Closed", closed, lambda x: DONE if x["bucket"] != "expired" else GREY),
        ]
        intro = (f"{facility_name} — Resubmission Worklist (Ghaf team). Window {data['window_days']} days from RA date. "
                 f"Facility resubmission recovery rate: {data['overall_rate']}%. Update status in the Ghaf App.")

    sm = wb.active; sm.title = "Summary"
    sm["A1"] = intro; sm["A1"].font = Font(bold=True, size=12, color="1E6B75")
    sm["A2"] = f"Generated {datetime.now():%d %b %Y %H:%M}"; sm["A2"].font = Font(italic=True, color="666666")
    s = summarize(items)
    labels = ([("New denials (last night's RAs)", "new"), ("Open — window running", "open"),
               (f"Urgent — {URGENT_DAYS} days or less", "urgent"), ("Ghaf team", "ghaf"),
               ("Needs facility input", "facility"), ("Resubmitted (last 7 days)", "resubmitted_7d"),
               ("Expired without resubmission (last 7 days)", "expired_7d")]
              if audience == "ghaf" else [("Needs your input", None)])
    for j, h in enumerate(["", "Activities", "Denied (AED)"], 1):
        c = sm.cell(4, j, h); c.font = WB; c.fill = TEAL
    for i, (lbl, k) in enumerate(labels):
        agg = s[k] if k else {"count": len(sheets[0][1]), "denied": round(sum(x["denied"] for x in sheets[0][1]), 2)}
        sm.cell(5 + i, 1, lbl); sm.cell(5 + i, 2, agg["count"])
        sm.cell(5 + i, 3, agg["denied"]).number_format = "#,##0.00"
    for col, w in zip("ABC", [48, 12, 16]): sm.column_dimensions[col].width = w

    for title, rows, fill_rule in sheets:
        ws = wb.create_sheet(title)
        ws.append([h for h, _ in cols])
        for i, c in enumerate(ws[1]):
            c.font = WB; c.fill = GREEN if i >= team_from else TEAL
            c.alignment = Alignment(wrap_text=True, vertical="center")
        ws.row_dimensions[1].height = 32
        for x in rows:
            vals = []
            for _, k in cols:
                v = x.get(k)
                if k in ("deadline", "ra_date", "treatment_date", "reply_by") and v:
                    v = date.fromisoformat(v)
                vals.append(v)
            ws.append(vals)
            f = fill_rule(x)
            if f:
                for c in ws[ws.max_row][:team_from]: c.fill = f
        for row in ws.iter_rows(min_row=2):
            for c in row:
                c.border = THIN
                c.alignment = Alignment(vertical="top", wrap_text=cols[c.column - 1][0] in WIDE)
                if isinstance(c.value, date): c.number_format = "DD/MM/YYYY"
        ws.freeze_panes = "E2"; ws.auto_filter.ref = ws.dimensions
        for i, (h, _) in enumerate(cols, 1):
            ws.column_dimensions[get_column_letter(i)].width = 50 if h in WIDE else max(10, min(24, len(h) + 3))
    return wb
