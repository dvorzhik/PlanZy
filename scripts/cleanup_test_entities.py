"""Удаляет тестовые сущности, созданные скриптом проверки команд.

Запуск:
    python -m scripts.cleanup_test_entities
"""

from __future__ import annotations

import asyncio
import sys

from sqlalchemy import delete, select

from app.core.logging import setup_logging
from app.db.base import dispose_engine, get_session
from app.db.models import DispatchLog, Entity, Lesson, Payment, ScheduleException, ScheduleSlot

# Имена, которые создавала проверка команд бота.
TEST_NAMES = ("Проверка Ученик", "Группа Проверка")

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


async def main() -> None:
    """Удаляет тестовые сущности и связанные с ними записи."""
    setup_logging()

    async with get_session() as session:
        entities = (
            (await session.execute(select(Entity).where(Entity.name.in_(TEST_NAMES))))
            .scalars()
            .all()
        )

        if not entities:
            print("Тестовых сущностей нет — чистить нечего.")
            await dispose_engine()
            return

        for entity in entities:
            entity_id = entity.id
            tenant_id = entity.tenant_id
            print(f"Удаляю: id={entity_id} name={entity.name!r} type={entity.entity_type}")

            await session.execute(
                delete(ScheduleSlot).where(
                    ScheduleSlot.tenant_id == tenant_id, ScheduleSlot.entity_id == entity_id
                )
            )
            await session.execute(
                delete(ScheduleException).where(
                    ScheduleException.tenant_id == tenant_id,
                    ScheduleException.entity_id == entity_id,
                )
            )
            await session.execute(
                delete(Lesson).where(Lesson.tenant_id == tenant_id, Lesson.entity_id == entity_id)
            )
            await session.execute(
                delete(Payment).where(
                    Payment.tenant_id == tenant_id, Payment.entity_id == entity_id
                )
            )
            await session.execute(
                delete(DispatchLog).where(
                    DispatchLog.tenant_id == tenant_id, DispatchLog.entity_id == entity_id
                )
            )
            await session.delete(entity)

    await dispose_engine()
    print("Готово.")


if __name__ == "__main__":
    asyncio.run(main())
