"""
presub_queries.py — Pre-submission claim queries in the Ghaf claim-query template wording.

Runs on a scrub session (XML or Excel batch) and writes ScrubQuery rows that export as the
batch query Excel the facility receives (1st / 2nd / final batch).

Two kinds of rules:
  * Coding corrections Ghaf applies in the ERP before submission (status CORRECTION APPLIED),
    or flags when an approval blocks the correction (CORRECTION REQUIRED):
      - ICD Excludes-1 pairs
      - drug administration code sequencing / wrong parent code
  * Queries to the facility, from fixed rules and from this facility's own denial history
    (first RA of original claims, last HISTORY_DAYS):
      - test pairs payers deny together
      - repeat / follow-up consultation (7 / 28-day rule)
      - approval missing / approval not covering all services
      - diagnosis–code fit: payer denies the code with this diagnosis category but pays it with others
        (MNEC-003/004); codes the payer denies whatever the diagnosis are listed as payer policy (info
        only, not exported) — documentation does not change those
      - alternative service for IV administration (MNEC-006 "alternative service should have been utilized")
      - dental billing rules (DHA Dubai Standards of Care 2018, all payers) — see dental_rules.py
      - follow-up (.01, free, ≤7 days) / repeat (.02, 8–28 days) consultation codes
      - diagnosis excluded by payer
      - clinician licence watch-list
"""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import date, timedelta
from types import SimpleNamespace
from typing import Dict, List, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models import ScrubQuery, ScrubSession
from app.services import dental_rules, doc_review, icd_excludes, resub_worklist as rw
from app.services.claim_scrubber import dx_support

HISTORY_DAYS = 180
LICENCE_DAYS = 90
LICENCE_MIN_LINES, LICENCE_MIN_RATE = 3, 0.30
AUTH_MIN_LINES, AUTH_MIN_RATE = 5, 0.30
DENTAL_CODE = re.compile(r"^D\d{4}$")                 # CDT codes — always need approval
DENTAL_DX = re.compile(r"^K0[0-8]")                    # K00–K08 teeth and gums
ORAL_DX = re.compile(r"^K(09|1[0-4])")                 # K09–K14 mouth / tongue / lips — dental only with a dentist
ERP_DUP_MIN_CLAIMS, ERP_DUP_SHARE = 10, 0.50   # ≥ half a day's invoices fully doubled → ERP duplication
DXAUTH_MIN_RATE = 0.20      # diagnosis categories a payer wants approved (e.g. maternity, refraction)
# diagnosis–code fit (MNEC-003/004 "without supporting diagnoses"): query only when this payer denies the code
# with this diagnosis category but pays it with others; payer-wide blanket denials are payer policy, not a query
DXFIT_MIN_LINES, DXFIT_MIN_RATE = 8, 0.60
DXFIT_OTHER_MIN_LINES, DXFIT_OTHER_PAID_RATE = 20, 0.60
POLICY_MIN_LINES, POLICY_RATE, POLICY_MIN_BLOCKS = 20, 0.75, 3
# panels a payer denies while paying the ordered components (e.g. Oman: lipid panel 80061 vs TG / HDL) —
# the doctor should order only the needed components; splitting a panel at billing is unbundling
PANEL_COMPONENTS = {
    "80061": {"82465": "CHOLESTEROL", "84478": "TRIGLYCERIDES", "83718": "HDL", "83721": "LDL DIRECT"},
}
PANEL_MAX_PAID, COMPONENT_MIN_PAID, COMPONENT_MIN_LINES = 0.25, 0.70, 20
# MNEC-006 "alternative service should have been utilized" — route of drug administration
ALT_MIN_LINES, ALT_MIN_RATE = 10, 0.50
NCOV_MIN_CLAIMS, NCOV_MIN_RATE = 5, 0.50

TEST_PAIRS = [
    ("85652", "ESR", "86140", "CRP"),
    ("85025", "CBC", "85048", "AEC"),
    ("80061", "LIPID PANEL", "83721", "LDL DIRECT"),
]
# why the pair is questioned — the query annotates the necessity, it does not ask to remove a test
PAIR_REASON = {
    ("85652", "86140"): "ESR is a slow-responding and CRP a fast-responding inflammatory marker; payers expect the "
                        "case sheet to justify why both markers are needed concurrently for this encounter (e.g. "
                        "distinguishing an acute flare from chronic activity)",
    ("85025", "85048"): "the CBC already includes the white cell count; payers expect the case sheet to justify a "
                        "separate absolute eosinophil count",
    ("80061", "83721"): "the lipid panel already reports LDL; payers expect the case sheet to justify a direct LDL "
                        "(e.g. high triglycerides)",
}
# (definitive dx block, symptom dx block) — symptom code is removed when both are coded
EXCLUDES1 = [
    ("E79", "R79"), ("E11", "R73"), ("M62", "R25"), ("N23", "R10"), ("R60", "R22"),
    ("H81", "R42"), ("E78", "R79"), ("E55", "R53"),
]
# IV / injection initial codes by CPT hierarchy (lower rank = higher in hierarchy) and their add-on code
INITIAL = {"96413": (1, "96417"), "96409": (2, "96411"), "96365": (3, "96367"),
           "96374": (4, "96375"), "96360": (5, "96361")}
# add-on code -> initial codes it may follow
ADDON_PARENT = {"96366": ("96365",), "96367": ("96365",), "96368": ("96365",),
                "96375": ("96365", "96374", "96409", "96413"), "96376": ("96374",),
                "96361": ("96360", "96365", "96374"), "96411": ("96409",), "96415": ("96413",), "96417": ("96413",)}
GP_REFERRAL_PAYERS = {"TPA004": "NAS"}   # receivers that require a GP referral for specialist consultations
CONSULT_BASE = re.compile(r"^(9|10|11)$")
CONSULT_CHECKED = re.compile(r"^(9|10|11)(\.0[12])?$")   # .01 = free follow-up (≤7 days, net 0), .02 = repeat (8–28 days)
# pregnancy / childbirth / puerperium: Chapter 15 (O00–O9A) and the pregnancy Z codes
# (Z32.01 positive test, Z33 pregnant state, Z34 normal pregnancy, Z36 antenatal screening, Z37 outcome of delivery,
#  Z39 postpartum, Z3A weeks of gestation, Z03.7 suspected maternal/fetal condition ruled out)
MATERNITY_DX = re.compile(r"^(O|Z32\.01|Z33|Z34|Z36|Z37|Z39|Z3A|Z03\.7)")
# kept for the Documentation review "Payer patterns" tab only — never a claim query
PATTERN_ONLY = ("DENIAL_RISK", "PAYER_POLICY", "LICENCE")
OPTICAL_CODES = {"92015"}   # refraction — optical benefit
DRUG_CODE = re.compile(r"^\d{4}-\d{6}-\d{4}$")   # DHA / DoH drug code, e.g. 0011-106618-1001
# administration codes where a payer denying the admin AND the drug together questions the route (clinical necessity)
ADMIN_ROUTE = {
    "96372": ("IM injection", "choosing IM injection over oral treatment (e.g. vomiting / unable to take orally, "
                              "severe pain needing rapid action, failed oral treatment)"),
    "96365": ("IV infusion", "IV infusion over oral treatment (e.g. dehydration, unable to take orally, severity, "
                             "failed oral treatment)"),
    "96374": ("IV push", "IV push over oral treatment (e.g. vomiting / unable to take orally, severity needing rapid "
                         "action, failed oral treatment)"),
}
ADMIN_BOTH_MIN_CASES, ADMIN_BOTH_MIN_RATE = 30, 0.20
_ADMIN_SQL = text(r"""
WITH x AS (
  SELECT upper(c.receiver_id) AS rcv, c.id AS cid, a.activity_code AS code, coalesce(d.denial_code,'') AS den
  FROM claims c JOIN claim_activities a ON a.claim_id_fk = c.id
  JOIN remittances r ON r.facility_id = c.facility_id AND r.claim_id = c.claim_id
  JOIN remittance_activity_details d ON d.remittance_id_fk = r.id AND d.activity_id = a.activity_id
  WHERE c.resubmission_key = '' AND c.encounter_start >= now() - make_interval(days => :days)),
drg AS (SELECT cid, bool_and(den <> '') AS all_den FROM x WHERE code ~ '^\d{4}-\d{6}-\d{4}$' GROUP BY cid)
SELECT x.rcv, x.code, count(*) AS n, count(*) FILTER (WHERE x.den <> '' AND drg.all_den) AS both
FROM x JOIN drg ON drg.cid = x.cid WHERE x.code IN ('96372','96365','96374') GROUP BY 1, 2
""")
_dstats_cache: Dict[str, dict] = {}


