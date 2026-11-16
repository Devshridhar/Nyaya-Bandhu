"""Nyaya Bandhu FastAPI application."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from app.api.v1 import router as api_router
from app.core.config import get_settings
from app.core.database import Base, engine, session_scope
from app.core.logging_config import configure_logging, request_context_middleware
from app.core.rate_limit import limiter
from app.models import domain as _domain  # register SQLAlchemy models
from app.services.nodal_directory import seed_nodal_directory
from app.workers.purge_scheduler import start_purge_scheduler, stop_purge_scheduler

settings = get_settings()
configure_logging()
logger = logging.getLogger("nyaya")


@asynccontextmanager
async def lifespan(_: FastAPI):
    if not settings.is_production:
        Base.metadata.create_all(bind=engine)
    with session_scope() as session:
        seed_nodal_directory(session)
    start_purge_scheduler()
    try:
        yield
    finally:
        stop_purge_scheduler()


app = FastAPI(
    title="Nyaya Bandhu",
    version="1.0.0",
    description="Secure consumer-grievance intake and filing assistance.",
    lifespan=lifespan,
    docs_url="/api/docs" if not settings.is_production else None,
    redoc_url=None,
)
app.state.limiter = limiter
app.add_middleware(SlowAPIMiddleware)
app.middleware("http")(request_context_middleware)
app.include_router(api_router, prefix="/api/v1")


@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
    return JSONResponse(status_code=429, content={"detail": "Rate limit exceeded. Please try again later."})


@app.exception_handler(Exception)
async def unexpected_error_handler(request: Request, exc: Exception):
    logger.exception("unhandled_request_error", extra={"path": request.url.path})
    return JSONResponse(status_code=500, content={"detail": "An internal error occurred.", "request_id": getattr(request.state, "request_id", None)})


@app.get("/healthz", tags=["system"])
async def healthz():
    return {"status": "ok", "service": "nyaya-bandhu"}


@app.get("/readyz", tags=["system"])
async def readyz():
    from app.services.storage_service import storage
    from app.services.ai_client import ai_configured

    def _check_connections():
        from sqlalchemy import text
        db_ok = False
        storage_ok = False
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
            db_ok = True
        except Exception:
            logger.exception("readiness_database_check_failed")
        try:
            storage.list_objects(max_results=1)
            storage_ok = True
        except Exception:
            logger.exception("readiness_storage_check_failed")
        return db_ok, storage_ok

    db_ok, storage_ok = await run_in_threadpool(_check_connections)
    checks = {
        "database": {"ok": db_ok, "kind": "postgresql" if settings.is_postgres else "sqlite"},
        "storage": {"ok": storage_ok, "kind": storage.kind},
        "encryption": {"ok": settings.crypto_ready},
        "openai": {"ok": ai_configured()},
        "bhashini_fallback": {"configured": bool(settings.bhashini_user_id and settings.bhashini_ulca_api_key and settings.bhashini_inference_key and settings.bhashini_service_id)},
        "sarvam_fallback": {"configured": bool(settings.sarvam_api_key)},
        "email": {"configured": bool(settings.resend_api_key or settings.smtp_host), "mode": settings.email_mode},
        "whatsapp": {
            "configured": bool(settings.twilio_account_sid and settings.twilio_auth_token and settings.twilio_whatsapp_number),
            "signature_enforced": settings.enforce_twilio_signature,
            "webhook_path": "/api/v1/webhooks/whatsapp",
        },
    }
    ready = db_ok and storage_ok and settings.crypto_ready
    return JSONResponse(status_code=200 if ready else 503, content={"status": "ready" if ready else "degraded", "checks": checks})


@app.get("/", include_in_schema=False)
async def operator_console():
    path = Path(__file__).resolve().parents[1] / "static" / "index.html"
    return FileResponse(path, media_type="text/html")
