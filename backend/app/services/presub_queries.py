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
from datetime import timedelta
from types import SimpleNamespace
from typing import Dict, List, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models import ScrubQuery, ScrubSession
from app.services import dental_rules, resub_worklist as rw

HISTORY_DAYS = 180
LICENCE_DAYS = 90
LICENCE_MIN_LINES, LICENCE_MIN_RATE = 3, 0.30
AUTH_MIN_LINES, AUTH_MIN_RATE = 5, 0.30
DENTAL_CODE = re.compile(r"^D\d{4}$")                 # CDT codes — always need approval
DENTAL_DX = re.compile(r"^K0[0-8]")                    # K00–K08 teeth and gums
ORAL_DX = re.compile(r"^K(09|1[0-4])")                 # K09–K14 mouth / tongue / lips — dental only with a dentist
DXAUTH_MIN_RATE = 0.20
NOAUTH_MIN_LINES, NOAUTH_PAID_RATE = 10, 0.70   # payer pays the code without approval → no partial-approval query      # diagnosis categories a payer wants approved (e.g. maternity, refraction)
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
CONSULT_BASE = re.compile(r"^(9|10|11)$")
CONSULT_CHECKED = re.compile(r"^(9|10|11)(\.0[12])?$")   # .01 = free follow-up (≤7 days, net 0), .02 = repeat (8–28 days)

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
        no_auth = defaultdict(lambda: [0, 0])            # (rcv, code) billed without approval -> [lines, paid]
        for r in db.execute(_HIST_SQL, {"f": fid, "days": HISTORY_DAYS}):
            if not r.has_auth:
                na = no_auth[(r.rcv, r.code)]
                na[0] += 1; na[1] += r.den == ""
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
        self.no_auth = no_auth
        self.blocks = defaultdict(dict)
        for (rcv, code, blk), v in bylock.items():
            self.blocks[(rcv, code)][blk] = v
        self.ncov = {k: (len(dx_ncov[k]), len(v)) for k, v in dx_claims.items() if k in dx_ncov}
        self.licence = {(r.doc, r.rcv): (r.den, r.n, r.last) for r in db.execute(
            _LICENCE_SQL, {"f": fid, "days": LICENCE_DAYS, "min_n": LICENCE_MIN_LINES, "min_rate": LICENCE_MIN_RATE})}

    def auth(self, rcv, code):
        n, a, _, _, _ = self.lines.get((rcv, code), (0, 0, 0, 0, 0))
        return (a, n) if n >= AUTH_MIN_LINES and a / n >= AUTH_MIN_RATE else None

    def paid_without_approval(self, rcv, code):
        """This payer usually pays the code without an approval number."""
        n, paid = self.no_auth.get((rcv, code), (0, 0))
        return n >= NOAUTH_MIN_LINES and paid / n >= NOAUTH_PAID_RATE

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
       upper(coalesce(nullif(sc.receiver_id,''), sc.payer_id, '')) AS rcv