def admin_stats(db: Session) -> dict:
    """Network-wide: (payer, admin code) → (cases with admin + drug both denied, cases) where that is common."""
    key = date.today().isoformat()
    if key not in _dstats_cache:
        _dstats_cache.clear()
        _dstats_cache[key] = {(r.rcv, r.code): (r.both, r.n) for r in db.execute(_ADMIN_SQL, {"days": 730})
                              if r.n >= ADMIN_BOTH_MIN_CASES and r.both / r.n >= ADMIN_BOTH_MIN_RATE}
    return _dstats_cache[key]
MATERNITY_MIN_LINES, MATERNITY_EXEMPT_RATE = 30, 0.02   # payer pays maternity without approval → no query

_MATERNITY_SQL = text("""
SELECT c.receiver_id AS rcv, count(*) AS n, count(*) FILTER (WHERE d.denial_code ~ '^AUTH') AS den
FROM claims c JOIN claim_activities a ON a.claim_id_fk = c.id AND nullif(trim(a.authorization_id),'') IS NULL
JOIN remittances r ON r.facility_id = c.facility_id AND r.claim_id = c.claim_id
JOIN remittance_activity_details d ON d.remittance_id_fk = r.id AND d.activity_id = a.activity_id
WHERE c.resubmission_key = '' AND c.encounter_start >= now() - make_interval(days => :days)
  AND EXISTS (SELECT 1 FROM claim_diagnoses x WHERE x.claim_id_fk = c.id
              AND upper(x.diagnosis_code) ~ '^(O|Z32\\.01|Z33|Z34|Z36|Z37|Z39|Z3A|Z03\\.7)')
GROUP BY 1
""")
_mstats_cache: Dict[str, object] = {}


def maternity_stats(db: Session):
    """Network-wide: per payer, maternity lines billed without approval and AUTH denials. Payers that pay them
    (≥30 lines, <2 % denied for approval) are exempt from the maternity approval query."""
    key = date.today().isoformat()
    if key not in _mstats_cache:
        rate = {r.rcv: (r.n, r.den) for r in db.execute(_MATERNITY_SQL, {"days": 730})}
        exempt = {k for k, (n, d) in rate.items() if n >= MATERNITY_MIN_LINES and d / n < MATERNITY_EXEMPT_RATE}
        _mstats_cache.clear()
        _mstats_cache[key] = SimpleNamespace(rate=rate, exempt=exempt)
    return _mstats_cache[key]

_HIST_SQL = text("""
WITH r AS (
  SELECT DISTINCT ON (claim_id) id, claim_id FROM remittances
  WHERE facility_id = :f AND remittance_date >= CURRENT_DATE - :days
  ORDER BY claim_id, remittance_date, id)
SELECT upper(coalesce(nullif(c.receiver_id,''), c.payer_id)) AS rcv, upper(d.activity_code) AS code,
       (SELECT upper(x.diagnosis_code) FROM claim_diagnoses x WHERE x.claim_id_fk = c.id ORDER BY x.sequence_no LIMIT 1) AS pdx,
       c.id AS cid, coalesce(d.denial_code, '') AS den,
       (SELECT nullif(a.authorization_id, '') IS NOT NULL FROM claim_activities a
         WHERE a.claim_id_fk = c.id AND a.activity_id = d.activity_id LIMIT 1) AS has_auth
FROM r JOIN remittance_activity_details d ON d.remittance_id_fk = r.id
JOIN claims c ON c.facility_id = :f AND c.claim_id = r.claim_id AND c.resubmission_key = ''
""")
_LICENCE_SQL = text("""
SELECT upper(a.clinician_id) AS doc, upper(coalesce(nullif(c.receiver_id,''), c.payer_id)) AS rcv,
       count(*) AS n, count(*) FILTER (WHERE d.denial_code = 'CLAI-012') AS den,
       max(r.remittance_date) FILTER (WHERE d.denial_code = 'CLAI-012') AS last
FROM remittances r JOIN remittance_activity_details d ON d.remittance_id_fk = r.id
JOIN claims c ON c.facility_id = r.facility_id AND c.claim_id = r.claim_id AND c.resubmission_key = ''
JOIN claim_activities a ON a.claim_id_fk = c.id AND a.activity_id = d.activity_id
WHERE r.facility_id = :f AND r.remittance_date >= CURRENT_DATE - :days AND a.clinician_id IS NOT NULL
GROUP BY 1, 2
HAVING count(*) FILTER (WHERE d.denial_code = 'CLAI-012') >= :min_n
   AND count(*) FILTER (WHERE d.denial_code = 'CLAI-012') >= :min_rate * count(*)
""")


class History:
    """Denial rates for one facility from the first RA of original claims."""

    def __init__(self, db: Session, fid: int):
        lines = defaultdict(lambda: [0, 0, 0, 0, 0])    # (rcv, code) -> [lines, auth, mnec-003/004, mnec-006, paid]
        bylock = defaultdict(lambda: [0, 0, 0])          # (rcv, code, dx category) -> [lines, mnec-003/004, paid]
        dx_claims = defaultdict(set); dx_ncov = defaultdict(set)
        dx_auth = defaultdict(lambda: [0, 0])            # (rcv, dx category) -> [lines without approval, AUTH-001/002]
        for r in db.execute(_HIST_SQL, {"f": fid, "days": HISTORY_DAYS}):
            if r.pdx and not r.has_auth:
                da = dx_auth[(r.rcv, r.pdx[:3])]
                da[0] += 1; da[1] += r.den in ("AUTH-001", "AUTH-002")
            k = lines[(r.rcv, r.code)]
            mnec = r.den in ("MNEC-003", "MNEC-004")
            k[0] += 1
            k[1] += r.den in ("AUTH-001", "AUTH-002")
            k[2] += mnec
            k[3] += r.den == "MNEC-006"
            k[4] += r.den == ""
            if r.pdx:
                b = bylock[(r.rcv, r.code, r.pdx[:3])]
                b[0] += 1; b[1] += mnec; b[2] += r.den == ""
                dx_claims[(r.rcv, r.pdx)].add(r.cid)
                if r.den.startswith("NCOV"):
                    dx_ncov[(r.rcv, r.pdx)].add(r.cid)
        self.lines = lines
        self.dx_auth = dx_auth
        self.blocks = defaultdict(dict)
        for (rcv, code, blk), v in bylock.items():
            self.blocks[(rcv, code)][blk] = v
        self.ncov = {k: (len(dx_ncov[k]), len(v)) for k, v in dx_claims.items() if k in dx_ncov}
        self.licence = {(r.doc, r.rcv): (r.den, r.n, r.last) for r in db.execute(
            _LICENCE_SQL, {"f": fid, "days": LICENCE_DAYS, "min_n": LICENCE_MIN_LINES, "min_rate": LICENCE_MIN_RATE})}

    def auth(self, rcv, code):
        n, a, _, _, _ = self.lines.get((rcv, code), (0, 0, 0, 0, 0))
        return (a, n) if n >= AUTH_MIN_LINES and a / n >= AUTH_MIN_RATE else None

    def dx_approval(self, rcv, dx):
        """Payer denies services for this diagnosis category when billed without approval → (denied, lines)."""
        n, a = self.dx_auth.get((rcv, (dx or "")[:3]), (0, 0))
        return (a, n) if n >= AUTH_MIN_LINES and a >= 3 and a / n >= DXAUTH_MIN_RATE else None

    def payer_policy(self, rcv, code):
        """Payer denies the code as not indicated whatever the diagnosis → (denied, lines), else None."""
        n, _, m, _, _ = self.lines.get((rcv, code), (0, 0, 0, 0, 0))
        if n < POLICY_MIN_LINES or m / n < POLICY_RATE:
            return None
        blocks = [v for v in self.blocks.get((rcv, code), {}).values() if v[0] >= 5]
        if len(blocks) >= POLICY_MIN_BLOCKS and all(v[1] / v[0] >= POLICY_RATE - 0.1 for v in blocks):
            return m, n
        return None

    def dx_fit(self, rcv, code, dx):
        """This payer denies `code` with this diagnosis category but pays it with others.
        Returns (denied, lines, [paid categories]) or None."""
        blocks = self.blocks.get((rcv, code), {})
        cat = (dx or "")[:3]
        n, m, _ = blocks.get(cat, (0, 0, 0))
        if n < DXFIT_MIN_LINES or m / n < DXFIT_MIN_RATE or self.payer_policy(rcv, code):
            return None
        other = [(b, v) for b, v in blocks.items() if b != cat]
        on = sum(v[0] for _, v in other); op = sum(v[2] for _, v in other)
        if on < DXFIT_OTHER_MIN_LINES or op / on < DXFIT_OTHER_PAID_RATE:
            return None
        paid = [b for b, v in sorted(other, key=lambda bv: -bv[1][2]) if v[0] >= 3 and v[2] / v[0] >= 0.7][:4]
        return (m, n, paid) if paid else None

    def panel_components(self, rcv, code):
        """Panel this payer mostly denies while paying its components → (panel paid, panel lines, [(name, paid %)])."""
        comps = PANEL_COMPONENTS.get(code)
        n, _, _, _, paid = self.lines.get((rcv, code), (0, 0, 0, 0, 0))
        if not comps or n < POLICY_MIN_LINES or paid / n > PANEL_MAX_PAID:
            return None
        good = []
        for c, name in comps.items():
            cn, _, _, _, cp = self.lines.get((rcv, c), (0, 0, 0, 0, 0))
            if cn >= COMPONENT_MIN_LINES and cp / cn >= COMPONENT_MIN_PAID:
                good.append((name, round(100 * cp / cn)))
        return (paid, n, good) if good else None

    def alternative(self, rcv, code):
        n, _, _, a, _ = self.lines.get((rcv, code), (0, 0, 0, 0, 0))
        return (a, n) if n >= ALT_MIN_LINES and a / n >= ALT_MIN_RATE else None

    def dx_excluded(self, rcv, dx):
        d, n = self.ncov.get((rcv, dx), (0, 0))
        return (d, n) if n >= NCOV_MIN_CLAIMS and d / n >= NCOV_MIN_RATE else None


