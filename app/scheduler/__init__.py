"""Планировщик задач PlanZy (APScheduler).

Содержит сервис рассылки напоминаний по правилам из БД и фабрику
``AsyncIOScheduler``, работающую в общем event loop с FastAPI и aiogram.
"""

from app.scheduler.dispatch_service import DispatchResult, MorningDispatchService
from app.scheduler.scheduler import (
    DISPATCH_TICK_JOB_ID,
    DISPATCH_TICK_MINUTES,
    create_scheduler,
    register_jobs,
    shutdown_scheduler,
    start_scheduler,
)

__all__ = [
    "DISPATCH_TICK_JOB_ID",
    "DISPATCH_TICK_MINUTES",
    "DispatchResult",
    "MorningDispatchService",
    "create_scheduler",
    "register_jobs",
    "shutdown_scheduler",
    "start_scheduler",
]
