"""Печатает текст приветствия /start для проверки.

Запуск:
    python -m scripts.check_start_text
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

from app.bot.handlers import cmd_start
from app.core.config import settings
from app.core.logging import setup_logging
from app.db.base import dispose_engine, get_session
from app.db.repository import Repository

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


class StubMessage:
    """Заглушка сообщения: собирает ответ."""

    def __init__(self) -> None:
        self.answers: list[str] = []

    async def answer(self, text: str, **_: Any) -> None:
        self.answers.append(text)

    @property
    def last(self) -> str:
        return self.answers[-1]


async def main() -> None:
    """Показывает, что бот присылает на /start."""
    setup_logging()

    async with get_session() as session:
        repo = Repository(session)
        tenant = await repo.tenants.get_by_telegram_id(settings.owner_telegram_id)
        if tenant is None:
            print("Тенант не найден.")
            await dispose_engine()
            return

        message = StubMessage()
        await cmd_start(message, repo=repo, tenant=tenant)
        print("=== Ответ на /start ===")
        print(message.last)

    await dispose_engine()


if __name__ == "__main__":
    asyncio.run(main())
