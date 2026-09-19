"""Telegram-бот PlanZy (aiogram 3.x).

Пакет содержит настройку Dispatcher, middleware (whitelist + резолв тенанта),
хендлеры команд и текстовых диктовок, а также клавиатуры.
"""

from app.bot.dispatcher import create_bot, create_dispatcher

__all__ = ["create_bot", "create_dispatcher"]
