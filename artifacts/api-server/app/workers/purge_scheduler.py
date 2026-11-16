"""Periodic seven-day retention purge."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import delete, select

from app.core.config import get_settings
from app.core.database import session_scope
from app.models.domain import GrievanceCase, IdempotencyRecord
from app.services.case_service import purge_case_data

logger = logging.getLogger("nyaya.purge")
_scheduler: BackgroundScheduler | None = None


def purge_expired_cases() -> int:
    now = datetime.now(timezone.utc)
    purged = 0
    with session_scope() as session:
        case_references = session.scalars(
            select(GrievanceCase.case_reference).where(
                GrievanceCase.expires_at <= now,
                GrievanceCase.status != "PURGED",
            )
        ).all()
        session.execute(delete(IdempotencyRecord).where(IdempotencyRecord.expires_at <= now))
    for reference in case_references:
        try:
            with session_scope() as session:
                case = session.scalar(
                    select(GrievanceCase).where(GrievanceCase.case_reference == reference)
                )
                if case and case.status != "PURGED":
                    purge_case_data(session, case, actor="retention_worker")
                    purged += 1
        except Exception:
            logger.exception("case_retention_purge_failed", extra={"case_reference": reference})
    if purged:
        logger.info("expired_cases_purged", extra={"event": "purge", "count": purged})
    return purged


def start_purge_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        return
    _scheduler = BackgroundScheduler(timezone="UTC", daemon=True)
    _scheduler.add_job(purge_expired_cases, "interval", hours=1, id="case-retention-purge", max_instances=1, coalesce=True)
    _scheduler.start()


def stop_purge_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
