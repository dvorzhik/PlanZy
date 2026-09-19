"""Скрипт первичного наполнения базы данных.

Создаёт первого тенанта (владельца) и, опционально, тестовых
учеников с уроками и платежами для проверки работы.

Запуск:
    python -m scripts.seed              # только тенант
    python -m scripts.seed --demo       # тенант + демо-данные
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import date, timedelta

from app.core.config import settings
from app.core.logging import get_logger, setup_logging
from app.db.base import dispose_engine, get_session, init_db
from app.db.repository import Repository

logger = get_logger(__name__)


async def create_tenant() -> int:
    """Создаёт тенанта-владельца, если его ещё нет. Возвращает tenant_id."""
    async with get_session() as session:
        repo = Repository(session)

        existing = await repo.tenants.get_by_telegram_id(settings.owner_telegram_id)
        if existing:
            logger.info(
                "Тенант уже существует: id=%s name=%r",
                existing.id,
                existing.name,
            )
            return existing.id

        tenant = await repo.tenants.create(
            name="Мой аккаунт",
            telegram_user_id=settings.owner_telegram_id,
        )
        logger.info("Создан тенант id=%s", tenant.id)
        return tenant.id


async def create_demo_data(tenant_id: int) -> None:
    """Создаёт демонстрационных учеников, уроки и платежи."""
    today = date.today()

    async with get_session() as session:
        repo = Repository(session)

        # Проверяем, нет ли уже данных
        existing = await repo.entities.list_by_tenant(tenant_id)
        if existing:
            logger.info("Демо-данные уже существуют (%d сущностей), пропускаем", len(existing))
            return

        # --- Ученик 1: Миша ---
        misha = await repo.entities.create(
            tenant_id=tenant_id,
            name="Миша",
            entity_type="student",
            subject="Шахматы",
            parent_phone="972501234567",
            schedule_days="mon,wed",
            schedule_time="16:00",
        )
        await repo.lessons.create(
            tenant_id=tenant_id,
            entity_id=misha.id,
            lesson_date=today - timedelta(days=2),
            topic="Итальянская партия: основные ловушки",
            homework="Решить 10 задач на мат в 2 хода",
            internal_notes="Плохо считает варианты в эндшпиле, теряет концентрацию к концу урока",
            rating="7/10",
            next_step="Разобрать ладейные окончания",
        )
        await repo.payments.create(
            tenant_id=tenant_id,
            entity_id=misha.id,
            payment_date=today - timedelta(days=10),
            amount=400.0,
            currency="ILS",
            lessons_added=8,
        )

        # --- Ученик 2: Аня ---
        anya = await repo.entities.create(
            tenant_id=tenant_id,
            name="Аня",
            entity_type="student",
            subject="Шахматы",
            parent_phone="972527654321",
            schedule_days="tue,thu",
            schedule_time="17:30",
        )
        await repo.lessons.create(
            tenant_id=tenant_id,
            entity_id=anya.id,
            lesson_date=today - timedelta(days=1),
            topic="Дебют четырёх коней",
            homework="Проиграть партию с родителем, записать ходы",
            internal_notes="Отличная память на дебюты, нужно развивать тактическое зрение",
            rating="8/10",
            next_step="Тактические задачи на связки",
        )
        await repo.payments.create(
            tenant_id=tenant_id,
            entity_id=anya.id,
            payment_date=today - timedelta(days=5),
            amount=200.0,
            currency="ILS",
            lessons_added=4,
        )

        # --- Группа: Новички ---
        group = await repo.entities.create(
            tenant_id=tenant_id,
            name="Группа Новички",
            entity_type="group",
            subject="Шахматы",
            schedule_days="sat",
            schedule_time="11:00",
        )
        await repo.lessons.create(
            tenant_id=tenant_id,
            entity_id=group.id,
            lesson_date=today - timedelta(days=3),
            topic="Правила рокировки и взятия на проходе",
            homework="Расставить позиции из учебника, стр. 24–25",
            internal_notes="В группе 5 человек, двое отстают — нужны индивидуальные задания",
            rating="Хорошо",
            next_step="Мат одинокому королю",
            attendance="Миша, Аня, Давид, Лев, Соня",
        )
        await repo.payments.create(
            tenant_id=tenant_id,
            entity_id=group.id,
            payment_date=today - timedelta(days=7),
            amount=600.0,
            currency="ILS",
            lessons_added=6,
        )

        logger.info("Демо-данные созданы: 2 ученика, 1 группа, 3 урока, 3 платежа")


async def main() -> None:
    """Точка входа скрипта."""
    parser = argparse.ArgumentParser(description="Наполнение базы PlanZy")
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Создать демонстрационные данные (ученики, уроки, платежи)",
    )
    args = parser.parse_args()

    setup_logging()

    logger.info("Инициализация схемы БД...")
    await init_db()

    tenant_id = await create_tenant()

    if args.demo:
        logger.info("Создание демо-данных...")
        await create_demo_data(tenant_id)

    await dispose_engine()
    logger.info("Готово. tenant_id=%s", tenant_id)


if __name__ == "__main__":
    asyncio.run(main())
