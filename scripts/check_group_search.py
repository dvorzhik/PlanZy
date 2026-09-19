"""Проверяет поиск групп и учеников по частичному имени.

Прогоняет типичные формулировки диктовки против реальной базы и печатает,
что нашлось.

Запуск:
    python -m scripts.check_group_search
"""

from __future__ import annotations

import asyncio
import sys

from app.core.config import settings
from app.core.logging import setup_logging
from app.db.base import dispose_engine, get_session
from app.db.repository import Repository
from app.services.entity_service import EntityService

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# Формулировки, которые тренер может произнести в диктовке.
QUERIES = [
    "группа Шпринцак 4",
    "Группа Шпринцак 4 - начинающие",
    "группа шпринцак 3",
    "группа Шпринцак 1",
    "группа Шпринцак 2",
    "группа Пятничная",
    "Пятничная группа",
    "группа Вторничная",
    "группа Детская",
    "Группа Новички",
    "Шпринцак 4",
    "Кирилл",
    "кирилл",
    "Алина",
    "Рон и Мия",
    "Неизвестный Ученик",
    # Варианты с опечатками и частичными названиями.
    "группа Шпринцак",
    "группа Шпринцак 05",
    "група Шпринцак 4",
    "Шпринцак 3",
    "Мия",
]


async def main() -> None:
    """Ищет каждую формулировку и печатает результат."""
    setup_logging()

    async with get_session() as session:
        repo = Repository(session)
        tenant = await repo.tenants.get_by_telegram_id(settings.owner_telegram_id)
        if tenant is None:
            print("Тенант не найден.")
            await dispose_engine()
            return

        service = EntityService(repo)
        found = 0
        for query in QUERIES:
            entity = await service.resolve(tenant.id, query)
            if entity is None:
                similar = await service.find_similar(tenant.id, query)
                hint = ", ".join(item.name for item in similar) or "—"
                print(f"❌ {query!r} → не найдено; похожие: {hint}")
            else:
                kind = "группа" if entity.is_group else "ученик"
                print(f"✅ {query!r} → {entity.name!r} ({kind})")
                found += 1

        print(f"\nНайдено: {found} из {len(QUERIES)}")

    await dispose_engine()


if __name__ == "__main__":
    asyncio.run(main())
