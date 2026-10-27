"""
recon_resub.py — prepare "reconciliation" resubmission files from an import batch.

Matching: workbook rows are grouped per (facility, claim ID). The workbook's total rejected
amount for the claim must equal, to the fils, the denied amount (net - paid) on the latest RA
for the same CPT lines. Only those Exact claims are written.

Files: one set per branch / sender licence / receiver (/ payer for Riayati), at most CHUNK claims
per file. DHA claims are rebuilt from the latest submitted XML as Claim.Submission; Riayati claims
from the latest submitted JSON in the same Entity / Header / Claim shape. Only the agreed denied
activities are kept, and each claim carries Resubmission Type "reconciliation" with RECON_COMMENT.
Nothing is transmitted — the files are saved under ~/ghaf_reports/reconciliation for upload by hand.
"""
from __future__ import annotations

import copy
import io
import json
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from openpyxl import Workbook
from sqlalchemy import text
from sqlalchemy.orm import Session

RECON_COMMENT = "Resubmission upon agreed reconcilation"
RECON_TYPE = "reconciliation"
CHUNK = 500
OUT_ROOT = Path.home() / "ghaf_reports" / "reconciliation"

CHECK_SQL = """
WITH wb AS (
  SELECT facility_id, claim_id, array_agg(DISTINCT upper(cpt_code)) cpts,
         round(sum(rejected_amount)::numeric, 2) wb_total, min(receiver_id) wb_receiver,
         min(facility_external_id) branch, count(*) wb_lines
  FROM recon_import_rows WHERE batch_id = :b AND facility_id IS NOT NULL AND claim_id <> ''
  GROUP BY 1, 2),
ra AS (
  SELECT DISTINCT ON (wb.facility_id, wb.claim_id) wb.facility_id, wb.claim_id, r.id ra_id,
         r.remittance_date, r.payer_id ra_payer, r.claim_id_payer
  FROM wb JOIN remittances r ON r.facility_id = wb.facility_id AND r.claim_id = wb.claim_id
  ORDER BY wb.facility_id, wb.claim_id, r.remittance_date DESC, r.id DESC),
den AS (
  SELECT ra.facility_id, ra.claim_id,
         round(sum(greatest(coalesce(d.net,0) - coalesce(d.paid_amount,0), 0))::numeric, 2) denied_total,
         array_agg(d.activity_id) FILTER (WHERE coalesce(d.net,0) - coalesce(d.paid_amount,0) > 0.005) activity_ids
  FROM ra JOIN wb USING (facility_id, claim_id)
  JOIN remittance_activity_details d ON d.remittance_id_fk = ra.ra_id AND upper(d.activity_code) = ANY (wb.cpts)
  GROUP BY 1, 2),
rec AS (
  SELECT c.facility_id, c.claim_id, max(c.submission_date) last_recon
  FROM claims c JOIN wb USING (facility_id, claim_id)
  WHERE c.resubmission_key = 'reconciliation' GROUP BY 1, 2)
SELECT wb.branch, f.facility_code, wb.facility_id, wb.claim_id, wb.cpts, wb.wb_lines, wb.wb_total, wb.wb_receiver,
       ra.ra_id, ra.remittance_date, ra.ra_payer, ra.claim_id_payer, den.denied_total, den.activity_ids, rec.last_recon
FROM wb JOIN facilities f ON f.id = wb.facility_id
LEFT JOIN ra USING (facility_id, claim_id) LEFT JOIN den USING (facility_id, claim_id)
LEFT JOIN rec USING (facility_id, claim_id)
ORDER BY wb.branch, wb.claim_id"""

LATEST_SQL = """
SELECT c.source_channel, x.xml_content, c.sender_id, c.receiver_id
FROM claims c JOIN raw_xml_store x ON x.id = c.raw_xml_id
WHERE c.facility_id = :f AND c.claim_id = :c AND x.xml_content IS NOT NULL
ORDER BY c.submission_date DESC NULLS LAST, c.id DESC LIMIT 1"""


