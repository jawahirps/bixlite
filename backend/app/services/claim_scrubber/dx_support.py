"""
dx_support.py — does the consultation note (Presenting-Complaint observation) support each coded Dx?

Three layers, all local (notes are PHI and never leave the Mac / VM):
  1. KEYWORDS  — ICD prefix → words that document it in a chief complaint / case sheet.
  2. EVIDENCE  — result-based Dx (lipids, sugar, CBC, vitamin D …) are supported by the lab / vital on the claim.
  3. LEARNED   — for other frequent codes, words learned from the facility's own past notes (see learn()).

check() returns one finding per unsupported Dx with the Ghaf query wording.
"""
from __future__ import annotations

import html
import math
import re
from collections import Counter, defaultdict
from typing import Dict, Iterable, List, Optional

QUERY = "Dx {code}{desc} – kindly mention the derivation to the coded Dx; {why}"
QUERY_INTEGRAL = "Dx {code}{desc} is a {why}; kindly remove it and keep the symptom documented in the case sheet"

# Layer 1: ICD prefix (longest match wins) → regex of supporting words
KEYWORDS: Dict[str, str] = {
    "R05": r"cough",
    "R50": r"fever|febrile|pyrexia|temp(erature)?\b|chills|rigor|hot body",
    "R51": r"head|h/a\b|cahe|cephal|migraine",
    "R52": r"pain|ache",
    "R53": r"fatigue|tired|weak|letharg|malaise|exhaust|body ?ache",
    "R42": r"dizz|giddi|vertigo|light ?head|imbalance",
    "R06": r"breath|dyspn|sob\b|wheez|chest tight|asthma|noisy",
    "R07": r"chest|throat|odynophag|swallow|tonsil|pharyn",
    "R09.81": r"nasal|nose|block|congest|stuffy|cold|rhinorr|runny",
    "R09.82": r"post ?nasal|throat|phlegm|mucus|drip|clear(ing)? throat",
    "R10": r"abd|stomach|belly|epigast|umbilic|flank|colic|cramp|pain",
    "R11": r"vomit|nause|emesis|retch",
    "R19.7": r"diarr|loose (stool|motion)|watery stool",
    "R00": r"palpit|heart ?beat|racing|tachy",
    "R03": r"\bbp\b|blood pressure|pressure|hypert|hypot",
    "R20": r"numb|tingl|pins|burning sensation|paresth",
    "R21": r"rash|erupt|red(ness)? (spot|patch)",
    "R22": r"swell|lump|mass|nodule",
    "R23": r"rash|skin|itch|pigment|discolo",
    "R25": r"cramp|spasm|twitch",
    "R30": r"dysuria|burning (micturition|urin)|painful urin|urin",
    "R31": r"hematuria|haematuria|blood in urine",
    "R35": r"frequen|polyuria|nocturia|urin",
    "R60": r"swell|edema|oedema|puff",
    "R63": r"appetite|weight|anorex|eating",
    "R68": r"",
    "J00": r"cold|runny|rhinorr|sneez|nasal|nose|congest|block|coryza",
    "J01": r"sinus|facial pain|nasal|nose|block|congest|headache|post ?nasal",
    "J02": r"throat|odynophag|swallow|tonsil|pharyn",
    "J03": r"throat|tonsil|swallow",
    "J06": r"cough|cold|throat|fever|runny|nasal|sneez|congest|uri\b|flu",
    "J09": r"flu|influenza|fever|body ?ache|cough",
    "J10": r"flu|influenza|fever|body ?ache|cough",
    "J11": r"flu|influenza|fever|body ?ache|cough",
    "J18": r"cough|fever|breath|pneumon|chest",
    "J20": r"cough|phlegm|sputum|chest|wheez|bronch",
    "J22": r"cough|phlegm|sputum|chest|wheez|breath",
    "J30": r"sneez|allerg|itch(y|ing)? (nose|eye)|runny|rhinit|nasal",
    "J45.991": r"asthma|bronchit|atop|allerg|inhaler|nebul|montelukast|singulair|salbutamol|chest tight|chronic cough|night cough|cough at night|nocturnal|cough.{0,20}night",
    "J45": r"asthma|wheez|breath|bronchit|atop|inhaler|nebul|chest tight",
    "K02": r"tooth|teeth|dental|caries|cavity|decay|filling",
    "K04": r"tooth|teeth|dental|pulp|abscess|swell|root canal",
    "K05": r"gum|gingiv|bleed|teeth|dental|scaling|plaque",
    "K21": r"reflux|heartburn|acid|gerd|burning",
    "K29": r"gastri|epigast|stomach|abd|acid|burning|heartburn|bloat|dyspeps|vomit|nause|pain",
    "K30": r"dyspeps|indigest|bloat|epigast|gas",
    "K59": r"constipat|hard stool|not passing|bowel",
    "K76.0": r"fatty liver|liver|ultrasound|usg",
    "A09": r"diarr|loose|vomit|gastroenter|stomach|abd",
    "A49": r"infect|fever|pus|discharge|urin|dysuria",
    "B30": r"eye|conjunctiv|red(ness)? eye|discharge|watering",
    "B35": r"fung|itch|ring ?worm|scal|rash|nail",
    "B36": r"fung|itch|patch|rash",
    "B83": r"worm|deworm|itch(ing)? anus|anal itch|stool",
    "H04": r"dry eye|eye|burning|gritty|watering|itch",
    "H10": r"eye|red(ness)?|discharge|itch|watering|conjunctiv",
    "H52": r"vision|blur|glasses|spectacle|reading|refract|eye",
    "H57": r"eye pain|eye|ocular",
    "H60": r"ear",
    "H61": r"ear|wax",
    "H66": r"ear",
    "H92": r"ear",
    "L08": r"skin|wound|boil|infect|pus|abscess|swell",
    "L20": r"itch|rash|eczema|dry skin|atopic",
    "L23": r"itch|rash|allerg|contact",
    "L30": r"itch|rash|derm|eczema|dry skin|patch|skin|lesion|papul|erythem|scal",
    "L50": r"urticar|hive|itch|rash|wheal|allerg",
    "M25": r"joint|knee|shoulder|ankle|elbow|wrist|hip|pain|stiff|swell",
    "M54": r"back|neck|spine|lumb|sciat|\bl(ba|bp)\b|radiat|\bl[1-5]\b|\bslr\b|lasegue|laseague|claudicat|cervical|pain",
    "M62": r"muscle|spasm|cramp|stiff|pain|ache|tender|\bl(ba|bp)\b|\bslr\b",
    "M79": r"pain|ache|leg|arm|hand|foot|feet|limb|muscle|body|myalg|calf|heel",
    "N39.0": r"urin|dysuria|burning|frequen|uti\b",
    "N91": r"period|menstru|mens\b|lmp|amenor|cycle",
    "N92": r"period|menstru|mens\b|lmp|bleed|cycle|irregular",
    "N94": r"period|menstru|dysmenor|cramp|lower abd",
    "E55": r"vit(amin)? ?d|deficien|\bdef\b|supplement|cramp|bone|calcium|joint|pain|ache|fatigue|tired|weak|numb|tingl",
    "E58": r"calcium|deficien|\bdef\b|cramp|supplement|bone|fracture|lactat|pregnan|pain",
    "D50": r"an(a)?emi|pallor|pale|iron|ferritin|\bhb\b|h(a)?emoglobin|\bida\b|fatigue|tired|weak|dizz|menorr|heavy (bleed|period)",
    "D64": r"an(a)?emi|pallor|pale|iron|ferritin|\bhb\b|h(a)?emoglobin|fatigue|tired|weak|dizz|cbc",
    "E79": r"uric|hyperur|gout|toe|joint",
    "A49": r"infect|pus|lesion|wound|crp|fever|febrile|discharge|cellul|boil|abscess|tonsil|throat|uti\b|dysuria|urin|eryth|papul",
    "R60": r"swell|edema|oedema|puff|injur|ankle|fracture|sprain|feet|leg",
    "I10": r"\bbp\b|hypert|htn|blood pressure|pressure",
    "E11": r"\bdm\b|diabet|sugar|glucose|t2dm|niddm|metformin|hba1c|a1c",
    "E78": r"cholest|lipid|lipe?d|dlp|triglycer|statin|ldl|\btg\b|fatty liver",
    "E03": r"thyro|tsh|hypothyr",
    "E86": r"dehydrat|thirst|dry mouth|vomit|diarr|loose|fluid|low intake",
    "Z00": r"check ?up|routine|screen|medical|fitness|wellness",
    "Z23": r"vaccin|immuni|flu shot|booster",
    "Z76": r"refill|prescription|repeat medic",
}

