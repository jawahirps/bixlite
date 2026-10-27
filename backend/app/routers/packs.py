"""
Client report packs — runs the PDF/Excel generators in the report-scripts folder
(monthly pack, quarterly, half-yearly, receiver workbooks, resubmission worklist,
recall audit, clinician ledger) as background jobs and lists the files they produce.

  GET  /api/packs/catalog                 → pack types and their options
  POST /api/packs/run                     → queue a pack for one facility
  GET  /api/packs/jobs                    → recent pack jobs (newest first)
  GET  /api/packs/jobs/{id}/file?path=…   → download one generated file

Jobs run one at a time (the generators are memory-heavy on an 8 GB Mac). Files are
found by scanning ~/ghaf_reports for files written while the job ran. Word output
is switched off (GHAF_NO_WORD) — performance reports are PDF only.
"""
from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, Session, mapped_column

from ..database import SessionLocal, get_db
from ..models import Base, Facility, User
from ..security import get_current_user

router = APIRouter(prefix="/api/packs", tags=["packs"])

SCRIPT_DIRS = ("~/ghaf-report-scripts", "~/Desktop/report-scripts")
OUT_ROOT = Path(os.path.expanduser("~/ghaf_reports"))
JOB_TIMEOUT = 2 * 60 * 60  # seconds


class PackJob(Base):
    __tablename__ = "pack_jobs"

    id           : Mapped[int]                = mapped_column(Integer, primary_key=True)
    user_id      : Mapped[Optional[int]]      = mapped_column(Integer, nullable=True)
    kind         : Mapped[str]                = mapped_column(String(40), nullable=False)
    label        : Mapped[str]                = mapped_column(String(200), nullable=False)
    facility_id  : Mapped[Optional[int]]      = mapped_column(Integer, nullable=True)
    params_json  : Mapped[Optional[str]]      = mapped_column(Text)
    status       : Mapped[str]                = mapped_column(String(20), nullable=False, default="queued")
    files_json   : Mapped[Optional[str]]      = mapped_column(Text)
    log_tail     : Mapped[Optional[str]]      = mapped_column(Text)
    created_at   : Mapped[datetime]           = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at   : Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    completed_at : Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


# ── Catalogue ────────────────────────────────────────────────────────────────
# section, label, script, fields the page asks for
CATALOG = {
    "monthly":        {"section": "performance", "label": "Monthly pack",
                       "help": "Performance report, AR ageing & resubmission tracker, clinician scorecards",
                       "fields": ["month"]},
    "quarterly":      {"section": "performance", "label": "Quarterly report",
                       "help": "Quarter or full year (DHA eClaimLink facilities)",
                       "fields": ["quarter"]},
    "half_yearly":    {"section": "performance", "label": "Half-yearly / FY report",
                       "help": "H1, H2, FY or a custom month range; denial addendum added when denials exceed 10%",
                       "fields": ["half"]},
    "followup":       {"section": "worklists", "label": "Receiver follow-up workbooks",
                       "help": "Open claims awaiting remittance, one sheet per receiver",
                       "fields": ["date_range"]},
    "reconciliation": {"section": "worklists", "label": "Reconciliation workbooks",
                       "help": "Denied resubmissions eligible for reconciliation, one sheet per receiver",
                       "fields": ["date_range"]},
    "resub":          {"section": "worklists", "label": "Resubmission worklist",
                       "help": "Ghaf and facility versions of the resubmission worklist",
                       "fields": []},
    "recall_audit":   {"section": "audits", "label": "Consultation recall audit",
                       "help": "Per receiver: audit review, recall detail and draft email (Jan 2025 to date)",
                       "fields": []},
    "clinician_ledger": {"section": "audits", "label": "Clinician denial ledger",
                       "help": "One sheet per clinician: claims with medical-necessity denials",
                       "fields": ["date_range"]},
}