def amount_check(db: Session, batch_id: int) -> list[dict]:
    out = []
    for r in db.execute(text(CHECK_SQL), {"b": batch_id}).mappings():
        d = dict(r)
        wb, den = float(d["wb_total"] or 0), d["denied_total"]
        if d["ra_id"] is None or den is None:
            d["status"], d["reason"] = "No RA", "No remittance line for these CPT codes"
        elif abs(wb - float(den)) >= 0.01:
            d["status"], d["reason"] = "Mismatch", f"Workbook {wb:.2f} vs RA denied {float(den):.2f}"
        elif not d["activity_ids"]:
            d["status"], d["reason"] = "Now paid", "Latest RA shows these lines paid"
        elif d["last_recon"] and d["remittance_date"] and d["last_recon"] > d["remittance_date"]:
            d["status"], d["reason"] = "Already filed", "Reconciliation already submitted after the latest RA"
        else:
            d["status"], d["reason"] = "Exact", ""
        out.append(d)
    return out


def _fmt(v: float) -> str:
    return f"{v:.2f}"


def _dha_claim(xml_text: str, claim_id: str, activity_ids: set[str], id_payer: str | None):
    root = ET.fromstring(xml_text.encode("utf-8") if isinstance(xml_text, str) else xml_text)
    for el in root.iter():
        if "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    hdr = root.find("Header")
    sender = hdr.findtext("SenderID") if hdr is not None else None
    receiver = hdr.findtext("ReceiverID") if hdr is not None else None
    for cl in root.findall("Claim"):
        if (cl.findtext("ID") or "").strip() != claim_id:
            continue
        cl = copy.deepcopy(cl)
        for res in cl.findall("Resubmission"):
            cl.remove(res)
        net = 0.0
        for act in cl.findall("Activity"):
            if (act.findtext("ID") or "").strip() in activity_ids:
                net += float(act.findtext("Net") or 0)
            else:
                cl.remove(act)
        if not cl.findall("Activity"):
            return None
        for tag, val in (("Gross", net), ("PatientShare", 0.0), ("Net", net)):
            el = cl.find(tag)
            if el is not None:
                el.text = _fmt(val)
        if id_payer and cl.find("IDPayer") is not None:
            cl.find("IDPayer").text = id_payer
        res = ET.SubElement(cl, "Resubmission")
        ET.SubElement(res, "Type").text = RECON_TYPE
        ET.SubElement(res, "Comment").text = RECON_COMMENT
        ET.SubElement(res, "Attachment")
        return cl, sender, receiver, None
    return None


def _riayati_claim(json_text: str, claim_id: str, activity_ids: set[str], id_payer: str | None):
    ent = json.loads(json_text).get("Entity") or {}
    hdr = ent.get("Header") or {}
    for cl in ent.get("Claim") or []:
        if str(cl.get("ID", "")).strip() != claim_id:
            continue
        cl = copy.deepcopy(cl)
        acts = [a for a in cl.get("Activity") or [] if str(a.get("ID", "")).strip() in activity_ids]
        if not acts:
            return None
        for a in acts:
            a["PatientShare"] = 0.0
        net = round(sum(float(a.get("Net") or 0) for a in acts), 2)
        cl.update({"Type": "Resubmission", "Activity": acts, "Gross": net, "PatientShare": 0.0, "Net": net,
                   "Resubmission": {"Attachment": "", "Comment": RECON_COMMENT, "Type": RECON_TYPE}})
        if id_payer:
            cl["IDPayer"] = id_payer
        return cl, hdr.get("SenderID"), hdr.get("ReceiverID"), hdr.get("PayerID")
    return None


def _write_dha(path: Path, sender: str, receiver: str, claims: list) -> None:
    root = ET.Element("Claim.Submission")
    hdr = ET.SubElement(root, "Header")
    for tag, val in (("SenderID", sender), ("ReceiverID", receiver),
                     ("TransactionDate", datetime.now().strftime("%d/%m/%Y %H:%M")),
                     ("RecordCount", str(len(claims))), ("DispositionFlag", "PRODUCTION")):
        ET.SubElement(hdr, tag).text = val
    root.extend(claims)
    ET.indent(root, space="  ")
    path.write_bytes(ET.tostring(root, encoding="utf-8", xml_declaration=True))


def _write_riayati(path: Path, sender: str, receiver: str, payer: str, claims: list) -> None:
    body = {"Entity": {"Header": {"SenderID": sender, "ReceiverID": receiver,
                                  "TransactionDate": datetime.now().strftime("%d/%m/%Y %H:%M"),
                                  "RecordCount": len(claims), "DispositionFlag": "PRODUCTION", "PayerID": payer},
                       "Claim": claims}}
    path.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")


