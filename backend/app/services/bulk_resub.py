"""
bulk_resub.py — Bulk resubmission and reconciliation-workbook import.

Adapted from the BIX (.NET) Bulk Resubmission module for this app's data model:

  * candidates      denied claims (latest RA has net > paid), newest first, with a
                    local readiness check (submission found, both transaction
                    references present, an active DHA or Riayati login).
  * workbook import reads a payer/facility reconciliation .xlsx (FacilityID + InvoiceNo
                    headers in the first 25 rows of any sheet), drops occurrences
                    copied across sheets, and classifies every row against our data:
                    Matched / Partial / Missing / Invalid.
  * fetch missing   bounded windows (≤ 33 days, one per facility and month) derived from
                    remittance file-name dates; runs the existing DHA and Riayati fetch.
  * bulk remarks    append a timestamped remark to resub_tracking for every denied
                    activity of up to 100 selected claims.

Nothing here transmits anything to DHA or Riayati — production submission is locked.
"""
from __future__ import annotations

import io
import re
import threading
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Dict, Iterable, List, Optional, Tuple

from sqlalchemy import BigInteger, Date, DateTime, ForeignKey, Integer, Numeric, String, Text, func, text
from sqlalchemy.orm import Mapped, Session, mapped_column

from ..database import SessionLocal
from ..models import Base, Facility, FacilityRiayatiAccount, ResubTracking

MAX_CLAIMS = 100
MIN_REMARK, MAX_REMARK = 10, 2000
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
MAX_ROWS = 100_000

MATCHED, PARTIAL, MISSING, INVALID, PENDING = "Matched", "Partial", "Missing", "Invalid", "Pending"


class ReconImportBatch(Base):
    __tablename__ = "recon_import_batches"

    id             : Mapped[int]                = mapped_column(Integer, primary_key=True)
    user_id        : Mapped[Optional[int]]      = mapped_column(Integer)
    filename       : Mapped[str]                = mapped_column(String(255), nullable=False)
    status         : Mapped[str]                = mapped_column(String(20), nullable=False, default="Analyzed")
    progress       : Mapped[Optional[str]]      = mapped_column(String(500))
    error          : Mapped[Optional[str]]      = mapped_column(Text)
    total_rows     : Mapped[int]                = mapped_column(Integer, default=0)
    unique_rows    : Mapped[int]                = mapped_column(Integer, default=0)
    duplicate_rows : Mapped[int]                = mapped_column(Integer, default=0)
    matched_rows   : Mapped[int]                = mapped_column(Integer, default=0)
    partial_rows   : Mapped[int]                = mapped_column(Integer, default=0)
    missing_rows   : Mapped[int]                = mapped_column(Integer, default=0)
    invalid_rows   : Mapped[int]                = mapped_column(Integer, default=0)
    fetch_windows  : Mapped[int]                = mapped_column(Integer, default=0)
    uploaded_at    : Mapped[datetime]           = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at   : Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class ReconImportRow(Base):
    __tablename__ = "recon_import_rows"

    id                   : Mapped[int]               = mapped_column(BigInteger, primary_key=True)
    batch_id             : Mapped[int]               = mapped_column(Integer, ForeignKey("recon_import_batches.id", ondelete="CASCADE"), index=True)
    sheet                : Mapped[str]               = mapped_column(String(100))
    source_row           : Mapped[int]               = mapped_column(Integer)
    facility_external_id : Mapped[Optional[str]]     = mapped_column(String(100))
    facility_id          : Mapped[Optional[int]]     = mapped_column(Integer)
    claim_id             : Mapped[Optional[str]]     = mapped_column(String(100))
    cpt_code             : Mapped[Optional[str]]     = mapped_column(String(50))
    receiver_id          : Mapped[Optional[str]]     = mapped_column(String(50))
    payer_id             : Mapped[Optional[str]]     = mapped_column(String(50))
    rejected_amount      : Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    denial_code          : Mapped[Optional[str]]     = mapped_column(String(50))
    comment              : Mapped[Optional[str]]     = mapped_column(Text)
    remittance_file      : Mapped[Optional[str]]     = mapped_column(String(255))
    inferred_date        : Mapped[Optional[date]]    = mapped_column(Date)
    date_source          : Mapped[str]               = mapped_column(String(20), default="Unavailable")
    match_status         : Mapped[str]               = mapped_column(String(20), default=PENDING, index=True)
    message              : Mapped[Optional[str]]     = mapped_column(Text)
    remittance_pk        : Mapped[Optional[int]]     = mapped_column(BigInteger)
    submission_ref       : Mapped[Optional[str]]     = mapped_column(String(255))
    remittance_ref       : Mapped[Optional[str]]     = mapped_column(String(255))