_SESSION_SQL = text("""
SELECT sc.id, sc.claim_id, sc.patient_id, coalesce(sc.member_id,'') AS member_id, sc.encounter_start AS d,
       sc.case_sheet, sc.obs_codes,
       upper(coalesce(nullif(sc.receiver_id,''), sc.payer_id, '')) AS rcv
FROM scrub_claims sc WHERE sc.session_id = :sid ORDER BY sc.encounter_start, sc.id
""")


def _q(rule, qtype, status, item, query, incharge="GENERAL", evidence=None, suggested=None):
    return dict(rule_code=rule, query_type=qtype, status=status, item=item, query_text=query.upper(),
                incharge=incharge, evidence=evidence, suggested_action=suggested.upper() if suggested else None)


def _drug_sequencing(acts) -> List[dict]:
    billed = [a for a in acts if a.activity_code in INITIAL or a.activity_code in ADDON_PARENT]
    initials = [a for a in billed if a.activity_code in INITIAL]
    addons = [a for a in billed if a.activity_code in ADDON_PARENT]
    out = []
    # wrong parent: an add-on that can only follow a higher-hierarchy initial code than the one billed
    for ad in addons:
        allowed = ADDON_PARENT[ad.activity_code]
        if initials and not any(i.activity_code in allowed for i in initials):
            wrong = min(initials, key=lambda i: INITIAL[i.activity_code][0])
            right = min(allowed, key=lambda c: INITIAL[c][0])
            if wrong.authorization_id:
                out.append(_q("DRUG_PARENT", "Wrong parent code with approval — correction required", "CORRECTION REQUIRED",
                              f"{wrong.activity_code} / {ad.activity_code}",
                              f"Wrong initial (parent) code {wrong.activity_code} billed; correct code is {right} "
                              f"(add-on {ad.activity_code} follows {right}). Approval no. {wrong.authorization_id} is for "
                              f"{wrong.activity_code}, so no code correction has been applied due to approval compliance. "
                              f"Kindly advise."))
            else:
                out.append(_q("DRUG_PARENT", "Wrong parent code — corrected", "CORRECTION APPLIED",
                              f"{wrong.activity_code} → {right}",
                              f"Wrong initial (parent) code {wrong.activity_code} billed — corrected to {right} as per CPT "
                              f"hierarchy (add-on {ad.activity_code} follows {right}). Kindly review."))
            return out
    # more than one initial code: keep the highest in the hierarchy, the others become add-ons
    if len(initials) > 1:
        parent = min(initials, key=lambda i: INITIAL[i.activity_code][0])
        for o in initials:
            if o is parent:
                continue
            addon = INITIAL[o.activity_code][1]
            if o.authorization_id:
                out.append(_q("DRUG_SEQUENCE", "Wrong parent code with approval — correction required", "CORRECTION REQUIRED",
                              f"{parent.activity_code} + {o.activity_code}",
                              f"Two initial (parent) codes billed ({parent.activity_code} and {o.activity_code}); only one is "
                              f"allowed per encounter — {o.activity_code} should be billed as add-on {addon}. Approval no. "
                              f"{o.authorization_id} is for {o.activity_code}, so no code correction has been applied due "
                              f"to approval compliance. Kindly advise."))
            else:
                out.append(_q("DRUG_SEQUENCE", "Drug code sequencing — correction applied", "CORRECTION APPLIED",
                              f"{o.activity_code} → {addon}",
                              f"Drug administration code sequencing applied — only one initial (parent) code is allowed per "
                              f"encounter ({parent.activity_code} retained). {o.activity_code} removed and billed as add-on "
                              f"{addon}. Kindly review."))
    return out


# consultations in every scrubbed file of the facility (not yet submitted) — a month is split over many
# HIMES files, so the initial visit for a follow-up is often in another file; latest session wins per claim
_PENDING_VISITS_SQL = text("""
SELECT DISTINCT ON (sc.claim_id) sc.claim_id, sc.patient_id, coalesce(sc.member_id,'') AS member_id,
       sc.encounter_start AS d, upper(a.activity_code) AS code, upper(a.clinician_id) AS doc,
       (SELECT array_agg(DISTINCT upper(left(x.diagnosis_code,3))) FROM scrub_claim_diagnoses x WHERE x.scrub_claim_id = sc.id) AS dx3s,
       (SELECT upper(x.diagnosis_code) FROM scrub_claim_diagnoses x WHERE x.scrub_claim_id = sc.id ORDER BY x.sequence_no LIMIT 1) AS pdx
FROM scrub_claims sc JOIN scrub_sessions s ON s.id = sc.session_id
JOIN scrub_claim_activities a ON a.scrub_claim_id = sc.id AND upper(a.activity_code) ~ '^(9|10|11)(\\.\\d+)?$'
WHERE s.facility_id = :f AND sc.encounter_start BETWEEN :d0 AND :d1
ORDER BY sc.claim_id, s.id DESC, a.sequence_no
""")


def _family(code: str) -> str:
    """9 / 9.01 / 9.02 are one sequence, 10 / 10.01 / 10.02 another — never mixed."""
    return (code or "").split(".")[0]


def _visit_txt(v, ref) -> str:
    gap = abs((ref - v.d).days)
    when = "same day" if not gap else f"{gap} day{'s' if gap > 1 else ''} {'before' if v.d < ref else 'after'}"
    return f"{v.d:%d/%m/%Y} ({when}) code {v.code}, {v.doc or 'no clinician'}, {'dx ' + v.pdx if v.pdx else 'no diagnosis'}, claim {v.claim_id}"