# The defining finding of a symptom / condition: if the case sheet negates this ("no fever"), the Dx is contradicted
CORE: Dict[str, str] = {
    "R50": r"fever|febrile|pyrexia", "R05": r"cough", "R51": r"head ?ache|cahe", "R42": r"dizz|giddi|vertigo",
    "J45.991": r"asthma", "R06.2": r"wheez", "R06.0": r"breath|dyspn|sob\b", "R11": r"vomit|nause", "R19.7": r"diarr|loose", "A09": r"diarr|loose|vomit",
    "R30": r"dysuria|burning (micturition|urin)", "R31": r"blood in urine|hematuria|haematuria", "R00": r"palpit",
    "R21": r"rash", "R60": r"swell|edema|oedema", "J45": r"asthma|wheez", "I10": r"\bhtn\b|hypert",
    "E11": r"\bdm\b|diabet", "E78": r"\bdlp\b|dyslipid|hyperlip|cholest", "E03": r"hypothyr|thyroid",
    "R07.0": r"throat pain|sore throat", "J02": r"throat pain|sore throat", "R10": r"abd(ominal)? pain|stomach pain",
    "K29": r"epigast|acidity|gastritis", "K59.0": r"constipat", "N39.0": r"burning (micturition|urin)|dysuria",
}

