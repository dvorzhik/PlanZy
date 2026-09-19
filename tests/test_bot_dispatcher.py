"""Тесты сборки диспетчера бота.

Проверяют, что whitelist-middleware подключается на уровне update —
иначе нажатия на инлайн-кнопки (``callback_query``) приходят без
``tenant`` и ``repo`` в контексте и падают с RuntimeError.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from aiogram import Bot
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from app.bot.dispatcher import create_dispatcher
from app.bot.middlewares import WhitelistMiddleware


class TestDispatcherWiring:
    """Проверки подключения middleware и роутеров."""

    def test_whitelist_attached_to_update_level(self) -> None:
        """Whitelist должен работать и для сообщений, и для callback'ов."""
        dispatcher = create_dispatcher()

        # У Dispatcher уже есть встроенные middleware (errors, user context, FSM),
        # поэтому проверяем, что whitelist добавлен именно на уровень update.
        middlewares = dispatcher.update.outer_middleware._middlewares
        assert any(isinstance(mw, WhitelistMiddleware) for mw in middlewares)

    def test_whitelist_not_bound_to_message_only(self) -> None:
        """Middleware не должен висеть только на message-событиях."""
        dispatcher = create_dispatcher()
        assert not any(
            isinstance(mw, WhitelistMiddleware) for mw in dispatcher.message.middleware._middlewares
        )

    def test_callback_handlers_are_registered(self) -> None:
        """В роутере есть callback-хендлеры для списков учеников и групп."""
        dispatcher = create_dispatcher()
        router = next(sub for sub in dispatcher.sub_routers if sub.name == "planzy")
        assert len(router.callback_query.handlers) >= 2

    def test_dispatcher_can_be_created_repeatedly(self) -> None:
        """Повторная сборка не падает: роутер создаётся заново, а не переиспользуется."""
        first = create_dispatcher()
        second = create_dispatcher()

        assert first is not second
        first_router = next(sub for sub in first.sub_routers if sub.name == "planzy")
        second_router = next(sub for sub in second.sub_routers if sub.name == "planzy")
        assert first_router is not second_router


class TestCallbackReachesHandlerThroughDispatcher:
    """Интеграционная проверка: callback доходит до хендлера с контекстом.

    Регрессия: пока whitelist висел на ``dispatcher.message``, нажатие
    на кнопку приводило к ``RuntimeError: Тенант не найден в контексте``.
    """

    async def test_callback_opens_dossier(
        self,
        repo: Any,
        tenant_id: int,
        tenant_telegram_id: int,
        monkeypatch: Any,
        caplog: Any,
    ) -> None:
        """Нажатие на кнопку списка проходит через dispatcher до конца.

        Middleware резолвит тенанта через :func:`app.db.base.get_session`,
        поэтому подменяем её на тестовую сессию — иначе whitelist не нашёл
        бы тенанта из in-memory БД.
        """
        import logging
        from contextlib import asynccontextmanager

        from app.bot import middlewares as middlewares_module

        await repo.entities.create(tenant_id=tenant_id, name="Миша", entity_type="student")
        entity = await repo.entities.get_by_name(tenant_id, "Миша")
        assert entity is not None

        @asynccontextmanager
        async def _test_session() -> Any:
            yield repo.session

        monkeypatch.setattr(middlewares_module, "get_session", _test_session)

        # Перехватываем обращения к Telegram API: важна логика бота, а не сеть.
        calls: list[tuple[str, dict[str, Any]]] = []

        async def _fake_call(self: Bot, method: Any, request_timeout: Any = None) -> Any:
            calls.append((type(method).__name__, method.model_dump()))
            return True

        monkeypatch.setattr(Bot, "__call__", _fake_call)

        caplog.set_level(logging.ERROR, logger="aiogram.event")

        dispatcher = create_dispatcher()
        bot = Bot(token="42:TEST")
        try:
            update = Update(
                update_id=1,
                callback_query=CallbackQuery(
                    id="cb-1",
                    from_user=User(
                        id=tenant_telegram_id,
                        is_bot=False,
                        first_name="Тренер",
                    ),
                    chat_instance="ci",
                    data=f"entity:student:{entity.id}:0",
                    message=Message(
                        message_id=1,
                        date=datetime.now(UTC),
                        chat=Chat(id=1, type="private"),
                        text="список",
                    ),
                ),
            )
            await dispatcher.feed_update(bot, update)
        finally:
            await bot.session.close()

        # aiogram логирует необработанные исключения хендлеров через aiogram.event.
        assert "Cause exception while process update" not in caplog.text, caplog.text

        # Хендлер должен ответить на callback и прислать досье.
        assert "AnswerCallbackQuery" in [name for name, _ in calls]
        sent_texts = [payload.get("text", "") for name, payload in calls if name == "SendMessage"]
        assert any("Досье" in text and "Миша" in text for text in sent_texts), sent_texts

    async def test_callback_from_stranger_is_ignored(
        self,
        repo: Any,
        tenant_id: int,
        monkeypatch: Any,
    ) -> None:
        """Посторонний пользователь не получает досье по чужой кнопке."""
        from contextlib import asynccontextmanager

        from app.bot import middlewares as middlewares_module

        await repo.entities.create(tenant_id=tenant_id, name="Миша", entity_type="student")
        entity = await repo.entities.get_by_name(tenant_id, "Миша")
        assert entity is not None

        @asynccontextmanager
        async def _test_session() -> Any:
            yield repo.session

        monkeypatch.setattr(middlewares_module, "get_session", _test_session)

        calls: list[str] = []

        async def _fake_call(self: Bot, method: Any, request_timeout: Any = None) -> Any:
            calls.append(type(method).__name__)
            return True

        monkeypatch.setattr(Bot, "__call__", _fake_call)

        dispatcher = create_dispatcher()
        bot = Bot(token="42:TEST")
        try:
            update = Update(
                update_id=2,
                callback_query=CallbackQuery(
                    id="cb-2",
                    # Чужой telegram_user_id — не совпадает с тенантом.
                    from_user=User(id=999_999, is_bot=False, first_name="Чужой"),
                    chat_instance="ci",
                    data=f"entity:student:{entity.id}:0",
                    message=Message(
                        message_id=2,
                        date=datetime.now(UTC),
                        chat=Chat(id=999_999, type="private"),
                        text="список",
                    ),
                ),
            )
            await dispatcher.feed_update(bot, update)
        finally:
            await bot.session.close()

        assert "SendMessage" not in calls, "Посторонний получил сообщение от бота"
