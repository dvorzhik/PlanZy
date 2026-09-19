"""Живая проверка команд бота на локальной базе.

Запуск:
    python -m scripts.check_bot_commands
"""

from __future__ import annotations

import asyncio
import sys
from datetime import date, timedelta
from typing import Any

from app.bot.handlers import (
    BOT_COMMANDS,
    callback_open_dossier,
    cmd_admin,
    cmd_group,
    cmd_student,
    cmd_today,
)
from app.core.logging import get_logger, setup_logging
from app.db.base import dispose_engine, get_session
from app.db.repository import Repository
from app.services.entity_service import EntityService
from app.services.schedule_service import ScheduleService

logger = get_logger(__name__)

OWNER_TELEGRAM_ID = 161752421

# Консоль Windows по умолчанию не умеет печатать эмодзи — переключаем на UTF-8.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


class StubMessage:
    """Заглушка сообщения для показа ответов бота в консоли."""

    def __init__(self) -> None:
        self.answers: list[str] = []

    async def answer(self, text: str, **_: Any) -> None:
        self.answers.append(text)

    @property
    def last(self) -> str:
        return self.answers[-1]


class StubCallback:
    """Заглушка нажатия на инлайн-кнопку."""

    def __init__(self, data: str) -> None:
        self.data = data
        self.message = StubMessage()
        self.alert: str | None = None

    async def answer(self, text: str | None = None, **_: Any) -> None:
        self.alert = text


async def main() -> None:
    """Прогоняет команды бота и печатает их вывод."""
    setup_logging()

    async with get_session() as session:
        repo = Repository(session)
        tenant = await repo.tenants.get_by_telegram_id(OWNER_TELEGRAM_ID)
        if tenant is None:
            print("Тенант не найден — сначала запустите python -m scripts.seed")
            await dispose_engine()
            return

        tenant_id = tenant.id
        print(f"=== Тенант: {tenant.name} (id={tenant_id}) ===")
        print("Меню команд:", [name for name, _ in BOT_COMMANDS])

        # Демо-данные: ученик на сегодня и группа на завтра.
        entities = EntityService(repo)
        student, _ = await entities.get_or_create(tenant_id, "Проверка Ученик")
        group, _ = await entities.get_or_create(tenant_id, "Группа Проверка")
        await repo.entities.update(tenant_id, group.id, entity_type="group")

        schedule = ScheduleService(repo)
        today = date.today()
        tomorrow = today + timedelta(days=1)
        weekday_codes = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
        await schedule.add_slot(tenant_id, student.id, weekday_codes[today.weekday()], "16:00")
        await schedule.add_slot(tenant_id, group.id, weekday_codes[tomorrow.weekday()], "18:30")

        context: dict[str, Any] = {"tenant": tenant, "repo": repo}

        print("\n=== /today ===")
        message = StubMessage()
        await cmd_today(message, **context)
        print(message.last)

        print("\n=== /admin ===")
        message = StubMessage()
        await cmd_admin(message, **context)
        print(message.last)

        print("\n=== /student ===")
        message = StubMessage()
        await cmd_student(message, **context)
        print(message.last)

        print("\n=== /group ===")
        message = StubMessage()
        await cmd_group(message, **context)
        print(message.last)

        print("\n=== Нажатие на ученика ===")
        callback = StubCallback(f"entity:student:{student.id}:0")
        await callback_open_dossier(callback, **context)
        print(callback.alert or "(без предупреждения)")
        print(callback.message.last)

    await dispose_engine()


if __name__ == "__main__":
    asyncio.run(main())