# Layer 2: Dx whose source is a result / vital on the claim, not the complaint
OBS_EVIDENCE = {
    "I10": {"BPS", "BPD"}, "R03": {"BPS", "BPD"},
    "E11": {"A1C", "RBS", "FBS", "GLU"}, "R73": {"A1C", "RBS", "FBS", "GLU"},
    "E78": {"CHOL", "LDL", "HDL", "TRIG", "NONHDL"},
}
LAB_EVIDENCE = {   # ICD prefix → CPT codes on the same claim that produce the finding
    "E11": {"83036", "82947", "82950", "82962"}, "R73": {"83036", "82947", "82950", "82962"},
    "E78": {"80061", "82465", "83718", "83721", "84478"}, "E55": {"82306", "82652"}, "E58": {"82310"},
    "E03": {"84443", "84439", "84436"}, "E05": {"84443", "84439"}, "E61": {"82728", "83540"}, "E79": {"84550"},
    "D50": {"85025", "85027", "85018", "82728", "83540"}, "D64": {"85025", "85027", "85018"},
    "D72": {"85025", "85027", "85048", "85004"}, "D69": {"85025", "85027", "85049"}, "D75": {"85025", "85027"},
    "R79": {"86140", "85651", "85652", "84145"}, "R74": {"84460", "84450", "80076"}, "R94.5": {"84460", "84450", "80076"},
    "R80": {"81001", "81003", "81002"}, "R82": {"81001", "81003", "87086"}, "R31": {"81001", "81003"},
    "N39.0": {"81001", "81003", "87086", "87088"}, "R70": {"85651", "85652"}, "E87": {"80048", "80051", "84295", "84132"},
    "N18": {"82565", "80048"}, "R77": {"84155", "82040"}, "K76.0": {"76700", "76705"},
}
SKIP = re.compile(r"^(Z|U07|V|W|X|Y)")  # status / external-cause codes are not derived from complaints


# "not" that does not negate a finding: "f/u not better", "not had food" (= low intake)
NOT_FINDING = [
    (re.compile(r"\bnot\s+(getting\s+)?(better|improv\w*|reliev\w*|subsid\w*|reduc\w*|settl\w*|resolv\w*|ok|well|"
                r"responding|respond\w*)\b"), " persistent "),
    (re.compile(r"\bnot\s+(had|having|taken|taking|eat\w*|able to eat)\s+(any\s+)?(food|meals?|anything|orally|well)\b"),
     " low intake "),
]
# a negation covers the next few words only ("no loose stools constipation - 1 d" keeps constipation), extended
# by "/", "or", "and", "nor" ("no vomiting / loose motion / abd pain"); it never crosses punctuation or c/o, h/o …
# a single word that is a whole finding ends the negated phrase: "no vomiting nausea present" keeps nausea
_FINDING_WORD = (r"(?:vomit\w*|nause\w*|fever|cough|cold|constipat\w*|diarr\w*|dysuria|headache|giddi\w*|dizz\w*|"
                 r"sob|wheez\w*|rash\w*|itch\w*|swelling|bleeding|palpitat\w*|fatigue|weakness|dehydrat\w*)")
_STOP = r"(?:k/c/o|c/o|h/o|o/e|imp|adv|dx|s-|o-|or|and|nor|no|nil|not|denies|without)\b"
_NEG_WORD = rf"(?!{_STOP})[a-z0-9][a-z0-9'.+\-]*"
_NEG_GROUP = rf"(?:{_FINDING_WORD}\b|(?!{_FINDING_WORD}\b){_NEG_WORD}(?:[ \t]+(?!{_FINDING_WORD}\b){_NEG_WORD})?)"
NEG_CLAUSE = (rf"\b(?:no|nil|not|denies|denied|without|negative for)\b[ \t]*(?:(?:h/o|history of|c/o|any)\b[ \t]*)?"
              rf"{_NEG_GROUP}(?:[ \t]*(?:/|\bor\b|\band\b|\bnor\b|&)[ \t]*{_NEG_GROUP})*")


