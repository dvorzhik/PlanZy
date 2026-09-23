"""Настройка APScheduler для PlanZy.

Создаёт ``AsyncIOScheduler``, работающий в том же event loop, что FastAPI
и aiogram. Регистрирует единственную задачу — периодическую проверку
правил напоминаний.

Время отправки берётся не из расписания cron, а из правил тренера
(``reminder_rules.time_of_day``): задача-тик просыпается каждые
``DISPATCH_TICK_MINUTES`` минут и отдаёт работу
:class:`~app.scheduler.dispatch_service.MorningDispatchService`.

Защита от дублирования реализована на двух уровнях:
    1. ``max_instances=1`` и ``coalesce=True`` — APScheduler не запустит
       задачу повторно, пока предыдущая не завершилась;
    2. журнал ``DispatchLog`` в БД — сервис не отправит одно и то же
       напоминание дважды, даже если задача была вызвана вручную или
       процесс перезапустился.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.core.logging import get_logger

if TYPE_CHECKING:
    from aiogram import Bot

    from app.scheduler.dispatch_service import DispatchResult

logger = get_logger(__name__)

# Идентификатор задачи периодической проверки правил напоминаний.
DISPATCH_TICK_JOB_ID = "reminder_tick"

# Периодичность проверки правил, в минутах.
DISPATCH_TICK_MINUTES = 5


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
            "misfire_grace_time": 300,
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
        job_id=DISPATCH_TICK_JOB_ID,
        name="Проверка правил напоминаний",
        func=_run_dispatch_tick,
        minute=f"*/{DISPATCH_TICK_MINUTES}",
        bot=bot,
    )


def _add_job(
    scheduler: AsyncIOScheduler,
    *,
    job_id: str,
    name: str,
    func: Callable[..., Any],
    minute: str,
    bot: Bot,
) -> None:
    """Добавляет одну cron-задачу и логирует её расписание.

    Args:
        scheduler: Планировщик APScheduler.
        job_id: Идентификатор задачи.
        name: Человекочитаемое имя задачи.
        func: Корутина задачи.
        minute: Минуты запуска (cron-выражение, например ``*/5``).
        bot: Экземпляр aiogram-бота.
    """
    job = scheduler.add_job(
        func,
        trigger=CronTrigger(
            minute=minute,
            timezone=settings.timezone,
        ),
        id=job_id,
        name=name,
        replace_existing=True,
        kwargs={"bot": bot},
    )
    logger.info(
        "Задача %r зарегистрирована (минуты: %s, %s), ближайший запуск: %s",
        job_id,
        minute,
        settings.timezone,
        # До старта планировщика job ещё не в очереди и next_run_time отсутствует.
        getattr(job, "next_run_time", None) or "будет рассчитан при запуске",
    )


async def _run_dispatch_tick(bot: Bot) -> DispatchResult | None:
    """Обёртка периодической проверки правил напоминаний.

    Открывает собственную сессию БД, так как выполняется вне HTTP-запроса.

    Args:
        bot: Экземпляр aiogram-бота.

    Returns:
        Итог рассылки (всегда, включая пустой прогон).
    """
    from app.db.base import get_session
    from app.db.repository import Repository
    from app.scheduler.dispatch_service import MorningDispatchService

    async with get_session() as session:
        service = MorningDispatchService(Repository(session), bot)
        return await service.run_due()


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
