"""
resub_ra_workbook.py — Resubmission workbook: the Remittance Activity report (search by RA transaction date)
with a remark on every RA service line saying how to resubmit it.

  Sheet 1  RA + Remarks               every RA activity (paid ones too), the report columns + remark columns
  Sheet 2  Consultation Corrections   consultation-rule lines the facility must recode / check — share with them
  Sheet 3  Summary                    lines and amounts per remark type, to work in batches

Remarks reuse the resubmission worklist logic: the denial-code playbook, the payer recovery model and the
consultation visit check (repeat / follow-up / new condition).

Used by routers/resub.py  GET /api/resub/ra-workbook
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from . import resub_worklist as rw

REMARK_COLS = ["Resub Remark", "Remark Type", "Category", "Action Owner", "Draft Resubmission Comment",
               "Documents to Attach", "Resubmit By", "Days Left", "Denied (AED)", "Payer Pays on Resub %",
               "Expected Recovery (AED)", "Resubmitted After RA On"]

CONSULT_COLS = ["Claim Number", "Activity Detail ID", "Member Id", "Encounter Start Date", "Clinician",
                "Receiver Name", "Activity Code", "Net Amt", "Payment Amt", "Denied (AED)", "Visit Check",
                "Correct Code", "Correct Net (AED)", "Rate Basis", "Previous Claim", "Previous Visit", "Gap (days)",
                "Previous Dx", "This Dx", "What To Do", "Facility Reply"]

SKIP = ("Duplicate RA",)   # not counted in the Summary totals

_RESUB_SQL = text("""
SELECT claim_id, array_agg(DISTINCT submission_date::date) ds FROM claims
WHERE facility_id = :f AND claim_id = ANY(:ids) AND resubmission_type IS NOT NULL AND resubmission_type <> ''
  AND submission_date IS NOT NULL
