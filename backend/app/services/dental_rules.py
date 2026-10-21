"""
dental_rules.py — DHA Dental Billing Rules (Dubai Standards of Care 2018) as pre-submission queries.

Scope: all payers. The rules were introduced for the Dubai government programmes (Enaya / Saada) and are
now applied by all payers.

Only rules that can be checked from the claim are included:
  * same-day combinations that are disallowed or included in another procedure
  * frequency limits, from the patient's earlier claims at the facility

Not checked (the claim XML carries no date of birth or tooth number): age limits (fluoride / sealant ≤ 18,
scaling ≥ 12), once-per-tooth limits, crown/endodontic x-ray attachments.
"""
from __future__ import annotations

import re
from datetime import timedelta
from typing import Callable, Dict, List

from sqlalchemy import text
from sqlalchemy.orm import Session

CDT = re.compile(r"^D\d{4}$")


def _rng(lo: str, hi: str) -> Callable[[str], bool]:
    a, b = int(lo[1:]), int(hi[1:])
    return lambda c: bool(CDT.match(c)) and a <= int(c[1:]) <= b


EVALUATION = _rng("D0120", "D0160")
TREATMENT = _rng("D1000", "D9999")
RESTORATION = lambda c: _rng("D2140", "D2161")(c) or _rng("D2330", "D2394")(c)
COMPOSITE = _rng("D2330", "D2394")
CROWN = _rng("D2710", "D2799")
ENDO = _rng("D3110", "D3999")
ROOT_CANAL = _rng("D3310", "D3330")
ORAL_SURGERY = _rng("D7111", "D7999")
SURGICAL_PERIO = _rng("D4210", "D4276")
SURGICAL = lambda c: ORAL_SURGERY(c) or SURGICAL_PERIO(c) or _rng("D3410", "D3430")(c)
ORTHO = _rng("D8000", "D8999")
LOCAL_ANAESTHESIA = {"D9210", "D9211", "D9212", "D9215"}
MAX_FILLINGS_PER_DAY = 4

# frequency limits: code group -> (codes, max count, window days, wording)
FREQUENCY = [
    ({"D0210"}, 1, 365, "complete intraoral x-ray series (D0210) is limited to once per year"),
    ({"D0330"}, 1, 365, "panoramic x-ray (D0330) is covered once a year"),
    ({"D1110", "D1120"}, 2, 365, "prophylaxis is covered twice a year"),
    ({"D4355"}, 1, 365, "full mouth debridement is covered once a year"),
]

_HISTORY_SQL = text("""
SELECT c.patient_id, coalesce(c.member_id,'') AS member_id, c.encounter_start::date AS d,
       upper(a.activity_code) AS code, upper(coalesce(a.clinician_id,'')) AS doc, c.claim_id
FROM claims c JOIN claim_activities a ON a.claim_id_fk = c.id
WHERE c.facility_id = :f AND c.resubmission_key = '' AND a.activity_code ~ '^D[0-9]{4}$'
  AND c.encounter_start >= :d0 AND c.encounter_start < :d1
""")


class DentalHistory:
    """Earlier dental services per patient at the facility (last 12 months before the batch)."""

    def __init__(self, db: Session, fid: int, d0, d1):
        self.by_patient: Dict[str, list] = {}
        for r in db.execute(_HISTORY_SQL, {"f": fid, "d0": d0 - timedelta(days=365), "d1": d1 + timedelta(days=1)}):
            for k in ("P:" + (r.patient_id or ""), "M:" + r.member_id):
                if k not in ("P:", "M:"):
                    self.by_patient.setdefault(k, []).append(r)

    def earlier(self, patient_id, member_id, d, claim_id):
        seen, out = set(), []
        for k in ("P:" + (patient_id or ""), "M:" + (member_id or "")):
            for r in self.by_patient.get(k, []):
                key = (r.claim_id, r.code, r.d)
                if r.claim_id != claim_id and r.d <= d and key not in seen:
                    seen.add(key)
                    out.append(r)
        return out