def generate(db: Session, session_id: int) -> int:
    sess = db.get(ScrubSession, session_id)
    if sess is None:
        raise ValueError(f"ScrubSession {session_id} not found")
    fid = sess.facility_id
    db.execute(text("DELETE FROM scrub_queries WHERE session_id = :s"), {"s": session_id})

    claims = db.execute(_SESSION_SQL, {"sid": session_id}).mappings().all()
    if not claims:
        db.flush()
        return 0
    acts = defaultdict(list)
    for a in db.execute(text("""SELECT scrub_claim_id, upper(trim(activity_code)) AS activity_code, net, quantity,
                                       nullif(trim(authorization_id),'') AS authorization_id, upper(clinician_id) AS clinician_id
                                FROM scrub_claim_activities WHERE session_id = :s ORDER BY sequence_no"""), {"s": session_id}):
        acts[a.scrub_claim_id].append(a)
    dxs = defaultdict(list)
    for d in db.execute(text("""SELECT scrub_claim_id, upper(diagnosis_code) AS code FROM scrub_claim_diagnoses
                                WHERE session_id = :s ORDER BY sequence_no"""), {"s": session_id}):
        dxs[d.scrub_claim_id].append(d.code)

    hist = History(db, fid)
    mstats = maternity_stats(db)
    dstats = admin_stats(db)
    doc_learned, doc_pref = doc_review.cached_model(fid)
    d0 = min(c["d"] for c in claims if c["d"]) if any(c["d"] for c in claims) else None
    d1 = max(c["d"] for c in claims if c["d"]) if d0 else None
    ctx = rw.ConsultContext(db, fid, d0 - timedelta(days=rw.REPEAT_DAYS), d1) if d0 else None
    spec = rw._specialties()
    has_dental = any(dental_rules.CDT.match(a.activity_code) for v in acts.values() for a in v)
    dhist = dental_rules.DentalHistory(db, fid, d0, d1) if d0 and has_dental else None
    visits: Dict[str, dict] = defaultdict(dict)   # P:/M: key -> {claim_id: visit}; submitted history + all scrubbed files
    if d0:
        for v in [*(x for vs in ctx.by_patient.values() for x in vs),
                  *db.execute(_PENDING_VISITS_SQL, {"f": fid, "d0": d0 - timedelta(days=rw.REPEAT_DAYS),
                                                    "d1": d1 + timedelta(days=rw.REPEAT_DAYS)})]:
            v = SimpleNamespace(claim_id=v.claim_id, d=v.d, code=(v.code or "").upper(), doc=(v.doc or "").upper(),
                                pdx=v.pdx, dx3s=v.dx3s or [], patient_id=v.patient_id, member_id=v.member_id)
            for k in ("P:" + (v.patient_id or ""), "M:" + (v.member_id or "")):
                if k not in ("P:", "M:"):
                    visits[k].setdefault(v.claim_id, v)

    # NAS members already denied for a missing GP referral (any facility, last 12 months)
    gp_ref_members = {r.member_id: (r.last, r.n) for r in db.execute(text("""
        SELECT c.member_id, max(r.remittance_date) last, count(*) n
        FROM remittances r JOIN claims c ON c.facility_id = r.facility_id AND c.claim_id = r.claim_id
         AND c.resubmission_key = ''
        WHERE r.remittance_date >= current_date - 365 AND r.tpa_comments ~* 'gp referral'
          AND coalesce(c.member_id, '') <> ''
          AND upper(coalesce(nullif(c.receiver_id, ''), c.payer_id)) = ANY(:rcv)
        GROUP BY 1"""), {"rcv": list(GP_REFERRAL_PAYERS)})}

    # days where most invoices are fully doubled point to an ERP problem, not individual billing errors
    def _fully_doubled(lines):
        n = defaultdict(int)
        for a in lines:
            n[(a.activity_code, float(a.quantity or 1), float(a.net or 0), a.authorization_id, a.clinician_id)] += 1
        return bool(n) and all(v == 2 for v in n.values())
    per_day = defaultdict(lambda: [0, 0])
    for c in claims:
        if c["d"]:
            per_day[c["d"]][0] += 1
            per_day[c["d"]][1] += _fully_doubled(acts[c["id"]])
    erp_days = {d: v for d, v in per_day.items() if v[1] >= ERP_DUP_MIN_CLAIMS and v[1] / v[0] >= ERP_DUP_SHARE}

    rows = []
    for c in claims:
        cid = c["id"]; A = acts[cid]; D = dxs[cid]; rcv = c["rcv"]
        out: List[dict] = []

        # ---- duplicate lines (same code, quantity, net, approval, clinician) ------------------
        # checked first; the other rules then see each service once, so a doubled parent code is not
        # also reported as a sequencing error
        # dental CDT codes repeat legitimately (one line per tooth) — the DHA dental rules check those instead
        groups: Dict[tuple, list] = defaultdict(list)
        for i, a in enumerate(A):
            k = ((a.activity_code, i) if DENTAL_CODE.match(a.activity_code) else
                 (a.activity_code, float(a.quantity or 1), float(a.net or 0), a.authorization_id, a.clinician_id))
            groups[k].append(a)
        dups = [(k, v) for k, v in groups.items() if len(v) > 1]
        if dups:
            extra = sum(len(v) - 1 for _, v in dups)
            extra_net = sum(k[2] * (len(v) - 1) for k, v in dups)
            codes_txt = ", ".join(f"{k[0]}{' x' + str(len(v)) if len(v) > 2 else ''}" for k, v in dups)
            whole = len(groups) == len(dups)
            lead = (f"All {len(groups)} services are billed twice on this invoice" if whole and all(len(v) == 2 for _, v in dups)
                    else f"{len(dups)} service(s) billed more than once with the same quantity and amount")
            out.append(_q("DUPLICATE_LINES", "Billing mistake (quantity / duplicate / price)", "BILLING MISTAKE",
                          codes_txt,
                          f"{lead} ({codes_txt}). Duplicate lines will be rejected (DUPL-001). Kindly remove the "
                          f"duplicate lines, or bill the quantity on one line if the service was given more than once.",
                          suggested=(f"Remove {extra} duplicate line(s), net AED {extra_net:.2f}; keep one line per service."
                                     + (f" ERP issue: {erp_days[c['d']][1]} of {erp_days[c['d']][0]} invoices dated "
                                        f"{c['d']:%d/%m/%Y} are doubled — report to Simplex HIMES support."
                                        if c["d"] in erp_days else ""))))
            A = [v[0] for v in groups.values()]
        codes = {a.activity_code for a in A}

        # ---- coding corrections -------------------------------------------------------
        # Official ICD-10-CM 2026 Excludes-1 notes (CMS tabular) + payer practice (symptom integral to diagnosis)
        for x in icd_excludes.excludes1_conflicts(D):
            if x["drop"]:
                out.append(_q("EXCLUDES1", "ICD Excludes-1 — correction applied", "CORRECTION APPLIED",
                              f"{x['keep']} / {x['drop']}",
                              f"ICD code correction applied as per Excludes-1 guideline — {x['drop']} removed, "
                              f"{x['keep']} retained. Kindly review.",
                              evidence=f"{x['note']} ({x['source']})", suggested=f"Remove {x['drop']}"))
            else:
                out.append(_q("EXCLUDES1", "ICD Excludes-1 — doctor to choose", "CODING CORRECTION",
                              f"{x['a']} / {x['b']}",
                              f"{x['a']} and {x['b']} cannot be coded together (ICD-10-CM Excludes-1). Kindly confirm "
                              f"which diagnosis is documented and remove the other, unless the two conditions are "
                              f"clearly unrelated and documented separately.",
                              incharge="DOCTOR", evidence=f"{x['note']} ({x['source']})"))
        for x in icd_excludes.practice_conflicts(D):
            out.append(_q("SYMPTOM_INTEGRAL", "Symptom code with definitive diagnosis", "CODING CORRECTION",
                          f"{x['keep']} / {x['drop']}",
                          f"{x['drop']} is a symptom code and {x['note']} ({x['keep']}). Payers flag this as an "
                          f"Excludes-1 edit. Kindly remove {x['drop']} unless it is unrelated to {x['keep']}.",
                          evidence=x["source"], suggested=f"Remove {x['drop']}"))
        preg = [d for d in D if d.startswith("O") or d.startswith("Z34")]
        z3a = next((d for d in D if d.startswith("Z3A")), None)
        if z3a and (D[0].startswith("Z3A") or not preg):
            out.append(_q("MATERNITY_CODING", "Diagnosis coding — pregnancy", "CODING CORRECTION", z3a,
                          f"{z3a} (weeks of gestation) is an additional code only and cannot be "
                          f"{'the principal diagnosis' if D[0].startswith('Z3A') else 'billed without a pregnancy code'}. "
                          f"Kindly code the pregnancy as principal — Z34.x normal pregnancy, O09.x high-risk pregnancy, "
                          f"or the O-code for the pregnancy complication — with {z3a} as secondary.", incharge="DOCTOR"))
        elif any(d.startswith("O") for d in D) and not D[0].startswith("O"):
            o = next(d for d in D if d.startswith("O"))
            out.append(_q("MATERNITY_CODING", "Diagnosis coding — pregnancy", "CODING CORRECTION", f"{D[0]} / {o}",
                          f"Principal dx {D[0]} is billed with pregnancy code {o}. Chapter 15 (O-codes) has sequencing "
                          f"priority — kindly sequence {o} as principal, or if the pregnancy is incidental to the visit "
                          f"use Z33.1 instead of the O-code.", incharge="DOCTOR"))
        out += _drug_sequencing(A)
        dental_violations = dental_rules.check(A, c, dhist, _q)
        out += dental_violations

        # ---- test pairs -----------------------------------------------------------------
        for ca, na, cb, nb in TEST_PAIRS:
            if ca in codes and cb in codes:
                n, _, m, _, _ = hist.lines.get((rcv, cb), (0, 0, 0, 0, 0))
                ev = (f"Payer history: {m} of {n} recent {nb} lines denied as not clinically indicated."
                      if n >= 10 and m / n >= 0.2 else None)
                out.append(_q("TEST_PAIR", "Test pair — necessity of both", "IMPENDING REJECTION", f"{ca} + {cb}",
                              f"{na} ({ca}) and {nb} ({cb}) are billed together — {PAIR_REASON[(ca, cb)]}. Without "
                              f"documented necessity for both there is a high chance of denial.", incharge="DOCTOR",
                              evidence=ev))

        # ---- approvals ------------------------------------------------------------------
        billable = [a for a in A if float(a.net or 0) > 0 and not CONSULT_BASE.match(a.activity_code.split(".")[0])]
        with_auth = [a for a in billable if a.authorization_id]
        no_auth = [a for a in billable if not a.authorization_id]
        by_dentist = any(DENTAL_CODE.match(a.activity_code) or "DENT" in (spec.get(a.clinician_id or "") or "").upper()
                         for a in A)
        dental_dx = next((d for d in D if DENTAL_DX.match(d) or (by_dentist and ORAL_DX.match(d))), None)
        dental = [a for a in A if float(a.net or 0) > 0 and not a.authorization_id
                  and (DENTAL_CODE.match(a.activity_code) or dental_dx)]
        if dental and dental_violations:
            # one dental query per claim: the rule violations, then approval missing for the services that stay
            to_remove = set(re.findall(r"KINDLY REMOVE (D\d{4}(?:, D\d{4})*)",
                                       " ".join(v["query_text"] for v in dental_violations)))
            to_remove = {x.strip() for grp in to_remove for x in grp.split(",")}
            miss = sorted({a.activity_code for a in dental} - to_remove)
            text_ = " ".join(f"{i}) {v['query_text']}" for i, v in enumerate(dental_violations, 1))
            if miss:
                text_ += f" {len(dental_violations) + 1}) APPROVAL MISSING FOR {', '.join(miss)}. KINDLY CHECK & UPDATE THE APPROVAL NUMBER."
            items = ", ".join(v["item"] for v in dental_violations if v["item"])
            for v in dental_violations:
                out.remove(v)
            out.append(_q("DENTAL_RULE", "Dental billing rule (DHA)", "IMPENDING REJECTION",
                          items + (f" / approval: {', '.join(miss)}" if miss else ""), text_))
            no_auth = [a for a in no_auth if a not in dental]
        elif dental:
            miss = ", ".join(sorted({a.activity_code for a in dental}))
            why = (f"Services for dental dx {dental_dx} require" if dental_dx
                   else "All dental (CDT) services require")
            out.append(_q("APPROVAL_DENTAL", "Approval missing", "APPROVAL MISSING", miss,
                          f"Approval missing for {miss}. {why} prior approval. "
                          f"Kindly check & update the approval number."))
            done_dental = {a.activity_code for a in dental}
            no_auth = [a for a in no_auth if a.activity_code not in done_dental]
        # refraction is an optical-benefit service: the query is about the member's optical cover, not the dx
        # when refraction is in the encounter, every service of the encounter (consultation included) needs approval
        refr = [a for a in A if a.activity_code in OPTICAL_CODES and float(a.net or 0) > 0]
        optical = ([a for a in A if float(a.net or 0) > 0 and not a.authorization_id and a not in dental]
                   if refr else [])
        if optical:
            codes_o = ", ".join(sorted({a.activity_code for a in refr}))
            miss = ", ".join(sorted({a.activity_code for a in optical}))
            h = [x for x in (hist.auth(rcv, a.activity_code) for a in optical) if x]
            out.append(_q("OPTICAL_BENEFIT", "Optical benefit / approval", "BENEFIT CHECK", miss,
                          f"Refraction {codes_o} comes under the optical benefit. Kindly check the member's policy covers "
                          f"optical and the approval requirement — when refraction is part of the encounter, all "
                          f"services of the encounter including consultation require approval. {miss} billed without "
                          f"approval — chance of denial.",
                          evidence=(f"Payer history: {h[0][0]} of {h[0][1]} recent lines without approval denied."
                                    if h else None)))
            no_auth = [a for a in no_auth if a not in optical]
        # an approval exists but does not cover the whole encounter: payers check approval compliance — every
        # service of the encounter, consultation included, must be on the approval and the dx must match it;
        # the unapproved services are annotated with the denial risk
        enc_with = [a for a in A if float(a.net or 0) > 0 and a.authorization_id]
        enc_without = [a for a in A if float(a.net or 0) > 0 and not a.authorization_id and a not in dental
                       and a not in optical]
        mat = [d for d in D if MATERNITY_DX.match(d) and not d.startswith("Z32.02")]
        mat_rule = mat and not enc_with and enc_without and rcv not in mstats.exempt
        if mat_rule:
            miss = ", ".join(sorted({a.activity_code for a in enc_without}))
            h = mstats.rate.get(rcv)
            ev = (f"Payer history: {h[1]} of {h[0]} recent maternity lines without approval denied for approval."
                  if h and h[1] >= 3 else None)
            out.append(_q("APPROVAL_MATERNITY", "Approval missing", "APPROVAL MISSING", f"{', '.join(mat[:3])} / {miss}",
                          f"Maternity encounter (dx {', '.join(mat[:3])}). Most payers require prior approval for all "
                          f"services of a pregnancy-related encounter. {miss} billed without approval — chance of denial "
                          f"for non-compliance with approval. Kindly check & update the approval number.", evidence=ev))
        elif enc_with and enc_without:
            miss = ", ".join(sorted({a.activity_code for a in enc_without}))
            approved = ", ".join(sorted({a.activity_code for a in enc_with}))
            auths = ", ".join(sorted({a.authorization_id for a in enc_with}))
            n_ok, n_all = len(enc_with), len(enc_with) + len(enc_without)
            net_ok = sum(float(a.net or 0) for a in enc_with)
            net_no = sum(float(a.net or 0) for a in enc_without)
            out.append(_q("APPROVAL_PARTIAL", "Approval missing — not all services", "APPROVAL MISSING", miss,
                          f"Approval {auths} covers {approved} ({n_ok} of {n_all} services). {miss} (net AED "
                          f"{net_no:.2f}) is billed without approval — chance of denial for non-compliance with "
                          f"approval. Kindly check if approval was taken for {miss} and update the approval number.",
                          evidence=f"approved net AED {net_ok:.2f}; unapproved net AED {net_no:.2f}"))
        elif not enc_with and no_auth and D and hist.dx_approval(rcv, D[0]):
            den, n = hist.dx_approval(rcv, D[0])
            miss = ", ".join(sorted({a.activity_code for a in no_auth}))
            out.append(_q("APPROVAL_DX", "Approval missing", "APPROVAL MISSING", f"{D[0]} / {miss}",
                          f"Approval missing for {miss} with dx {D[0]}. This payer requires approval for {D[0][:3]} "
                          f"cases. Kindly check & update the approval number.",
                          evidence=f"Payer history: {den} of {n} recent lines without approval denied (AUTH-001/002)."))
        elif not enc_with and no_auth:
            risky = [(a, hist.auth(rcv, a.activity_code)) for a in no_auth]
            risky = [(a, h) for a, h in risky if h]
            if risky:
                miss = ", ".join(sorted({a.activity_code for a, _ in risky}))
                ev = "Payer history: " + "; ".join(f"{a.activity_code} {h[0]} of {h[1]} recent lines without approval denied"
                                                  for a, h in risky) + "."
                out.append(_q("APPROVAL_MISSING", "Approval missing", "APPROVAL MISSING", miss,
                              f"Approval missing for {miss}. This payer requires approval for this service. "
                              f"Kindly check & update the approval number.", evidence=ev))

        # ---- diagnosis–code fit (MNEC-003/004) and alternative service (MNEC-006) ------------
        done = {x["item"] for x in out}
        for a in A:
            code = a.activity_code
            if any(code in (i or "") for i in done):
                continue
            drugs = sorted({d.activity_code for d in A if DRUG_CODE.match(d.activity_code)})
            both = dstats.get((rcv, code)) if code in ADMIN_ROUTE and drugs else None
            if both:
                den, n = both
                route, why = ADMIN_ROUTE[code]
                out.append(_q("ADMIN_NECESSITY", "Clinical necessity — injection / IV over oral", "CLINICAL JUSTIFICATION",
                              f"{code} + {', '.join(drugs[:3])}",
                              f"{route} {code} billed with drug {', '.join(drugs[:3])}. Oral treatment is preferred "
                              f"over injection by this payer. Kindly document in the case sheet the clinical necessity "
                              f"for {why}.", incharge="DOCTOR",
                              evidence=f"Payer history: administration and drug both denied in {100 * den // n}% of "
                                       f"recent cases ({den:,} of {n:,})."))
                done.add(code)
                continue
            panel = hist.panel_components(rcv, code)
            if panel:
                paid, n, good = panel
                out.append(_q("DENIAL_RISK", "High chance of denial — payer pattern", "DENIAL RISK", code,
                              f"{code} — high chance of denial as not clinically indicated. Kindly confirm the full "
                              f"panel was clinically needed and is documented in the case sheet.",
                              evidence=f"Payer pattern: {rcv} pays only {paid} of {n} recent {code} panels but pays "
                                       f"{', '.join(f'{nm} {pct}%' for nm, pct in good)} when ordered individually. "
                                       f"Standing rule: doctor orders only the components clinically needed; do not "
                                       f"split a panel already done (unbundling).", incharge="INFO"))
                continue
            pol = hist.payer_policy(rcv, code)
            if pol:
                # the query annotates the per-code denial chance; the payer pattern goes in the remark column
                out.append(_q("DENIAL_RISK", "High chance of denial — payer pattern", "DENIAL RISK", code,
                              f"{code} — high chance of denial as not clinically indicated by this payer.",
                              evidence=f"Payer pattern: {rcv} denies {code} whatever the diagnosis ({pol[0]} of "
                                       f"{pol[1]} recent lines) — documentation is unlikely to change this; take up "
                                       f"with the payer / facility contract.", incharge="INFO"))
                continue
            fit = hist.dx_fit(rcv, code, D[0] if D else "")
            if fit:
                m, n, paid = fit
                if code.startswith("963"):
                    out.append(_q("DX_FIT", "NEW · Justification for IV / injection and routine labs (Oman)",
                                  "CLINICAL JUSTIFICATION", f"{code} / {D[0]}",
                                  f"IV / injection {code} billed for dx {D[0]}. Oral / IM treatment is expected by this "
                                  f"payer for {D[0][:3]}. Kindly confirm the procedure note states why IV was required "
                                  f"(e.g. vomiting, unable to take orally, failed oral treatment), else remove {code}.",
                                  incharge="DOCTOR", evidence=f"Payer history: {m} of {n} recent lines denied."))
                    continue
                out.append(_q("DX_FIT", "Diagnosis does not support the service", "CLINICAL JUSTIFICATION",
                              f"{code} / {D[0]}",
                              f"{code} is billed with principal dx {D[0]}, which does not support {code} for this "
                              f"payer. Kindly check if a supporting diagnosis is documented in the case sheet and code "
                              f"it, else remove {code}.", incharge="DOCTOR",
                              evidence=f"Payer history: {code} with {D[0][:3]} denied on {m} of {n} recent lines; paid "
                                       f"with {', '.join(paid)}."))
                continue
            if code.startswith("963"):
                alt = hist.alternative(rcv, code)
                if alt:
                    out.append(_q("ALT_SERVICE", "NEW · Justification for IV / injection and routine labs (Oman)",
                                  "CLINICAL JUSTIFICATION", code,
                                  f"{code} billed. This payer expects an alternative (oral / IM) to IV administration. "
                                  f"Kindly confirm the procedure note states why IV was required (e.g. vomiting, "
                                  f"dehydration, failed oral treatment).", incharge="DOCTOR",
                                  evidence=f"Payer history: {alt[0]} of {alt[1]} recent lines denied MNEC-006."))

        # ---- diagnosis excluded by payer ---------------------------------------------------
        if D:
            h = hist.dx_excluded(rcv, D[0])
            if h:
                out.append(_q("DX_EXCLUDED", "Diagnosis covered under policy (SOB)", "SOB", D[0],
                              f"Dx {D[0]} may not be covered by this payer. Kindly confirm coverage / collect from "
                              f"patient.", evidence=f"Payer history: denied as not covered on {h[0]} of {h[1]} recent claims."))

        # ---- licence watch-list -----------------------------------------------------------
        for doc in sorted({a.clinician_id for a in A if a.clinician_id and (a.clinician_id, rcv) in hist.licence}):
            den, n, last = hist.licence[(doc, rcv)]
            out.append(_q("LICENCE", "Clinician licence watch-list", "CONFIRMATION", doc,
                          f"Kindly confirm the licence of {doc} is active and registered with this payer.",
                          evidence=f"Payer history: {den} of {n} recent lines for {doc} denied for licence status "
                                   f"(last on {last:%d/%m/%Y}).", incharge="INFO"))

        # ---- NAS: specialist consultation needs a GP referral -------------------------------
        # NAS (TPA004) denies specialist consultations without a GP referral (CLAI-012 "GP referral
        # required but not found") — 13 lines in Jul 2026, 443 in Oct. A same-day GP visit (9-series)
        # for the patient counts as the referral; otherwise ask for the referral to be attached.
        spec_cons = [a for a in A if re.match(r"^1[01](\.02)?$", a.activity_code or "")]
        prior_gp = gp_ref_members.get(c["member_id"]) if c["member_id"] else None
        # only members NAS has already denied for a missing referral: 89.5% of their next specialist visits
        # are denied again (Oct 2026), against 5.6% for other members — the plan, not the payer, decides
        if rcv in GP_REFERRAL_PAYERS and spec_cons and c["d"] and prior_gp:
            gp_today = any(
                v.d == c["d"] and _family(v.code) == "9"
                for k in ("P:" + (c["patient_id"] or ""), "M:" + c["member_id"]) if k not in ("P:", "M:")
                for cl, v in visits.get(k, {}).items() if cl != c["claim_id"])
            if not gp_today:
                out.append(_q("GP_REFERRAL", "GP referral for specialist visit", "DOCUMENT REQUIRED",
                              spec_cons[0].activity_code,
                              f"Specialist consultation {spec_cons[0].activity_code} for a {GP_REFERRAL_PAYERS[rcv]} "
                              f"member. {GP_REFERRAL_PAYERS[rcv]} denies specialist visits without a GP referral "
                              f"(CLAI-012). Kindly attach the GP referral, or bill as GP consultation 9 if the "
                              f"patient was seen without one.",
                              evidence=f"This member was denied by NAS for 'GP referral required but not found' on "
                                       f"{prior_gp[0]:%d/%m/%Y} ({prior_gp[1]} line(s)). Members with a past referral "
                                       f"denial were denied again on 89.5% of specialist visits (Oct 2026)."))

        # ---- repeat / follow-up consultation -----------------------------------------------
        cons = [a for a in A if CONSULT_CHECKED.match(a.activity_code)]
        if ctx and cons and D and c["d"] and not D[0].startswith("Z"):   # Z = routine / screening visits
            cur = SimpleNamespace(doc=cons[0].clinician_id)
            dx = D[0][:3]
            code = cons[0].activity_code
            base = _family(code)
            net = float(cons[0].net or 0)
            mine = {cl: v for k in ("P:" + (c["patient_id"] or ""), "M:" + c["member_id"]) if k not in ("P:", "M:")
                    for cl, v in visits.get(k, {}).items() if cl != c["claim_id"] and _family(v.code) == base}
            prev = [v for v in mine.values() if 0 < (c["d"] - v.d).days <= rw.REPEAT_DAYS]
            nxt = sorted((v for v in mine.values() if 0 < (v.d - c["d"]).days <= rw.REPEAT_DAYS), key=lambda v: v.d)
            # 9 + 9.01 on the same day is a correct initial + follow-up pair; only the same code twice is questioned
            same_day = [v for v in mine.values() if v.d == c["d"] and v.code == code]
            initial_today = any(v.d == c["d"] and v.code == base for v in mine.values())
            # GP referral: the GP records the visit as 9.01 (net 0) and the specialist bills 10 / 11 the same day
            referral = code.endswith(".01") and any(
                v.d == c["d"] and _family(v.code) != base
                for k in ("P:" + (c["patient_id"] or ""), "M:" + c["member_id"]) if k not in ("P:", "M:")
                for cl, v in visits.get(k, {}).items() if cl != c["claim_id"] and CONSULT_CHECKED.match(v.code))
            if same_day and c["claim_id"] > min(v.claim_id for v in same_day):   # raise once per pair
                out.append(_q("CONSULT_SAMEDAY", "Follow-up / repeat visit rule", "REPEAT TEST", code,
                              f"{code} billed, and another {code} consultation is billed for this patient on the "
                              f"same day: {'; '.join(_visit_txt(v, c['d']) for v in same_day)}. Kindly check for a "
                              f"duplicate or wrong visit date."))
            matched = None
            for p in sorted(prev, key=lambda v: v.d, reverse=True):
                same = dx in (p.dx3s or []) or any(rw._block(x) == rw._block(dx) for x in (p.dx3s or []) if rw._block(x))
                same_doc = cur.doc and cur.doc == (p.doc or "").upper()
                same_spec = spec.get((cur.doc or "").upper()) and spec.get((cur.doc or "").upper()) == spec.get((p.doc or "").upper())
                if same and (same_doc or same_spec):
                    matched = (p, (c["d"] - p.d).days, "same doctor" if same_doc else "same specialty")
                    break
            if matched:
                p, gap, who = matched
                seen = f"Patient visited on {p.d:%d/%m/%Y} ({gap} days ago) for {p.pdx}, same condition ({who})."
                if gap <= rw.FOLLOWUP_DAYS and not code.endswith(".01"):
                    out.append(_q("FOLLOWUP", "Follow-up / repeat visit rule", "REPEAT TEST", f"{code} → {base}.01",
                                  f"{seen} Visit within 7 days is a free follow-up. Kindly bill as follow-up "
                                  f"{base}.01 with net 0."))
                elif gap > rw.FOLLOWUP_DAYS and not code.endswith(".02"):
                    out.append(_q("REPEAT", "Follow-up / repeat visit rule", "REPEAT TEST", f"{code} → {base}.02",
                                  f"{seen} Visit within 8–28 days is a repeat consultation. Kindly bill as {base}.02."))
            elif (code.endswith(".01") and not initial_today and not referral
                  and not any((c["d"] - p.d).days <= rw.FOLLOWUP_DAYS for p in prev)):
                last = max(prev, key=lambda v: v.d) if prev else None
                after = (f" Next {base}-series visit: {'; '.join(_visit_txt(v, c['d']) for v in nxt[:3])}." if nxt
                         else f" No later {base}-series visit within 28 days.")
                if last:    # 8–28 days → repeat, the chain of later visits does not change
                    out.append(_q("FOLLOWUP_NO_VISIT", "Follow-up / repeat visit rule", "REPEAT TEST", f"{code} → {base}.02",
                                  f"Follow-up {code} billed, but the last {base}-series visit is {_visit_txt(last, c['d'])} "
                                  f"— more than 7 days. Kindly bill as repeat consultation {base}.02.{after}"))
                else:
                    knock = [v for v in nxt if (v.d - c["d"]).days <= rw.FOLLOWUP_DAYS and v.code == base]
                    effect = (f" If changed to {base}, this becomes the initial visit, so "
                              + "; ".join(f"claim {v.claim_id} on {v.d:%d/%m/%Y} billed {v.code} must change to {base}.01"
                                          for v in knock) + "." if knock else
                              f" Changing to {base} does not affect the later visits." if nxt else "")
                    out.append(_q("FOLLOWUP_NO_VISIT", "Follow-up / repeat visit rule", "REPEAT TEST", f"{code} → {base}",
                                  f"Follow-up {code} billed, but there is no {base}-series visit in the previous 28 days "
                                  f"(checked all files of the month and submitted claims). Kindly check — if there is no "
                                  f"initial visit, bill as a new consultation {base}.{after}{effect}"))
            if code.endswith(".01") and net > 0:
                out.append(_q("FOLLOWUP_NET", "Billing mistake (quantity / duplicate / price)", "BILLING MISTAKE", code,
                              f"Follow-up {code} is billed with net AED {net:g}. Free follow-up must be billed with net 0. "
                              f"Kindly check & update."))

        # ---- case sheet does not support a coded Dx: Tier A only (B / C go to Documentation review) -------
        if D and (c["case_sheet"] or any(dx_support.CONSULT.match(a.activity_code) for a in A)):
            for f in dx_support.evaluate([(d, None) for d in D], c["case_sheet"], set((c["obs_codes"] or "").split(",")),
                                         [(a.activity_code, a.net) for a in A], doc_learned, doc_pref):
                if f["tier"] != "A":
                    continue
                risk = f" Services at risk: {f['services']} (AED {f['amount_at_risk']:g})." if f["services"] else ""
                out.append(_q("DX_DOC", "Diagnosis not supported by case sheet", "CLINICAL JUSTIFICATION",
                              f"{f['code']} {f['description']}", f"{f['query']}.{risk}", incharge="DOCTOR",
                              evidence=f["reason"], suggested=f["suggestion"]))

        for o in out:
            rows.append(ScrubQuery(session_id=session_id, scrub_claim_id=cid, **o))

    db.add_all(rows)
    db.flush()
    return sum(r.rule_code not in PATTERN_ONLY for r in rows)


