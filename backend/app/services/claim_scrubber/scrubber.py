#!/usr/bin/env python3
"""Local-first UAE claim scrubber.

The input is never modified. The tool emits a JSON review report. UAE and
competitor rules are intentionally conservative: uncertain coding/payer rules
are warnings, not payment decisions.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import defaultdict
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
import xml.etree.ElementTree as ET

ROOT_EXPECTED = "Claim.Submission"
DATE_FORMATS = ("%d/%m/%Y %H:%M", "%d/%m/%Y", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d")

PROFILE_SOURCES = {
    "UAE_COMMON": [
        "https://www.eclaimlink.ae/NoneRegisteredCodingSets.aspx",
        "https://www.doh.gov.ae/en/Shafafiya/standards",
    ],
    "DUBAI": [
        "https://www.eclaimlink.ae/",
        "https://dhpo.eclaimlink.ae/ValidateTransactions.asmx?WSDL",
    ],
    "ABU_DHABI": [
        "https://www.doh.gov.ae/en/Shafafiya/dictionary/public-test-environment",
        "https://www.doh.gov.ae/en/Shafafiya/standards",
    ],
    "NORTHERN_EMIRATES": [
        "https://mohap.gov.ae/en/riayati/e-claims-post-office",
        "https://mohap.gov.ae/en/riayati/resources",
    ],
}

PROFILE_NOTES = {
    "UAE_COMMON": "Common local checks plus conservative crosswalk and denial-risk review; not a regulator or payer adjudication.",
    "DUBAI": "DHA/eClaimLink-oriented profile. DHPO validation requires authorized credentials; this local run does not call DHPO.",
    "ABU_DHABI": "DoH/Shafafiya-oriented profile. PTE_VALIDATE_ONLY is the official validation benchmark when authorized credentials are available.",
    "NORTHERN_EMIRATES": "MoHAP/Riayati-oriented profile. Public sources do not expose a standalone anonymous claims validator.",
}

DEFAULT_RULES = {
    "consultation_codes": {"9", "9.01", "9.02", "10", "10.01", "10.02", "11", "11.02"},
    "consultation_followup_codes": {"9.01", "9.02", "10.01", "10.02", "11.02"},
    "administration_prefixes": ("963",),
    "injection_codes": {"96372", "96373", "96374", "96375", "96376", "96377", "96379"},
    "infusion_codes": {"96360", "96361", "96365", "96366", "96367", "96368", "96369", "96370", "96371"},
    # Pure RCM publicly illustrates missing modifier on CPT 99214. Keep this
    # as a UAE comparison warning, not a universal payer rule.
    "modifier_review_codes": {"99214"},
    "source_urls": {
        "consultation": "https://www.nextcarehealth.com/news-and-cues/medical-bulletin/consultation-rules-for-healthcare-providers/",
        "injection_candidate": "https://www.hopkinsmedicine.org/-/media/compliance/documents/infusion-guideline-092020.pdf",
        "xml_sample": "https://www.eclaimlink.ae/dhd/samples/claimsubmission.xml",
        "pure_rcm": "https://claims.purercm.ae/",
        "healthorbit": "https://healthorbit.ai/uae/claim-scrubber/",
    },
}

RULE_PACK_PATH = Path(__file__).with_name("rules") / "uae-rules-2025.json"
NAS_GUIDELINES_PATH = Path(__file__).with_name("rules") / "nas-guidelines-2026.json"


def load_json_file(path: Path, fallback):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return fallback


def load_nas_guidelines(path: Path = NAS_GUIDELINES_PATH):
    return load_json_file(path, {"guideline_pack_id": "NONE", "version": "none", "rules": [], "source": {}})


def load_calibration(path: Path | None = None):
    if path is None:
        return None
    return load_json_file(path, {"calibration_id": "INVALID", "rule_weights": {}})


COMPETITOR_COMPARISON = {
    "pure_rcm": {
        "name": "Pure RCM Claims Validator",
        "url": "https://claims.purercm.ae/",
        "evidence": "Public product page reviewed 2026-09-22",
        "documented_capabilities": ["CPT-ICD checks", "bundling/MNEC and excludes-1 checks", "modifier detection", "XML/JSON/CSV uploads", "impact/denial-risk flags"],
        "external_test": "Not submitted: no account or claim transmission authorized.",
    },
    "healthorbit": {
        "name": "HealthOrbit AI UAE Claim Scrubber",
        "url": "https://healthorbit.ai/uae/claim-scrubber/",
        "evidence": "Public product page identified; detailed feature extraction was not independently verified in this run.",
        "documented_capabilities": ["UAE pre-submission claim scrubbing advertised"],
        "external_test": "Not submitted: no account or claim transmission authorized.",
    },
}


def load_rule_pack(path: Path = RULE_PACK_PATH):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"rule_pack_id": "NONE", "version": "none", "sources": [], "cpt_icd": {}, "bundling": {}, "service_frequency": [], "denial_impact": {"rules": []}}


def csv_to_xml(path: Path):
    """Normalize a flat claim/activity CSV into the internal XML shape.

    Expected columns: claim_id, member_id, payer_id, provider_id,
    emirates_id, gross, patient_share, net, encounter_* and activity_*.
    Missing optional columns remain empty and are handled by normal rules.
    """
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise ValueError("CSV input has no data rows")
    root = ET.Element(ROOT_EXPECTED)
    header = ET.SubElement(root, "Header")
    transaction_date = rows[0].get("transaction_date") or rows[0].get("TransactionDate") or rows[0].get("activity_start") or rows[0].get("ActivityStart", "")
    for key, value in {"SenderID": "CSV", "ReceiverID": "LOCAL", "TransactionDate": transaction_date, "RecordCount": "", "DispositionFlag": "TEST"}.items():
        ET.SubElement(header, key).text = value
    claims = {}
    for row in rows:
        cid = row.get("claim_id") or row.get("ClaimID") or row.get("claim") or "claim-1"
        if cid not in claims:
            claim = ET.SubElement(root, "Claim")
            claims[cid] = claim
            mapping = {
                "ID": cid, "MemberID": row.get("member_id") or row.get("MemberID", ""),
                "PayerID": row.get("payer_id") or row.get("PayerID", ""), "ProviderID": row.get("provider_id") or row.get("ProviderID", ""),
                "EmiratesIDNumber": row.get("emirates_id") or row.get("EmiratesIDNumber", ""),
                "Gross": row.get("gross") or row.get("Gross", ""), "PatientShare": row.get("patient_share") or row.get("PatientShare", ""), "Net": row.get("net") or row.get("Net", ""),
            }
            for tag, value in mapping.items(): ET.SubElement(claim, tag).text = value
            encounter = ET.SubElement(claim, "Encounter")
            for tag, aliases in {"FacilityID": ("facility_id", "FacilityID"), "Type": ("encounter_type", "EncounterType"), "PatientID": ("patient_id", "PatientID"), "Start": ("encounter_start", "EncounterStart"), "End": ("encounter_end", "EncounterEnd"), "StartType": ("encounter_start_type",), "EndType": ("encounter_end_type",)}.items():
                ET.SubElement(encounter, tag).text = next((row.get(a, "") for a in aliases if row.get(a)), "")
            for dcode, dtype in [(row.get("diagnosis_code", ""), row.get("diagnosis_type", "Secondary"))]:
                if dcode:
                    d = ET.SubElement(claim, "Diagnosis"); ET.SubElement(d, "Type").text = dtype; ET.SubElement(d, "Code").text = dcode
        claim = claims[cid]
        activity = ET.SubElement(claim, "Activity")
        for tag, aliases in {"ID": ("activity_id", "ActivityID"), "Start": ("activity_start", "ActivityStart"), "Type": ("activity_type", "ActivityType"), "Code": ("activity_code", "ActivityCode"), "Quantity": ("quantity", "Quantity"), "Net": ("activity_net", "ActivityNet"), "Clinician": ("clinician", "Clinician"), "Modifier": ("modifier", "Modifier"), "PriorAuthorizationID": ("prior_authorization_id", "PriorAuthorizationID")}.items():
            ET.SubElement(activity, tag).text = next((row.get(a, "") for a in aliases if row.get(a)), "")
    header.find("RecordCount").text = str(len(claims))
    return ET.ElementTree(root)


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def child(parent: ET.Element, name: str):
    for node in list(parent):
        if local_name(node.tag) == name:
            return node
    return None


def children(parent: ET.Element, name: str):
    return [node for node in list(parent) if local_name(node.tag) == name]


def text(parent: ET.Element, name: str, default: str = "") -> str:
    node = child(parent, name)
    return (node.text or "").strip() if node is not None else default


def parse_dt(value: str):
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(value.strip(), fmt)
        except (ValueError, AttributeError):
            pass
    return None


def money(value: str):
    try:
        return Decimal(value.strip())
    except (InvalidOperation, AttributeError):
        return None


def finding(findings, severity, rule_id, location, message, claim_id="", activity_id="", source="local rule"):
    findings.append({
        "severity": severity,
        "rule_id": rule_id,
        "location": location,
        "claim_id": claim_id,
        "activity_id": activity_id,
        "message": message,
        "source": source,
    })


def _json_value(value):
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _append_json(parent, tag, value):
    if isinstance(value, list):
        for item in value:
            _append_json(parent, tag, item)
        return
    node = ET.SubElement(parent, tag)
    if isinstance(value, dict):
        for key, child_value in value.items():
            _append_json(node, str(key), child_value)
    else:
        node.text = _json_value(value)


def json_to_xml(path: Path):
    """Accept a small normalized JSON shape or a JSON-serialized XML tree."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and data.get("root"):
        data = data["root"]
    if isinstance(data, dict) and data.get("tag"):
        def from_tree(item):
            node = ET.Element(str(item["tag"]))
            value = item.get("text")
            if value is not None:
                node.text = _json_value(value)
            for child_item in item.get("children", []):
                node.append(from_tree(child_item))
            return node
        return ET.ElementTree(from_tree(data))
    if not isinstance(data, dict):
        raise ValueError("JSON input must be an object")
    submission = ET.Element(ROOT_EXPECTED)
    header_data = data.get("Header") or data.get("header") or {}
    header = ET.SubElement(submission, "Header")
    if isinstance(header_data, dict):
        for key, value in header_data.items():
            _append_json(header, str(key), value)
    claims_data = data.get("Claim") or data.get("claims") or []
    if isinstance(claims_data, dict):
        claims_data = [claims_data]
    for claim_data in claims_data:
        if not isinstance(claim_data, dict):
            continue
        claim = ET.SubElement(submission, "Claim")
        for key, value in claim_data.items():
            _append_json(claim, str(key), value)
    if not text(header, "RecordCount"):
        ET.SubElement(header, "RecordCount").text = str(len(children(submission, "Claim")))
    return ET.ElementTree(submission)


