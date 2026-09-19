"""Показывает кнопки досье для ученика и группы.

Запуск:
    python -m scripts.check_dossier_buttons
"""

from __future__ import annotations

import asyncio
import sys

from sqlalchemy import select

from app.bot.keyboards import dossier_keyboard
from app.core.config import settings
from app.core.logging import setup_logging
from app.db.base import dispose_engine, get_session
from app.db.models import Entity
from app.db.repository import Repository
from app.services.dossier_service import DossierService
from app.services.whatsapp_service import WhatsAppLinkService

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


async def main() -> None:
    """Печатает подписи и ссылки кнопок для ученика и группы."""
    setup_logging()

    async with get_session() as session:
        repo = Repository(session)
        tenant = await repo.tenants.get_by_telegram_id(settings.owner_telegram_id)
        if tenant is None:
            print("Тенант не найден.")
            await dispose_engine()
            return

        for kind in ("student", "group"):
            entity = (
                await session.execute(
                    select(Entity)
                    .where(Entity.tenant_id == tenant.id, Entity.entity_type == kind)
                    .order_by(Entity.id)
                    .limit(1)
                )
            ).scalar_one_or_none()
            if entity is None:
                continue

            dossier = await DossierService(repo).build(tenant.id, entity.id)
            assert dossier is not None
            text = DossierService.format_for_chat(dossier)

            whatsapp = WhatsAppLinkService()
            keyboard = dossier_keyboard(
                card_url=whatsapp.build_card_url(entity),
                whatsapp=whatsapp.build_dossier_message(entity, text),
            )

            label = "группа" if kind == "group" else "ученик"
            print(f"\n=== {entity.name} ({label}) ===")
            print(f"parent_phone={entity.parent_phone!r} chat_url={entity.whatsapp_chat_url!r}")
            if keyboard is None:
                print("!!! Клавиатуры нет")
                continue
            for row in keyboard.inline_keyboard:
                for button in row:
                    target = button.url or button.callback_data or "—"
                    print(f"  [{button.text}] → {target}")

    await dispose_engine()


if __name__ == "__main__":
    asyncio.run(main())