# ---------------------------------------------------------------------------
# Export in the claim-query template layout
# ---------------------------------------------------------------------------

BASE_HEADERS = ["Batch", "Insurance", "MRN / File no", "Invoice no", "Claim ID", "Date", "Patient name",
                "Procedure / Code / Dx", "Type", "Query type", "Status", "Query", "Suggested action", "Remark",
                "In-charge"]

# ---- code type (Dental / Consultation / Laboratory / Radiology …) for the "Type" column -------------------
# CPT groups come from the DHA CPT list, kept outside the repo (AMA-licensed) as code,type in
# report-scripts assets/cpt_types.csv (see resub_worklist.asset_path); without it, CPT number ranges are used.
_CPT_TYPES: Optional[Dict[str, str]] = None
_TOKEN = re.compile(r"\b(D\d{4}|\d{4}-\d{6}-\d{4}|[A-Z]\d{2}(?:\.[0-9A-Z]{1,4})?|[A-Z]\d{4}|\d{4}[FTU]|\d{5}|\d{1,2}(?:\.\d{2})?)\b")


def _cpt_types() -> Dict[str, str]:
    global _CPT_TYPES
    if _CPT_TYPES is None:
        import csv, os
        _CPT_TYPES = {}
        try:
            with open(rw.asset_path("cpt_types.csv"), newline="") as f:
                _CPT_TYPES = {r["code"].upper(): r["type"] for r in csv.DictReader(f)}
        except OSError:
            pass
    return _CPT_TYPES


