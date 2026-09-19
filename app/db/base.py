"""Базовый модуль базы данных.

Содержит DeclarativeBase, настройку async-движка SQLAlchemy,
фабрику сессий и включение WAL-режима для SQLite.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)


class Base(DeclarativeBase):
    """Базовый класс для всех ORM-моделей PlanZy."""


def _configure_sqlite(engine: AsyncEngine) -> None:
    """Включает WAL-режим и нужные PRAGMA для SQLite.

    WAL (Write-Ahead Logging) позволяет читателям работать параллельно
    с писателем — критично, когда бот пишет урок, а родитель открывает
    публичную страницу.

    busy_timeout заставляет SQLite ждать освобождения блокировки вместо
    мгновенной ошибки "database is locked".

    foreign_keys=ON включает проверку внешних ключей (в SQLite по умолчанию выключена).
    """
    if not settings.database_url.startswith("sqlite"):
        return

    @event.listens_for(engine.sync_engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, connection_record) -> None:  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA synchronous=NORMAL")
        finally:
            cursor.close()


def create_engine() -> AsyncEngine:
    """Создаёт async-движок SQLAlchemy с настройками под SQLite/PostgreSQL."""
    is_sqlite = settings.database_url.startswith("sqlite")

    engine_kwargs: dict = {
        "echo": False,
        "future": True,
    }

    if is_sqlite:
        # SQLite не поддерживает пул соединений в привычном смысле
        engine_kwargs["connect_args"] = {"check_same_thread": False}
    else:
        # Для PostgreSQL — разумные размеры пула
        engine_kwargs["pool_size"] = 5
        engine_kwargs["max_overflow"] = 10
        engine_kwargs["pool_pre_ping"] = True

    engine = create_async_engine(settings.database_url, **engine_kwargs)
    _configure_sqlite(engine)
    return engine


# Глобальный движок и фабрика сессий
engine: AsyncEngine = create_engine()

async_session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


@asynccontextmanager
async def get_session() -> AsyncIterator[AsyncSession]:
    """Контекстный менеджер сессии БД.

    Автоматически откатывает транзакцию при ошибке и закрывает сессию.

    Пример:
        async with get_session() as session:
            result = await session.execute(...)
    """
    session = async_session_factory()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


async def init_db() -> None:
    """Создаёт все таблицы по ORM-моделям.

    Используется для быстрого старта и в тестах.
    В продакшене схема управляется через Alembic.
    """
    # Импорт моделей обязателен, чтобы они зарегистрировались в metadata
    from app.db import models  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    logger.info("Схема базы данных инициализирована")


async def check_db_connection() -> bool:
    """Проверяет доступность базы данных."""
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("Ошибка подключения к БД: %s", exc)
        return False


async def dispose_engine() -> None:
    """Корректно закрывает движок и освобождает соединения."""
    await engine.dispose()
    logger.info("Соединения с БД закрыты")