def _fix_not(t: str) -> str:
    for rx, rep in NOT_FINDING:
        t = rx.sub(rep, t)
    return t


def norm(text: Optional[str], drop_negated: bool = True) -> str:
    t = _fix_not(html.unescape(text or "").lower())
    t = re.sub(r"\bd[sy]{1,2}[sy]?ur[ie]a\b|\bdisuria\b", "dysuria", t)   # common misspellings
    if not drop_negated:
        t = re.sub(r"\b(no|nil|not|denies|denied|without|negative for)\b", " ", t)
    # negations first, while "h/o" is still there: "no h/o vomiting /loose motion" drops the clause
    t = re.sub(NEG_CLAUSE, " ", t)
    t = re.sub(r"\bafebrile\b", " ", t)
    t = re.sub(r"\bk/c/o\b|\bc/o\b|\bh/o\b|[\n\r,;:]+", " xx ", t)  # keep phrases from running together
    return re.sub(r"[^a-z0-9/\.]+", " ", t)


NEG = re.compile(r"\b(no|nil|not|denies|denied|without|negative for)\b", re.I)


def negated_lines(note: Optional[str], core: str, limit: int = 3) -> List[str]:
    """The case-sheet lines that negate the finding, quoted as written."""
    out = []
    for line in re.split(r"[\r\n]+", html.unescape(note or "")):
        line = line.strip(" -•\t")
        low = _fix_not(line.lower())
        if any(re.search(core, m.group(0)) for m in re.finditer(NEG_CLAUSE, low)):
            out.append(line[:120])
    return out[:limit]


def _prefix_lookup(table: dict, code: str):
    for n in (len(code), 6, 5, 4, 3):
        v = table.get(code[:n])
        if v is not None:
            return v
    return None


TOKEN = re.compile(r"[a-z]{3,}")
STOP = set("the and with for since days day patient came complaint complaints has had his her also from not are was were any "
           "this that per history old male female years year yrs taking take treatment week weeks month months".split())


class Learned:
    """Words that co-occur with each Dx far more than average in this facility's past notes."""

    def __init__(self, min_claims: int = 80, top: int = 25):
        self.min_claims, self.top = min_claims, top
        self.words: Dict[str, set] = {}

    def fit(self, rows: Iterable[tuple]):          # rows: (complaint_text, [dx codes])
        dx_n, dx_w, all_w, n = Counter(), defaultdict(Counter), Counter(), 0
        for text, codes in rows:
            ws = set(TOKEN.findall(norm(text))) - STOP
            n += 1
            all_w.update(ws)
            for c in set(codes):
                dx_n[c] += 1
                dx_w[c].update(ws)
        for c, k in dx_n.items():
            if k < self.min_claims:
                continue
            scored = []
            for w, f in dx_w[c].items():
                if f < 5:
                    continue
                p_dx, p_all = f / k, all_w[w] / n
                scored.append((p_dx * math.log(p_dx / p_all), w))
            self.words[c] = {w for s, w in sorted(scored, reverse=True)[: self.top] if s > 0}
        return self


