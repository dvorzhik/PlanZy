"""Планировщик задач PlanZy (APScheduler).

Содержит утренний диспетчер напоминаний и фабрику ``AsyncIOScheduler``,
работающую в общем event loop с FastAPI и aiogram.
"""

from app.scheduler.dispatch_service import DispatchResult, MorningDispatchService
from app.scheduler.scheduler import (
    MORNING_DISPATCH_JOB_ID,
    create_scheduler,
    register_jobs,
    shutdown_scheduler,
    start_scheduler,
)

__all__ = [
    "MORNING_DISPATCH_JOB_ID",
    "DispatchResult",
    "MorningDispatchService",
    "create_scheduler",
    "register_jobs",
    "shutdown_scheduler",
    "start_scheduler",
]
