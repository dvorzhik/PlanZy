"""Сборка Telegram-бота: Bot, Dispatcher, middleware и роутеры.

Модуль не запускает polling — это делает ``app/main.py`` в общем event loop
вместе с FastAPI и планировщиком.
"""

from __future__ import annotations

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from app.bot.handlers import create_router
from app.bot.middlewares import WhitelistMiddleware
from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)


def create_bot(token: str | None = None) -> Bot:
    """Создаёт экземпляр Bot с настройками по умолчанию.

    Args:
        token: Токен бота. По умолчанию берётся из настроек.

    Returns:
        Готовый экземпляр Bot.
    """
    return Bot(
        token=token or settings.telegram_bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )


def create_dispatcher() -> Dispatcher:
    """Создаёт Dispatcher с подключёнными middleware и хендлерами.

    Whitelist вешается на уровень ``update``, а не только на ``message``:
    так он срабатывает и для сообщений, и для нажатий на инлайн-кнопки
    (``callback_query``). Иначе в callback-хендлерах не оказалось бы
    ``tenant`` и ``repo`` в контексте.

    Returns:
        Настроенный Dispatcher.
    """
    dispatcher = Dispatcher(name="planzy")

    # Whitelist применяется ко всем входящим событиям: сообщения и callback'и.
    whitelist = WhitelistMiddleware()
    dispatcher.update.outer_middleware(whitelist)

    router = create_router()
    dispatcher.include_router(router)

    logger.info(
        "Dispatcher бота настроен: whitelist + %s хендлер(ов) сообщений, %s callback-хендлер(ов)",
        len(router.message.handlers),
        len(router.callback_query.handlers),
    )
    return dispatcher
