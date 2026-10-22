"""
Ghaf App — FastAPI application entrypoint.

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

from .config import get_settings
from .database import SessionLocal, init_db
from .routers import analytics, auth, calibration, clinical_profiles, dha_sync, facilities, reports, portal, receipts, resub, scrub
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


@app.on_event("startup")
def on_startup() -> None:
    init_db()
    db = SessionLocal()
    try:
        run_seed(db)
        _cleanup_stale_jobs(db)
    finally:
        db.close()


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

    # ── ReportJob cleanup ────────────────────────────────────────────────────
    # Any report_job stuck in 'pending' for >10 min means the server restarted
    # mid-generation. Mark as 'error' so the frontend stops polling.
    report_cutoff = now - timedelta(minutes=10)
    stale_reports = (
        db.query(ReportJob)
        .filter(ReportJob.status == "pending", ReportJob.created_at < report_cutoff)
        .all()
    )
    if stale_reports:
        for rj in stale_reports:
            rj.status        = "error"
            rj.error_message = "Report generation was interrupted (server restart). Please regenerate."
            rj.completed_at  = now
        db.commit()
        print(f"[startup] Cleaned up {len(stale_reports)} stale report job(s).")


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