# Symptom codes (R… and a few symptom-type codes) that are routinely part of a confirmed Dx on the same claim.
# ICD-10-CM guideline I.B.5: such signs / symptoms are documented in the case sheet, not coded separately.
_URI = ("R50", "R05", "R09.8", "R07.0", "R51", "R06.7", "J00", "M79.1", "R53")
INTEGRAL = {
    "J00": _URI, "J01": _URI, "J02": _URI, "J03": _URI, "J04": _URI, "J06": _URI,
    "J09": _URI + ("R06",), "J10": _URI + ("R06",), "J11": _URI + ("R06",), "U07.1": _URI + ("R06", "R43"),
    "J12": _URI + ("R06", "R07.1", "R07.8"), "J18": _URI + ("R06", "R07.1", "R07.8"),
    "J20": _URI + ("R06", "R07.8"), "J21": _URI + ("R06",), "J22": _URI + ("R06", "R07.8"), "J40": _URI + ("R06",),
    "J30": ("R09.8", "R06.7", "J00"), "J31": ("R09.8", "R06.7"), "J32": ("R09.8", "R51", "R05"),
    "J45": ("R06", "R05", "R09.8"), "J44": ("R06", "R05"),
    "A09": ("R19.7", "R11", "R10", "R50", "K59.1"), "K52": ("R19.7", "R11", "R10"), "A08": ("R19.7", "R11", "R10", "R50"),
    "K29": ("R10.1", "R10.8", "R10.9", "R11", "R14", "R12"), "K30": ("R10.1", "R14", "R12", "R11"),
    "K21": ("R12", "R10.1", "R07.89", "R13"), "K59.0": ("R10", "R14"), "K58": ("R10", "R19.4", "R19.7", "R14"),
    "N39.0": ("R30", "R35", "R39.1"), "N30": ("R30", "R35", "R39.1", "R31"), "N20": ("R10", "R31", "R11"),
    "B34": ("R50", "R53", "M79.1"), "A49": ("R50",), "A90": ("R50", "M79.1", "R51", "R21"), "A91": ("R50", "M79.1", "R51", "R21"),
    "B01": ("R50", "R21", "L29"), "B05": ("R50", "R21", "R05"), "B26": ("R50", "R22"),
    "H65": ("H92.0", "R50"), "H66": ("H92.0", "R50"), "H60": ("H92.0", "H92.1"), "H10": ("H57.1",),
    "L50": ("L29", "R21"), "L20": ("L29", "R21"), "L23": ("L29", "R21"), "L30": ("L29", "R21"),
    "I10": ("R03.0",), "E11": ("R73",), "E10": ("R73",), "G43": ("R51", "R11"), "H81": ("R42", "R11"),
    "M54.4": ("M54.5", "M79.1"), "M54.5": ("M79.1",), "M17": ("M25.56",), "M19.01": ("M25.51",), "S93.4": ("M25.57", "R60"),
}


def integral_to(code: str, coded: List[str]) -> Optional[str]:
    """The confirmed Dx on the claim that this symptom code is part of, if any."""
    for other in coded:
        if other == code or other.startswith("R"):
            continue
        sym = _prefix_lookup(INTEGRAL, other)
        if sym and code.startswith(sym):
            return other
    return None


# What the case sheet should show, in reviewer words (falls back to the keyword list)
FINDING_LABEL = {
    "R05": "cough", "R50": "fever / chills", "R51": "headache", "R53": "fatigue / weakness / malaise",
    "R42": "dizziness / giddiness / vertigo", "R06": "breathlessness / wheeze", "R07": "chest or throat pain",
    "R09.81": "nasal congestion / blocked nose", "R09.82": "post-nasal drip / throat phlegm", "R10": "abdominal pain",
    "R11": "nausea / vomiting", "R19.7": "diarrhoea / loose motion", "R00": "palpitations", "R03": "BP complaint or reading",
    "R20": "numbness / tingling", "R21": "rash", "R22": "swelling / lump", "R23": "skin change / itch", "R25": "cramps / spasm",
    "R30": "dysuria / burning urination", "R31": "blood in urine", "R35": "urinary frequency", "R60": "swelling / oedema",
    "R63": "appetite or weight change", "J00": "cold / runny nose / sneezing", "J01": "sinus symptoms / facial pain",
    "J02": "sore throat / painful swallowing", "J03": "tonsillitis / sore throat", "J06": "URI symptoms (cough, cold, throat, fever)",
    "J09": "flu symptoms", "J10": "flu symptoms", "J11": "flu symptoms", "J18": "pneumonia findings (cough, fever, breathlessness)",
    "J20": "cough with phlegm / chest findings", "J22": "cough / chest findings", "J30": "allergic nasal symptoms (sneezing, itchy / runny nose)",
    "J45.991": "asthma / atopy / allergy / bronchitic changes / chronic or night cough", "J45": "asthma / wheeze / breathlessness",
    "K02": "tooth decay / cavity", "K04": "tooth pain / pulp / dental abscess", "K05": "gum problem / bleeding gums",
    "K21": "reflux / heartburn", "K29": "epigastric pain / acidity / heartburn / bloating / vomiting", "K30": "dyspepsia / indigestion / bloating",
    "K59": "constipation", "K76.0": "fatty liver (history, ultrasound)", "A09": "diarrhoea / vomiting / abdominal pain",
    "A49": "infection signs (pus, lesion, wound, fever, raised CRP)", "B30": "eye redness / discharge", "B35": "fungal skin or nail signs",
    "B36": "fungal skin patch / itch", "B83": "worms / anal itch", "H04": "dry or gritty eyes", "H10": "red eye / discharge / itchy eye",
    "H52": "vision complaint / glasses", "H57": "eye pain", "H60": "ear complaint", "H61": "ear wax / ear complaint", "H66": "ear pain / discharge",
    "H92": "ear pain", "L08": "skin infection (boil, pus, wound)", "L20": "eczema / itch / dry skin", "L23": "contact rash / itch",
    "L30": "dermatitis / rash / itch", "L50": "urticaria / hives / itch", "M25": "joint pain / stiffness / swelling",
    "M54": "back or neck pain", "M62": "muscle spasm / cramp / stiffness", "M79": "limb or muscle pain",
    "N39.0": "urinary symptoms (burning, frequency)", "N91": "menstrual history (LMP / missed period)", "N92": "menstrual complaint (irregular / heavy)",
    "N94": "menstrual pain", "I10": "hypertension history or BP", "E11": "diabetes history / sugar", "E78": "lipid history / cholesterol",
    "E03": "thyroid history", "E86": "dehydration signs (vomiting, diarrhoea, low intake)", "E55": "vitamin D deficiency / bone or muscle pain / supplements",
    "E58": "calcium deficiency / cramps", "D50": "anaemia / pallor / iron deficiency", "D64": "anaemia / pallor / low Hb",
    "E79": "uric acid / gout / joint pain", "Z00": "routine check-up", "Z23": "vaccination", "Z76": "prescription refill",
}
TEST_NAME = {"83036": "HbA1c", "82947": "glucose", "82950": "glucose", "82962": "glucose", "80061": "lipid panel",
             "82465": "cholesterol", "83718": "HDL", "83721": "LDL", "84478": "triglycerides", "82306": "vitamin D",
             "82652": "vitamin D", "82310": "calcium", "84443": "TSH", "84439": "free T4", "84436": "T4", "82728": "ferritin",
             "83540": "iron", "84550": "uric acid", "85025": "CBC", "85027": "CBC", "85018": "haemoglobin", "85048": "WBC",
             "85004": "WBC differential", "85049": "platelets", "86140": "CRP", "85651": "ESR", "85652": "ESR", "84145": "procalcitonin",
             "84460": "ALT", "84450": "AST", "80076": "liver panel", "81001": "urinalysis", "81003": "urinalysis", "81002": "urinalysis",
             "87086": "urine culture", "87088": "urine culture", "80048": "basic metabolic panel", "80051": "electrolytes",
             "84295": "sodium", "84132": "potassium", "82565": "creatinine", "84155": "protein", "82040": "albumin",
             "76700": "abdominal ultrasound", "76705": "abdominal ultrasound"}