def parse_input(path: Path):
    suffix = path.suffix.lower()
    if suffix == ".json":
        return json_to_xml(path), "JSON normalized locally"
    if suffix == ".csv":
        return csv_to_xml(path), "CSV normalized locally"
    return ET.parse(path), "XML parsed locally"


def scrub(path: Path, rules=None, profile="UAE_COMMON", rule_pack=None, nas_guidelines=None, calibration=None):
    rules = rules or DEFAULT_RULES
    rule_pack = rule_pack or load_rule_pack()
    nas_guidelines = nas_guidelines or None
    calibration = calibration or None
    profile = profile.upper()
    if profile not in PROFILE_SOURCES:
        raise ValueError(f"Unknown profile {profile}; choose from {', '.join(PROFILE_SOURCES)}")
    findings = []
    try:
        tree, input_mode = parse_input(path)
        root = tree.getroot()
    except (ET.ParseError, OSError, ValueError, json.JSONDecodeError) as exc:
        return {"status": "BLOCK", "input": str(path), "profile": profile, "findings": [{
            "severity": "BLOCK", "rule_id": "INPUT_PARSE", "location": "/",
            "claim_id": "", "activity_id": "", "message": str(exc), "source": "local parser"
        }]}

    root_name = local_name(root.tag)
    if root_name != ROOT_EXPECTED:
        finding(findings, "BLOCK", "ROOT_NAME", "/", f"Expected root {ROOT_EXPECTED}; found {root_name}.")

    header = child(root, "Header")
    claims = children(root, "Claim")
    if header is None:
        finding(findings, "BLOCK", "HEADER_MISSING", "/Header", "Submission Header is missing.")
    else:
        for field in ("SenderID", "ReceiverID", "TransactionDate", "RecordCount", "DispositionFlag"):
            if not text(header, field):
                finding(findings, "BLOCK", "HEADER_REQUIRED", f"/Header/{field}", f"Required Header field {field} is missing.")
        record_count = text(header, "RecordCount")
        if record_count and (not record_count.isdigit() or int(record_count) != len(claims)):
            finding(findings, "BLOCK", "RECORD_COUNT", "/Header/RecordCount", f"RecordCount={record_count} does not match {len(claims)} Claim element(s).")
        if text(header, "TransactionDate") and parse_dt(text(header, "TransactionDate")) is None:
            finding(findings, "BLOCK", "DATE_FORMAT", "/Header/TransactionDate", "TransactionDate is not in a recognized date/time format.")

    parsed_claims = []
    for ci, claim in enumerate(claims, 1):
        cid = text(claim, "ID") or f"claim-{ci}"
        base = f"/Claim[{ci}]"
        required = ("ID", "MemberID", "PayerID", "ProviderID", "EmiratesIDNumber", "Gross", "PatientShare", "Net", "Encounter")
        for field in required:
            if field == "Encounter":
                if child(claim, field) is None:
                    finding(findings, "BLOCK", "CLAIM_REQUIRED", f"{base}/{field}", f"Required Claim section {field} is missing.", cid)
            elif not text(claim, field):
                finding(findings, "BLOCK", "CLAIM_REQUIRED", f"{base}/{field}", f"Required Claim field {field} is missing.", cid)

        gross, share, net = (money(text(claim, f)) for f in ("Gross", "PatientShare", "Net"))
        if any(v is None for v in (gross, share, net)):
            finding(findings, "BLOCK", "MONEY_FORMAT", f"{base}/Gross|PatientShare|Net", "Gross, PatientShare, and Net must be numeric.", cid)
        elif gross - share != net:
            finding(findings, "BLOCK", "CLAIM_TOTALS", base, f"Gross ({gross}) - PatientShare ({share}) != Net ({net}).", cid)
        elif min(gross, share, net) < 0:
            finding(findings, "BLOCK", "NEGATIVE_AMOUNT", base, "Claim monetary amounts cannot be negative.", cid)

        encounter = child(claim, "Encounter")
        encounter_key = None
        if encounter is not None:
            for field in ("FacilityID", "Type", "PatientID", "Start", "End", "StartType", "EndType"):
                if not text(encounter, field):
                    finding(findings, "BLOCK", "ENCOUNTER_REQUIRED", f"{base}/Encounter/{field}", f"Required Encounter field {field} is missing.", cid)
            estart = parse_dt(text(encounter, "Start")); eend = parse_dt(text(encounter, "End"))
            if estart is None or eend is None:
                finding(findings, "BLOCK", "ENCOUNTER_DATE", f"{base}/Encounter", "Encounter Start and End must use a recognized date/time format.", cid)
            elif estart > eend:
                finding(findings, "BLOCK", "ENCOUNTER_ORDER", f"{base}/Encounter", "Encounter Start is after End.", cid)
            encounter_key = (text(claim, "MemberID"), text(encounter, "FacilityID"), text(encounter, "Start"), text(encounter, "End"))

        diagnoses = children(claim, "Diagnosis")
        diagnosis_codes = [text(d, "Code") for d in diagnoses]
        principal_codes = [text(d, "Code") for d in diagnoses if text(d, "Type").lower() == "principal"]
        if not diagnoses:
            finding(findings, "BLOCK", "DIAGNOSIS_MISSING", f"{base}/Diagnosis", "At least one Diagnosis is required for the UAE profile.", cid)
        if diagnoses and not principal_codes:
            finding(findings, "WARN", "PRINCIPAL_DX_MISSING", f"{base}/Diagnosis", "No Diagnosis is marked Principal; verify CPT–ICD and payer principal-diagnosis rules.", cid, source="UAE profile comparison")
        for di, diagnosis in enumerate(diagnoses, 1):
            code = text(diagnosis, "Code")
            if not code:
                finding(findings, "BLOCK", "DIAGNOSIS_CODE_MISSING", f"{base}/Diagnosis[{di}]/Code", "Diagnosis Code is missing.", cid)
            elif not re.fullmatch(r"[A-Z][0-9][A-Z0-9.]{1,7}", code, re.I):
                finding(findings, "WARN", "ICD10_FORMAT", f"{base}/Diagnosis[{di}]/Code", f"{code} does not look like a standard ICD-10-CM-style code; verify code set/version.", cid)

        activities = children(claim, "Activity")
        if not activities:
            finding(findings, "BLOCK", "ACTIVITY_MISSING", f"{base}/Activity", "At least one Activity is required for the UAE profile.", cid)
        activity_ids = set(); line_total = Decimal("0"); parsed_activities = []
        for ai, activity in enumerate(activities, 1):
            aid = text(activity, "ID") or f"activity-{ai}"
            apath = f"{base}/Activity[{ai}]"
            for field in ("ID", "Start", "Type", "Code", "Quantity", "Net", "Clinician"):
                if not text(activity, field):
                    finding(findings, "BLOCK", "ACTIVITY_REQUIRED", f"{apath}/{field}", f"Required Activity field {field} is missing.", cid, aid)
            if aid in activity_ids:
                finding(findings, "BLOCK", "ACTIVITY_ID_DUPLICATE", f"{apath}/ID", f"Activity ID {aid} is duplicated within the claim.", cid, aid)
            activity_ids.add(aid)
            ast = parse_dt(text(activity, "Start"))
            if ast is None and text(activity, "Start"):
                finding(findings, "BLOCK", "ACTIVITY_DATE", f"{apath}/Start", "Activity Start is not in a recognized date/time format.", cid, aid)
            amount = money(text(activity, "Net"))
            if amount is not None: line_total += amount
            code = text(activity, "Code")
            modifiers = text(activity, "Modifier") or text(activity, "Modifiers") or text(activity, "ProcedureModifier")
            numeric_code = bool(re.fullmatch(r"\d{5}", code))
            if text(activity, "Type") in {"3", "8"} and code and not (numeric_code or code in rules["consultation_codes"]):
                finding(findings, "WARN", "CPT_CODE_FORMAT", f"{apath}/Code", f"Activity code {code} is not a recognizable five-digit CPT-style code or configured UAE consultation code; verify the current code set.", cid, aid, "UAE profile comparison")
            if code in rules["modifier_review_codes"] and not modifiers:
                finding(findings, "WARN", "MODIFIER_REVIEW", f"{apath}/Code", f"CPT {code} has no Modifier/Modifiers field. Review modifier requirements before submission; this is a comparison warning, not a universal UAE rule.", cid, aid, rules["source_urls"]["pure_rcm"])
            if not diagnosis_codes and (numeric_code or text(activity, "Type") in {"3", "8"}):
                finding(findings, "WARN", "CPT_ICD_LINK_MISSING", f"{apath}", f"Procedure activity {code} has no claim diagnosis to support CPT–ICD review.", cid, aid, "UAE profile comparison")
            parsed_activities.append({"id": aid, "code": code, "start": ast, "start_raw": text(activity, "Start"), "clinician": text(activity, "Clinician"), "type": text(activity, "Type"), "path": apath, "modifiers": modifiers, "quantity": money(text(activity, "Quantity")) or Decimal("0")})
            if encounter is not None and ast is not None:
                estart = parse_dt(text(encounter, "Start")); eend = parse_dt(text(encounter, "End"))
                if estart and eend and not (estart <= ast <= eend):
                    finding(findings, "WARN", "ACTIVITY_OUTSIDE_ENCOUNTER", f"{apath}/Start", "Activity Start falls outside Encounter Start/End; verify dates.", cid, aid)
            for obs in children(activity, "Observation"):
                if not text(obs, "Type") or not text(obs, "Code") or not text(obs, "ValueType"):
                    finding(findings, "WARN", "OBSERVATION_INCOMPLETE", f"{apath}/Observation", "Observation should include Type, Code, and ValueType.", cid, aid)

        if net is not None and line_total != net:
            finding(findings, "BLOCK", "LINE_TOTALS", f"{base}/Activity", f"Sum of Activity Net ({line_total}) != Claim Net ({net}).", cid)

        parsed_claims.append({
            "id": cid, "member": text(claim, "MemberID"), "payer": text(claim, "PayerID"), "provider": text(claim, "ProviderID"),
            "diagnoses": tuple(diagnosis_codes), "encounter_key": encounter_key, "activities": parsed_activities,
        })

    # Source-backed rule-pack layer. DoH public rules apply only to the
    # Abu Dhabi/common profiles; they are not silently treated as DHA rules.
    if profile in {"UAE_COMMON", "ABU_DHABI"}:
        bundling_pack = rule_pack.get("bundling", {})
        for c in parsed_claims:
            for mue in bundling_pack.get("mue", []):
                matching = [a for a in c["activities"] if a["code"] == mue.get("code") and a["start"]]
                by_date = defaultdict(list)
                for a in matching:
                    by_date[a["start"].date()].append(a)
                for service_date, activities_on_date in by_date.items():
                    units = sum((a.get("quantity") or Decimal("0") for a in activities_on_date), Decimal("0"))
                    if units > Decimal(str(mue.get("max_units", 0))):
                        finding(findings, mue.get("severity", "WARN"), "MUE_EXCEEDED", f"/Claim[{c['id']}]/Activity", f"Code {mue['code']} has {units} unit(s) on {service_date.isoformat()}, above the configured MUE of {mue['max_units']}; verify the current payer table/contract.", c["id"], source=f"{mue.get('rule_id', 'MUE')} · {', '.join(mue.get('source_ids', []))}")
            # Configured mutually exclusive pairs are only enforced when a
            # versioned rule pack supplies them.
            for pair in bundling_pack.get("mutually_exclusive_pairs", []):
                codes = set(pair.get("codes", []) if isinstance(pair, dict) else pair)
                present = [a["code"] for a in c["activities"] if a["code"] in codes]
                if len(set(present)) > 1:
                    finding(findings, pair.get("severity", "WARN"), "MUTUALLY_EXCLUSIVE_CODES", f"/Claim[{c['id']}]/Activity", f"Mutually exclusive services {sorted(set(present))} appear in the same claim; verify the current rule release.", c["id"], source="versioned rule pack")

        # Excludes-1: official ICD-10-CM 2026 notes (CMS tabular, all 5k+ rules) plus payer practice,
        # then any extra pairs configured in the rule pack.
        from app.services import icd_excludes
        for c in parsed_claims:
            for x in icd_excludes.excludes1_conflicts(list(c["diagnoses"])):
                act = f"remove {x['drop']}, keep {x['keep']}" if x["drop"] else "the doctor confirms which one is documented"
                finding(findings, "WARN", "ICD_EXCLUDES1", f"/Claim[{c['id']}]/Diagnosis",
                        f"Diagnosis codes {x['a']} and {x['b']} cannot be coded together ({x['note']}): {act}, "
                        f"unless the conditions are clearly unrelated.", c["id"], source="ICD-10-CM 2026 Excludes-1")
            for x in icd_excludes.practice_conflicts(list(c["diagnoses"])):
                finding(findings, "WARN", "SYMPTOM_INTEGRAL", f"/Claim[{c['id']}]/Diagnosis",
                        f"Symptom code {x['drop']} coded with {x['keep']} — {x['note']}. Payers flag this as an "
                        f"Excludes-1 edit; remove {x['drop']} unless unrelated.", c["id"], source="payer practice")
        for c in parsed_claims:
            dx = set(c["diagnoses"])
            for pair in rule_pack.get("cpt_icd", {}).get("excludes1_pairs", []):
                pair_codes = set(pair.get("codes", []) if isinstance(pair, dict) else pair)
                if pair_codes and pair_codes.issubset(dx):
                    finding(findings, "WARN", "ICD_EXCLUDES1", f"/Claim[{c['id']}]/Diagnosis", f"Diagnosis codes {sorted(pair_codes)} are configured as an Excludes-1 pair; do not report together unless the conditions are clearly unrelated.", c["id"], source="versioned rule pack")

        # Frequency limits use same-member and same-facility context when
        # available. Exceptions remain a human/payer review item.
        for freq in rule_pack.get("service_frequency", []):
            entries = []
            for c in parsed_claims:
                facility = c.get("encounter_key")[1] if c.get("encounter_key") else ""
                for a in c["activities"]:
                    if a["code"] in set(freq.get("codes", [])) and a["start"]:
                        entries.append((c, a, facility))
            entries.sort(key=lambda item: item[1]["start"])
            for idx, (c, a, facility) in enumerate(entries):
                related = []
                for prior_c, prior_a, prior_facility in entries[:idx]:
                    if prior_c["member"] != c["member"]:
                        continue
                    if facility and prior_facility and facility != prior_facility:
                        continue
                    gap = (a["start"].date() - prior_a["start"].date()).days
                    if freq.get("min_days", 0) <= gap <= freq.get("window_days", 0):
                        related.append((prior_c, prior_a, gap))
                if related and len(related) + 1 > int(freq.get("max_count", 1)):
                    finding(findings, "WARN", "SERVICE_FREQUENCY_LIMIT", a["path"], f"Service {a['code']} occurs within the configured {freq.get('window_days')} day frequency window for the same member; review the {freq.get('action')} rule and exceptions.", c["id"], a["id"], source=f"{freq.get('rule_id')} · {', '.join(freq.get('source_ids', []))}")
                    if freq.get("required_modifier") and any(gap >= freq.get("min_days", 0) for _, _, gap in related) and freq.get("required_modifier") not in (a.get("modifiers") or ""):
                        finding(findings, "WARN", "FREQUENCY_MODIFIER_REQUIRED", f"{a['path']}/Modifier", f"Service {a['code']} falls in the configured day {freq.get('min_days')}–{freq.get('window_days')} follow-up window; review required modifier {freq['required_modifier']} and payment treatment.", c["id"], a["id"], source=f"{freq.get('rule_id')} · {', '.join(freq.get('source_ids', []))}")

    fingerprints = defaultdict(list)
    for c in parsed_claims:
        signature = (c["member"], c["payer"], c["provider"], c["encounter_key"], tuple((a["code"], a["start_raw"], a["type"]) for a in c["activities"]))
        fingerprints[signature].append(c["id"])
    for ids in fingerprints.values():
        if len(ids) > 1:
            finding(findings, "BLOCK", "DUPLICATE_CLAIM", "/Claim", f"Claims {', '.join(ids)} have the same member, encounter, and activity signature.", ids[0], source="local duplicate signature")

    all_activities = [(c, a) for c in parsed_claims for a in c["activities"]]
    for i, (c1, a1) in enumerate(all_activities):
        for c2, a2 in all_activities[i+1:]:
            if c1["member"] and c1["member"] == c2["member"] and a1["code"] and a1["code"] == a2["code"] and a1["start"] and a2["start"] and a1["start"] == a2["start"]:
                finding(findings, "WARN", "OVERLAPPING_ACTIVITY", f"/Claim[{c2['id']}]/Activity[{a2['id']}]/Start", f"Same member has service code {a2['code']} at the same date/time in claims {c1['id']} and {c2['id']}; review for overlap or legitimate repeat service.", c2["id"], a2["id"])

    consultations = [(c, a) for c in parsed_claims for a in c["activities"] if a["code"] in rules["consultation_codes"] and a["start"]]
    consultations_by_member = defaultdict(list)
    for consultation in consultations:
        consultations_by_member[consultation[0]["member"]].append(consultation)
    for member_consultations in consultations_by_member.values():
        member_consultations.sort(key=lambda pair: pair[1]["start"])
        for (c1, a1), (c2, a2) in zip(member_consultations, member_consultations[1:]):
            days = (a2["start"].date() - a1["start"].date()).days
            if days == 0:
                finding(findings, "WARN", "CONSULT_SAME_DAY_DUPLICATE", f"/Claim[{c2['id']}]/Activity[{a2['id']}]/Code", "Same member has more than one consultation on the same calendar date; review as a same-day duplicate/follow-up.", c2["id"], a2["id"], rules["source_urls"]["consultation"])
            elif 1 <= days <= 7 and a2["code"] in {"9", "10", "11"}:
                finding(findings, "WARN", "CONSULT_FOLLOWUP_7D", f"/Claim[{c2['id']}]/Activity[{a2['id']}]/Code", f"Initial consultation code {a2['code']} occurs {days} calendar day(s) after consultation {a1['code']} for the same member; apply the seven-day follow-up rule if applicable.", c2["id"], a2["id"], rules["source_urls"]["consultation"])
            elif 8 <= days <= 28:
                finding(findings, "WARN", "CONSULT_REVIEW_8_28D", f"/Claim[{c2['id']}]/Activity[{a2['id']}]/Code", f"Consultation code {a2['code']} occurs {days} calendar day(s) after the previous consultation for the same member; review the 8–28 day consultation rule.", c2["id"], a2["id"], rules["source_urls"]["consultation"])

    for c in parsed_claims:
        admin = [a for a in c["activities"] if a["code"] in rules["injection_codes"] | rules["infusion_codes"]]
        if len(admin) > 1:
            codes = [a["code"] for a in admin]
            finding(findings, "WARN", "BUNDLING_SEQUENCE_REVIEW", f"/Claim[{c['id']}]/Activity", f"Multiple administration services {codes} require bundling/sequencing review. The current XML does not expose access site, stop time, or modifier detail needed for a definitive pass/fail.", c["id"], source=rules["source_urls"]["injection_candidate"])

    # NAS is an opt-in payer/TPA guideline layer. It remains separate from
    # the UAE regulator baseline because it is contract- and payer-specific.
    nas_applied = None
    if nas_guidelines and nas_guidelines.get("rules"):
        nas_applied = {"id": nas_guidelines.get("guideline_pack_id"), "version": nas_guidelines.get("version"), "scope": nas_guidelines.get("scope"), "source": nas_guidelines.get("source", {})}
        nas_source = nas_guidelines.get("source", {}).get("url", "NAS guideline pack")
        code_map_all = defaultdict(list)
        for claim in parsed_claims:
            for activity in claim["activities"]:
                code_map_all[activity["code"]].append((claim, activity))
        for rule in nas_guidelines.get("rules", []):
            rule_id = rule.get("rule_id", "NAS_REVIEW")
            source = f"{nas_source} · {rule.get('guideline', 'NAS') }"
            if rule_id == "NAS_VITD_COMBINATION_REVIEW":
                for claim in parsed_claims:
                    codes = {a["code"] for a in claim["activities"]}
                    if set(rule.get("codes", [])).issubset(codes):
                        activity = next(a for a in claim["activities"] if a["code"] == "82652")
                        finding(findings, rule.get("severity", "WARN"), rule_id, activity["path"], rule.get("message", "Review combined Vitamin D testing."), claim["id"], activity["id"], source)
            elif rule_id == "NAS_ECHO_DOPPLER_BUNDLE_REVIEW":
                for claim in parsed_claims:
                    codes = {a["code"] for a in claim["activities"]}
                    if rule.get("parent_code") in codes and codes.intersection(set(rule.get("component_codes", []))):
                        activity = next(a for a in claim["activities"] if a["code"] == rule["parent_code"])
                        finding(findings, rule.get("severity", "WARN"), rule_id, activity["path"], rule.get("message", "Review echocardiography bundling."), claim["id"], activity["id"], source)
            elif rule_id == "NAS_INFUSION_96376_30MIN_REVIEW":
                for claim, activity in code_map_all.get("96376", []):
                    finding(findings, rule.get("severity", "WARN"), rule_id, activity["path"], rule.get("message", "Review 96376 timing and same-drug relationship."), claim["id"], activity["id"], source)
            elif rule_id == "NAS_VITD_82652_COMPLEX_INDICATION":
                for claim, activity in code_map_all.get("82652", []):
                    finding(findings, rule.get("severity", "WARN"), rule_id, activity["path"], rule.get("message", "Review the complex indication for 82652."), claim["id"], activity["id"], source)
            elif rule_id == "NAS_VITD_REPEAT_90D":
                entries = [(claim, activity) for code in set(rule.get("codes", [])) for claim, activity in code_map_all.get(code, []) if activity["start"]]
                entries.sort(key=lambda pair: pair[1]["start"])
                for idx, (claim, activity) in enumerate(entries):
                    for prior_claim, prior_activity in entries[:idx]:
                        if claim["member"] and claim["member"] == prior_claim["member"]:
                            gap = (activity["start"].date() - prior_activity["start"].date()).days
                            if 0 <= gap < int(rule.get("window_days", 90)):
                                finding(findings, rule.get("severity", "WARN"), rule_id, activity["path"], rule.get("message", "Review repeat Vitamin D testing."), claim["id"], activity["id"], source)
                                break

    blocks = sum(1 for f in findings if f["severity"] == "BLOCK")
    warns = sum(1 for f in findings if f["severity"] == "WARN")
    denial_rules = {r.get("code"): r for r in rule_pack.get("denial_impact", {}).get("rules", [])} if profile in {"UAE_COMMON", "ABU_DHABI"} else {}
    finding_to_denial = {
        "DIAGNOSIS_MISSING": "CODE-001", "PRINCIPAL_DX_MISSING": "CODE-001",
        "CPT_ICD_LINK_MISSING": "CODE-006", "CPT_CODE_FORMAT": "CODE-006",
        "MODIFIER_REVIEW": "CODE-016", "FREQUENCY_MODIFIER_REQUIRED": "CODE-016",
        "SERVICE_FREQUENCY_LIMIT": "CODE-016", "MUE_EXCEEDED": "CODE-016",
        "BUNDLING_SEQUENCE_REVIEW": "MNEC-007", "MUTUALLY_EXCLUSIVE_CODES": "MNEC-007",
        "NAS_VITD_REPEAT_90D": "CODE-016", "NAS_VITD_82652_COMPLEX_INDICATION": "MNEC-007",
        "NAS_VITD_COMBINATION_REVIEW": "MNEC-007", "NAS_ECHO_DOPPLER_BUNDLE_REVIEW": "MNEC-007",
        "NAS_INFUSION_96376_30MIN_REVIEW": "CODE-016",
        "ICD_EXCLUDES1": "CODE-001", "SYMPTOM_INTEGRAL": "CODE-001",   # payers deny Excludes-1 edits as CODE-001 / CODE-012
    }
    denial_score = 0
    drivers = defaultdict(int)
    calibrated_rules = (calibration or {}).get("rule_weights", {})
    for item in findings:
        code = finding_to_denial.get(item["rule_id"])
        if code and code in denial_rules:
            default_weight = int(denial_rules[code].get("weight", 0))
            calibrated_weight = (calibrated_rules.get(item["rule_id"], {}) or {}).get("recommended_weight")
            weight = int(calibrated_weight) if calibrated_weight is not None else default_weight
            denial_score += weight
            drivers[code] += 1
    risk_level = "HIGH" if blocks or denial_score >= 40 else ("MEDIUM" if warns or denial_score else "LOW")
    return {
        "status": "BLOCK" if blocks else ("WARN" if warns else "PASS"),
        "input": str(path),
        "input_mode": input_mode,
        "profile": {"id": profile, "note": PROFILE_NOTES[profile], "sources": PROFILE_SOURCES[profile]},
        "rule_pack": {"id": rule_pack.get("rule_pack_id"), "version": rule_pack.get("version"), "effective_from": rule_pack.get("effective_from"), "authority": rule_pack.get("authority"), "scope": "active for UAE_COMMON and ABU_DHABI; other profiles retain local checks only"},
        "guidelines": {"nas": nas_applied or {"id": None, "version": None, "scope": "not applied", "note": "NAS is an opt-in payer/TPA-specific layer."}},
        "calibration": {"id": (calibration or {}).get("calibration_id"), "status": "applied" if calibration else "not applied", "note": "Empirical weights affect local ranking only; they are not payer probabilities."},
        "summary": {"blocks": blocks, "warnings": warns, "claims": len(claims), "denial_risk": {"level": risk_level, "score": denial_score, "high": blocks, "medium": warns, "drivers": dict(drivers), "note": "Local evidence-linked risk ranking; not an official payer probability or severity taxonomy."}},
        "comparison": COMPETITOR_COMPARISON,
        "findings": findings,
    }


def main():
    ap = argparse.ArgumentParser(description="Local UAE eClaimLink-style XML/JSON claim scrubber")
    ap.add_argument("input", type=Path)
    ap.add_argument("-o", "--output", type=Path, default=None)
    ap.add_argument("--profile", choices=sorted(PROFILE_SOURCES), default="UAE_COMMON")
    ap.add_argument("--rules", type=Path, default=RULE_PACK_PATH, help="Versioned JSON rule pack")
    ap.add_argument("--nas-guidelines", type=Path, default=None, help="Optional NAS payer/TPA guideline pack")
    ap.add_argument("--calibration", type=Path, default=None, help="Optional local remittance calibration report")
    args = ap.parse_args()
    report = scrub(
        args.input,
        profile=args.profile,
        rule_pack=load_rule_pack(args.rules),
        nas_guidelines=load_nas_guidelines(args.nas_guidelines) if args.nas_guidelines else None,
        calibration=load_calibration(args.calibration),
    )
    rendered = json.dumps(report, indent=2)
    if args.output:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    else:
        print(rendered)
    return 1 if report["status"] == "BLOCK" else 0


if __name__ == "__main__":
    sys.exit(main())
