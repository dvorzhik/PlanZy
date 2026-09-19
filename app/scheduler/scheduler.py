"""Настройка APScheduler для PlanZy.

Создаёт ``AsyncIOScheduler``, работающий в том же event loop, что FastAPI
и aiogram. Регистрирует единственную задачу — утреннюю рассылку напоминаний.

Защита от дублирования реализована на двух уровнях:
    1. ``max_instances=1`` и ``coalesce=True`` — APScheduler не запустит
       задачу повторно, пока предыдущая не завершилась;
    2. ``_last_dispatch_date`` — сервис не отправит рассылку дважды за день,
       даже если job был вызван вручную или после перезапуска процесса.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from typing import TYPE_CHECKING, Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.core.logging import get_logger

if TYPE_CHECKING:
    from aiogram import Bot

    from app.scheduler.dispatch_service import DispatchResult

logger = get_logger(__name__)

# Идентификатор задачи утренней рассылки.
MORNING_DISPATCH_JOB_ID = "morning_dispatch"

# Идентификатор задачи вечерней рассылки накануне занятий.
DAY_BEFORE_DISPATCH_JOB_ID = "day_before_dispatch"

# Дата последней успешной утренней рассылки — защита от дублирования в процессе.
_last_dispatch_date: date | None = None

# Дата последней успешной вечерней рассылки — защита от дублирования в процессе.
_last_day_before_date: date | None = None


def _reset_dispatch_guard() -> None:
    """Сбрасывает защиту от дублирования (используется в тестах)."""
    global _last_dispatch_date, _last_day_before_date  # noqa: PLW0603
    _last_dispatch_date = None
    _last_day_before_date = None


def get_last_dispatch_date() -> date | None:
    """Возвращает дату последней утренней рассылки.

    Returns:
        Дата последней рассылки или None.
    """
    return _last_dispatch_date


def get_last_day_before_date() -> date | None:
    """Возвращает дату последней вечерней рассылки.

    Returns:
        Дата последней рассылки или None.
    """
    return _last_day_before_date


def create_scheduler() -> AsyncIOScheduler:
    """Создаёт ``AsyncIOScheduler`` с настройками по умолчанию.

    Returns:
        Готовый планировщик (ещё не запущенный).
    """
    scheduler = AsyncIOScheduler(
        timezone=settings.timezone,
        job_defaults={
            "coalesce": True,
            "max_instances": 1,
            "misfire_grace_time": 3600,
        },
    )
    logger.debug("Создан AsyncIOScheduler (timezone=%s)", settings.timezone)
    return scheduler


def register_jobs(scheduler: AsyncIOScheduler, bot: Bot) -> None:
    """Регистрирует задачи в планировщике.

    После регистрации логирует фактическое время следующего запуска: если
    задача по каким-то причинам не встала в очередь, это видно в логе сразу,
    а не только по отсутствию напоминаний.

    Args:
        scheduler: Планировщик APScheduler.
        bot: Экземпляр aiogram-бота для отправки напоминаний.
    """
    _add_job(
        scheduler,
        job_id=MORNING_DISPATCH_JOB_ID,
        name="Утренняя рассылка напоминаний",
        func=_run_morning_dispatch,
        hour=settings.dispatch_hour,
        minute=settings.dispatch_minute,
        bot=bot,
    )
    _add_job(
        scheduler,
        job_id=DAY_BEFORE_DISPATCH_JOB_ID,
        name="Вечерняя рассылка накануне занятий",
        func=_run_day_before_dispatch,
        hour=settings.day_before_hour,
        minute=settings.day_before_minute,
        bot=bot,
    )


def _add_job(
    scheduler: AsyncIOScheduler,
    *,
    job_id: str,
    name: str,
    func: Callable[..., Any],
    hour: int,
    minute: int,
    bot: Bot,
) -> None:
    """Добавляет одну cron-задачу и логирует её расписание.

    Args:
        scheduler: Планировщик APScheduler.
        job_id: Идентификатор задачи.
        name: Человекочитаемое имя задачи.
        func: Корутина задачи.
        hour: Час запуска.
        minute: Минуты запуска.
        bot: Экземпляр aiogram-бота.
    """
    job = scheduler.add_job(
        func,
        trigger=CronTrigger(
            hour=hour,
            minute=minute,
            timezone=settings.timezone,
        ),
        id=job_id,
        name=name,
        replace_existing=True,
        kwargs={"bot": bot},
    )
    logger.info(
        "Задача %r зарегистрирована на %02d:%02d (%s), ближайший запуск: %s",
        job_id,
        hour,
        minute,
        settings.timezone,
        # До старта планировщика job ещё не в очереди и next_run_time отсутствует.
        getattr(job, "next_run_time", None) or "будет рассчитан при запуске",
    )


async def _run_morning_dispatch(bot: Bot) -> DispatchResult | None:
    """Обёртка запуска утренней рассылки с защитой от дублирования.

    Открывает собственную сессию БД, так как выполняется вне HTTP-запроса.

    Args:
        bot: Экземпляр aiogram-бота.

    Returns:
        Итог рассылки или None, если рассылка уже выполнялась сегодня.
    """
    global _last_dispatch_date  # noqa: PLW0603

    from app.db.base import get_session
    from app.db.repository import Repository
    from app.scheduler.dispatch_service import MorningDispatchService

    async with get_session() as session:
        service = MorningDispatchService(Repository(session), bot)
        today = service.today()

        if _last_dispatch_date == today:
            logger.info("Рассылка на %s уже выполнялась — пропуск", today.isoformat())
            return None

        result = await service.run(dispatch_date=today)
        _last_dispatch_date = today
        return result


async def _run_day_before_dispatch(bot: Bot) -> DispatchResult | None:
    """Обёртка запуска вечерней рассылки с защитой от дублирования.

    Открывает собственную сессию БД, так как выполняется вне HTTP-запроса.

    Args:
        bot: Экземпляр aiogram-бота.

    Returns:
        Итог рассылки или None, если рассылка уже выполнялась сегодня.
    """
    global _last_day_before_date  # noqa: PLW0603

    from app.db.base import get_session
    from app.db.repository import Repository
    from app.scheduler.dispatch_service import MorningDispatchService

    async with get_session() as session:
        service = MorningDispatchService(Repository(session), bot)
        today = service.today()

        if _last_day_before_date == today:
            logger.info("Вечерняя рассылка на %s уже выполнялась — пропуск", today.isoformat())
            return None

        result = await service.run_day_before(dispatch_date=today)
        _last_day_before_date = today
        return result


def start_scheduler(scheduler: AsyncIOScheduler) -> None:
    """Запускает планировщик, если он ещё не запущен.

    Args:
        scheduler: Планировщик APScheduler.
    """
    if scheduler.running:
        logger.debug("Планировщик уже запущен")
        return
    scheduler.start()
    logger.info("Планировщик запущен")


def shutdown_scheduler(scheduler: AsyncIOScheduler, *, wait: bool = False) -> None:
    """Корректно останавливает планировщик.

    Args:
        scheduler: Планировщик APScheduler.
        wait: Дождаться завершения выполняющихся задач.
    """
    if not scheduler.running:
        logger.debug("Планировщик не запущен — остановка не требуется")
        return
    scheduler.shutdown(wait=wait)
    logger.info("Планировщик остановлен")