def ensure_tables(bind) -> None:
    ReconImportBatch.__table__.create(bind=bind, checkfirst=True)
    ReconImportRow.__table__.create(bind=bind, checkfirst=True)


# ── Policy ───────────────────────────────────────────────────────────────────
def validate_request(claim_keys: Iterable, remarks: Optional[str]) -> Optional[str]:
    n = len({k for k in (claim_keys or []) if k})
    if n == 0:
        return "Select at least one claim."
    if n > MAX_CLAIMS:
        return f"Select no more than {MAX_CLAIMS} claims per batch."
    v = (remarks or "").strip()
    if len(v) < MIN_REMARK:
        return f"Enter a meaningful resubmission remark (at least {MIN_REMARK} characters)."
    if len(v) > MAX_REMARK:
        return f"Resubmission remarks cannot exceed {MAX_REMARK:,} characters."
    return None


def sn_ineligible(resubs: int, first_sub, ra_date, recon_agreed: bool, today: Optional[date] = None) -> Optional[str]:
    """eClaimLink SN 03/2019 per claim (same rules as resub_prepare.sn_status / sn_errors).
    first_sub = first ORIGINAL submission date (fallback: earliest submission); ra_date = latest RA date."""
    from .resub_worklist import _valid_until
    today = today or date.today()
    if (resubs or 0) >= 2:
        return "limit reached (2 resubmissions)"
    if first_sub:
        fd = first_sub.date() if hasattr(first_sub, "date") else first_sub
        if today > _valid_until(fd, None):
            return "claim validity ended (24 months)"
    if ra_date and not recon_agreed:
        rd = ra_date.date() if hasattr(ra_date, "date") else ra_date
        if today > rd + timedelta(days=21):
            return "21-day window closed"
    return None


def readiness(submission_found: bool, sub_ref: Optional[str], ra_ref: Optional[str], has_login: bool,
              sn_reason: Optional[str] = None) -> Tuple[bool, str]:
    fails = [sn_reason] if sn_reason else []
    if not submission_found:
        fails.append("source submission not found")
    if not (sub_ref or "").strip():
        fails.append("submission transaction reference missing")
    if not (ra_ref or "").strip():
        fails.append("remittance transaction reference missing")
    if not has_login:
        fails.append("active DHA/Riayati login missing")
    return (True, "Ready") if not fails else (False, "; ".join(fails))


def _facilities_with_login(db: Session) -> set:
    ids = {f.id for f in db.query(Facility).filter(Facility.is_active.is_(True)).all()
           if (f.credential_key and (f.eclaimlink_username or "").upper() not in ("", "N/A", "DEMO_USER"))
           or (f.riayati_username and f.riayati_password)}
    ids |= {a.facility_id for a in db.query(FacilityRiayatiAccount).filter_by(is_active=True).all() if a.password}
    return ids