OBS_NAME = {"BPS": "BP reading", "BPD": "BP reading", "A1C": "HbA1c result", "RBS": "blood sugar result",
            "FBS": "blood sugar result", "GLU": "blood sugar result", "CHOL": "lipid result", "LDL": "lipid result"}


def _label(code: str) -> str:
    for n in (len(code), 6, 5, 4, 3):
        if code[:n] in FINDING_LABEL:
            return FINDING_LABEL[code[:n]]
    return describe(code).lower()


def _tests(code: str) -> str:
    names = []
    for c in sorted(_prefix_lookup(LAB_EVIDENCE, code) or ()):
        n = TEST_NAME.get(c, c)
        if n not in names:
            names.append(n)
    for c in sorted(_prefix_lookup(OBS_EVIDENCE, code) or ()):
        n = OBS_NAME.get(c, c)
        if n not in names:
            names.append(n)
    return " / ".join(names[:4])


CATEGORY = {
    "negated": "1. Contradicted by case sheet",
    "keywords": "2. Not documented in case sheet",
    "evidence": "3. Result-based Dx, test not on claim",
    "learned": "4. Unusual for the case sheet (review)",
    "no note": "5. No case sheet",
}


def reason(finding: dict) -> str:
    code, layer = finding["code"], finding["layer"]
    if layer == "negated":
        q = finding.get("quotes") or []
        return "Case sheet: " + "; ".join(f'"{x}"' for x in q) if q else f"Case sheet says no {_label(code)}"
    if layer == "keywords":
        r = f"No {_label(code)} documented"
        t = _tests(code)
        return r + (f"; no {t} on the claim" if t else "")
    if layer == "evidence":
        return f"No {_tests(code) or 'supporting test'} on the claim"
    if layer == "learned":
        return f"None of the findings usually written for {describe(code).lower() or code}"
    return "Consultation has no chief complaint / notes"


