"""Проверяет, что нажатие на кнопку списка доходит до хендлера.

Прогоняет callback через настоящий Dispatcher (с whitelist-middleware)
и печатает, какое сообщение отправил бы бот.

Запуск:
    python -m scripts.check_bot_commands --callbacks
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime
from typing import Any

from aiogram import Bot
from aiogram.types import CallbackQuery, Chat, Message, Update, User
from sqlalchemy import select

from app.bot.dispatcher import create_dispatcher
from app.core.config import settings
from app.core.logging import get_logger, setup_logging
from app.db.base import dispose_engine, get_session
from app.db.models import Entity

logger = get_logger(__name__)

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


async def main() -> None:
    """Прогоняет callback-события через dispatcher и печатает ответы."""
    setup_logging()

    async with get_session() as session:
        entity = (
            await session.execute(
                select(Entity).where(Entity.entity_type == "student").order_by(Entity.id).limit(1)
            )
        ).scalar_one_or_none()
        group = (
            await session.execute(
                select(Entity).where(Entity.entity_type == "group").order_by(Entity.id).limit(1)
            )
        ).scalar_one_or_none()

    if entity is None:
        print("В базе нет учеников — нечего проверять.")
        await dispose_engine()
        return

    print(f"Ученик: {entity.name} (id={entity.id})")
    if group is not None:
        print(f"Группа: {group.name} (id={group.id})")

    sent: list[str] = []

    async def _fake_call(self: Bot, method: Any, request_timeout: Any = None) -> Any:
        """Перехватывает вызовы Telegram API вместо реальной отправки."""
        name = type(method).__name__
        payload = method.model_dump()
        if name == "SendMessage":
            sent.append(payload.get("text", ""))
        elif name == "AnswerCallbackQuery":
            sent.append(f"[answer] {payload.get('text') or '(без текста)'}")
        return True

    Bot.__call__ = _fake_call  # type: ignore[method-assign]

    dispatcher = create_dispatcher()
    bot = Bot(token=settings.telegram_bot_token)

    async def run(data: str, title: str) -> None:
        """Отправляет один callback в dispatcher."""
        sent.clear()
        update = Update(
            update_id=1,
            callback_query=CallbackQuery(
                id="cb-1",
                from_user=User(id=settings.owner_telegram_id, is_bot=False, first_name="Тренер"),
                chat_instance="ci",
                data=data,
                message=Message(
                    message_id=1,
                    date=datetime.now(UTC),
                    chat=Chat(id=settings.owner_telegram_id, type="private"),
                    text="список",
                ),
            ),
        )
        await dispatcher.feed_update(bot, update)
        print(f"\n=== {title} ({data}) ===")
        for text in sent:
            print(text[:600])
        if not sent:
            print("!!! Хендлер ничего не отправил")

    try:
        await run(f"entity:student:{entity.id}:0", "Нажатие на ученика")
        if group is not None:
            await run(f"entity:group:{group.id}:0", "Нажатие на группу")
        await run("entity_page:student:0", "Возврат к списку")
    finally:
        await bot.session.close()

    await dispose_engine()


if __name__ == "__main__":
    asyncio.run(main())