# ── Candidates ───────────────────────────────────────────────────────────────
CANDIDATE_SQL = """
WITH latest AS (
    SELECT DISTINCT ON (r.facility_id, r.claim_id)
           r.id, r.facility_id, r.claim_id, r.claim_id_payer, r.payer_id, r.remittance_date,
           COALESCE(r.net_amount, 0) AS net, COALESCE(r.paid_amount, 0) AS paid,
           r.denial_code, x.source_filename AS ra_ref
    FROM remittances r
    LEFT JOIN raw_xml_store x ON x.id = r.raw_xml_id
    WHERE r.facility_id = ANY(:fids) {date_filter}
    ORDER BY r.facility_id, r.claim_id, r.remittance_date DESC, r.id DESC
)
SELECT l.*, f.facility_name,
       (SELECT cx.source_filename FROM claims c LEFT JOIN raw_xml_store cx ON cx.id = c.raw_xml_id
         WHERE c.facility_id = l.facility_id AND c.claim_id = l.claim_id
         ORDER BY c.submission_date DESC NULLS LAST, c.id DESC LIMIT 1)                 AS sub_ref,
       EXISTS (SELECT 1 FROM claims c WHERE c.facility_id = l.facility_id AND c.claim_id = l.claim_id) AS has_sub,
       (SELECT string_agg(t.remarks, E'\\n' ORDER BY t.updated_at) FROM resub_tracking t
         WHERE t.facility_id = l.facility_id AND t.claim_id = l.claim_id AND t.ra_date = l.remittance_date) AS existing_remarks,
       sn.resubs, COALESCE(sn.first_orig, sn.first_any) AS first_sub,
       EXISTS (SELECT 1 FROM resub_tracking t WHERE t.facility_id = l.facility_id AND t.claim_id = l.claim_id
                 AND t.status = :recon_agreed) AS recon_agreed
FROM latest l JOIN facilities f ON f.id = l.facility_id
LEFT JOIN LATERAL (
    SELECT count(*) FILTER (WHERE c.resubmission_key <> '')           AS resubs,
           min(c.submission_date) FILTER (WHERE c.resubmission_key = '') AS first_orig,
           min(c.submission_date)                                       AS first_any
    FROM claims c WHERE c.facility_id = l.facility_id AND c.claim_id = l.claim_id) sn ON TRUE
WHERE l.net > l.paid {search_filter} {id_filter}
ORDER BY l.remittance_date DESC, l.id DESC
LIMIT :lim
"""


def candidates(db: Session, facility_ids: List[int], search: Optional[str] = None,
               only_keys: Optional[List[Tuple[int, str]]] = None, limit: int = 250) -> List[dict]:
    if not facility_ids:
        return []
    from .resub_worklist import RECON_AGREED
    params = {"fids": facility_ids, "lim": limit, "recon_agreed": RECON_AGREED}
    search_filter = id_filter = ""
    date_filter = "AND r.remittance_date >= CURRENT_DATE - INTERVAL '400 days'"
    if search:
        params["s"] = f"%{search.strip()}%"
        search_filter = "AND (l.claim_id ILIKE :s OR l.claim_id_payer ILIKE :s OR l.ra_ref ILIKE :s)"
    if only_keys is not None:
        if not only_keys:
            return []
        params["keys"] = [f"{f}|{c}" for f, c in only_keys]
        id_filter = "AND (l.facility_id::text || '|' || l.claim_id) = ANY(:keys)"
        date_filter = ""
    sql = CANDIDATE_SQL.format(date_filter=date_filter, search_filter=search_filter, id_filter=id_filter)
    logins = _facilities_with_login(db)
    out = []
    for r in db.execute(text(sql), params).mappings():
        sn_reason = sn_ineligible(r["resubs"], r["first_sub"], r["remittance_date"], r["recon_agreed"])
        ok, msg = readiness(r["has_sub"], r["sub_ref"], r["ra_ref"], r["facility_id"] in logins, sn_reason)
        out.append({
            "key": f"{r['facility_id']}|{r['claim_id']}", "remittance_id": r["id"],
            "facility_id": r["facility_id"], "facility_name": r["facility_name"],
            "claim_id": r["claim_id"], "payer_claim_id": r["claim_id_payer"], "payer_id": r["payer_id"],
            "ra_date": r["remittance_date"].isoformat(), "net": float(r["net"]), "paid": float(r["paid"]),
            "outstanding": float(r["net"] - r["paid"]), "denial_code": r["denial_code"],
            "submission_ref": r["sub_ref"], "remittance_ref": r["ra_ref"],
            "existing_remarks": r["existing_remarks"], "eligible": ok, "readiness": msg,
            "sn_ineligible": sn_reason, "resubmissions_used": int(r["resubs"] or 0),
            "recon_agreed": bool(r["recon_agreed"]),
        })
    return out