def check(dx: List[tuple], complaint: Optional[str], obs_codes: set, act_codes: set,
          learned: Optional[Learned] = None) -> List[dict]:
    """dx = [(code, description)]. Returns findings for Dx the note / claim does not support."""
    text = norm(complaint)
    out = []
    coded = [c for c, _ in dx]
    # A coded symptom that is part of a confirmed Dx on the claim is kept (payers may need it for medical necessity)
    # and needs no separate derivation — the confirmed Dx carries it.
    done = {c for c in coded if integral_to(c, coded)}
    for code, desc in dx if text.strip() else []:
        if SKIP.match(code) or code in done:
            continue
        labs = _prefix_lookup(LAB_EVIDENCE, code)
        if labs and labs & act_codes:
            continue
        obs = _prefix_lookup(OBS_EVIDENCE, code)
        if obs and obs & obs_codes:
            continue
        kw = _prefix_lookup(KEYWORDS, code)
        if kw:
            if re.search(kw, text):
                continue
            core = _prefix_lookup(CORE, code)
            if core and re.search(core, norm(complaint, drop_negated=False)):
                quotes = negated_lines(complaint, core)
                out.append({"code": code, "layer": "negated", "quotes": quotes,
                            "why": "case sheet records it as absent: " + "; ".join(f'"{q}"' for q in quotes)})
                continue
            why = "case sheet does not document it" + (" and no supporting result on the claim" if labs or obs else "")
            out.append({"code": code, "layer": "keywords", "why": why})
            continue
        if labs or obs:
            out.append({"code": code, "layer": "evidence", "why": "result-based Dx without the supporting test / reading on the claim"})
            continue
        words = learned.words.get(code) if learned else None
        if words and not (set(TOKEN.findall(text)) & words):
            out.append({"code": code, "layer": "learned", "why": "case sheet has none of the findings usually documented for it"})
    if not text.strip():
        out += [{"code": c, "layer": "no note", "why": "no chief complaint / case sheet note on the consultation"}
                for c, _ in dx if not SKIP.match(c) and c not in done]
    for f in out:
        d = dict(dx).get(f["code"]) or describe(f["code"])
        f["reason"] = reason(f)
        f["category"] = CATEGORY.get(f["layer"], "")
        tmpl = QUERY_INTEGRAL if f["layer"] == "integral" else QUERY
        f["query"] = tmpl.format(code=f["code"], desc=f" ({d})" if d else "", why=f["why"])
    return out


# ── Descriptions and suggested coding ─────────────────────────────────────────
_ICD: Optional[Dict[str, str]] = None


def describe(code: str) -> str:
    """ICD-10-CM description (CMS 2026 order file); falls back to the parent category."""
    global _ICD
    if _ICD is None:
        import json
        from pathlib import Path
        _ICD = json.loads((Path(__file__).parent / "rules" / "icd10cm_2026.json").read_text())
    c = (code or "").strip().upper()
    return _ICD.get(c) or _ICD.get(c.rstrip("0").rstrip(".")) or _ICD.get(c[:3]) or ""


# Findings written in a case sheet → ICD category; the exact code is the one this facility uses most for it
FINDINGS = [
    (r"fever|febrile|pyrexia|temp(erature)? (high|\d)", "R50"), (r"cough", "R05"),
    (r"sore throat|throat pain|pain(ful)? throat|odynophag|painful swallow", "J02"), (r"tonsil", "J03"),
    (r"runny nose|rhinorr|sneez|common cold|\bcold\b|coryza", "J00"), (r"nasal (congest|block)|blocked nose|stuffy", "R09.81"),
    (r"post ?nasal|phlegm in throat", "R09.82"), (r"sinus", "J01"), (r"wheez", "R06.2"),
    (r"short(ness)? of breath|breathless|dyspn|\bsob\b|\bdoe\b", "R06.0"), (r"chest pain", "R07.9"),
    (r"head ?ache|h/a\b|migraine", "R51"), (r"dizz|giddi|vertigo|light ?head", "R42"),
    (r"fatigue|tired|letharg|malaise|weakness|generali[sz]ed weak", "R53"), (r"body ?ache|myalg|muscle pain", "M79.1"),
    (r"epigast|heartburn|acidity|gastritis", "K29"), (r"abd(ominal)? pain|stomach (pain|ache)|tummy", "R10"),
    (r"vomit|nause", "R11"), (r"diarr|loose (stool|motion)", "R19.7"), (r"constipat", "K59.0"), (r"bloat", "R14"),
    (r"dysuria|burning (micturition|urin)|painful urin", "R30"), (r"frequen(cy|t) (of )?urin|polyuria", "R35"),
    (r"low back|lower back|back ?ache|back pain|lumb", "M54.5"), (r"neck pain|cervical pain", "M54.2"),
    (r"knee pain", "M25.56"), (r"shoulder pain", "M25.51"), (r"ankle pain", "M25.57"), (r"joint pain|arthralg", "M25.50"),
    (r"swell|edema|oedema", "R60"), (r"palpit", "R00.2"), (r"rash", "R21"), (r"itch|prurit", "L29"),
    (r"urticar|hives", "L50"), (r"red(ness)? eye|eye discharge|conjunctiv", "H10"), (r"dry eye", "H04.12"),
    (r"ear (pain|ache)|otalg", "H92.0"), (r"tooth ?ache|dental pain", "K08.8"), (r"dehydrat", "E86"),
    (r"hypert|\bhtn\b", "I10"), (r"\bdm\b|diabet|t2dm", "E11"), (r"\bdlp\b|dyslipid|hyperlip|cholest", "E78"),
    (r"hypothyr", "E03"), (r"an(a)?emi", "D64"), (r"vit(amin)? ?d (def|low)", "E55"), (r"fatty liver", "K76.0"),
    (r"asthma", "J45"), (r"allergic rhinit", "J30"), (r"wound|laceration|cut injury", "T14"),
]