GROUP BY 1
""")


def _d(s) -> Optional[date]:
    try:
        return datetime.strptime(s, "%d/%m/%Y").date() if s else None
    except (TypeError, ValueError):
        return None


def _num(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def add_remarks(db: Session, fid: int, wb) -> int:
    """Append remark columns to the Remittance Activity sheet in `wb` and add the consultation and summary
    sheets. Returns the number of RA lines."""
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    ws = wb.active
    ws.title = "RA + Remarks"
    head = [c.value for c in ws[1]]
    ix = {h: i for i, h in enumerate(head)}
    rows = list(ws.iter_rows(min_row=2, values_only=True))
    g = lambda r, h: r[ix[h]] if r[ix[h]] is not None else ""

    today = date.today()
    match = rw._matcher(rw.ensure_playbook(db))
    rate, _ = rw.recovery_model(db, fid)
    resub_dates = {r.claim_id: sorted(r.ds) for r in db.execute(
        _RESUB_SQL, {"f": fid, "ids": list({str(g(r, "Claim Number")) for r in rows})})}

    # pass 1: playbook remark per line
    out, consult_lines, seen = [], [], set()
    for r in rows:
        net, paid = _num(g(r, "Net Amt")), _num(g(r, "Payment Amt"))
        denied = round(max(net - paid, 0), 2)
        ra_d = _d(g(r, "Transaction Date"))
        ds = resub_dates.get(str(g(r, "Claim Number")), [])
        resub_d = next((d for d in ds if ra_d and d >= ra_d), None)      # resubmitted after this RA
        resub_before = any(ra_d and d < ra_d for d in ds)                # this RA answers a resubmission
        # the same RA line loaded again from another file → "Duplicate RA"
        line = (g(r, "Claim Number"), g(r, "RAFille Id"), g(r, "Activity Detail ID"), net, paid)
        dup = bool(g(r, "RAFille Id")) and line in seen
        seen.add(line)
        x = {"remark": "", "type": "", "category": "", "owner": "", "comment": "", "docs": "", "by": "",
             "days": "", "denied": denied, "pct": "", "exp": "", "resub": resub_d.strftime("%d/%m/%Y") if resub_d else ""}
        if dup:
            x.update(denied=0.0, remark="Duplicate RA load — already counted in the other copy of this RA. No action.",
                     type="Duplicate RA")
        elif denied <= 0.009:
            x.update(remark="Paid in full — no action.", type="Paid")
        else:
            code = rw.norm_code(g(r, "Denial Code"))
            act = str(g(r, "Activity Code")).strip()
            pb = match(code, act, f"{g(r, 'Denial Description')} {g(r, 'Response Comments')}")
            p = rate(str(g(r, "Receiver ID")), code)
            deadline = ra_d + timedelta(days=rw.WINDOW_DAYS) if ra_d else None
            partial = f"Partly paid (AED {paid:,.2f} of {net:,.2f}). " if paid > 0.009 else ""
            x.update(category=pb.category if pb else "Other", owner=pb.owner if pb else "Ghaf",
                     comment=(pb.draft_comment if pb else "") or "", docs=(pb.documents if pb else "") or "",
                     by=deadline.strftime("%d/%m/%Y") if deadline else "",
                     days=(deadline - today).days if deadline else "",
                     pct=round(p * 100), exp=round(denied * p, 2))
            if resub_d:
                x.update(remark=f"{partial}Already resubmitted on {resub_d:%d/%m/%Y} — wait for the RA.",
                         type="Already resubmitted")
            elif resub_before:
                x.update(remark=f"{partial}Denied again after resubmission — reconciliation. Send to the facility "
                                f"to take up with the payer; resubmit as type 'Reconciliation' once agreed. "
                                + rw.RECON_REMARKS.get(x["category"], rw.RECON_DEFAULT),
                         type="Reconciliation", owner="Facility")
            elif deadline and deadline < today:
                x.update(remark=f"{partial}Resubmission window closed on {deadline:%d/%m/%Y} — reconciliation or "
                                f"write-off.", type="Window closed")
            else:
                low = (x["owner"] == "Facility" and p * 100 < rw.FACILITY_MIN_RECOVERY_PCT)
                x.update(remark=partial + (rw.LOW_VALUE_ACTION if low else (pb.action if pb else
                                "No playbook rule for this denial code — review the RA comment and resubmit.")),
                         type="Low recovery" if low else
                              ("Facility input" if x["owner"] == "Facility" else "Resubmit (Ghaf)"),
                         owner="Ghaf" if low else x["owner"])
                if x["category"] == "Consultation visit rules" and rw.CONSULT_RE.match(act):
                    consult_lines.append(len(out))
        out.append(x)

    # pass 2: consultation visit check (repeat / follow-up / new condition) on open consultation-rule lines
    consult_rows = []
    tds = [_d(g(rows[i], "Encounter Start Date")) for i in consult_lines]
    tds = [t for t in tds if t]
    if tds:
        ctx = rw.ConsultContext(db, fid, min(tds) - timedelta(days=rw.REPEAT_DAYS), max(tds))
        by_claim = defaultdict(list)
        for i, (r, x) in enumerate(zip(rows, out)):
            if x["denied"] > 0.009:
                by_claim[(g(r, "Claim Number"), g(r, "Transaction Date"))].append(i)
        for i in consult_lines:
            r, x = rows[i], out[i]
            item = {"claim_id": str(g(r, "Claim Number")), "activity_id": str(g(r, "Activity Detail ID")),
                    "activity_code": str(g(r, "Activity Code")), "receiver_id": str(g(r, "Receiver ID"))}
            others = [f"{g(rows[j], 'Activity Code')} ({rw.norm_code(g(rows[j], 'Denial Code'))})"
                      for j in by_claim[(g(r, "Claim Number"), g(r, "Transaction Date"))] if j != i]
            chk = rw.consult_check(ctx, item, others)
            if not chk:
                continue
            kind = chk["consult_action"]
            x.update(remark=chk["action"], comment=chk["draft_comment"] or "")
            if kind == "close":
                x.update(type="Consultation — close", exp=0.0)
            elif kind == "resubmit":
                x["type"] = "Consultation — resubmit (Ghaf)"
                x["owner"] = "Ghaf"
            else:
                x.update(type="Consultation — code correction" if kind == "recode" else "Consultation — check doctor",
                         owner="Facility")
                if kind == "recode":
                    x["exp"] = round(min(x["denied"], chk["repeat_rate"]), 2)
                ev = chk["evidence"] or {}
                consult_rows.append([
                    g(r, "Claim Number"), g(r, "Activity Detail ID"), g(r, "Member Id"), g(r, "Encounter Start Date"),
                    g(r, "Clinician"), g(r, "Receiver Name"), g(r, "Activity Code"), g(r, "Net Amt"),
                    g(r, "Payment Amt"), x["denied"], chk["verdict"], chk.get("recode") or "",
                    chk.get("repeat_rate") or "", chk.get("rate_basis") or "", ev.get("prev_claim") or "",
                    datetime.fromisoformat(ev["prev_date"]).strftime("%d/%m/%Y") if ev.get("prev_date") else "",
                    ev.get("gap_days") if ev.get("gap_days") is not None else "", ev.get("prev_dx") or "",
                    ev.get("dx") or "", chk["action"], ""])

    # write remark columns
    TEAL = PatternFill("solid", fgColor="1E6B75")
    WHITE = Font(name="Calibri", bold=True, color="FFFFFF", size=10)
    WRAP = Alignment(wrap_text=True, vertical="top")
    TINT = {"Paid": "E8F5E9", "Duplicate RA": "EEEEEE", "Already resubmitted": "E3F1F3",
            "Consultation — code correction": "FDE2C4", "Consultation — check doctor": "FDE2C4",
            "Window closed": "FBE4E4", "Reconciliation": "F3E5F5"}
    base = len(head)
    for j, h in enumerate(REMARK_COLS, start=base + 1):
        c = ws.cell(row=1, column=j, value=h)
        c.fill, c.font = TEAL, WHITE
    for i, x in enumerate(out, start=2):
        vals = [x["remark"], x["type"], x["category"], x["owner"], x["comment"], x["docs"], x["by"], x["days"],
                x["denied"], x["pct"], x["exp"], x["resub"]]
        for j, v in enumerate(vals, start=base + 1):
            ws.cell(row=i, column=j, value=v)
        ws.cell(row=i, column=base + 1).alignment = WRAP
        if x["type"] in TINT:
            ws.cell(row=i, column=base + 2).fill = PatternFill("solid", fgColor=TINT[x["type"]])
    for j, h in enumerate(REMARK_COLS, start=base + 1):
        ws.column_dimensions[get_column_letter(j)].width = (
            70 if h in ("Resub Remark", "Draft Resubmission Comment") else 30 if h in ("Remark Type", "Category",
                                                                                    "Documents to Attach") else 14)
    ws.auto_filter.ref = f"A1:{get_column_letter(base + len(REMARK_COLS))}{len(out) + 1}"

    def sheet(title, cols, data, widths):
        s = wb.create_sheet(title)
        s.append(cols)
        for c in s[1]:
            c.fill, c.font = TEAL, WHITE
        for d in data:
            s.append(d)
        for j, h in enumerate(cols, start=1):
            s.column_dimensions[get_column_letter(j)].width = widths.get(h, 15)
        s.freeze_panes = "A2"
        return s

    cs = sheet("Consultation Corrections", CONSULT_COLS, consult_rows,
               {"What To Do": 80, "Rate Basis": 40, "Visit Check": 32, "Clinician": 26, "Receiver Name": 26,
                "Facility Reply": 30})
    for row in cs.iter_rows(min_row=2):
        row[CONSULT_COLS.index("What To Do")].alignment = WRAP
    if not consult_rows:
        cs.append(["No consultation-rule lines needing a code correction in this RA period."])

    agg = defaultdict(lambda: [0, 0.0, 0.0, 0.0])
    for x, r in zip(out, rows):
        a = agg[x["type"]]
        a[0] += 1; a[1] += _num(g(r, "Net Amt")); a[2] += x["denied"]; a[3] += _num(x["exp"])
    order = sorted(agg.items(), key=lambda kv: (kv[0] in SKIP, -kv[1][2]))
    real = [v for k, v in agg.items() if k not in SKIP]
    sheet("Summary", ["Remark Type", "RA Lines", "Net (AED)", "Denied (AED)", "Expected Recovery (AED)"],
          [[k, v[0], round(v[1], 2), round(v[2], 2), round(v[3], 2)] for k, v in order]
          + [["Total (excluding duplicate RAs)", sum(v[0] for v in real),
              round(sum(v[1] for v in real), 2), round(sum(v[2] for v in real), 2), round(sum(v[3] for v in real), 2)]],
          {"Remark Type": 46})
    return len(out)
