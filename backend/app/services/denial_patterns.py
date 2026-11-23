"""
denial_patterns.py — evidence for the Denial intelligence page (/calibration).

One pass over the last 12 months of encounters (latest submission per claim, RA rows de-duplicated on
facility_id, claim_id, ra_file_id, net_amount, paid_amount, denial_amount) gives one row per activity line:
  first-RA denial code / paid, total paid over all RAs (final denied = net - all payments), and whether the
  claim was resubmitted after its first RA and got an RA back (resubmission outcome known).
From that it builds pattern cards: code family, payer-specific, diagnosis-service mismatch, repeat tests
(presub_queries.REPEAT_TESTS), consultation follow-up (7 / 28-day rule), missing prior authorization.
Every card keeps FACTS (counts, rates, amounts) apart from the interpretation (cause / action).

Computing takes ~25 s for all facilities, so the result is cached for an hour and refreshed in a
background thread while the stale copy is served (first call after start returns {"warming": true}).
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict

from sqlalchemy import text

from .denial_codes import denial_description
from .presub_queries import REPEAT_TESTS

TTL = 3600
_CACHE: dict = {"t": 0.0, "data": None, "busy": False, "err": None}
_LOCK = threading.Lock()

CONSULT_RX = r"^(9|10|11)(\.0[12])?$"
FAMILIES = ("MNEC", "AUTH", "CODE", "PRCE", "CLAI", "DUPL", "ELIG", "TIME", "COPY", "NCOV", "BENX")

_SETUP = [
    """CREATE TEMP TABLE dp_l AS
    WITH c AS (
      SELECT DISTINCT ON (facility_id, claim_id) id, facility_id, claim_id, encounter_start::date enc, member_id,
             upper(coalesce(nullif(receiver_id,''), payer_id)) payer
      FROM claims WHERE encounter_start >= current_date - 365
      ORDER BY facility_id, claim_id, submission_date DESC NULLS LAST, id DESC),
    rs AS (SELECT x.facility_id, x.claim_id, min(x.submission_date) first_resub
           FROM claims x JOIN c USING (facility_id, claim_id) WHERE x.resubmission_key <> '' GROUP BY 1, 2),
    r AS (SELECT DISTINCT ON (r.facility_id, r.claim_id, r.ra_file_id, r.net_amount, r.paid_amount, r.denial_amount)
                 r.id, r.facility_id, r.claim_id, r.remittance_date
          FROM remittances r JOIN c USING (facility_id, claim_id)
          ORDER BY r.facility_id, r.claim_id, r.ra_file_id, r.net_amount, r.paid_amount, r.denial_amount, r.id),
    g AS (SELECT r.facility_id, r.claim_id, ad.activity_id, sum(ad.paid_amount) paid,
                 min(r.remittance_date) first_ra, max(r.remittance_date) last_ra,
                 (array_agg(ad.denial_code ORDER BY r.remittance_date, coalesce(ad.denial_code,'') = ''))[1] code,
                 (array_agg(ad.paid_amount ORDER BY r.remittance_date))[1] first_paid
          FROM r JOIN remittance_activity_details ad ON ad.remittance_id_fk = r.id GROUP BY 1, 2, 3)
    SELECT c.facility_id fid, c.claim_id, c.id cpk, c.enc, coalesce(c.member_id, '') member_id, c.payer,
           a.activity_code act, a.net, g.paid, g.first_ra, g.last_ra, g.first_paid,
           coalesce(nullif(a.authorization_id, ''), '') auth,
           CASE WHEN a.net - g.first_paid > 0.01 AND coalesce(g.code, '') <> '' THEN upper(g.code) END code,
           (rs.first_resub > g.first_ra AND g.last_ra > rs.first_resub) AS rs_ra,
           NULL::int gap, false rep
    FROM c JOIN claim_activities a ON a.claim_id_fk = c.id
    JOIN g ON g.facility_id = c.facility_id AND g.claim_id = c.claim_id AND g.activity_id = a.activity_id
    LEFT JOIN rs ON rs.facility_id = c.facility_id AND rs.claim_id = c.claim_id""",
    "ANALYZE dp_l",
    """CREATE TEMP TABLE dp_dx AS SELECT DISTINCT ON (d.claim_id_fk) d.claim_id_fk cpk, d.diagnosis_code dx
       FROM claim_diagnoses d JOIN (SELECT DISTINCT cpk FROM dp_l) u ON u.cpk = d.claim_id_fk
       WHERE d.diagnosis_type = 'Primary' ORDER BY d.claim_id_fk, d.sequence_no""",
    "ANALYZE dp_dx",
    f"""CREATE TEMP TABLE dp_h AS SELECT DISTINCT c.member_id, a.activity_code code, c.encounter_start::date d
       FROM claims c JOIN claim_activities a ON a.claim_id_fk = c.id
       WHERE c.encounter_start >= current_date - 365 - 180 AND coalesce(c.member_id, '') <> ''
         AND (a.activity_code = ANY(:rep) OR a.activity_code ~ '{CONSULT_RX}')""",
    "CREATE INDEX ON dp_h (member_id, code, d)",
    "ANALYZE dp_h",
    # repeat test within the payer interval (same member, any facility)
    """UPDATE dp_l l SET rep = true WHERE l.act = ANY(:rep) AND l.member_id <> '' AND EXISTS (
         SELECT 1 FROM dp_h h WHERE h.member_id = l.member_id AND h.code = l.act AND h.d < l.enc
           AND h.d >= l.enc - CASE l.act WHEN '80061' THEN :d80061 ELSE :d83036 END)""",
    # days since the previous consultation of the same member (any facility, 60-day look-back)
    f"""UPDATE dp_l l SET gap = l.enc - (SELECT max(h.d) FROM dp_h h WHERE h.member_id = l.member_id
           AND h.code ~ '{CONSULT_RX}' AND h.d < l.enc AND h.d >= l.enc - 60)
       WHERE l.act ~ '^(9|10|11)$' AND l.net > 0 AND l.member_id <> ''""",
]

_AGG = """
    SELECT fid, payer, act, code, count(*) n, sum(net) net,
           sum(CASE WHEN code IS NOT NULL THEN net - first_paid ELSE 0 END) den_amt,
           sum(CASE WHEN code IS NOT NULL THEN greatest(net - paid, 0) ELSE 0 END) fin_amt,
           count(*) FILTER (WHERE code IS NOT NULL AND rs_ra) rs_n,
           count(*) FILTER (WHERE code IS NOT NULL AND rs_ra AND net - paid <= 0.01) rs_paid,
           sum(CASE WHEN code IS NOT NULL AND rs_ra THEN net - first_paid ELSE 0 END) rs_den,
           sum(CASE WHEN code IS NOT NULL AND rs_ra THEN least(paid - first_paid, net - first_paid) ELSE 0 END) rs_rec,
           count(*) FILTER (WHERE auth = '') no_auth,
           count(*) FILTER (WHERE auth = '' AND left(code, 4) = 'AUTH') no_auth_den,
           count(*) FILTER (WHERE left(code, 4) = 'AUTH') auth_den,
           CASE WHEN act = ANY(:rep) THEN rep END rep,
           CASE WHEN gap IS NULL THEN NULL WHEN gap <= 7 THEN 'le7' WHEN gap <= 28 THEN 'd8_28' ELSE 'gt28' END gapb
    FROM dp_l GROUP BY fid, payer, act, code, 16, 17"""

_DXAGG = """
    SELECT l.fid, l.act, x.dx, count(*) n, count(*) FILTER (WHERE left(l.code, 4) IN ('MNEC', 'CODE')) den,
           sum(CASE WHEN left(l.code, 4) IN ('MNEC', 'CODE') THEN l.net - l.first_paid ELSE 0 END) den_amt
    FROM dp_l l JOIN dp_dx x USING (cpk) GROUP BY 1, 2, 3 HAVING count(*) >= 10"""

# worst still-unpaid lines per facility x family (+ repeat / follow-up lines) for the per-claim list
_SAMPLES = """
    SELECT * FROM (
      SELECT fid, claim_id, act, code, payer, enc, first_ra, round(greatest(net - paid, 0), 2) unpaid,
             rep, gap, x.dx,
             row_number() OVER (PARTITION BY fid, CASE WHEN rep THEN 'REP' WHEN gap <= 28 THEN 'FUP'
                                ELSE left(code, 4) END ORDER BY first_ra DESC, net - paid DESC) rn
      FROM dp_l LEFT JOIN dp_dx x USING (cpk)
      WHERE code IS NOT NULL AND net - paid > 0.01 AND first_ra >= current_date - 60) s WHERE rn <= 8"""


def _compute(db) -> dict:
    t0 = time.time()
    p = {"rep": list(REPEAT_TESTS), "d80061": REPEAT_TESTS["80061"][1], "d83036": REPEAT_TESTS["83036"][1]}
    try:
        for s in _SETUP:
            db.execute(text(s), p)
        agg = [dict(r._mapping) for r in db.execute(text(_AGG), p)]
        dx = [dict(r._mapping) for r in db.execute(text(_DXAGG))]
        smp = [dict(r._mapping) for r in db.execute(text(_SAMPLES))]
        asof = db.execute(text("SELECT max(first_ra) FROM dp_l")).scalar()
    finally:
        db.rollback()   # drops nothing permanent; temp tables go with the transaction / connection
        for t in ("dp_l", "dp_dx", "dp_h"):
            try:
                db.execute(text(f"DROP TABLE IF EXISTS {t}"))
            except Exception:
                pass
        db.commit()
    for rows in (agg, dx):
        for r in rows:
            for k, v in r.items():
                if hasattr(v, "as_integer_ratio") and not isinstance(v, (int, float)):
                    r[k] = float(v)
    for r in smp:
        r["unpaid"] = float(r["unpaid"]); r["enc"] = str(r["enc"]); r["first_ra"] = str(r["first_ra"])
    return {"agg": agg, "dx": dx, "samples": smp, "as_of": str(asof) if asof else None,
            "computed_at": time.strftime("%Y-%m-%d %H:%M"), "compute_s": round(time.time() - t0, 1)}


def _refresh(session_factory):
    db = session_factory()
    try:
        data = _compute(db)
        with _LOCK:
            _CACHE.update(t=time.time(), data=data, err=None)
    except Exception as e:  # keep serving the stale copy
        with _LOCK:
            _CACHE["err"] = str(e)
    finally:
        db.close()
        with _LOCK:
            _CACHE["busy"] = False


def get_data(session_factory, force: bool = False, wait: bool = False):
    """Cached dataset; refreshes in the background when older than TTL (stale copy served meanwhile)."""
    with _LOCK:
        stale = force or _CACHE["data"] is None or time.time() - _CACHE["t"] > TTL
        start = stale and not _CACHE["busy"]
        if start:
            _CACHE["busy"] = True
    if start:
        if wait:
            _refresh(session_factory)
        else:
            threading.Thread(target=_refresh, args=(session_factory,), daemon=True).start()
    return _CACHE["data"], _CACHE["busy"], _CACHE["err"]


# ---------------------------------------------------------------------------------------------------------
# cards
# ---------------------------------------------------------------------------------------------------------
FAMILY_TEXT = {
    "MNEC": ("Payer judged the service not medically necessary for the coded diagnosis.",
             "Clinicians: document the indication and code the diagnosis that supports the service before billing."),
    "AUTH": ("Prior approval missing, invalid or not matching the billed service.",
             "Clinic admin: take approval before the service and put the approval number on every line it covers."),
    "CODE": ("Coding error: code combination, modifier or diagnosis specificity rejected.",
             "Coders: correct the code / diagnosis pairing and resubmit with the corrected codes."),
    "PRCE": ("Price above the payer's contracted tariff or package.",
             "Billing: load the payer's current price list in the ERP; underpaid-to-tariff lines are rarely recovered."),
    "CLAI": ("Claim data problem (missing / invalid field, attachment or member detail).",
             "Billing: fix the field or attach the document and resubmit — usually recovered."),
    "DUPL": ("Payer sees the service as already billed (same member, date, code).",
             "Billing: check for a genuine double bill before resubmitting; resubmit only true separate services with a note."),
    "ELIG": ("Member not eligible / not covered on the service date.",
             "Front desk: verify eligibility at registration; resubmit with corrected member / card details."),
    "TIME": ("Submission or resubmission after the payer's time limit.",
             "Billing: submit by the 15th and resubmit within 21 days of the RA."),
    "COPY": ("Patient share / co-pay not deducted as per policy.",
             "Front desk: collect the co-pay per the member's policy; recovery from payer is rare."),
    "NCOV": ("Service not covered by the member's plan.",
             "Front desk: check plan benefits before the service; bill the patient where allowed."),
    "BENX": ("Benefit limit exhausted.", "Front desk: check remaining benefit before the service."),
}


def _pct(a, b):
    return round(100.0 * a / b, 1) if b else None


def _resub(rows):
    n = sum(r["rs_n"] for r in rows); paid = sum(r["rs_paid"] for r in rows)
    den = sum(r["rs_den"] for r in rows); rec = sum(r["rs_rec"] for r in rows)
    if not n:
        return None
    pp = _pct(paid, n)
    verdict = "yes" if pp >= 40 else "selective" if pp >= 20 else "no"
    return {"n": n, "paid": paid, "paid_pct": pp, "recovered_pct": _pct(rec, den), "verdict": verdict}


def _top(rows, key, k=3):
    s = defaultdict(lambda: [0, 0.0])
    for r in rows:
        s[r[key]][0] += r["n"]; s[r[key]][1] += r["den_amt"]
    return sorted(s.items(), key=lambda x: -x[1][1])[:k]


def _claims(samples, pred, k=8):
    out = [s for s in samples if pred(s)]
    out.sort(key=lambda s: (s["first_ra"], s["unpaid"]), reverse=True)
    return [{"facility_id": s["fid"], "claim_id": s["claim_id"], "activity_code": s["act"], "denial_code": s["code"],
             "payer": s["payer"], "encounter_date": s["enc"], "first_ra": s["first_ra"], "unpaid": s["unpaid"],
             "dx": s["dx"]} for s in out[:k]]


def build(data: dict, facility_id: int | None, stopped: set, pname=lambda x: x) -> dict:
    keep = (lambda f: f == facility_id) if facility_id else (lambda f: f not in stopped)
    A = [r for r in data["agg"] if keep(r["fid"])]
    S = [s for s in data["samples"] if keep(s["fid"])]
    DX = [r for r in data["dx"] if keep(r["fid"])]
    tot_n = sum(r["n"] for r in A); tot_net = sum(r["net"] for r in A)
    D = [r for r in A if r["code"]]
    den_n = sum(r["n"] for r in D); den_amt = sum(r["den_amt"] for r in D); fin_amt = sum(r["fin_amt"] for r in D)
    cards = []

    # 1) code families
    fam_rows = defaultdict(list)
    for r in D:
        fam_rows[r["code"][:4]].append(r)
    for fam, rows in sorted(fam_rows.items(), key=lambda x: -sum(r["den_amt"] for r in x[1])):
        if fam not in FAMILIES:
            continue
        n = sum(r["n"] for r in rows); amt = sum(r["den_amt"] for r in rows)
        if n < 20 or amt < 0.01 * den_amt:
            continue
        codes = _top([dict(r, code_=r["code"]) for r in rows], "code_", 3)
        payers = _top(rows, "payer", 3); acts = _top(rows, "act", 3)
        pay_share = payers[0][1][1] / amt if payers else 0
        act_share = acts[0][1][1] / amt if acts else 0
        facts = [f"{n:,} lines denied at first RA ({_pct(n, tot_n)}% of all lines), AED {amt:,.0f}",
                 f"AED {sum(r['fin_amt'] for r in rows):,.0f} still unpaid after all RAs"]
        cause = FAMILY_TEXT[fam][0]
        if pay_share >= 0.5:
            cause += f" Concentrated at {pname(payers[0][0])} ({pay_share:.0%} of the amount) — looks like that payer's policy."
        if act_share >= 0.3:
            cause += f" Mostly one service ({acts[0][0]}, {act_share:.0%})."
        cards.append({
            "id": f"fam-{fam}", "kind": "family", "family": fam,
            "title": f"{fam} — {denial_description(codes[0][0]) or codes[0][0]}" if codes else fam,
            "key_figure": {"value": round(amt), "label": "AED denied (first RA)"},
            "facts": facts,
            "chips": ([{"label": f"{c} · {v[0]:,}", "tone": "denied", "title": denial_description(c) or ""} for c, v in codes]
                      + [{"label": f"{pname(p)} · AED {v[1]:,.0f}", "tone": "ink"} for p, v in payers[:2]]
                      + [{"label": f"CPT {a} · {v[0]:,}", "tone": "pending"} for a, v in acts[:2]]),
            "cause": cause, "action": FAMILY_TEXT[fam][1],
            "resub": _resub(rows),
            "claims": _claims(S, lambda s, f=fam: (s["code"] or "")[:4] == f and not s["rep"] and not (s["gap"] is not None and s["gap"] <= 28)),
        })

    # 2) payer-specific: one payer denies a service that the others pay
    by_pa = defaultdict(lambda: [0, 0, 0.0, []])
    by_a = defaultdict(lambda: [0, 0])
    for r in A:
        k = (r["payer"], r["act"]); by_pa[k][0] += r["n"]; by_a[r["act"]][0] += r["n"]
        if r["code"]:
            by_pa[k][1] += r["n"]; by_pa[k][2] += r["den_amt"]; by_pa[k][3].append(r); by_a[r["act"]][1] += r["n"]
    ps = []
    for (p, a), (n, d, amt, rows) in by_pa.items():
        on, od = by_a[a][0] - n, by_a[a][1] - d
        if n < 30 or on < 100:
            continue
        pr, orr = d / n, od / on
        if pr >= 0.4 and pr >= 3 * max(orr, 0.01):
            ps.append((amt, p, a, n, d, on, od, rows))
    for amt, p, a, n, d, on, od, rows in sorted(ps, reverse=True)[:4]:
        top = _top([dict(r, code_=r["code"]) for r in rows], "code_", 2)
        cards.append({
            "id": f"payer-{p}-{a}", "kind": "payer", "family": top[0][0][:4] if top else None,
            "title": f"{pname(p)} denies {a} that other payers pay",
            "key_figure": {"value": _pct(d, n), "label": f"% denied at {pname(p)} vs {_pct(od, on)}% elsewhere", "unit": "%"},
            "facts": [f"{pname(p)}: {d:,} of {n:,} lines of {a} denied (AED {amt:,.0f})",
                      f"Other payers: {od:,} of {on:,} denied ({_pct(od, on)}%)"],
            "chips": [{"label": f"{c} · {v[0]:,}", "tone": "denied", "title": denial_description(c) or ""} for c, v in top],
            "cause": "Same service, same facilities, very different outcome — a payer policy or contract rule, not a documentation slip.",
            "action": f"Agree the rule with {pname(p)} (contract / policy). Until then, check this service for {pname(p)} members before billing; resubmit only with new evidence.",
            "resub": _resub(rows),
            "claims": _claims(S, lambda s, p=p, a=a: s["payer"] == p and s["act"] == a),
        })

    # 3) repeat tests within interval
    for code, (name, days) in REPEAT_TESTS.items():
        rr = [r for r in A if r["act"] == code]
        rep = [r for r in rr if r["rep"]]; first = [r for r in rr if not r["rep"]]
        rn, rd = sum(r["n"] for r in rep), sum(r["n"] for r in rep if r["code"])
        fn, fd = sum(r["n"] for r in first), sum(r["n"] for r in first if r["code"])
        if rn < 20:
            continue
        top = _top([dict(r, code_=r["code"]) for r in rep if r["code"]], "code_", 2)
        cards.append({
            "id": f"repeat-{code}", "kind": "repeat", "family": "MNEC",
            "title": f"{name} ({code}) repeated within {days} days",
            "key_figure": {"value": _pct(rd, rn), "label": f"% denied when repeated vs {_pct(fd, fn)}% otherwise", "unit": "%"},
            "facts": [f"{rn:,} repeat tests within {days} days, {rd:,} denied (AED {sum(r['den_amt'] for r in rep):,.0f})",
                      f"Not repeated: {fd:,} of {fn:,} denied ({_pct(fd, fn)}%)"],
            "chips": [{"label": f"{c} · {v[0]:,}", "tone": "denied", "title": denial_description(c) or ""} for c, v in top],
            "cause": f"Payers allow one {name.lower()} per {days} days unless a clinical change is documented.",
            "action": f"Check the last {name.lower()} date before ordering; if repeated, document the reason (new symptoms, therapy change) in the case sheet.",
            "resub": _resub([r for r in rep if r["code"]]),
            "claims": _claims(S, lambda s, c=code: s["rep"] and s["act"] == c),
        })

    # 4) consultation follow-up
    cons = [r for r in A if r["act"] in ("9", "10", "11")]
    b = {k: [r for r in cons if r["gapb"] == k] for k in ("le7", "d8_28")}
    base = [r for r in cons if r["gapb"] in (None, "gt28")]
    bn, bd = sum(r["n"] for r in base), sum(r["n"] for r in base if r["code"])
    st = {k: (sum(r["n"] for r in v), sum(r["n"] for r in v if r["code"]), sum(r["den_amt"] for r in v)) for k, v in b.items()}
    if st["le7"][0] + st["d8_28"][0] >= 20:
        fu = [r for r in b["le7"] + b["d8_28"] if r["code"]]
        top = _top([dict(r, code_=r["code"]) for r in fu], "code_", 2)
        cards.append({
            "id": "consult-followup", "kind": "followup", "family": top[0][0][:4] if top else None,
            "title": "Full consultation billed soon after a previous visit",
            "key_figure": {"value": _pct(st["d8_28"][1], st["d8_28"][0]), "label": f"% denied at 8–28 days vs {_pct(bd, bn)}% for new visits", "unit": "%"},
            "facts": [f"≤7 days: {st['le7'][1]:,} of {st['le7'][0]:,} denied ({_pct(st['le7'][1], st['le7'][0])}%)",
                      f"8–28 days: {st['d8_28'][1]:,} of {st['d8_28'][0]:,} denied ({_pct(st['d8_28'][1], st['d8_28'][0])}%)",
                      f"No visit in 28 days: {bd:,} of {bn:,} ({_pct(bd, bn)}%) · AED {st['le7'][2] + st['d8_28'][2]:,.0f} at stake"],
            "chips": [{"label": f"{c} · {v[0]:,}", "tone": "denied", "title": denial_description(c) or ""} for c, v in top],
            "cause": "Payers expect a free follow-up (.01, net 0) within 7 days and a repeat consultation (.02) at 8–28 days, not a new full consultation.",
            "action": "Bill 9.01/10.01 (net 0) within 7 days and the .02 code at 8–28 days; the ERP should look up the last visit date.",
            "resub": _resub(fu),
            "claims": _claims(S, lambda s: s["gap"] is not None and s["gap"] <= 28 and s["act"] in ("9", "10", "11")),
        })

    # 5) missing authorization
    na = sum(r["no_auth"] for r in A); nad = sum(r["no_auth_den"] for r in A); ad_ = sum(r["auth_den"] for r in A)
    wa, wad = tot_n - na, ad_ - nad
    if ad_ >= 20:
        cards.append({
            "id": "auth-missing", "kind": "auth", "family": "AUTH",
            "title": "Authorization denials: blank vs. filled approval number",
            "key_figure": {"value": _pct(nad, ad_), "label": "% of AUTH denials had no approval number on the line", "unit": "%"},
            "facts": [f"AUTH denial rate {_pct(nad, na)}% on lines without approval vs {_pct(wad, wa)}% with one",
                      f"{ad_:,} AUTH-denied lines in total"],
            "chips": [],
            "cause": ("Most AUTH denials are lines billed without any approval — a front-office step was skipped."
                      if nad >= wad else
                      "Most AUTH denials carry an approval number — approvals are taken but do not match the billed service / dates."),
            "action": ("Make approval number mandatory in the ERP for approval-required services."
                       if nad >= wad else "Check approved codes, quantity and validity dates against what is billed before submission."),
            "resub": None, "claims": [],
        })

    # 6) diagnosis-service mismatch (MNEC/CODE)
    act_t = defaultdict(lambda: [0, 0])
    for r in DX:
        act_t[r["act"]][0] += r["n"]; act_t[r["act"]][1] += r["den"]
    combo = defaultdict(lambda: [0, 0, 0.0])
    for r in DX:
        c = combo[(r["act"], r["dx"])]; c[0] += r["n"]; c[1] += r["den"]; c[2] += r["den_amt"]
    mm = []
    for (a, dx), (n, d, amt) in combo.items():
        on, od = act_t[a][0] - n, act_t[a][1] - d
        if n >= 20 and d / n >= 0.5 and on >= 50 and od / on <= d / n / 2:
            mm.append((amt, a, dx, n, d, on, od))
    mm.sort(reverse=True)
    if mm:
        top = mm[:5]
        cards.append({
            "id": "dx-mismatch", "kind": "dx", "family": "MNEC",
            "title": "Service × principal diagnosis pairs that payers reject",
            "key_figure": {"value": round(sum(x[0] for x in top)), "label": "AED denied (MNEC/CODE) on the top pairs"},
            "facts": [f"{a} with {dx}: {d:,}/{n:,} denied ({_pct(d, n)}%) vs {_pct(od, on)}% with other diagnoses" for _, a, dx, n, d, on, od in top[:3]],
            "chips": [{"label": f"{a} + {dx}", "tone": "denied"} for _, a, dx, *_r in top],
            "cause": "The same service is paid with other principal diagnoses — the coded diagnosis does not justify it.",
            "action": "Clinicians: order these services only with a supporting diagnosis, or code the actual indication as principal.",
            "resub": _resub([r for r in D if r["code"][:4] in ("MNEC", "CODE")]),
            "claims": _claims(S, lambda s, keys={(x[1], x[2]) for x in top}: (s["act"], s["dx"]) in keys),
        })

    return {
        "as_of": data["as_of"], "computed_at": data["computed_at"], "compute_s": data["compute_s"],
        "summary": {"lines": tot_n, "net": round(tot_net), "denied_lines": den_n, "denied_rate": _pct(den_n, tot_n),
                    "denied_amt": round(den_amt), "final_unpaid": round(fin_amt), "resub": _resub(D)},
        "cards": cards,
    }