def _summary_xlsx(rows: list[dict]) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "By branch"
    ws.append(["Branch", "Status", "Claims", "Workbook AED", "RA denied AED"])
    agg = defaultdict(lambda: [0, 0.0, 0.0])
    for r in rows:
        a = agg[(r["branch"], r["status"])]
        a[0] += 1
        a[1] += float(r["wb_total"] or 0)
        a[2] += float(r["denied_total"] or 0)
    for (br, st), a in sorted(agg.items()):
        ws.append([br, st, a[0], round(a[1], 2), round(a[2], 2)])
    ws = wb.create_sheet("Claims")
    ws.append(["Branch", "Claim ID", "CPT codes", "Workbook lines", "Workbook AED", "RA denied AED", "Difference",
               "Latest RA date", "Status", "Reason", "File"])
    for r in rows:
        den = float(r["denied_total"]) if r["denied_total"] is not None else None
        ws.append([r["branch"], r["claim_id"], ", ".join(r["cpts"] or []), r["wb_lines"], float(r["wb_total"] or 0),
                   den, None if den is None else round(float(r["wb_total"] or 0) - den, 2),
                   r["remittance_date"].strftime("%d/%m/%Y") if r["remittance_date"] else "",
                   r["status"], r["reason"], r.get("file", "")])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def prepare(db: Session, batch_id: int) -> dict:
    rows = amount_check(db, batch_id)
    groups = defaultdict(list)  # (fac_code, channel, sender, receiver, payer) -> [(row, claim)]
    for r in rows:
        if r["status"] != "Exact":
            continue
        src = db.execute(text(LATEST_SQL), {"f": r["facility_id"], "c": r["claim_id"]}).first()
        if not src:
            r["status"], r["reason"] = "No original", "Original submission file not stored"
            continue
        ids = {str(a).strip() for a in r["activity_ids"]}
        try:
            if (src.xml_content or "").lstrip().startswith("{"):
                got, chan = _riayati_claim(src.xml_content, r["claim_id"], ids, r["claim_id_payer"]), "RIAYATI"
            else:
                got, chan = _dha_claim(src.xml_content, r["claim_id"], ids, r["claim_id_payer"]), "DHA"
        except (ValueError, ET.ParseError) as exc:
            got, chan = None, ""
            r["reason"] = f"Original file unreadable: {exc}"[:200]
        if not got:
            r["status"] = "No original"
            r["reason"] = r["reason"] or "Denied activity IDs not found in the original claim"
            continue
        claim, sender, receiver, payer = got
        sender = sender or src.sender_id or ""
        receiver = receiver or src.receiver_id or r["wb_receiver"] or ""
        groups[(r["facility_code"], chan, sender, receiver, payer or r["ra_payer"] or "")].append((r, claim))

    out_dir = OUT_ROOT / f"import-{batch_id}" / datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)
    files = []
    for (fac, chan, sender, receiver, payer), items in sorted(groups.items()):
        for n, i in enumerate(range(0, len(items), CHUNK), start=1):
            part = items[i:i + CHUNK]
            safe = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in f"{fac}_{sender}_{receiver}" + (f"_{payer}" if chan == "RIAYATI" else ""))
            name = f"{safe}_RECON_{n:02d}.{'json' if chan == 'RIAYATI' else 'xml'}"
            if chan == "RIAYATI":
                _write_riayati(out_dir / name, sender, receiver, payer, [c for _, c in part])
            else:
                _write_dha(out_dir / name, sender, receiver, [c for _, c in part])
            for r, _ in part:
                r["file"] = name
            files.append({"name": name, "channel": chan, "sender": sender, "receiver": receiver,
                          "claims": len(part), "net": round(sum(float(r["wb_total"]) for r, _ in part), 2)})
    summary_name = f"Reconciliation_check_import-{batch_id}.xlsx"
    (out_dir / summary_name).write_bytes(_summary_xlsx(rows))

    by_branch = defaultdict(lambda: defaultdict(lambda: [0, 0.0]))
    for r in rows:
        a = by_branch[r["branch"]][r["status"]]
        a[0] += 1
        a[1] = round(a[1] + float(r["wb_total"] or 0), 2)
    return {"folder": str(out_dir), "summary": summary_name, "files": files,
            "by_branch": {b: {s: {"claims": v[0], "aed": v[1]} for s, v in st.items()} for b, st in by_branch.items()}}