class Preferred:
    """Most-used code under each category at this facility, so suggestions match its own coding."""

    def __init__(self, codes: Iterable[str]):
        self.count = Counter(codes)

    def pick(self, prefix: str) -> Optional[str]:
        best = [(n, c) for c, n in self.count.items() if c.startswith(prefix)]
        if best:
            return max(best)[1]
        return prefix if describe(prefix) else None


def supported_not_coded(note: Optional[str], dx: List[tuple], pref: Preferred) -> List[str]:
    text = norm(note)
    coded = [c for c, _ in dx]
    out = []
    for pat, prefix in FINDINGS:
        if not re.search(pat, text) or any(c.startswith(prefix[:3]) for c in coded) or integral_to(prefix, coded):
            continue
        code = pref.pick(prefix)
        if code and code not in out:
            out.append(code)
    return out


def suggestion(finding: dict, note: Optional[str], dx: List[tuple], pref: Preferred) -> str:
    code = finding["code"]
    extra = supported_not_coded(note, dx, pref)
    if finding["layer"] == "integral":
        p = finding["parent"]
        return f"Remove {code} ({describe(code)}); covered by {p} ({describe(p)})"
    if finding["layer"] == "no note":
        return f"Add the case sheet / chief complaint supporting {code} {describe(code)}"
    s = f"Document {code} ({describe(code)}) in the case sheet, or remove it"
    if finding["layer"] == "negated":
        s = f"Case sheet records {_label(code)} as absent: correct the case sheet or remove {code} ({describe(code)})"
    if finding["layer"] == "evidence":
        s = f"Attach / bill the supporting result for {code} ({describe(code)}), or remove it"
    if extra:
        s += ". Case sheet supports, not coded: " + "; ".join(f"{c} {describe(c)}" for c in extra[:4])
    return s


# ── Tiers: who acts on a finding ─────────────────────────────────────────────
# A = claim query before submission · B = monthly doctor feedback · C = Ghaf internal review
CONSULT = re.compile(r"^(9|10|11|12|13|14|15|16|17|18|19|20|21|22|23|24|25)(\.\d{2})?$|^992\d\d$")


def evaluate(dx: List[tuple], complaint: Optional[str], obs_codes: set, acts: List[tuple],
             learned: Optional[Learned] = None, pref: Optional[Preferred] = None) -> List[dict]:
    """check() plus tier, service at risk and suggestion. acts = [(code, net)]."""
    act_codes = {c for c, _ in acts}
    found = check(dx, complaint, obs_codes, act_codes, learned)
    if not found:
        return []
    flagged = {f["code"] for f in found}
    coded = [c for c, _ in dx]
    principal = coded[0] if coded else None
    supported = [c for c in coded if c not in flagged and not SKIP.match(c)]
    services = [(c, float(n or 0)) for c, n in acts if not CONSULT.match(c) and float(n or 0) > 0]
    pref = pref or Preferred([])
    for f in found:
        sole = f["code"] == principal or not supported
        if f["layer"] == "negated":
            f["tier"] = "A"
        elif f["layer"] in ("learned", "no note"):   # no case sheet: Ghaf manual review, never a Dx query
            f["tier"] = "C"
        elif services and sole:
            f["tier"] = "A"
            f["layer_note"] = "principal Dx" if f["code"] == principal else "only Dx on the claim"
        else:
            f["tier"] = "B"
        at_risk = services if f["tier"] == "A" and f["layer"] != "no note" else []
        f["services"] = ", ".join(c for c, _ in at_risk[:8])
        f["amount_at_risk"] = round(sum(n for _, n in at_risk), 2)
        f["suggestion"] = suggestion(f, complaint, dx, pref).split(". Case sheet supports")[0]
        f["not_coded"] = "; ".join(f"{k} {describe(k)}" for k in supported_not_coded(complaint, dx, pref)[:4])
        f["description"] = describe(f["code"])
    return found
