"""Общие фикстуры pytest для тестов PlanZy.

Каждый тест получает изолированную in-memory базу SQLite со свежей схемой
и готовый Repository. Боевая база при этом не затрагивается.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models import Tenant  # noqa: F401  (регистрация моделей в metadata)
from app.db.repository import Repository

# In-memory SQLite: статический пул обязателен, иначе каждое соединение
# получит собственную пустую базу и таблицы «исчезнут».
TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"

# telegram_user_id тестового тенанта (используется фикстурами tenant_id
# и tenant_telegram_id).
TEST_TELEGRAM_ID = 1001


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """Создаёт изолированную сессию с чистой схемой БД.

    Yields:
        AsyncSession, привязанная к in-memory SQLite.
    """
    engine = create_async_engine(
        TEST_DATABASE_URL,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    # SQLite по умолчанию не применяет ON DELETE CASCADE — включаем
    # внешние ключи, чтобы поведение совпадало с боевым движком
    # (см. ``app.db.base._configure_sqlite``).
    @event.listens_for(engine.sync_engine, "connect")
    def _enable_foreign_keys(dbapi_connection, connection_record) -> None:  # noqa: ANN001, ARG001
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )
    async with factory() as db_session:
        yield db_session

    await engine.dispose()


@pytest_asyncio.fixture
async def repo(session: AsyncSession) -> Repository:
    """Возвращает Repository поверх тестовой сессии."""
    return Repository(session)


@pytest_asyncio.fixture
async def tenant_id(repo: Repository) -> int:
    """Создаёт тестового тенанта и возвращает его id."""
    tenant = await repo.tenants.create(name="Тестовый тренер", telegram_user_id=TEST_TELEGRAM_ID)
    await repo.session.flush()
    return tenant.id


@pytest_asyncio.fixture
async def tenant_telegram_id() -> int:
    """Возвращает telegram_user_id тестового тенанта.

    Нужен тестам, которые прогоняют событие через Dispatcher: whitelist
    middleware сопоставляет отправителя с тенантом по этому идентификатору.
    """
    return TEST_TELEGRAM_ID


@pytest.fixture
def anyio_backend() -> str:
    """Ограничивает anyio бэкендом asyncio."""
    return "asyncio"
