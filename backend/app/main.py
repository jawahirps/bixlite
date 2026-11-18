"""
BIX Lite — FastAPI application entrypoint.

Run with:
    uvicorn app.main:app --reload --port 8000
"""
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import threading

from .config import get_settings
from .database import SessionLocal, init_db
from .routers import analytics, auth, bulk_resub, dashboard, calibration, clinical_profiles, dha_sync, doc_review, facilities, packs, reports, portal, receipts, resub, scrub, turnaround, denial_trends, alerts
from .seed import run_seed

settings = get_settings()

app = FastAPI(
    title=settings.APP_NAME,
    description="Core API for clinical profiles, login tracking, and the DHA XML Sync pipeline.",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _prepare_bulk_resub() -> None:
    """Create the import tables; imports left mid-fetch by a restart are marked failed."""
    from .services import bulk_resub as br
    db = SessionLocal()
    try:
        br.ensure_tables(db.get_bind())
        for b in db.query(br.ReconImportBatch).filter(br.ReconImportBatch.status.in_(("Queued", "Fetching", "Matching"))):
            b.status, b.error = "Failed", "Interrupted by app restart — queue the fetch again."
        db.commit()
    finally:
        db.close()


def _prepare_doc_review() -> None:
    """Documentation review tables (and the case-sheet columns on scrub claims); interrupted runs marked failed."""
    from .services import doc_review as dr
    dr.ensure_tables(SessionLocal().get_bind())
    dr.resume_on_startup()


@app.on_event("startup")
def on_startup() -> None:
    init_db()
    db = SessionLocal()
    try:
        run_seed(db)
        _cleanup_stale_jobs(db)
    finally:
        db.close()
    packs.resume_on_startup()
    _prepare_bulk_resub()
    _prepare_doc_review()
    from .services import scrub_inbox
    scrub_inbox.start()                        # ~/Downloads/*to Scrub* → Claim scrubber sessions
    threading.Thread(target=_warm_caches, daemon=True).start()


def _warm_caches() -> None:
    """Fill the Dashboard (all facilities, last 12 months) and Analytics filter caches so first visits are fast;
    repeated hourly because both caches expire after an hour."""
    import time
    from .models import User
    while True:
        db = SessionLocal()
        try:
            admin = db.query(User).filter_by(role="admin").first()
            if admin:
                analytics._FILTER_CACHE.clear()
                dashboard._cache.clear()
                analytics.analytics_filter_options(db=db, _=admin)
                dashboard.claim_cycle(None, None, None, db, admin)
                # Dashboard table tabs, default view (all facilities, last 12 months by submission date)
                from datetime import date, timedelta
                d1 = date.today()
                d0 = (d1.replace(day=1) - timedelta(days=335)).replace(day=1)
                analytics._REPORT_CACHE.clear()
                f = analytics.get_analytics_filters(date_from=d0, date_to=d1)
                for rep in (analytics.insurance_report, analytics.clinicians_report, analytics.department_report,
                            analytics.denials_report, analytics.submissions_report):
                    rep(f=f, db=db, _=admin)
        except Exception as e:  # never take the app down for a cache
            print(f"cache warm-up failed: {e}")
        finally:
            db.close()
        time.sleep(3300)


def _cleanup_stale_jobs(db) -> None:
    """
    Mark any fetch_log rows stuck in 'running' as 'failed'.
    Mark any report_jobs stuck in 'pending' as 'error' (server restart mid-generation).
    Runs on every startup so interrupted jobs are never left in a permanent running state.
    """
    from datetime import datetime, timedelta
    from .models import FetchLog, ReportJob

    now = datetime.utcnow()
    cutoff = now - timedelta(hours=2)

    # ── FetchLog cleanup ─────────────────────────────────────────────────────
    stale = (
        db.query(FetchLog)
        .filter(FetchLog.status == "running", FetchLog.started_at < cutoff)
        .all()
    )
    if stale:
        for log in stale:
            log.status = "failed"
            log.completed_at = now
            log.error_detail = {
                "error": "Job timed out or was interrupted (server restart / browser closed). Re-run manually."
            }
        db.commit()
        print(f"[startup] Cleaned up {len(stale)} stale fetch job(s).")

    # ── ReportJob recovery ───────────────────────────────────────────────────
    # A report still 'pending' at startup was queued but never built (the server restarted first).
    # Recent ones (< 1 day) are rebuilt in the background; older ones are marked interrupted.
    report_cutoff = now - timedelta(days=1)
    pending = db.query(ReportJob).filter(ReportJob.status == "pending").all()
    # Only one automatic retry: a report that was already resumed and is pending again most likely killed
    # the server itself (e.g. out of memory), so resuming it again would crash the backend in a loop.
    RESUMED_MARK = "resumed after restart"
    resume = [rj for rj in pending if rj.created_at and rj.created_at.replace(tzinfo=None) >= report_cutoff
              and RESUMED_MARK not in (rj.error_message or "")]
    for rj in pending:
        if rj in resume:
            rj.error_message = RESUMED_MARK      # cleared when the job completes
        else:
            rj.status        = "error"
            rj.error_message = ("Report generation was interrupted twice (server restart / out of memory). "
                                "Please regenerate with a shorter date range or fewer facilities.")
            rj.completed_at  = now
    db.commit()
    if resume:
        import json, threading
        from .routers.reports import REPORT_BUILDERS, _run_report_background
        for rj in resume:
            builder = REPORT_BUILDERS.get(rj.report_type)
            if not builder:
                continue
            f = json.loads(rj.filters_json or "{}")
            threading.Thread(
                target=_run_report_background, daemon=True,
                args=(rj.id, builder, f.get("facility_id"), f.get("payer_id"), f.get("encounter_type"),
                      f.get("date_from"), f.get("date_to"), f.get("date_field") or "treatment_date"),
                kwargs={"facility_ids": f.get("facility_ids")},
            ).start()
        print(f"[startup] Resumed {len(resume)} queued report job(s).")


@app.get("/api/health", tags=["health"])
def health_check():
    return {"status": "ok", "app": settings.APP_NAME, "environment": settings.ENVIRONMENT}


app.add_middleware(GZipMiddleware, minimum_size=50_000)  # large worklist / report payloads

app.include_router(auth.router)
app.include_router(clinical_profiles.router)
app.include_router(dha_sync.router)
app.include_router(analytics.router)
app.include_router(facilities.router)
app.include_router(reports.router)
app.include_router(portal.router, prefix="/api")  # /api/portal/* — no auth middleware
app.include_router(receipts.router)               # /api/receipts/* — JWT-protected
app.include_router(scrub.router)                  # /api/scrub/*   — JWT-protected
app.include_router(calibration.router)            # /api/calibration/* — JWT-protected
app.include_router(resub.router)                  # /api/resub/* — JWT-protected
app.include_router(doc_review.router)             # /api/docreview/* — JWT-protected
app.include_router(dashboard.router)              # /api/dashboard/* — JWT-protected
app.include_router(bulk_resub.router)             # /api/resub/bulk/* — JWT-protected
app.include_router(turnaround.router)             # /api/turnaround/* — JWT-protected
app.include_router(denial_trends.router)          # /api/denial-trends/* — JWT-protected
app.include_router(alerts.router)                 # /api/alerts — JWT-protected


@app.on_event("startup")
def _warm_alerts():
    alerts.warm()

app.include_router(packs.router)                  # /api/packs/* — JWT-protected

# ── Serve React frontend (built with `npm run build`) ────────────────────────
# Routes are always registered so they survive uvicorn --reload cycles where
# the dist folder may not exist at the exact moment the module is imported.
_DIST = Path(__file__).parent.parent.parent / "frontend" / "dist"

# Mount /assets only if the directory exists right now (safe to add later via restart)
if (_DIST / "assets").exists():
    app.mount("/assets", StaticFiles(directory=str(_DIST / "assets")), name="assets")

_NO_CACHE = {"Cache-Control": "no-store, no-cache, must-revalidate"}


@app.get("/", include_in_schema=False)
def serve_root():
    """Serve SPA root."""
    index = _DIST / "index.html"
    if index.exists():
        return FileResponse(str(index), headers=_NO_CACHE)
    return {"detail": "Frontend not built — run `npm run build` in frontend/"}


@app.get("/{full_path:path}", include_in_schema=False)
def serve_spa(full_path: str):
    """Catch-all: serve index.html for any non-API route (SPA routing)."""
    # Unknown API paths get a JSON 404, never the SPA shell.
    if full_path.startswith("api/"):
        raise HTTPException(status_code=404, detail="Not found")
    # Resolve and contain inside dist/ so encoded "../" can't read files elsewhere
    # on disk (e.g. backend/.env).
    dist_root = _DIST.resolve()
    file_path = (dist_root / full_path).resolve()
    if file_path.is_relative_to(dist_root) and file_path.is_file():
        return FileResponse(str(file_path), headers=_NO_CACHE)
    index = _DIST / "index.html"
    if index.exists():
        return FileResponse(str(index), headers=_NO_CACHE)
    return {"detail": "Frontend not built — run `npm run build` in frontend/"}