def code_type(code: str) -> Optional[str]:
    c = (code or "").strip().upper()
    if re.fullmatch(r"D\d{4}", c):
        return "Dental"
    if re.fullmatch(r"\d{4}-\d{6}-\d{4}", c):
        return "Drug"
    if re.fullmatch(r"\d{1,2}(\.\d{2})?", c):
        return "Consultation"
    if re.fullmatch(r"[A-Z]\d{2}(\.[0-9A-Z]{1,4})?", c):
        return "Diagnosis (ICD)"
    if re.fullmatch(r"[A-Z]\d{4}", c):
        return "HCPCS"
    if re.fullmatch(r"\d{4}F", c):
        return "Category II"
    if re.fullmatch(r"\d{4}[TU]", c):
        return "Category III"
    if re.fullmatch(r"\d{5}", c):
        t = _cpt_types().get(c)
        if t:
            return t
        n = int(c)
        if n < 2000:
            return "Anesthesia"
        if 99000 <= n <= 99499:
            return "Consultation (E&M)"
        return ("Surgery" if n < 70000 else "Radiology" if n < 80000 else "Laboratory" if n < 90000 else "Medicine")
    return None


def item_type(item: Optional[str]) -> str:
    """Type(s) of the codes in the Procedure / Code / Dx cell; activity types first, diagnosis last."""
    seen = []
    for tok in _TOKEN.findall((item or "").upper()):
        t = code_type(tok)
        if t and t not in seen:
            seen.append(t)
    act = [t for t in seen if t != "Diagnosis (ICD)"]
    return ", ".join(act or seen)