def apply_remarks(db: Session, facility_ids: List[int], keys: List[str], remarks: str, actor: str) -> int:
    """Append a timestamped remark to resub_tracking for each denied activity of the selected claims'
    latest RA. New items move to 'In progress'; other statuses are kept."""
    pairs = []
    for k in dict.fromkeys(keys):
        fid, _, cid = k.partition("|")
        if fid.isdigit() and int(fid) in facility_ids and cid:
            pairs.append((int(fid), cid))
    rows = candidates(db, facility_ids, only_keys=pairs, limit=MAX_CLAIMS + 1)
    if len(rows) != len(pairs):
        raise ValueError("One or more selected claims no longer exist or are not denied claims.")
    line = f"[{datetime.now(timezone.utc).astimezone():%Y-%m-%d %H:%M}] Bulk remark by {actor}: {remarks.strip()}"
    n = 0
    for c in rows:
        acts = db.execute(text("""
            SELECT DISTINCT d.activity_id FROM remittance_activity_details d
            WHERE d.remittance_id_fk = :rid
              AND (COALESCE(d.paid_amount, 0) < COALESCE(d.net, 0) OR d.denial_code IS NOT NULL)"""),
            {"rid": c["remittance_id"]}).scalars().all() or [""]
        ra_date = date.fromisoformat(c["ra_date"])
        for act in acts:
            t = (db.query(ResubTracking).filter_by(facility_id=c["facility_id"], claim_id=c["claim_id"],
                                                    activity_id=act, ra_date=ra_date).first())
            if not t:
                t = ResubTracking(facility_id=c["facility_id"], claim_id=c["claim_id"], activity_id=act,
                                  ra_date=ra_date, status="In progress", remarks=line)
                db.add(t)
            else:
                t.remarks = f"{t.remarks}\n{line}" if (t.remarks or "").strip() else line
                if t.status == "New":
                    t.status = "In progress"
            t.updated_by = actor
        n += 1
    db.commit()
    return n


# ── Workbook import ──────────────────────────────────────────────────────────
HEADERS = {
    "facilityid": "facility", "invoiceno": "claim", "invoice": "claim", "claimid": "claim",
    "cptcode": "cpt", "activitycode": "cpt", "receiverid": "receiver", "payerid": "payer",
    "rejectedamt": "rejected", "rejectedamount": "rejected",
    "lastdenailcode": "denial", "lastdenialcode": "denial", "denialcode": "denial",
    "resubmissioncomment": "comment", "comments": "comment",
    "lastremittancefilename": "rafile", "remittancefilename": "rafile",
}
COMPACT_DATE = re.compile(r"(?<!\d)(\d{8})(?:\d{6})?(?!\d)")


def _norm_header(v) -> str:
    return re.sub(r"[^a-z0-9]", "", str(v or "").lower())


def _norm(v) -> str:
    return str(v or "").strip().upper()


def infer_date(filename: Optional[str]) -> Optional[date]:
    for m in COMPACT_DATE.finditer(filename or ""):
        s = m.group(1)
        for fmt in ("%d%m%Y", "%Y%m%d"):
            try:
                d = datetime.strptime(s, fmt).date()
            except ValueError:
                continue
            if 2000 <= d.year <= 2100:
                return d
    return None


def fetch_windows(dates: Iterable[date]) -> List[Tuple[date, date]]:
    """One window per calendar month: earliest − 1 day … latest + 1 day (≤ 33 days)."""
    by_month: Dict[Tuple[int, int], List[date]] = defaultdict(list)
    for d in set(dates):
        by_month[(d.year, d.month)].append(d)
    return [(min(v) - timedelta(days=1), max(v) + timedelta(days=1)) for _, v in sorted(by_month.items())]


def facility_lookup(db: Session) -> Dict[str, int]:
    """Aliases → active facility id: each part of the combined licence ID, eClaimLink username,
    DHPO provider id, Riayati usernames/licences, and Riayati sender IDs seen on claims."""
    look: Dict[str, int] = {}
    for f in db.query(Facility).filter(Facility.is_active.is_(True)).all():
        if f.facility_code == "GHAF-001":
            continue
        parts = [*(f.license_id or "").split("/"), f.eclaimlink_username, f.dhpo_provider_id,
                 f.riayati_username, f.facility_name, f.facility_code]
        for p in parts:
            if _norm(p) and _norm(p) not in ("N/A", "DEMO_USER"):
                look.setdefault(_norm(p), f.id)
    for a in db.query(FacilityRiayatiAccount).all():
        fac = db.get(Facility, a.facility_id)
        if fac and fac.is_active:
            for p in (a.username, a.license_id):
                if _norm(p):
                    look.setdefault(_norm(p), a.facility_id)
    for fid, sender in db.execute(text("SELECT DISTINCT facility_id, sender_id FROM claims WHERE sender_id IS NOT NULL")):
        look.setdefault(_norm(sender), fid)
    return look