FROM scrub_claims sc WHERE sc.session_id = :sid ORDER BY sc.encounter_start, sc.id
""")


def _q(rule, qtype, status, item, query, incharge="GENERAL", evidence=None):
    return dict(rule_code=rule, query_type=qtype, status=status, item=item, query_text=query.upper(),
                incharge=incharge, evidence=evidence)


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
    d0 = min(c["d"] for c in claims if c["d"]) if any(c["d"] for c in claims) else None
    d1 = max(c["d"] for c in claims if c["d"]) if d0 else None
    ctx = rw.ConsultContext(db, fid, d0 - timedelta(days=rw.REPEAT_DAYS), d1) if d0 else None
    spec = rw._specialties()
    has_dental = any(dental_rules.CDT.match(a.activity_code) for v in acts.values() for a in v)
    dhist = dental_rules.DentalHistory(db, fid, d0, d1) if d0 and has_dental else None
    session_visits: Dict[str, list] = defaultdict(list)   # earlier consults in this same batch
    policy_seen: set = set()

    rows = []
    for c in claims:
        cid = c["id"]; A = acts[cid]; D = dxs[cid]; rcv = c["rcv"]
        codes = {a.activity_code for a in A}
        out: List[dict] = []

        # ---- coding corrections -------------------------------------------------------
        blocks = {d[:3]: d for d in D}
        for definitive, symptom in EXCLUDES1:
            if definitive in blocks and symptom in blocks:
                out.append(_q("EXCLUDES1", "ICD Excludes-1 — correction applied", "CORRECTION APPLIED",
                              f"{blocks[definitive]} / {blocks[symptom]}",
                              f"ICD code correction applied as per Excludes-1 guideline — {blocks[symptom]} removed, "
                              f"{blocks[definitive]} retained. Kindly review."))
        out += _drug_sequencing(A)
        dental_violations = dental_rules.check(A, c, dhist, _q)
        out += dental_violations

        # ---- test pairs -----------------------------------------------------------------
        for ca, na, cb, nb in TEST_PAIRS:
            if ca in codes and cb in codes:
                out.append(_q("TEST_PAIR", "Test pair — remove one or justify", "IMPENDING REJECTION", f"{ca} + {cb}",
                              f"{na} ({ca}) and {nb} ({cb}) are billed together. Payer denies this combination unless both "
                              f"are clinically needed. Kindly remove one, or add the doctor's reason for both in the case sheet."))

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
        if with_auth and no_auth:
            no_auth = [a for a in no_auth if not hist.paid_without_approval(rcv, a.activity_code)]
        if with_auth and no_auth:
            miss = ", ".join(sorted({a.activity_code for a in no_auth}))
            out.append(_q("APPROVAL_PARTIAL", "Approval missing — not all services", "APPROVAL MISSING", miss,
                          f"Approval added for all billed services except {miss}. Kindly take approval along with other "
                          f"services, or bill the unapproved services separately."))
        elif no_auth and D and hist.dx_approval(rcv, D[0]):
            den, n = hist.dx_approval(rcv, D[0])
            miss = ", ".join(sorted({a.activity_code for a in no_auth}))
            out.append(_q("APPROVAL_DX", "Approval missing", "APPROVAL MISSING", f"{D[0]} / {miss}",
                          f"Approval missing for {miss} with dx {D[0]}. This payer requires approval for {D[0][:3]} "
                          f"cases (denied for no approval on {den} of {n} recent lines). Kindly check & update the "
                          f"approval number.", evidence=f"{den}/{n} lines without approval denied AUTH-001/002"))
        elif no_auth:
            risky = [(a, hist.auth(rcv, a.activity_code)) for a in no_auth]
            risky = [(a, h) for a, h in risky if h]
            if risky:
                miss = ", ".join(sorted({a.activity_code for a, _ in risky}))
                ev = "; ".join(f"{a.activity_code}: {h[0]}/{h[1]} denied for approval" for a, h in risky)
                out.append(_q("APPROVAL_MISSING", "Approval missing", "APPROVAL MISSING", miss,
                              f"Approval missing for {miss}. This payer requires approval for this service. "
                              f"Kindly check & update the approval number.", evidence=ev))

        # ---- diagnosis–code fit (MNEC-003/004) and alternative service (MNEC-006) ------------
        done = {x["item"] for x in out}
        for a in A:
            code = a.activity_code
            if any(code in (i or "") for i in done):
                continue
            panel = hist.panel_components(rcv, code)
            if panel:
                if (rcv, code) not in policy_seen:
                    policy_seen.add((rcv, code))
                    paid, n, good = panel
                    out.append(_q("PAYER_POLICY", "Payer policy — not a facility query", "INFO", code,
                                  f"{rcv} pays only {paid} of {n} recent {code} panels but pays "
                                  f"{', '.join(f'{nm} {pct}%' for nm, pct in good)} when ordered individually. "
                                  f"Standing rule: doctor orders only the components clinically needed. "
                                  f"Do not split a panel already done (unbundling).", incharge="INFO"))
                continue
            pol = hist.payer_policy(rcv, code)
            if pol:
                if (rcv, code) not in policy_seen:      # info only — goes to the payer rules matrix, not the facility
                    policy_seen.add((rcv, code))
                    out.append(_q("PAYER_POLICY", "Payer policy — not a facility query", "INFO", code,
                                  f"{rcv} denies {code} as not clinically indicated whatever the diagnosis "
                                  f"({pol[0]} of {pol[1]} recent lines). Documentation will not change this — "
                                  f"take up with the payer / facility contract.", incharge="INFO"))
                continue
            fit = hist.dx_fit(rcv, code, D[0] if D else "")
            if fit:
                m, n, paid = fit
                if code.startswith("963"):
                    out.append(_q("DX_FIT", "NEW · Justification for IV / injection and routine labs (Oman)",
                                  "CLINICAL JUSTIFICATION", f"{code} / {D[0]}",
                                  f"IV / injection {code} billed for dx {D[0]}. This payer denies IV administration for "
                                  f"{D[0][:3]} ({m} of {n} recent lines) — oral / IM treatment is expected. Kindly confirm "
                                  f"the procedure note states why IV was required (e.g. vomiting, unable to take orally, "
                                  f"failed oral treatment), else remove {code}.", incharge="DOCTOR"))
                    continue
                out.append(_q("DX_FIT", "Diagnosis does not support the service", "CLINICAL JUSTIFICATION",
                              f"{code} / {D[0]}",
                              f"{code} is billed with principal dx {D[0]}. This payer denies {code} with {D[0][:3]} "
                              f"({m} of {n} recent lines) but pays it with {', '.join(paid)}. Kindly check if a supporting "
                              f"diagnosis is documented in the case sheet and code it, else remove {code}.",
                              incharge="DOCTOR"))
                continue
            if code.startswith("963"):
                alt = hist.alternative(rcv, code)
                if alt:
                    out.append(_q("ALT_SERVICE", "NEW · Justification for IV / injection and routine labs (Oman)",
                                  "CLINICAL JUSTIFICATION", code,
                                  f"{code} billed. This payer expects an alternative (oral / IM) to IV administration "
                                  f"({alt[0]} of {alt[1]} recent lines denied MNEC-006). Kindly confirm the procedure note "
                                  f"states why IV was required (e.g. vomiting, dehydration, failed oral treatment).",
                                  incharge="DOCTOR"))

        # ---- diagnosis excluded by payer ---------------------------------------------------
        if D:
            h = hist.dx_excluded(rcv, D[0])
            if h:
                out.append(_q("DX_EXCLUDED", "Diagnosis covered under policy (SOB)", "SOB", D[0],
                              f"Dx {D[0]} is not covered by this payer (denied as not covered on {h[0]} of {h[1]} recent "
                              f"claims). Kindly confirm coverage / collect from patient."))

        # ---- licence watch-list -----------------------------------------------------------
        for doc in sorted({a.clinician_id for a in A if a.clinician_id and (a.clinician_id, rcv) in hist.licence}):
            den, n, last = hist.licence[(doc, rcv)]
            out.append(_q("LICENCE", "NEW · Clinician licence watch-list", "CONFIRMATION", doc,
                          f"This payer denied {den} of {n} recent lines for {doc} for licence status (last on "
                          f"{last:%d/%m/%Y}). Kindly confirm the licence is active and registered with this payer."))

        # ---- repeat / follow-up consultation -----------------------------------------------
        cons = [a for a in A if CONSULT_CHECKED.match(a.activity_code)]
        if ctx and cons and D and c["d"] and not D[0].startswith("Z"):   # Z = routine / screening visits
            cur = SimpleNamespace(id=-cid, patient_id=c["patient_id"], member_id=c["member_id"], d=c["d"],
                                  doc=cons[0].clinician_id, pdx=D[0], dx3s=[x[:3] for x in D])
            prev = ctx.previous(cur) + [v for k in ("P:" + (c["patient_id"] or ""), "M:" + c["member_id"])
                                         for v in session_visits.get(k, []) if 0 <= (c["d"] - v.d).days <= rw.REPEAT_DAYS]
            dx = D[0][:3]
            code = cons[0].activity_code
            base = code.split(".")[0]
            net = float(cons[0].net or 0)
            matched = None
            for p in sorted(prev, key=lambda v: v.d, reverse=True):
                if p.claim_id == c["claim_id"]:
                    continue
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
            elif code.endswith(".01") and not any(0 < (c["d"] - p.d).days <= rw.FOLLOWUP_DAYS
                                                  for p in prev if p.claim_id != c["claim_id"]):
                out.append(_q("FOLLOWUP_NO_VISIT", "Follow-up / repeat visit rule", "REPEAT TEST", code,
                              f"Follow-up {code} billed, but there is no visit in the previous 7 days. "
                              f"Kindly check — if there is no initial visit, bill as a new consultation {base}."))
            if code.endswith(".01") and net > 0:
                out.append(_q("FOLLOWUP_NET", "Billing mistake (quantity / duplicate / price)", "BILLING MISTAKE", code,
                              f"Follow-up {code} is billed with net AED {net:g}. Free follow-up must be billed with net 0. "
                              f"Kindly check & update."))
            v = SimpleNamespace(id=-cid, claim_id=c["claim_id"], d=c["d"], doc=cons[0].clinician_id, pdx=D[0],
                                dx3s=[x[:3] for x in D], code=cons[0].activity_code)
            for k in ("P:" + (c["patient_id"] or ""), "M:" + c["member_id"]):
                if k not in ("P:", "M:"):
                    session_visits[k].append(v)

        for o in out:
            rows.append(ScrubQuery(session_id=session_id, scrub_claim_id=cid, **o))

    db.add_all(rows)
    db.flush()
    return len(rows)


# ---------------------------------------------------------------------------
# Export in the claim-query template layout
# ---------------------------------------------------------------------------

BASE_HEADERS = ["Batch", "Insurance", "MRN / File no", "Invoice no", "Claim ID", "Date", "Patient name",
                "Procedure / Code / Dx", "Type", "Query type", "Status", "Query", "In-charge"]

# ---- code type (Dental / Consultation / Laboratory / Radiology …) for the "Type" column -------------------
# CPT groups come from the DHA CPT list, kept outside the repo (AMA-licensed) as code,type in
# ~/Desktop/report-scripts/assets/cpt_types.csv; without it, CPT number ranges are used.
_CPT_TYPES: Optional[Dict[str, str]] = None
_TOKEN = re.compile(r"\b(D\d{4}|\d{4}-\d{6}-\d{4}|[A-Z]\d{2}(?:\.[0-9A-Z]{1,4})?|[A-Z]\d{4}|\d{4}[FTU]|\d{5}|\d{1,2}(?:\.\d{2})?)\b")


def _cpt_types() -> Dict[str, str]:
    global _CPT_TYPES
    if _CPT_TYPES is None:
        import csv, os
        _CPT_TYPES = {}
        try:
            with open(os.path.expanduser("~/Desktop/report-scripts/assets/cpt_types.csv"), newline="") as f:
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
FACILITY_RESPONSES = ["FIXED", "LINE REMOVED", "JUSTIFICATION ADDED", "APPROVAL ATTACHED",
                      "CORRECTION ACCEPTED", "CORRECTION NOT ACCEPTED", "NO ACTION"]
GHAF_REVIEW = ["APPROVAL FOUND & ENTERED", "CORRECTION APPLIED", "NOT FOUND - SEND TO FACILITY",
               "SEND TO FACILITY", "NO ISSUE - CLOSE"]
# first pass: Ghaf enters approval numbers from the eClaimLink auth portal and applies coding corrections in the ERP
GHAF_APPROVAL_RULES = {"APPROVAL_MISSING", "APPROVAL_PARTIAL", "APPROVAL_DX", "APPROVAL_DENTAL"}
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
               q.rule_code, q.item, q.query_type, q.status, q.query_text, q.incharge
        FROM scrub_queries q JOIN scrub_claims sc ON sc.id = q.scrub_claim_id
        WHERE q.session_id = ANY(:s) AND q.rule_code <> 'PAYER_POLICY'
          AND (CAST(:d0 AS date) IS NULL OR sc.encounter_start >= CAST(:d0 AS date))
          AND (CAST(:d1 AS date) IS NULL OR sc.encounter_start <= CAST(:d1 AS date))
        ORDER BY q.incharge, sc.encounter_start, sc.claim_id, q.id"""),
        {"s": list(session_ids), "d0": date_from, "d1": date_to}).all()
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
                r.query_text, r.incharge]
        who = action_by(r.rule_code, r.status)
        if ghaf:
            line += [who, None, None]
        line += [None, None, "N"]
        ws.append(line)
        ws.cell(ws.max_row, headers.index("Date") + 1).number_format = "DD/MM/YYYY"
        ws.cell(ws.max_row, headers.index("Query") + 1).alignment = Alignment(wrap_text=True, vertical="top")
        if ghaf and who != "FACILITY":
            ws.cell(ws.max_row, headers.index("Action by") + 1).fill = ghaf_fill
    widths = [10, 28, 14, 14, 30, 12, 18, 22, 18, 34, 22, 70, 11] + ([24, 28, 30] if ghaf else []) + [22, 30, 10]
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
    return wb, len(rows)