GHAF_HEADERS = ["Action by", "Ghaf review", "Ghaf remarks"]
FACILITY_HEADERS = ["Facility response", "Facility remarks", "Closed (Y/N)"]
FACILITY_RESPONSES = ["FIXED", "LINE REMOVED", "JUSTIFICATION ADDED", "APPROVAL ATTACHED", "APPROVAL TAKEN",
                      "SUBMITTED - DENIAL RISK NOTED",
                      "CORRECTION ACCEPTED", "CORRECTION NOT ACCEPTED", "NO ACTION"]
GHAF_REVIEW = ["APPROVAL FOUND & ENTERED", "CORRECTION APPLIED", "NOT FOUND - SEND TO FACILITY",
               "NOT FOUND - ANNOTATED (DENIAL RISK)",
               "SEND TO FACILITY", "NO ISSUE - CLOSE"]
# first pass: Ghaf enters approval numbers from the eClaimLink auth portal and applies coding corrections in the ERP
GHAF_APPROVAL_RULES = {"APPROVAL_MISSING", "APPROVAL_PARTIAL", "APPROVAL_DX", "APPROVAL_DENTAL", "APPROVAL_MATERNITY"}
# batch by visit day: 1st = 1–10, 2nd = 11–20, final = 21–end of month
BATCH_DAYS = (("1ST", 1, 10), ("2ND", 11, 20), ("FINAL", 21, 31))


def action_by(rule_code: str, status: str) -> str:
    if rule_code in GHAF_APPROVAL_RULES:
        return "GHAF - CHECK AUTH PORTAL"
    if status == "CORRECTION APPLIED":
        return "GHAF - APPLY CORRECTION"
    return "FACILITY"


def batch_window(month_start, batch: str):
    """(first day, last day) of a batch in the month of `month_start`."""
    import calendar
    lo, hi = next((lo, hi) for name, lo, hi in BATCH_DAYS if name == batch)
    last = calendar.monthrange(month_start.year, month_start.month)[1]
    return month_start.replace(day=lo), month_start.replace(day=min(hi, last))


def batch_for(d0, d1) -> Optional[str]:
    """Batch label when the date range sits inside one batch window of one month."""
    if not d0 or not d1 or (d0.year, d0.month) != (d1.year, d1.month):
        return None
    for name, lo, hi in BATCH_DAYS:
        if lo <= d0.day and d1.day <= hi:
            return name
    return None


def invoice_no(claim_id: str) -> str:
    m = re.search(r"_(\d+)\.", claim_id or "")
    return m.group(1) if m else (claim_id or "")