def _dec(v) -> Optional[Decimal]:
    try:
        return Decimal(str(v).replace(",", "").strip()) if str(v or "").strip() else None
    except InvalidOperation:
        return None


def import_workbook(db: Session, data: bytes, filename: str, user_id: int, allowed: List[int]) -> ReconImportBatch:
    import openpyxl
    if not data or data[:2] != b"PK":
        raise ValueError("The uploaded file is not a valid .xlsx workbook.")
    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    look = {k: v for k, v in facility_lookup(db).items() if v in allowed}
    batch = ReconImportBatch(user_id=user_id, filename=filename[:255], status="Analyzed", progress="Reading workbook")
    db.add(batch)
    db.commit()

    prior_max: Dict[tuple, int] = {}   # highest occurrence count of a signature in earlier sheets
    buf: List[ReconImportRow] = []
    recognised = 0
    for ws in wb.worksheets:
        rows = ws.iter_rows(values_only=True)
        header, hdr_row = None, 0
        for i, r in enumerate(rows, start=1):
            cols = {HEADERS[_norm_header(v)]: j for j, v in enumerate(r or []) if _norm_header(v) in HEADERS}
            if "facility" in cols and "claim" in cols:
                header, hdr_row = cols, i
                break
            if i >= 25:
                break
        if not header:
            continue
        recognised += 1
        seen: Dict[tuple, int] = defaultdict(int)
        for n, r in enumerate(rows, start=hdr_row + 1):
            get = lambda k: (str(r[header[k]]).strip() if k in header and header[k] < len(r) and r[header[k]] is not None else "")
            fac_ext, claim = get("facility"), get("claim")
            if not fac_ext and not claim:
                continue
            batch.total_rows += 1
            if batch.total_rows > MAX_ROWS:
                raise ValueError(f"Workbook exceeds the {MAX_ROWS:,}-row safety limit.")
            rejected = _dec(get("rejected"))
            sig = (_norm(fac_ext), _norm(claim), _norm(get("cpt")), _norm(get("receiver")), _norm(get("payer")),
                   str(rejected), _norm(get("denial")), get("comment"), _norm(get("rafile")))
            seen[sig] += 1
            if seen[sig] <= prior_max.get(sig, 0):     # same occurrence already counted in an earlier sheet
                batch.duplicate_rows += 1
                continue
            d = infer_date(get("rafile"))
            fid = look.get(_norm(fac_ext))
            buf.append(ReconImportRow(
                batch_id=batch.id, sheet=ws.title[:100], source_row=n, facility_external_id=fac_ext[:100],
                facility_id=fid, claim_id=claim[:100] or None, cpt_code=get("cpt")[:50] or None,
                receiver_id=get("receiver")[:50] or None, payer_id=get("payer")[:50] or None,
                rejected_amount=rejected, denial_code=get("denial")[:50] or None, comment=get("comment") or None,
                remittance_file=get("rafile")[:255] or None, inferred_date=d,
                date_source="FileName" if d else "Unavailable",
                match_status=INVALID if not fid or not claim else PENDING,
                message=("FacilityID is not mapped to an active facility." if not fid
                         else "InvoiceNo is missing." if not claim else None)))
            batch.unique_rows += 1
            if len(buf) >= 500:
                db.add_all(buf); db.commit(); buf.clear()
        for sig, cnt in seen.items():
            prior_max[sig] = max(prior_max.get(sig, 0), cnt)
    if not recognised:
        db.rollback()
        db.delete(db.get(ReconImportBatch, batch.id)); db.commit()
        raise ValueError("No worksheet has the required FacilityID and InvoiceNo headers (first 25 rows).")
    if buf:
        db.add_all(buf)
    db.commit()
    refresh_matches(db, batch.id)
    batch = db.get(ReconImportBatch, batch.id)
    batch.status, batch.progress = "Completed", "Workbook analysed against the current database"
    batch.completed_at = datetime.now(timezone.utc)
    db.commit()
    return batch


