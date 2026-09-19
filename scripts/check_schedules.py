"""Показывает расписание всех учеников и групп из слотов.

Запуск:
    python -m scripts.check_schedules
"""

from __future__ import annotations

import asyncio
import sys

from sqlalchemy import select

from app.core.config import settings
from app.core.logging import setup_logging
from app.db.base import dispose_engine, get_session
from app.db.models import Entity
from app.db.repository import Repository
from app.services.schedule_service import ScheduleService

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


async def main() -> None:
    """Печатает расписание каждой сущности тенанта."""
    setup_logging()

    async with get_session() as session:
        repo = Repository(session)
        tenant = await repo.tenants.get_by_telegram_id(settings.owner_telegram_id)
        if tenant is None:
            print("Тенант не найден — сначала запустите python -m scripts.seed")
            await dispose_engine()
            return

        entities = (
            (
                await session.execute(
                    select(Entity).where(Entity.tenant_id == tenant.id).order_by(Entity.id)
                )
            )
            .scalars()
            .all()
        )

        print(f"=== Тенант {tenant.name} (id={tenant.id}): {len(entities)} сущностей ===")
        schedule = ScheduleService(repo)
        for entity in entities:
            slots = await repo.schedule.list_by_entity(tenant.id, entity.id)
            text = await schedule.format_entity_schedule(tenant.id, entity.id)
            kind = "группа" if entity.entity_type == "group" else "ученик"
            print(f"[{entity.id:>3}] {entity.name} ({kind}), слотов={len(slots)}: {text}")

    await dispose_engine()


if __name__ == "__main__":
    asyncio.run(main())