def to_workbook(db: Session, session_ids: List[int], batch: Optional[str], audience: str = "ghaf",
                date_from=None, date_to=None):
    """One query sheet for one or more scrub sessions; a claim queried in two batches appears once.
    audience 'ghaf' adds Action by / Ghaf review columns (first pass); 'facility' is the sheet sent to the facility.
    batch None → label each row by its visit day (BATCH_DAYS)."""
    import openpyxl
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.worksheet.datavalidation import DataValidation

    payer_names, receiver_names = rw.party_names(db)
    rows = db.execute(text("""
        SELECT sc.claim_id, sc.patient_id, sc.encounter_start AS d,
               upper(coalesce(nullif(sc.receiver_id,''), sc.payer_id,'')) AS rcv,
               q.rule_code, q.item, q.query_type, q.status, q.query_text, q.suggested_action, q.incharge, q.evidence
        FROM scrub_queries q JOIN scrub_claims sc ON sc.id = q.scrub_claim_id
        WHERE q.session_id = ANY(:s) AND q.rule_code NOT IN :pattern_only
          AND (CAST(:d0 AS date) IS NULL OR sc.encounter_start >= CAST(:d0 AS date))
          AND (CAST(:d1 AS date) IS NULL OR sc.encounter_start <= CAST(:d1 AS date))
        ORDER BY q.incharge, sc.encounter_start, sc.claim_id, q.id"""),
        {"s": list(session_ids), "d0": date_from, "d1": date_to, "pattern_only": PATTERN_ONLY}).all()
    seen = set()
    rows = [r for r in rows if not ((r.claim_id, r.query_text) in seen or seen.add((r.claim_id, r.query_text)))]

    ghaf = audience == "ghaf"
    headers = BASE_HEADERS + (GHAF_HEADERS if ghaf else []) + FACILITY_HEADERS
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Query template"
    ws.append(headers)
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="1F4E78")
        c.alignment = Alignment(wrap_text=True, vertical="center")
    ghaf_fill = PatternFill("solid", fgColor="FFF2CC")
    for r in rows:
        label = batch or (batch_for(r.d, r.d) if r.d else "") or ""
        line = [label, receiver_names.get(r.rcv) or payer_names.get(r.rcv) or r.rcv, r.patient_id,
                invoice_no(r.claim_id), r.claim_id, r.d, None, r.item,
                item_type(r.item) or ("Dental" if r.rule_code in ("DENTAL_RULE", "APPROVAL_DENTAL") else None),
                r.query_type, r.status,
                r.query_text, r.suggested_action, r.evidence, r.incharge]
        who = action_by(r.rule_code, r.status)
        if ghaf:
            line += [who, None, None]
        line += [None, None, "N"]
        ws.append(line)
        ws.cell(ws.max_row, headers.index("Date") + 1).number_format = "DD/MM/YYYY"
        ws.cell(ws.max_row, headers.index("Query") + 1).alignment = Alignment(wrap_text=True, vertical="top")
        ws.cell(ws.max_row, headers.index("Suggested action") + 1).alignment = Alignment(wrap_text=True, vertical="top")
        if ghaf and who != "FACILITY":
            ws.cell(ws.max_row, headers.index("Action by") + 1).fill = ghaf_fill
    widths = [10, 28, 14, 14, 30, 12, 18, 22, 18, 34, 22, 70, 50, 50, 11] + ([24, 28, 30] if ghaf else []) + [22, 30, 10]
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[openpyxl.utils.get_column_letter(i)].width = w
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions

    lists = wb.create_sheet("lists")
    for i, v in enumerate(FACILITY_RESPONSES, 1):
        lists.cell(i, 1, v)
    for i, v in enumerate(GHAF_REVIEW, 1):
        lists.cell(i, 2, v)
    lists.sheet_state = "hidden"
    col = lambda name: openpyxl.utils.get_column_letter(headers.index(name) + 1)
    dv = DataValidation(type="list", formula1=f"=lists!$A$1:$A${len(FACILITY_RESPONSES)}", allow_blank=True)
    dv.add(f"{col('Facility response')}2:{col('Facility response')}2000")
    ws.add_data_validation(dv)
    if ghaf:
        gv = DataValidation(type="list", formula1=f"=lists!$B$1:$B${len(GHAF_REVIEW)}", allow_blank=True)
        gv.add(f"{col('Ghaf review')}2:{col('Ghaf review')}2000")
        ws.add_data_validation(gv)
    yn = DataValidation(type="list", formula1='"Y,N"', allow_blank=True)
    yn.add(f"{col('Closed (Y/N)')}2:{col('Closed (Y/N)')}2000")
    ws.add_data_validation(yn)
    _claim_wise(wb, rows, batch, receiver_names, payer_names)
    return wb, len(rows)


CLAIM_WISE_HEADERS = ["Batch", "Insurance", "MRN / File no", "Invoice no", "Claim ID", "Date", "Patient name",
                      "Query — Admin team", "Query — Clinician"]


def _to_clinician(r) -> bool:
    # Excludes-1 is an ICD derivation question for the doctor (scrubber in-charge to follow after verification)
    return r.incharge == "DOCTOR" or r.rule_code == "EXCLUDES1"


def _claim_wise(wb, rows, batch, receiver_names, payer_names) -> None:
    """First sheet: one row per claim; admin (General) and clinician (Doctor) queries numbered in two columns.
    The per-query sheet stays behind it for Ghaf's first pass and the facility response."""
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    ws = wb.create_sheet("Claim-wise queries", 0)
    ws.append(CLAIM_WISE_HEADERS)
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="1F4E78")
        c.alignment = Alignment(wrap_text=True, vertical="center")
    claims: Dict[str, list] = {}
    for r in sorted(rows, key=lambda r: (r.d or date.min, r.claim_id)):
        claims.setdefault(r.claim_id, []).append(r)
    num = lambda g: "\n".join(f"{i}) {r.query_text}" for i, r in enumerate(g, 1)) or None
    top = Alignment(wrap_text=True, vertical="top")
    for claim_id, g in claims.items():
        r0 = g[0]
        ws.append([batch or (batch_for(r0.d, r0.d) if r0.d else "") or "",
                   receiver_names.get(r0.rcv) or payer_names.get(r0.rcv) or r0.rcv, r0.patient_id,
                   invoice_no(claim_id), claim_id, r0.d, None,
                   num([r for r in g if not _to_clinician(r)]), num([r for r in g if _to_clinician(r)])])
        for c in ws[ws.max_row]:
            c.alignment = top
        ws.cell(ws.max_row, CLAIM_WISE_HEADERS.index("Date") + 1).number_format = "DD/MM/YYYY"
    for i, w in enumerate([10, 28, 14, 14, 30, 12, 18, 80, 80], 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions


def month_patterns(db: Session, fid: int, d0: date, d1: date) -> List[dict]:
    """Payer patterns and licence watch-list for submitted claims of a period — the same tests the scrubber
    runs, for months that were never scrubbed. Rows: kind, rcv, code, pattern, claims, net."""
    hist = History(db, fid)
    agg: Dict[tuple, list] = {}
    for r in db.execute(text("""
            SELECT c.id, upper(coalesce(nullif(c.receiver_id,''), c.payer_id, '')) AS rcv, c.net_amount AS claim_net,
                   upper(a.activity_code) AS code, upper(a.clinician_id) AS doc
            FROM claims c JOIN claim_activities a ON a.claim_id_fk = c.id
            WHERE c.facility_id = :f AND c.resubmission_key = '' AND c.encounter_start >= :d0
              AND c.encounter_start < :d1"""), {"f": fid, "d0": d0, "d1": d1}):
        found = []
        pol = hist.payer_policy(r.rcv, r.code)
        if pol:
            found.append(("DENIAL_RISK", r.code, f"{r.rcv} denies {r.code} whatever the diagnosis ({pol[0]} of {pol[1]} "
                                                 f"recent lines) — documentation is unlikely to change this; take up "
                                                 f"with the payer / facility contract."))
        panel = hist.panel_components(r.rcv, r.code)
        if panel:
            paid, n, good = panel
            found.append(("DENIAL_RISK", r.code, f"{r.rcv} pays only {paid} of {n} recent {r.code} panels but pays "
                                                 f"{', '.join(f'{nm} {pct}%' for nm, pct in good)} when ordered "
                                                 f"individually. Standing rule: doctor orders only the components "
                                                 f"clinically needed; do not split a panel already done (unbundling)."))
        if r.doc and (r.doc, r.rcv) in hist.licence:
            den, n, last = hist.licence[(r.doc, r.rcv)]
            found.append(("LICENCE", r.doc, f"{den} of {n} recent lines for {r.doc} denied for licence status "
                                            f"(last on {last:%d/%m/%Y})."))
        for kind, code, pattern in found:
            a = agg.setdefault((kind, r.rcv, code, pattern), [set(), {}])
            a[0].add(r.id); a[1][r.id] = float(r.claim_net or 0)
    rows = [SimpleNamespace(kind=k, rcv=rcv, code=code, pattern=p, claims=len(v[0]), net=sum(v[1].values()))
            for (k, rcv, code, p), v in agg.items()]
    return sorted(rows, key=lambda r: (r.kind, -r.claims, -r.net))
