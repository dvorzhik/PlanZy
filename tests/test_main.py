"""Тесты единой точки входа PlanZy (``app.main``).

Проверяем:
    * сборку приложения с lifespan;
    * порядок инициализации ресурсов (БД, бот, планировщик);
    * корректное освобождение ресурсов при остановке;
    * поведение при недоступной БД.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Any

import pytest

from app import main as main_module


class _StubSession:
    """Заглушка сессии бота."""

    def __init__(self) -> None:
        """Инициализирует флаг закрытия."""
        self.closed = False

    async def close(self) -> None:
        """Помечает сессию закрытой."""
        self.closed = True


class _StubBot:
    """Заглушка aiogram-бота."""

    def __init__(self) -> None:
        """Создаёт заглушку сессии."""
        self.session = _StubSession()
        self.webhook_deleted = False

    async def delete_webhook(self, *, drop_pending_updates: bool = False) -> None:
        """Запоминает факт удаления вебхука."""
        self.webhook_deleted = drop_pending_updates


class _StubDispatcher:
    """Заглушка aiogram-Dispatcher."""

    def __init__(self) -> None:
        """Инициализирует флаги состояния."""
        self.polling_started = False
        self.polling_stopped = False

    async def start_polling(self, bot: Any, **kwargs: Any) -> None:
        """Имитирует бесконечный polling до отмены задачи."""
        self.polling_started = True
        await asyncio.sleep(3600)

    async def stop_polling(self) -> None:
        """Помечает polling остановленным."""
        self.polling_stopped = True


class _StubScheduler:
    """Заглушка APScheduler."""

    def __init__(self) -> None:
        """Инициализирует флаги состояния."""
        self.running = False
        self.jobs: list[str] = []

    def add_job(self, func: Any, **kwargs: Any) -> None:
        """Запоминает добавленную задачу."""
        self.jobs.append(kwargs.get("id", "unknown"))

    def start(self) -> None:
        """Помечает планировщик запущенным."""
        self.running = True

    def shutdown(self, wait: bool = False) -> None:
        """Помечает планировщик остановленным."""
        self.running = False


@pytest.fixture
def patched_lifespan(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Подменяет внешние зависимости lifespan заглушками.

    Args:
        monkeypatch: Фикстура pytest для подмены атрибутов.

    Returns:
        Словарь с созданными заглушками для проверок.
    """
    bot = _StubBot()
    dispatcher = _StubDispatcher()
    scheduler = _StubScheduler()
    state: dict[str, Any] = {
        "bot": bot,
        "dispatcher": dispatcher,
        "scheduler": scheduler,
        "db_checked": False,
        "db_disposed": False,
        "db_initialized": False,
    }

    async def _check_db() -> bool:
        state["db_checked"] = True
        return True

    async def _init_db() -> None:
        state["db_initialized"] = True

    async def _dispose() -> None:
        state["db_disposed"] = True

    monkeypatch.setattr(main_module, "check_db_connection", _check_db)
    monkeypatch.setattr(main_module, "init_db", _init_db)
    monkeypatch.setattr(main_module, "dispose_engine", _dispose)
    monkeypatch.setattr(main_module, "create_bot", lambda: bot)
    monkeypatch.setattr(main_module, "create_dispatcher", lambda: dispatcher)
    monkeypatch.setattr(main_module, "create_scheduler", lambda: scheduler)
    monkeypatch.setattr(
        main_module,
        "register_jobs",
        lambda sched, b: sched.add_job(None, id="morning_dispatch"),
    )
    monkeypatch.setattr(main_module, "start_scheduler", lambda sched: sched.start())
    monkeypatch.setattr(
        main_module,
        "shutdown_scheduler",
        lambda sched, wait=False: sched.shutdown(wait=wait),
    )
    return state


class TestApplicationFactory:
    """Проверки сборки приложения."""

    def test_create_application_returns_fastapi(self) -> None:
        """Фабрика возвращает FastAPI с установленным lifespan."""
        app = main_module.create_application()
        assert app.title == "PlanZy"
        assert app.router.lifespan_context is main_module.lifespan

    def test_module_level_app_exists(self) -> None:
        """Модульный ``app`` доступен для uvicorn."""
        assert main_module.app is not None
        assert main_module.app.title == "PlanZy"


class TestLifespan:
    """Проверки жизненного цикла ресурсов."""

    async def test_lifespan_starts_and_stops_resources(
        self,
        patched_lifespan: dict[str, Any],
    ) -> None:
        """Lifespan запускает и корректно останавливает все ресурсы."""
        app = main_module.create_application()

        async with main_module.lifespan(app):
            # ``asyncio.create_task`` только планирует корутину — даём
            # event loop прокрутиться, чтобы polling реально стартовал.
            await asyncio.sleep(0)
            assert patched_lifespan["db_checked"] is True
            assert patched_lifespan["bot"].webhook_deleted is True
            assert patched_lifespan["dispatcher"].polling_started is True
            assert patched_lifespan["scheduler"].running is True
            assert patched_lifespan["scheduler"].jobs == ["morning_dispatch"]
            assert app.state.bot is patched_lifespan["bot"]
            assert app.state.scheduler is patched_lifespan["scheduler"]

        assert patched_lifespan["scheduler"].running is False
        assert patched_lifespan["dispatcher"].polling_stopped is True
        assert patched_lifespan["bot"].session.closed is True
        assert patched_lifespan["db_disposed"] is True

    async def test_lifespan_raises_when_db_unavailable(
        self,
        patched_lifespan: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """При недоступной БД запуск прерывается с ошибкой."""

        async def _check_db_fail() -> bool:
            return False

        monkeypatch.setattr(main_module, "check_db_connection", _check_db_fail)
        app = main_module.create_application()

        with pytest.raises(RuntimeError, match="База данных недоступна"):
            async with main_module.lifespan(app):
                pass  # pragma: no cover

    async def test_lifespan_skips_init_db_in_production(
        self,
        patched_lifespan: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """В продакшене схема не создаётся автоматически."""
        monkeypatch.setattr(
            type(main_module.settings),
            "is_production",
            property(lambda self: True),
        )
        app = main_module.create_application()

        async with main_module.lifespan(app):
            assert patched_lifespan["db_initialized"] is False


class TestBotPollingHelpers:
    """Проверки вспомогательных функций запуска/остановки polling."""

    async def test_start_bot_polling_creates_task(self) -> None:
        """Запуск polling создаёт фоновую задачу и удаляет вебхук."""
        bot = _StubBot()
        dispatcher = _StubDispatcher()

        task = await main_module._start_bot_polling(bot, dispatcher)  # type: ignore[arg-type]
        try:
            assert bot.webhook_deleted is True
            assert task.get_name() == "planzy-bot-polling"
        finally:
            task.cancel()
            # ``CancelledError`` наследуется от ``BaseException``,
            # поэтому ``suppress(Exception)`` его не поймает.
            with suppress(asyncio.CancelledError):
                await task

    async def test_stop_bot_polling_closes_session(self) -> None:
        """Остановка polling закрывает сессию бота."""
        bot = _StubBot()
        dispatcher = _StubDispatcher()
        task = await main_module._start_bot_polling(bot, dispatcher)  # type: ignore[arg-type]

        await main_module._stop_bot_polling(bot, dispatcher, task)  # type: ignore[arg-type]

        assert dispatcher.polling_stopped is True
        assert bot.session.closed is True
