"""Сквозной smoke-тест PlanZy на реальной базе данных.

Прогоняет полный путь: БД → Repository → Service Layer → веб-роуты.
Запуск:
    python -m scripts.smoke_test
"""

from __future__ import annotations

import asyncio

from httpx import ASGITransport, AsyncClient

from app.core.config import settings
from app.db.base import dispose_engine, get_session
from app.db.repository import Repository
from app.services.dossier_service import DossierService
from app.services.entity_service import EntityService
from app.services.whatsapp_service import WhatsAppLinkService
from app.web.app import create_app


async def check_services() -> str:
    """Проверяет Repository и Service Layer, возвращает токен ученика."""
    async with get_session() as session:
        repo = Repository(session)

        tenant = await repo.tenants.get_by_telegram_id(settings.owner_telegram_id)
        assert tenant is not None, "Тенант не найден — сначала запустите scripts.seed"
        print(f"[OK] Тенант: id={tenant.id} name={tenant.name!r}")

        entities = await repo.entities.list_by_tenant(tenant.id)
        assert entities, "Нет сущностей — запустите scripts.seed --demo"
        print(f"[OK] Сущностей: {len(entities)}")
        for entity in entities:
            print(
                f"     - {entity.name} ({entity.entity_type}), токен={entity.public_token[:8]}..."
            )

        student = next((e for e in entities if e.entity_type == "student"), entities[0])

        dossier = await DossierService(repo).build(tenant.id, student.id)
        assert dossier is not None, "Досье не собралось"
        print(
            f"[OK] Досье {dossier.entity.name}: "
            f"уроков={len(dossier.history)}, "
            f"средняя={dossier.average_rating}, "
            f"тренд={dossier.rating_trend}, "
            f"слабые темы={dossier.weak_topics}"
        )

        whatsapp = WhatsAppLinkService()
        card_url = whatsapp.build_card_url(student)
        dossier_url = whatsapp.build_dossier_url(student)
        assert card_url and dossier_url, "Не собрались публичные ссылки"
        print(f"[OK] Ссылка на карточку: {card_url}")
        print(f"[OK] Ссылка на досье:   {dossier_url}")

        print(f"[OK] Расписание: {EntityService.format_schedule(student)}")

        return student.public_token


async def check_web(token: str) -> None:
    """Проверяет веб-роуты через ASGI-транспорт (без реального сервера)."""
    app = create_app()
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        health = await client.get("/health")
        assert health.status_code == 200, f"/health вернул {health.status_code}"
        print(f"[OK] GET /health -> {health.status_code} {health.json()}")

        index = await client.get("/")
        assert index.status_code == 200, f"/ вернул {index.status_code}"
        print(f"[OK] GET / -> {index.status_code} ({len(index.text)} байт)")

        card = await client.get(f"/l/{token}")
        assert card.status_code == 200, f"/l/{{token}} вернул {card.status_code}"
        assert "Миша" in card.text or "Аня" in card.text or "Группа" in card.text
        print(f"[OK] GET /l/{{token}} -> {card.status_code} ({len(card.text)} байт)")

        dossier = await client.get(f"/dossier/{token}")
        assert dossier.status_code == 200, f"/dossier/{{token}} вернул {dossier.status_code}"
        print(f"[OK] GET /dossier/{{token}} -> {dossier.status_code} ({len(dossier.text)} байт)")

        missing = await client.get("/l/no-such-token-xyz")
        assert missing.status_code == 404, f"Ожидался 404, получен {missing.status_code}"
        print(f"[OK] GET /l/no-such-token-xyz -> {missing.status_code} (корректный 404)")


async def main() -> None:
    """Запускает сквозную проверку."""
    print("=" * 60)
    print("PlanZy — сквозной smoke-тест")
    print("=" * 60)
    try:
        token = await check_services()
        print("-" * 60)
        await check_web(token)
    finally:
        await dispose_engine()
    print("=" * 60)
    print("ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ")


if __name__ == "__main__":
    asyncio.run(main())