def _scripts_dir() -> Path:
    for d in SCRIPT_DIRS:
        p = Path(os.path.expanduser(d))
        if (p / "monthly_pack.py").exists():
            return p
    raise HTTPException(500, "Report scripts folder not found (~/ghaf-report-scripts or ~/Desktop/report-scripts)")


def _command(kind: str, fac: Facility, p: dict) -> tuple[list[str], str]:
    """Return (argv after the interpreter, human label suffix)."""
    def need(key, pattern, msg):
        v = str(p.get(key) or "").strip()
        if not re.fullmatch(pattern, v):
            raise HTTPException(400, msg)
        return v

    if kind == "monthly":
        m = need("month", r"\d{4}-\d{2}", "Month must be YYYY-MM")
        return ["monthly_pack.py", "--month", m, "--facility", fac.facility_code], m
    if kind == "quarterly":
        q = need("period", r"(Q[1-4]|annual)-\d{4}", "Period must be Q1-YYYY … Q4-YYYY or annual-YYYY")
        return ["quarterly_report_generator.py", "--period", q], q
    if kind == "half_yearly":
        h = need("period", r"(H1|H2|FY)-\d{4}|\d{4}-\d{2}:\d{4}-\d{2}", "Period must be H1-YYYY, H2-YYYY, FY-YYYY or YYYY-MM:YYYY-MM")
        return ["half_yearly_report_generator.py", "--period", h], h
    if kind in ("followup", "reconciliation", "clinician_ledger"):
        d0 = need("date_from", r"\d{4}-\d{2}-\d{2}", "From date required")
        d1 = need("date_to", r"\d{4}-\d{2}-\d{2}", "To date required")
        if kind == "clinician_ledger":
            return ["clinician_ledger.py", "--facility", fac.facility_code, "--from", d0, "--to", d1], f"{d0} to {d1}"
        return ["receiver_workbooks.py", kind, "--facility", fac.facility_code, "--from", d0, "--to", d1], f"{d0} to {d1}"
    if kind == "resub":
        return ["resub_worklist.py"], date.today().isoformat()
    if kind == "recall_audit":
        folder = re.sub(r"[^A-Za-z0-9]+", "_", fac.facility_name).strip("_")[:40]
        lic = (fac.license_id or "").split("/")[0].strip()
        return ["audit_facility.py", str(fac.id), folder, fac.facility_name, lic], "Jan 2025 to date"
    raise HTTPException(400, f"Unknown pack type {kind}")


# ── Runner (one job at a time) ───────────────────────────────────────────────
_queue: "queue.Queue[int]" = queue.Queue()
_worker_started = False
_worker_lock = threading.Lock()


def _ensure_worker():
    global _worker_started
    with _worker_lock:
        if not _worker_started:
            threading.Thread(target=_worker, daemon=True, name="pack-runner").start()
            _worker_started = True


def _new_files(since: float) -> list[str]:
    out = []
    for f in OUT_ROOT.rglob("*"):
        if f.is_file() and f.suffix.lower() in (".pdf", ".xlsx", ".docx", ".csv") and f.stat().st_mtime >= since:
            if f.suffix.lower() == ".docx" and "Email" not in f.name:
                continue  # performance reports: PDF only
            out.append(str(f))
    return sorted(out)