def refresh_matches(db: Session, batch_id: int) -> None:
    rows = db.query(ReconImportRow).filter(ReconImportRow.batch_id == batch_id,
                                           ReconImportRow.facility_id.isnot(None),
                                           ReconImportRow.claim_id.isnot(None)).all()
    for i in range(0, len(rows), 300):
        chunk = rows[i:i + 300]
        fids = list({r.facility_id for r in chunk}); cids = list({r.claim_id for r in chunk})
        ras = {}
        for m in db.execute(text("""
            SELECT DISTINCT ON (r.facility_id, upper(r.claim_id)) r.id, r.facility_id, upper(r.claim_id) AS cid,
                   r.remittance_date, x.source_filename
            FROM remittances r LEFT JOIN raw_xml_store x ON x.id = r.raw_xml_id
            WHERE r.facility_id = ANY(:f) AND r.claim_id = ANY(:c)
            ORDER BY r.facility_id, upper(r.claim_id), r.remittance_date DESC, r.id DESC"""), {"f": fids, "c": cids}).mappings():
            ras[(m["facility_id"], m["cid"])] = m
        subs: Dict[tuple, list] = defaultdict(list)
        for m in db.execute(text("""
            SELECT c.facility_id, upper(c.claim_id) AS cid, x.source_filename,
                   array_agg(DISTINCT upper(a.activity_code)) FILTER (WHERE a.activity_code IS NOT NULL) AS codes
            FROM claims c LEFT JOIN raw_xml_store x ON x.id = c.raw_xml_id
            LEFT JOIN claim_activities a ON a.claim_id_fk = c.id
            WHERE c.facility_id = ANY(:f) AND c.claim_id = ANY(:c)
            GROUP BY c.id, c.facility_id, upper(c.claim_id), x.source_filename, c.submission_date
            ORDER BY c.submission_date DESC NULLS LAST"""), {"f": fids, "c": cids}).mappings():
            subs[(m["facility_id"], m["cid"])].append(m)
        for r in chunk:
            key = (r.facility_id, _norm(r.claim_id))
            ra, sl = ras.get(key), subs.get(key, [])
            cpt = _norm(r.cpt_code)
            sub = next((s for s in sl if cpt and cpt in (s["codes"] or [])), None)
            r.remittance_pk = ra["id"] if ra else None
            r.remittance_ref = ra["source_filename"] if ra else None
            r.submission_ref = (sub or (sl[0] if sl else {})).get("source_filename") if sl else None
            warn = None
            if ra:
                if r.inferred_date and abs((ra["remittance_date"] - r.inferred_date).days) > 3:
                    warn = f"File-name date differs from RA date {ra['remittance_date']:%Y-%m-%d}."
                r.inferred_date, r.date_source = ra["remittance_date"], "Remittance"
            r.match_status = MATCHED if ra and sub else PARTIAL if ra or sl else MISSING
            if sl and not sub:
                r.message = ("Submission found, but the workbook has no CPT to verify." if not cpt
                             else f"Submission found, but CPT '{r.cpt_code}' is not on it.")
            else:
                r.message = warn or {
                    MATCHED: "Submission, CPT and remittance verified.",
                    PARTIAL: "Only one side of submission / remittance is available.",
                }.get(r.match_status, "Claim missing; a fetch window can be derived." if r.inferred_date
                      else "Claim missing and no reliable date for a fetch window.")
        db.commit()
    b = db.get(ReconImportBatch, batch_id)
    counts = dict(db.execute(text("SELECT match_status, count(*) FROM recon_import_rows WHERE batch_id = :b GROUP BY 1"),
                             {"b": batch_id}).all())
    b.matched_rows, b.partial_rows = counts.get(MATCHED, 0), counts.get(PARTIAL, 0)
    b.missing_rows, b.invalid_rows = counts.get(MISSING, 0), counts.get(INVALID, 0)
    b.fetch_windows = db.execute(text("""
        SELECT count(DISTINCT (facility_id, date_trunc('month', inferred_date))) FROM recon_import_rows
        WHERE batch_id = :b AND facility_id IS NOT NULL AND inferred_date IS NOT NULL
          AND match_status IN ('Missing', 'Partial')"""), {"b": batch_id}).scalar() or 0
    db.commit()


# ── Fetch missing (background) ───────────────────────────────────────────────
_fetch_lock = threading.Lock()


