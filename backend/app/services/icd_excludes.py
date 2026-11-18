"""
ICD-10-CM Excludes-1 checks for the Claim Scrubber.

1. Official Excludes-1 notes from the CMS ICD-10-CM 2026 tabular
   (rules/icd10cm_2026_excludes1.json, 5,464 rules, built from icd10cm_tabular_2026.xml).
   A note under code X ("Excludes1: allergic rhinitis (J30.1-J30.9)") applies to X and every code below it.
   Excludes-1 means the two conditions are not coded together, unless they are clearly unrelated.

2. Payer practice (PRACTICE_PAIRS): pairs that are not ICD Excludes-1 but that payers flag as
   "symptom integral to the diagnosis" (ICD guideline I.B.5), learned from TPA denial comments and the
   Ghaf team's claim queries (Jan–Nov 2026). Labelled separately so coders know which is which.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

_FILE = Path(__file__).resolve().parent / "claim_scrubber" / "rules" / "icd10cm_2026_excludes1.json"

# Switched OFF (19 Nov 2026): measured on May–Nov 2026 remittances, payers flagged these on only 0.1–0.7%
# of 50k+ claims (official Excludes-1 pairs: 7%, ~50x the base rate), so they would create queries without
# preventing denials. Kept for reference; set PRACTICE_ACTIVE = True to re-enable.
PRACTICE_ACTIVE = False
# (definitive-diagnosis prefixes, symptom prefix, why) — symptom is dropped when a definitive dx explains it
PRACTICE_PAIRS = [
    (("A09", "A08", "K52"), "R19.7", "diarrhoea is part of gastroenteritis"),
    (("J00", "J01", "J02", "J03", "J04", "J06", "J20", "J21", "J22"), "R05", "cough is part of the respiratory infection"),
    (("J00", "J01", "J02", "J03", "J06", "J20", "J22", "N39.0", "A09"), "R50.9", "fever is part of the infection"),
    (("J02", "J03", "J06"), "R07.0", "throat pain is part of the throat infection"),
    (("J00", "J01", "J02", "J03", "J06", "J20", "J22", "N39.0"), "R79.82", "raised CRP is explained by the infection"),
    (("N39.0", "N30"), "R82.8", "abnormal urine findings are part of the UTI"),
    (("N39.0", "N30"), "R30.0", "painful urination is part of the UTI"),
    (("M79.1",), "R25.2", "cramp coded with myalgia — code the documented one"),
    (("M62.81", "M62.83"), "M79.1", "muscle weakness / spasm coded with myalgia — code the documented one"),
    (("J06.9",), "J22", "upper and lower acute respiratory infection NOS together — code the documented site"),
]


def _norm(code: str) -> str:
    return (code or "").strip().upper()


@lru_cache(maxsize=1)
def _rules() -> dict:
    """holder code -> list of (from, to, note)."""
    try:
        data = json.loads(_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    idx: dict = {}
    for r in data.get("rules", []):
        idx.setdefault(r["holder"].upper(), []).append((r["from"].upper(), r["to"].upper(), r["note"]))
    return idx


def _in_range(code: str, lo: str, hi: str) -> bool:
    """code falls under lo..hi; a range bound matches every code below it (J30.1-J30.9 covers J30.81)."""
    c = code.replace(".", "")
    lo_, hi_ = lo.replace(".", ""), hi.replace(".", "")
    if lo_ == hi_:
        return c.startswith(lo_)
    return lo_ <= c[:len(lo_)] and c[:len(hi_)] <= hi_


def _holders(code: str):
    """The code and each of its parents (J30.81 -> J30.81, J30.8, J30) that carry Excludes-1 notes."""
    idx = _rules()
    c = _norm(code)
    out = []
    while c:
        if c in idx:
            out.append(c)
        if "." in c:
            c = c[:-1].rstrip(".")
        else:
            break
    return out


def _is_symptom(code: str) -> bool:
    return code.startswith("R")


def excludes1_conflicts(dx: list[str]) -> list[dict]:
    """Official ICD-10-CM Excludes-1 pairs among a claim's diagnoses."""
    codes = list(dict.fromkeys(_norm(d) for d in dx if d))
    found, seen = [], set()
    for a in codes:
        for h in _holders(a):
            for lo, hi, note in _rules()[h]:
                for b in codes:
                    if b == a or not _in_range(b, lo, hi):
                        continue
                    key = tuple(sorted((a, b)))
                    if key in seen:
                        continue
                    seen.add(key)
                    # keep the definitive (non-R) code; if both or neither are symptoms the doctor decides
                    if _is_symptom(a) and not _is_symptom(b):
                        keep, drop = b, a
                    elif _is_symptom(b) and not _is_symptom(a):
                        keep, drop = a, b
                    else:
                        keep, drop = None, None
                    found.append({"a": a, "b": b, "keep": keep, "drop": drop, "note": f"{h} Excludes1: {note}",
                                  "source": "ICD-10-CM 2026 Excludes-1"})
    return found


def practice_conflicts(dx: list[str]) -> list[dict]:
    """Payer-practice pairs (symptom integral to a definitive diagnosis), not covered by Excludes-1."""
    if not PRACTICE_ACTIVE:
        return []
    codes = list(dict.fromkeys(_norm(d) for d in dx if d))
    official = {tuple(sorted((f["a"], f["b"]))) for f in excludes1_conflicts(codes)}
    out = []
    for defs, symptom, why in PRACTICE_PAIRS:
        s = next((x for x in codes if x.startswith(symptom)), None)
        d = next((x for x in codes if x != s and any(x.startswith(p) for p in defs)), None)
        if s and d and tuple(sorted((s, d))) not in official:
            out.append({"a": d, "b": s, "keep": d, "drop": s, "note": why,
                        "source": "Payer practice (symptom integral to diagnosis)"})
    return out
