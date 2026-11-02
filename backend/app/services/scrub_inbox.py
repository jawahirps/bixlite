"""
scrub_inbox.py — watched folder for the Claim scrubber.

Any folder in ~/Downloads whose name contains "to Scrub" (e.g. "October to Scrub") is checked every
30 seconds. Each finished .xml (DHA) or .json (Riayati) file is scrubbed exactly like a manual upload — facility taken from the
file header (SenderID) — then moved to "Scrubbed/" (or "Failed/" with a .txt reason) so it runs once.
Nothing is sent anywhere; the session appears under Recent Sessions in the Claim scrubber.
"""
from __future__ import annotations

import logging
import re
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from ..database import SessionLocal
from ..models import ScrubSession
from . import presub_queries
from .bulk_resub import _norm, facility_lookup
from .scrub_extractor import ScrubXmlExtractor
from .scrub_rules import ScrubRuleEngine
from .scrub_xml_service import ScrubXmlService

log = logging.getLogger(__name__)
ROOT = Path.home() / "Downloads"
PATTERN = re.compile(r"to scrub", re.I)
INTERVAL = 30
SETTLE = 15          # seconds a file must be unchanged before it is picked up
_started = False


def folders():
    return [p for p in ROOT.iterdir() if p.is_dir() and PATTERN.search(p.name)] if ROOT.exists() else []


def riayati_to_xml(data: bytes) -> bytes:
    """Riayati JSON (Entity / Header / Claim[]) → the same Claim.Submission layout as a DHA file."""
    import json
    from xml.sax.saxutils import escape as x
    doc = json.loads(data.decode("utf-8-sig")) or {}
    ent = doc.get("Entity") or doc.get("Submission") or {}   # stored files use Entity, HIMES test files Submission
    h = ent.get("Header") or {}
    tag = lambda t, v: f"<{t}>{x(str(v))}</{t}>" if v not in (None, "") else ""
    out = ["<Claim.Submission><Header>", tag("SenderID", h.get("SenderID")), tag("ReceiverID", h.get("ReceiverID")),
           tag("TransactionDate", h.get("TransactionDate")), tag("RecordCount", h.get("RecordCount")),
           tag("DispositionFlag", h.get("DispositionFlag")), "</Header>"]
    for c in ent.get("Claim") or []:
        e = c.get("Encounter") or {}
        out += ["<Claim>", tag("ID", c.get("ID")), tag("IDPayer", c.get("IDPayer")), tag("MemberID", c.get("MemberID")),
                tag("PayerID", c.get("PayerID") or h.get("PayerID")), tag("ProviderID", c.get("ProviderID")),
                tag("EmiratesIDNumber", c.get("NationalIDNumber")), tag("Gross", c.get("Gross")),
                tag("PatientShare", c.get("PatientShare")), tag("Net", c.get("Net")), "<Encounter>",
                *(tag(k, e.get(k)) for k in ("FacilityID", "Type", "PatientID", "Start", "End", "StartType", "EndType")),
                "</Encounter>"]
        for d in c.get("Diagnosis") or []:
            out += ["<Diagnosis>", tag("Type", d.get("Type")), tag("Code", d.get("Code")), "</Diagnosis>"]
        for a in c.get("Activity") or []:
            out += ["<Activity>", *(tag(k, a.get(k)) for k in ("ID", "Start", "Type", "Code", "Quantity", "Net", "Clinician",
                                                              "PriorAuthorizationID"))]
            for o in a.get("Observation") or []:
                o = dict(o)
                if o.get("Type") == "Text" and o.get("Code") == "Description":   # Riayati case-sheet text
                    o["Code"] = "Presenting-Complaint"
                out += ["<Observation>", *(tag(k, o.get(k)) for k in ("Type", "Code", "Value", "ValueType")), "</Observation>"]
            out.append("</Activity>")
        r = c.get("Resubmission") or {}
        if r.get("Type"):
            out += ["<Resubmission>", tag("Type", r.get("Type")), tag("Comment", r.get("Comment")), "</Resubmission>"]
        out.append("</Claim>")
    out.append("</Claim.Submission>")
    return "".join(out).encode("utf-8")


def _sender(data: bytes) -> Optional[str]:
    m = re.search(rb"<SenderID>\s*([^<\s]+)\s*</SenderID>", data[:4000])
    return m.group(1).decode() if m else None


def _move(path: Path, sub: str, note: Optional[str] = None) -> None:
    dest = path.parent / sub
    dest.mkdir(exist_ok=True)
    target = dest / path.name
    if target.exists():
        target = dest / f"{path.stem}_{datetime.now():%H%M%S}{path.suffix}"
    shutil.move(str(path), str(target))
    if note:
        target.with_suffix(".txt").write_text(note)


def scrub_file(path: Path) -> str:
    data = path.read_bytes()
    riayati = path.suffix.lower() == ".json"
    if riayati:
        data = riayati_to_xml(data)
    sender = _sender(data)
    db = SessionLocal()
    try:
        fid = facility_lookup(db).get(_norm(sender)) if sender else None
        if not fid:
            raise ValueError(f"Facility not recognised from SenderID {sender!r}")
        s = ScrubSession(facility_id=fid, uploaded_by=None, uploaded_filename=path.name, status="uploaded")
        db.add(s); db.flush()
        s.claims_count = ScrubXmlExtractor(db, session_id=s.id, facility_id=fid).parse(data)
        s.status = "parsed"; db.commit()
        steps = [lambda: presub_queries.generate(db, s.id)]
        if not riayati:   # DHA schema / structure checks do not apply to Riayati JSON
            steps.append(lambda: ScrubXmlService(db).run(session_id=s.id, xml_bytes=data))
        for step in steps:
            try:
                step(); db.commit()
            except Exception as exc:  # noqa: BLE001 — same as the upload route: later checks still run
                db.rollback(); log.warning("Scrub inbox step failed for %s: %s", path.name, exc)
        s.status = "running"; db.commit()
        ScrubRuleEngine(db, session_id=s.id).run()
        s.status, s.run_completed_at = "completed", datetime.utcnow()
        db.commit()
        return f"session {s.id}: {s.claims_count} claims"
    finally:
        db.close()


def scan_once() -> None:
    now = time.time()
    for folder in folders():
        for f in sorted([*folder.glob("*.xml"), *folder.glob("*.json")]):
            st = f.stat()
            if now - st.st_mtime < SETTLE or st.st_size == 0:
                continue
            try:
                msg = scrub_file(f)
                _move(f, "Scrubbed")
                log.info("Scrub inbox: %s → %s", f.name, msg)
            except Exception as exc:  # noqa: BLE001
                log.exception("Scrub inbox failed for %s", f.name)
                try:
                    _move(f, "Failed", f"{datetime.now():%d/%m/%Y %H:%M} {exc}")
                except OSError:
                    pass


def _loop() -> None:
    while True:
        try:
            scan_once()
        except Exception:  # noqa: BLE001
            log.exception("Scrub inbox scan failed")
        time.sleep(INTERVAL)


def start() -> None:
    global _started
    if not _started:
        _started = True
        threading.Thread(target=_loop, daemon=True, name="scrub-inbox").start()