def check(acts, claim, hist: DentalHistory | None, q) -> List[dict]:
    """Violations only — a compliant claim returns []. acts: activity rows (activity_code, clinician_id, quantity); claim: mapping with patient_id, member_id, d,
    claim_id; q: the query constructor from presub_queries."""
    codes = [a.activity_code for a in acts]
    dental = [c for c in codes if CDT.match(c)]
    if not dental:
        return []
    have = set(dental)
    out: List[dict] = []
    rule = "DENTAL_RULE"
    qt = "Dental billing rule (DHA)"

    def flag(item, text_, status="IMPENDING REJECTION"):
        out.append(q(rule, qt, status, item, text_))

    # ---- same day ------------------------------------------------------------------------
    evals = sorted(c for c in have if EVALUATION(c))
    treat = sorted(c for c in have if TREATMENT(c))
    if evals and treat:
        flag(", ".join(evals), f"Consultation / oral evaluation {', '.join(evals)} is billed with dental treatment "
             f"({', '.join(treat[:4])}) on the same day. As per DHA dental billing rules, no consultation is billed "
             f"when a procedure is done on the same visit — the fee is included in the treatment. Kindly remove "
             f"{', '.join(evals)}.")
    if {"D1110", "D1120"} <= have:
        flag("D1110 + D1120", "Prophylaxis adult (D1110) and child (D1120) are billed together. Both codes cannot be "
             "used together. Kindly keep the one that applies to the patient's age.")
    perio = sorted(have & {"D4341", "D4342", "D4910"})
    if perio and have & {"D1110", "D1120"}:
        flag(f"D1110 / {', '.join(perio)}", f"Prophylaxis is billed on the same day as {', '.join(perio)}. Prophylaxis "
             f"is part of scaling and root planing / periodontal maintenance and the fee is disallowed. Kindly remove "
             f"the prophylaxis.")
    fillings = sum(float(a.quantity or 1) for a in acts if RESTORATION(a.activity_code))
    if fillings > MAX_FILLINGS_PER_DAY:
        flag("Fillings", f"{fillings:g} fillings are billed on one day. Fillings are limited to four per claim per day "
             f"(except general anaesthesia cases). If several surfaces of one tooth were filled, kindly use the "
             f"multi-surface code instead of billing each surface.")
    if {"D2920", "D2915"} <= have:
        flag("D2915", "Re-cement crown (D2920) and re-cement post and core (D2915) are billed together. D2915 is "
             "disallowed on the same tooth and date. Kindly remove D2915 if it is the same tooth.")
    if "D2940" in have and any(RESTORATION(c) or ENDO(c) for c in have):
        flag("D2940", "Protective restoration (D2940) is billed with a restoration / endodontic treatment. A separate "
             "fee for protective restoration is not covered in combination with a restoration or as a temporary "
             "filling. Kindly remove D2940.")
    if "D2950" in have and any(COMPOSITE(c) for c in have) and any(CROWN(c) for c in have):
        flag("D2950", "Core build-up (D2950) is billed with a composite filling and a crown. Either the composite "
             "filling or the core build-up is covered with the crown, not both. Kindly keep one.")
    if "D3221" in have and ("D3220" in have or any(ROOT_CANAL(c) for c in have)):
        flag("D3221", "Pulpal debridement (D3221) is billed on the same day as pulpotomy / root canal treatment. D3221 "
             "is disallowed with these. Kindly remove D3221.")
    if "D7510" in have and any(ENDO(c) or ORAL_SURGERY(c) or SURGICAL_PERIO(c) for c in have - {"D7510"}):
        flag("D7510", "Incision and drainage of abscess (D7510) is billed with endodontic / oral surgery / surgical "
             "periodontal treatment on the same day. It is not covered in combination and is part of endodontic "
             "treatment. Kindly remove D7510.")
    if "D7910" in have and any(SURGICAL(c) for c in have - {"D7910"}):
        flag("D7910", "Suture (D7910) is billed with a surgical procedure. Suturing is part of any surgical treatment "
             "and a separate fee is not covered. Kindly remove D7910.")
    la = sorted(have & LOCAL_ANAESTHESIA)
    if la and any(TREATMENT(c) for c in have - LOCAL_ANAESTHESIA):
        flag(", ".join(la), f"Local anaesthesia ({', '.join(la)}) is billed separately. Local anaesthesia is part of "
             f"any dental treatment and a separate fee is not covered. Kindly remove {', '.join(la)}.")
    biopsy = sorted(have & {"D7285", "D7286"})
    if biopsy and have & {"D3410", "D3421", "D3425", "D3426", "D7210"}:
        flag(", ".join(biopsy), "Biopsy is billed with apicoectomy / surgical extraction on the same day. It is "
             "disallowed in the same surgical area and date. Kindly remove if it is the same area.")
    srp = sorted(have & {"D4341", "D4342"})
    if srp and have & {"D4240", "D4241", "D4260", "D4261"}:
        flag(", ".join(srp), f"Scaling and root planing ({', '.join(srp)}) is billed with gingival flap / osseous "
             f"surgery, which already includes root planing. Kindly remove {', '.join(srp)}.")
    if have & {"D4210", "D4211"} and have & {"D4260", "D4261"}:
        flag("D4210 / D4211", "Gingivectomy is billed with osseous surgery, which includes gingivectomy. Kindly remove "
             "the gingivectomy.")
    if "D4249" in have and any(CROWN(c) or RESTORATION(c) for c in have):
        flag("D4249", "Crown lengthening (D4249) is billed on the same day as crown preparation / restoration. Crown "
             "lengthening is disallowed on the same day. Kindly check & remove.")
    if "D4212" in have and any(CROWN(c) or RESTORATION(c) for c in have):
        flag("D4212", "Gingivectomy per tooth (D4212) is billed with crown preparation / restoration by the same "
             "dentist. A separate fee is disallowed. Kindly remove D4212.")
    if {"D1555", "D1550"} <= have:
        flag("D1555", "Removal of fixed space maintainer (D1555) is billed with re-cementation (D1550). D1555 is "
             "disallowed with re-cementation. Kindly remove D1555.")
    if "D0340" in have and not any(ORTHO(c) for c in have):
        flag("D0340", "Cephalometric x-ray (D0340) is billed without orthodontic treatment. It is covered only with "
             "orthodontic treatment for members with orthodontic benefits. Kindly check & remove.")
    # ---- frequency (patient's earlier claims) --------------------------------------------
    if hist and claim["d"]:
        earlier = hist.earlier(claim["patient_id"], claim["member_id"], claim["d"], claim["claim_id"])
        for group, limit, days, wording in FREQUENCY:
            if not have & group:
                continue
            prev = sorted({(r.d, r.code) for r in earlier
                           if r.code in group and (claim["d"] - r.d).days < days and r.d != claim["d"]}, reverse=True)
            if len(prev) >= limit:
                last = ", ".join(f"{c} on {d:%d/%m/%Y}" for d, c in prev[:limit])
                flag(", ".join(sorted(have & group)), f"As per DHA dental billing rules, {wording}. Already billed for "
                     f"this patient: {last}. Kindly check — payer will deny.")
        if evals:
            docs = {(a.clinician_id or "").upper() for a in acts if a.activity_code in evals}
            prev_ev = sorted({r.d for r in earlier if EVALUATION(r.code) and r.doc in docs
                              and 0 < (claim["d"] - r.d).days <= 90}, reverse=True)
            if prev_ev:
                flag(", ".join(evals), f"Dental consultation / evaluation by the same dentist on {prev_ev[0]:%d/%m/%Y} "
                     f"({(claim['d'] - prev_ev[0]).days} days ago). A dental consultation covers 3 months; visits "
                     f"after it are follow-ups and are not billable. Kindly remove {', '.join(evals)}.")
    return out
