#!/usr/bin/env python3
"""Calibrate local claim-risk weights from sanitized remittance outcomes.

This utility never changes claims or submits data. It joins a remittance CSV to
local scrubber JSON reports by claim_id and, when available, line_id/activity_id.
It emits transparent observed rates and recommended local weights; it does not
claim to estimate a payer's true probability of denial.
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import math
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

try:
    from openpyxl import load_workbook
except ImportError:  # CSV calibration remains usable if Excel support is unavailable.
    load_workbook = None

DENIED_OUTCOMES = {"denied", "rejected", "not paid", "no pay", "declined", "refused"}
ADVERSE_OUTCOMES = DENIED_OUTCOMES | {"partial", "underpaid", "pended"}


def norm(value):
    return str(value or "").strip().lower().replace("_", " ").replace("-", " ")


def case_key(row):
    claim = str(row.get("claim_id") or row.get("claim") or row.get("ClaimID") or "").strip()
    line = str(row.get("line_id") or row.get("activity_id") or row.get("line") or row.get("LineID") or "").strip()
    return claim, line


def amount(value):
    try:
        return float(str(value or "").replace(",", "").strip())
    except (TypeError, ValueError):
        return 0.0


def outcome_flags(row):
    explicit = norm(row.get("outcome") or row.get("status") or row.get("disposition"))
    payment = norm(row.get("payment_status"))
    cycle = norm(row.get("claim_cycle_status"))
    rejection = norm(row.get("final_rejection"))
    denial_code = str(row.get("denial_code") or row.get("denial_reason_code") or "").strip()
    denied_amount = amount(row.get("denied_amount"))
    paid_amount = amount(row.get("paid_amount"))

    yes_values = {"yes", "y", "true", "1", "rejected", "final rejection", "final rejected"}
    denial_words = {"denied", "rejected", "declined", "refused", "not paid", "no pay"}
    paid_words = {"paid", "fully paid", "approved", "adjudicated paid", "settled"}
    partial_words = {"partial", "partially paid", "underpaid", "pended", "unsettled", "ar", "outstanding"}

    partial = explicit in partial_words or payment in partial_words or (denied_amount > 0 and paid_amount > 0)
    pending = payment in {"pending", "pended", "unsettled", "ar", "outstanding"} or cycle in {"pending", "pended", "unsettled", "ar", "outstanding"}
    denied = (
        explicit in DENIED_OUTCOMES
        or payment in denial_words
        or cycle in denial_words
        or rejection in yes_values
        or (denied_amount > 0 and paid_amount <= 0 and not partial and not pending)
    )
    adverse = denied or partial or pending
    outcome = explicit or payment or cycle or ("denied" if denied else "partial" if partial else "pending" if pending else "paid" if payment in paid_words else "unknown")
    return denied, adverse, outcome


def normalized_header(value):
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")


USER_HEADER_ALIASES = {
    "claim_number": "claim_id",
    "claim_no": "claim_id",
    "payer_id": "payer_id",
    "facility_name": "provider_id",
    "encounter_start": "service_date",
    "net_submitted": "submitted_amount",
    "paid_amount": "paid_amount",
    "denial_amount": "denied_amount",
    "denial_code": "denial_code",
    "denial_description": "denial_reason",
    "payment_status": "payment_status",
    "claim_cycle_status": "claim_cycle_status",
    "final_rejection": "final_rejection",
    "current_submission_stage": "submission_stage",
    "activity_count": "activity_count",
    "denied_activity_count": "denied_activity_count",
}


def normalize_row(raw):
    row = {}
    for key, value in raw.items():
        canonical = normalized_header(key)
        row[USER_HEADER_ALIASES.get(canonical, canonical)] = "" if value is None else str(value).strip()
    if not row.get("claim_id"):
        row["claim_id"] = row.get("claim_number", "")
    # The supplied export has no line/activity ID. This deliberately remains
    # blank so matching is claim-level rather than falsely line-specific.
    row.setdefault("line_id", "")
    row.setdefault("service_date", row.get("encounter_start", ""))
    return row


def read_remittance(path: Path):
    if path.suffix.lower() in {".xlsx", ".xlsm"}:
        if load_workbook is None:
            raise ValueError("Excel support requires openpyxl; export the sheet as UTF-8 CSV instead.")
        workbook = load_workbook(path, read_only=True, data_only=True)
        sheet = workbook[workbook.sheetnames[0]]
        rows = sheet.iter_rows(values_only=True)
        try:
            headers = [str(value or "").strip() for value in next(rows)]
        except StopIteration as exc:
            raise ValueError(f"No rows found in remittance workbook: {path}") from exc
        records = [dict(zip(headers, values)) for values in rows if any(value not in (None, "") for value in values)]
    else:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            records = list(csv.DictReader(handle))
    if not records:
        raise ValueError(f"No rows found in remittance file: {path}")
    normalized = [normalize_row(record) for record in records]
    # Some operational exports repeat the header once inside the worksheet.
    return [row for row in normalized if norm(row.get("claim_id")) != "claim number"]


def read_reports(patterns):
    paths = []
    for pattern in patterns:
        matches = glob.glob(pattern)
        if matches:
            paths.extend(matches)
        elif Path(pattern).is_file():
            paths.append(pattern)
    unique = []
    seen = set()
    for raw in paths:
        path = Path(raw)
        if path in seen:
            continue
        seen.add(path)
        unique.append(path)
    findings = []
    for path in unique:
        data = json.loads(path.read_text(encoding="utf-8"))
        for item in data.get("findings", []):
            claim = str(item.get("claim_id") or "").strip()
            if not claim:
                continue
            findings.append({
                "claim_id": claim,
                "line_id": str(item.get("activity_id") or "").strip(),
                "rule_id": str(item.get("rule_id") or "").strip(),
                "severity": str(item.get("severity") or "").strip(),
                "source_report": str(path),
            })
    return findings, [str(p) for p in unique]


def smoothed_rate(positive, total, alpha=1.0):
    return (positive + alpha) / (total + 2 * alpha) if total >= 0 else 0.5


def recommended_weight(rate, baseline, observations):
    """Convert empirical lift into a bounded local ranking weight.

    The transform is deliberately conservative and needs at least five matched
    cases before it is treated as a recommendation rather than a low-confidence
    observation.
    """
    if observations < 5:
        return None
    odds = max(rate, 1e-6) / max(1 - rate, 1e-6)
    base_odds = max(baseline, 1e-6) / max(1 - baseline, 1e-6)
    lift = math.log(odds / base_odds)
    return int(max(5, min(60, round(10 * lift + 20))))


def calibrate(remittance_path: Path, report_patterns):
    remittance = read_remittance(remittance_path)
    findings, report_paths = read_reports(report_patterns)

    outcome_by_key = {}
    observed_denial_codes = defaultdict(lambda: {"rows": 0, "denied_or_adverse_rows": 0})
    for row in remittance:
        key = case_key(row)
        if not key[0]:
            continue
        denied, adverse, outcome = outcome_flags(row)
        denial_code = str(row.get("denial_code") or row.get("denial_reason_code") or "").strip()
        outcome_by_key[key] = {
            "denied": denied,
            "adverse": adverse,
            "outcome": outcome,
            "denial_code": denial_code,
        }
        for code in re.split(r"\\s*\\|\\s*", denial_code):
            if code:
                observed_denial_codes[code]["rows"] += 1
                if adverse:
                    observed_denial_codes[code]["denied_or_adverse_rows"] += 1

    total = len(outcome_by_key)
    denied_total = sum(v["denied"] for v in outcome_by_key.values())
    adverse_total = sum(v["adverse"] for v in outcome_by_key.values())
    baseline = smoothed_rate(denied_total, total)

    by_rule = defaultdict(lambda: {"matched": set(), "denied": set(), "adverse": set(), "denial_codes": defaultdict(int)})
    unmatched_findings = 0
    matched_findings = 0
    for finding in findings:
        exact = (finding["claim_id"], finding["line_id"])
        claim_only = (finding["claim_id"], "")
        outcome = outcome_by_key.get(exact) or outcome_by_key.get(claim_only)
        if not outcome:
            # A claim-level remittance row can calibrate all finding lines for it.
            outcome = outcome_by_key.get((finding["claim_id"], ""))
        if not outcome:
            unmatched_findings += 1
            continue
        matched_findings += 1
        rule = finding["rule_id"] or "UNCLASSIFIED_FINDING"
        bucket = by_rule[rule]
        case = exact if finding["line_id"] else claim_only
        bucket["matched"].add(case)
        if outcome["denied"]:
            bucket["denied"].add(case)
        if outcome["adverse"]:
            bucket["adverse"].add(case)
        if outcome["denial_code"]:
            bucket["denial_codes"][outcome["denial_code"]] += 1

    rule_weights = {}
    for rule, bucket in sorted(by_rule.items()):
        matched = len(bucket["matched"])
        denied = len(bucket["denied"])
        adverse = len(bucket["adverse"])
        rate = smoothed_rate(denied, matched)
        weight = recommended_weight(rate, baseline, matched)
        rule_weights[rule] = {
            "observations": matched,
            "denied_cases": denied,
            "adverse_cases": adverse,
            "denial_rate": round(denied / matched, 6) if matched else None,
            "adverse_rate": round(adverse / matched, 6) if matched else None,
            "smoothed_denial_rate": round(rate, 6),
            "recommended_weight": weight,
            "confidence": "usable" if matched >= 30 else ("directional" if matched >= 5 else "insufficient"),
            "denial_codes": dict(bucket["denial_codes"]),
        }

    return {
        "calibration_id": "LOCAL_REMITTANCE_OUTCOME_CALIBRATION",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "inputs": {"remittance": str(remittance_path), "scrubber_reports": report_paths},
        "matching": {
            "remittance_source_rows": len(remittance),
            "remittance_cases": total,
            "remittance_denied_cases": denied_total,
            "remittance_adverse_cases": adverse_total,
            "findings_seen": len(findings),
            "findings_matched": matched_findings,
            "findings_unmatched": unmatched_findings,
            "join_policy": "Exact claim_id plus line_id/activity_id when available; otherwise claim-level join.",
        },
        "baseline": {
            "denial_rate": round(denied_total / total, 6) if total else None,
            "adverse_rate": round(adverse_total / total, 6) if total else None,
            "smoothed_denial_rate": round(baseline, 6) if total else None,
        },
        "observed_denial_codes": {
            code: {
                "rows": values["rows"],
                "denied_or_adverse_rows": values["denied_or_adverse_rows"],
                "row_rate": round(values["denied_or_adverse_rows"] / values["rows"], 6) if values["rows"] else None,
            }
            for code, values in sorted(observed_denial_codes.items(), key=lambda item: (-item[1]["rows"], item[0]))
        },
        "rule_weights": rule_weights,
        "limitations": [
            "Recommended weights are local ranking weights, not payer probabilities.",
            "At least five matched cases are required for a directional recommendation; 30 or more is labeled usable.",
            "Do not mix payers, contracts, or materially different coding periods without stratifying the calibration.",
            "Validate any new weights on a held-out period before making them operational.",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description="Calibrate local claim-risk weights from sanitized remittance outcomes")
    parser.add_argument("--remittance", required=True, type=Path, help="Sanitized CSV remittance/denial outcomes")
    parser.add_argument("--reports", required=True, nargs="+", help="Scrubber report paths or glob patterns")
    parser.add_argument("-o", "--output", required=True, type=Path)
    args = parser.parse_args()
    result = calibrate(args.remittance, args.reports)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "remittance_cases": result["matching"]["remittance_cases"], "rules_calibrated": len(result["rule_weights"])}, indent=2))


if __name__ == "__main__":
    main()