def fetch_missing(batch_id: int) -> None:
    from ..services.fetch_service import DHASyncService
    from ..services.riayati_service import RiayatiSyncService, riayati_accounts
    with _fetch_lock:
        db = SessionLocal()
        b = db.get(ReconImportBatch, batch_id)
        try:
            b.status, b.progress, b.error = "Fetching", "Fetching missing claim and remittance windows", None
            db.commit()
            rows = db.execute(text("""
                SELECT facility_id, inferred_date FROM recon_import_rows
                WHERE batch_id = :b AND facility_id IS NOT NULL AND inferred_date IS NOT NULL
                  AND match_status IN ('Missing', 'Partial')"""), {"b": batch_id}).all()
            by_fac: Dict[int, List[date]] = defaultdict(list)
            for fid, d in rows:
                by_fac[fid].append(d)
            errors = []
            for fid, dates in by_fac.items():
                fac = db.get(Facility, fid)
                has_dha = bool(fac.credential_key) and (fac.eclaimlink_username or "").upper() not in ("", "N/A", "DEMO_USER")
                has_riayati = bool(riayati_accounts(db, fac))
                for d0, d1 in fetch_windows(dates):
                    b.progress = f"{fac.facility_name}: fetching {d0:%d %b} – {d1:%d %b %Y}"
                    db.commit()
                    if has_dha:
                        fl = DHASyncService(session=db).run_sync_pipeline(fid, d0, d1)
                        if fl.status not in ("success", "completed"):
                            errors.append(f"DHA {fac.facility_name} {d0}–{d1}: {fl.status}")
                    if has_riayati:
                        fl = RiayatiSyncService(session=db).run_sync_pipeline(fid, d0, d1)
                        if fl.status not in ("success", "completed"):
                            errors.append(f"Riayati {fac.facility_name} {d0}–{d1}: {fl.status}")
                    if not (has_dha or has_riayati):
                        errors.append(f"{fac.facility_name}: no active DHA or Riayati login")
            b = db.get(ReconImportBatch, batch_id)
            b.status, b.progress = "Matching", "Rechecking rows against the fetched data"
            db.commit()
            refresh_matches(db, batch_id)
            b = db.get(ReconImportBatch, batch_id)
            b.status = "Completed"
            b.progress = "Fetch and matching completed" + (" with warnings" if errors else "")
            b.error = "\n".join(errors)[:4000] or None
            b.completed_at = datetime.now(timezone.utc)
            db.commit()
        except Exception as exc:
            db.rollback()
            b = db.get(ReconImportBatch, batch_id)
            b.status, b.error = "Failed", str(exc)[:4000]
            b.progress = "Background fetch failed; nothing was submitted"
            b.completed_at = datetime.now(timezone.utc)
            db.commit()
        finally:
            db.close()


def exceptions_workbook(db: Session, batch_id: int) -> bytes:
    import openpyxl
    from openpyxl.styles import Font
    wb = openpyxl.Workbook(); ws = wb.active; ws.title = "Exceptions"
    hdr = ["Status", "Facility ID", "Facility", "Invoice No", "CPT", "Receiver", "Payer", "Rejected Amount",
           "Denial Code", "Resubmission Comment", "Transaction Date", "Date Source", "Validation", "Sheet", "Row"]
    ws.append(hdr)
    names = {f.id: f.facility_name for f in db.query(Facility).all()}
    for r in (db.query(ReconImportRow).filter(ReconImportRow.batch_id == batch_id, ReconImportRow.match_status != MATCHED)
              .order_by(ReconImportRow.match_status, ReconImportRow.facility_external_id, ReconImportRow.claim_id)):
        ws.append([r.match_status, r.facility_external_id, names.get(r.facility_id, ""), r.claim_id, r.cpt_code,
                   r.receiver_id, r.payer_id, float(r.rejected_amount) if r.rejected_amount is not None else None,
                   r.denial_code, r.comment, r.inferred_date, r.date_source, r.message, r.sheet, r.source_row])
    for c in ws[1]:
        c.font = Font(bold=True)
    ws.freeze_panes = "A2"; ws.auto_filter.ref = ws.dimensions
    out = io.BytesIO(); wb.save(out)
    return out.getvalue()
