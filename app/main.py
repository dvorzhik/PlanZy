"""Единая точка входа PlanZy.

Запускает в одном процессе и одном event loop:

    * FastAPI (веб-приложение: публичные карточки, досье, health-check);
    * aiogram (Telegram-бот, polling как фоновая задача);
    * APScheduler (утренняя рассылка напоминаний).

Все ресурсы инициализируются в ``lifespan`` и корректно освобождаются
при остановке приложения.

Запуск для разработки::

    python -m app.main

Запуск для продакшена (systemd)::

    uvicorn app.main:app --host 127.0.0.1 --port 8000
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from aiogram import Bot, Dispatcher
from fastapi import FastAPI

from app.bot.dispatcher import create_bot, create_dispatcher
from app.bot.handlers import set_bot_commands
from app.core.config import settings
from app.core.logging import get_logger, setup_logging
from app.db.base import check_db_connection, dispose_engine, init_db
from app.scheduler.scheduler import (
    create_scheduler,
    register_jobs,
    shutdown_scheduler,
    start_scheduler,
)
from app.web.app import create_app

logger = get_logger(__name__)


async def _start_bot_polling(bot: Bot, dispatcher: Dispatcher) -> asyncio.Task[None]:
    """Запускает polling бота как фоновую задачу.

    Args:
        bot: Экземпляр aiogram-бота.
        dispatcher: Настроенный Dispatcher.

    Returns:
        Задача asyncio, выполняющая polling.
    """
    # Удаляем вебхук, если он был установлен ранее — иначе polling не стартует.
    await bot.delete_webhook(drop_pending_updates=True)
    task = asyncio.create_task(
        dispatcher.start_polling(bot, handle_signals=False),
        name="planzy-bot-polling",
    )
    logger.info("Polling Telegram-бота запущен фоновой задачей")
    return task


async def _stop_bot_polling(bot: Bot, dispatcher: Dispatcher, task: asyncio.Task[None]) -> None:
    """Останавливает polling и закрывает сессию бота.

    Args:
        bot: Экземпляр aiogram-бота.
        dispatcher: Настроенный Dispatcher.
        task: Задача polling, запущенная ранее.
    """
    with suppress(Exception):
        await dispatcher.stop_polling()
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
    await bot.session.close()
    logger.info("Polling Telegram-бота остановлен")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Управляет жизненным циклом ресурсов приложения.

    Порядок запуска:
        1. Проверка соединения с БД и инициализация схемы (для dev).
        2. Создание бота, диспетчера и запуск polling.
        3. Создание и запуск планировщика.

    Порядок остановки — обратный.

    Args:
        app: Экземпляр FastAPI.

    Yields:
        None: управление передаётся приложению до остановки.
    """
    setup_logging()
    logger.info("Запуск PlanZy (env=%s)", settings.app_env)

    if not await check_db_connection():
        logger.error("База данных недоступна — запуск прерван")
        raise RuntimeError("База данных недоступна")

    # В dev-режиме схема создаётся автоматически; в проде — только Alembic.
    if not settings.is_production:
        await init_db()

    bot = create_bot()
    dispatcher = create_dispatcher()
    await set_bot_commands(bot)
    polling_task = await _start_bot_polling(bot, dispatcher)

    scheduler = create_scheduler()
    register_jobs(scheduler, bot)
    start_scheduler(scheduler)

    app.state.bot = bot
    app.state.dispatcher = dispatcher
    app.state.scheduler = scheduler

    logger.info("PlanZy готов к работе")

    try:
        yield
    finally:
        logger.info("Остановка PlanZy")
        shutdown_scheduler(scheduler, wait=False)
        await _stop_bot_polling(bot, dispatcher, polling_task)
        await dispose_engine()
        logger.info("PlanZy остановлен")


def create_application() -> FastAPI:
    """Собирает единое FastAPI-приложение с lifespan.

    Returns:
        Настроенное приложение PlanZy.
    """
    app = create_app()
    app.router.lifespan_context = lifespan
    return app


# Экземпляр для uvicorn: ``uvicorn app.main:app``.
app = create_application()


def main() -> None:
    """Запускает приложение через uvicorn (для ``python -m app.main``)."""
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.web_host,
        port=settings.web_port,
        log_config=None,
        reload=False,
    )


if __name__ == "__main__":
    main()
