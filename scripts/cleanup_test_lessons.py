"""Удаляет уроки, созданные проверкой scripts.check_lesson_flow.

Запуск:
    python -m scripts.cleanup_test_lessons
"""

from __future__ import annotations

import asyncio
import sys

from sqlalchemy import delete, select

from app.core.config import settings
from app.core.logging import setup_logging
from app.db.base import dispose_engine, get_session
from app.db.models import Entity, Lesson
from app.db.repository import Repository

# Маркеры тем, которые используют проверочные скрипты.
TEST_TOPICS = ("проверка бота", "проверка группы")

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


async def main() -> None:
    """Удаляет тестовые уроки вместе с платежами по ним."""
    setup_logging()

    async with get_session() as session:
        repo = Repository(session)
        tenant = await repo.tenants.get_by_telegram_id(settings.owner_telegram_id)
        if tenant is None:
            print("Тенант не найден.")
            await dispose_engine()
            return

        lessons = (
            (
                await session.execute(
                    select(Lesson).where(
                        Lesson.tenant_id == tenant.id,
                        Lesson.topic.in_(TEST_TOPICS),
                    )
                )
            )
            .scalars()
            .all()
        )

        if not lessons:
            print("Тестовых уроков нет.")
            await dispose_engine()
            return

        for lesson in lessons:
            entity = await session.get(Entity, lesson.entity_id)
            name = entity.name if entity else "?"
            print(f"Удаляю урок id={lesson.id} ({name}, {lesson.lesson_date}): {lesson.topic}")

        await session.execute(
            delete(Lesson).where(
                Lesson.tenant_id == tenant.id,
                Lesson.topic.in_(TEST_TOPICS),
            )
        )

    await dispose_engine()
    print("Готово.")


if __name__ == "__main__":
    asyncio.run(main())