def _worker():
    while True:
        job_id = _queue.get()
        db = SessionLocal()
        try:
            job = db.get(PackJob, job_id)
            if not job or job.status != "queued":
                continue
            params = json.loads(job.params_json or "{}")
            job.status, job.started_at = "running", datetime.now(timezone.utc)
            db.commit()
            env = {**os.environ, "GHAF_FACILITY_ID": str(job.facility_id), "GHAF_NO_WORD": "1", "MPLBACKEND": "Agg"}
            started = time.time() - 2
            try:
                proc = subprocess.run([sys.executable, *params["argv"]], cwd=params["cwd"], env=env,
                                      capture_output=True, text=True, timeout=JOB_TIMEOUT)
                log = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")
                ok = proc.returncode == 0
            except subprocess.TimeoutExpired:
                log, ok = "Timed out after 2 hours", False
            files = _new_files(started)
            job.files_json = json.dumps(files)
            job.log_tail = log[-4000:]
            job.status = "completed" if ok and files else ("completed" if ok else "error")
            if ok and not files:
                job.log_tail = (job.log_tail or "") + "\n(no files produced — likely no data for this facility/period)"
            job.completed_at = datetime.now(timezone.utc)
            db.commit()
        except Exception as exc:  # keep the worker alive
            try:
                job = db.get(PackJob, job_id)
                job.status, job.log_tail, job.completed_at = "error", str(exc)[-4000:], datetime.now(timezone.utc)
                db.commit()
            except Exception:
                pass
        finally:
            db.close()


def resume_on_startup():
    """Jobs left running by a restart are marked interrupted; queued ones are re-queued."""
    db = SessionLocal()
    try:
        PackJob.__table__.create(bind=db.get_bind(), checkfirst=True)
        for j in db.query(PackJob).filter(PackJob.status.in_(("queued", "running"))).all():
            if j.status == "running":
                j.status, j.log_tail = "error", (j.log_tail or "") + "\nInterrupted by app restart — run again."
                j.completed_at = datetime.now(timezone.utc)
            else:
                _queue.put(j.id)
        db.commit()
    finally:
        db.close()
    _ensure_worker()


# ── API ──────────────────────────────────────────────────────────────────────
class RunIn(BaseModel):
    kind: str
    facility_id: int
    month: Optional[str] = None
    period: Optional[str] = None
    date_from: Optional[str] = None
    date_to: Optional[str] = None


def _out(j: PackJob) -> dict:
    files = json.loads(j.files_json or "[]")
    return {
        "id": j.id, "kind": j.kind, "label": j.label, "facility_id": j.facility_id,
        "status": j.status, "log_tail": j.log_tail,
        "files": [{"path": f, "name": os.path.basename(f),
                   "folder": os.path.relpath(os.path.dirname(f), OUT_ROOT)} for f in files],
        "created_at": j.created_at, "started_at": j.started_at, "completed_at": j.completed_at,
    }


@router.get("/catalog")
def catalog(current_user: User = Depends(get_current_user)):
    return CATALOG


@router.post("/run")
def run_pack(body: RunIn, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    if body.kind not in CATALOG:
        raise HTTPException(400, "Unknown pack type")
    fac = db.get(Facility, body.facility_id)
    if not fac or not fac.is_active or fac.facility_code == "GHAF-001":
        raise HTTPException(400, "Choose an active facility")
    argv, suffix = _command(body.kind, fac, body.model_dump())
    job = PackJob(user_id=current_user.id, kind=body.kind, facility_id=fac.id, status="queued",
                  label=f"{CATALOG[body.kind]['label']} — {fac.facility_name} — {suffix}",
                  params_json=json.dumps({"argv": argv, "cwd": str(_scripts_dir()), **body.model_dump()}))
    db.add(job)
    db.commit()
    _ensure_worker()
    _queue.put(job.id)
    return _out(job)


@router.get("/jobs")
def list_jobs(facility_id: Optional[int] = None, limit: int = 50,
              db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    q = db.query(PackJob)
    if facility_id:
        q = q.filter(PackJob.facility_id == facility_id)
    return [_out(j) for j in q.order_by(PackJob.id.desc()).limit(min(limit, 200)).all()]


@router.get("/jobs/{job_id}/file")
def download(job_id: int, path: str, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    job = db.get(PackJob, job_id)
    if not job or path not in json.loads(job.files_json or "[]"):
        raise HTTPException(404, "File not found for this job")
    p = Path(path).resolve()
    if OUT_ROOT.resolve() not in p.parents or not p.is_file():
        raise HTTPException(404, "File no longer exists")
    return FileResponse(str(p), filename=p.name)
