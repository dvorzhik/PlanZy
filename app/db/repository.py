"""Repository-слой: весь доступ к базе данных.

Ключевой принцип: каждый метод, работающий с данными тенанта,
обязательно принимает tenant_id. Это гарантирует изоляцию данных
между тренерами (мультитенантность) на уровне доступа к БД.

Слой не содержит бизнес-логики — только CRUD и выборки.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models import (
    AdminSession,
    AuditLog,
    DispatchLog,
    Entity,
    Lesson,
    LessonShareLink,
    Payment,
    ReminderRule,
    ScheduleException,
    ScheduleSlot,
    Tenant,
    generate_admin_token,
    generate_public_token,
)

logger = get_logger(__name__)


def _dump_payload(payload: dict[str, Any] | str | None) -> str | None:
    """Приводит payload аудита к строке для хранения в колонке ``Text``.

    Словарь сериализуется в JSON с ``ensure_ascii=False``, чтобы русские
    имена в журнале оставались читаемыми. Строка возвращается как есть.
    """
    if payload is None:
        return None
    if isinstance(payload, str):
        return payload
    return json.dumps(payload, ensure_ascii=False, default=str)


class TenantRepository:
    """Операции с тенантами (тренерами)."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(
        self,
        name: str,
        telegram_user_id: int,
        is_active: bool = True,
    ) -> Tenant:
        """Создаёт нового тенанта."""
        tenant = Tenant(
            name=name,
            telegram_user_id=telegram_user_id,
            is_active=is_active,
        )
        self.session.add(tenant)
        await self.session.flush()
        logger.info("Создан тенант id=%s name=%r", tenant.id, tenant.name)
        return tenant

    async def get_by_id(self, tenant_id: int) -> Tenant | None:
        """Возвращает тенанта по id."""
        return await self.session.get(Tenant, tenant_id)

    async def get_by_telegram_id(self, telegram_user_id: int) -> Tenant | None:
        """Возвращает тенанта по Telegram user_id (для whitelist бота)."""
        stmt = select(Tenant).where(Tenant.telegram_user_id == telegram_user_id)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_admin_token(self, admin_token: str) -> Tenant | None:
        """Возвращает тенанта по секретному токену кабинета.

        Используется при входе по ссылке ``/admin/{admin_token}``.
        """
        stmt = select(Tenant).where(Tenant.admin_token == admin_token)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def ensure_admin_token(self, tenant_id: int) -> str | None:
        """Гарантирует наличие admin_token у тенанта.

        Возвращает актуальный токен или ``None``, если тенант не найден.
        Нужен для команды бота ``/admin`` и для входа в кабинет.
        """
        tenant = await self.get_by_id(tenant_id)
        if tenant is None:
            return None
        if not tenant.admin_token:
            tenant.admin_token = generate_admin_token()
            await self.session.flush()
            logger.info("Сгенерирован admin_token для тенанта id=%s", tenant_id)
        return tenant.admin_token

    async def update_settings(self, tenant_id: int, **fields: Any) -> Tenant | None:
        """Обновляет настройки тренера из веб-кабинета.

        Разрешены только поля-настройки, чтобы случайно не переписать
        служебные поля (``telegram_user_id``, ``admin_token``).
        """
        allowed = {
            "name",
            "timezone",
            "lesson_price",
            "currency",
            "payment_instructions",
        }
        payload = {key: value for key, value in fields.items() if key in allowed}
        if not payload:
            return await self.get_by_id(tenant_id)
        stmt = update(Tenant).where(Tenant.id == tenant_id).values(**payload)
        await self.session.execute(stmt)
        await self.session.flush()
        logger.info("Обновлены настройки тенанта id=%s: %s", tenant_id, sorted(payload))
        return await self.get_by_id(tenant_id)

    async def list_active(self) -> list[Tenant]:
        """Возвращает всех активных тенантов (для утренней рассылки)."""
        stmt = select(Tenant).where(Tenant.is_active.is_(True)).order_by(Tenant.id)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def update(self, tenant_id: int, **fields: Any) -> Tenant | None:
        """Обновляет поля тенанта."""
        if not fields:
            return await self.get_by_id(tenant_id)
        stmt = update(Tenant).where(Tenant.id == tenant_id).values(**fields)
        await self.session.execute(stmt)
        await self.session.flush()
        return await self.get_by_id(tenant_id)

    async def delete(self, tenant_id: int) -> bool:
        """Удаляет тенанта вместе со всеми его данными (каскадно)."""
        stmt = delete(Tenant).where(Tenant.id == tenant_id)
        result = await self.session.execute(stmt)
        await self.session.flush()
        return result.rowcount > 0


class EntityRepository:
    """Операции с учениками и группами."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(
        self,
        tenant_id: int,
        name: str,
        entity_type: str = "student",
        subject: str = "Шахматы",
        parent_phone: str | None = None,
        members: str | None = None,
        whatsapp_chat_url: str | None = None,
        lesson_price: float | None = None,
        schedule_days: str | None = None,
        schedule_time: str | None = None,
        public_token: str | None = None,
    ) -> Entity:
        """Создаёт ученика или группу."""
        entity = Entity(
            tenant_id=tenant_id,
            name=name,
            entity_type=entity_type,
            subject=subject,
            parent_phone=parent_phone,
            members=members,
            whatsapp_chat_url=whatsapp_chat_url,
            lesson_price=lesson_price,
            schedule_days=schedule_days,
            schedule_time=schedule_time,
            public_token=public_token or generate_public_token(),
        )
        self.session.add(entity)
        await self.session.flush()
        logger.info(
            "Создана сущность id=%s tenant_id=%s type=%s name=%r",
            entity.id,
            tenant_id,
            entity_type,
            name,
        )
        return entity

    async def get_by_id(self, tenant_id: int, entity_id: int) -> Entity | None:
        """Возвращает сущность по id в рамках тенанта."""
        stmt = select(Entity).where(Entity.id == entity_id, Entity.tenant_id == tenant_id)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_token(self, public_token: str) -> Entity | None:
        """Возвращает сущность по публичному токену (для страницы /l/{token}).

        Токен глобально уникален, поэтому tenant_id не требуется.
        """
        stmt = select(Entity).where(Entity.public_token == public_token)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_name(self, tenant_id: int, name: str) -> Entity | None:
        """Ищет сущность по имени (без учёта регистра) в рамках тенанта.

        Регистронезависимое сравнение выполняется в Python, а не через
        SQL-функцию ``lower()``: встроенный ``lower()`` в SQLite работает
        только с ASCII и не понижает регистр кириллицы. Сущностей у одного
        тренера немного, поэтому фильтрация в памяти не создаёт нагрузки.
        """
        target = name.strip().casefold()
        stmt = select(Entity).where(Entity.tenant_id == tenant_id)
        result = await self.session.execute(stmt)
        for entity in result.scalars().all():
            if entity.name.strip().casefold() == target:
                return entity
        return None

    async def list_by_tenant(
        self,
        tenant_id: int,
        entity_type: str | None = None,
    ) -> list[Entity]:
        """Возвращает всех учеников/группы тенанта."""
        stmt = select(Entity).where(Entity.tenant_id == tenant_id)
        if entity_type:
            stmt = stmt.where(Entity.entity_type == entity_type)
        stmt = stmt.order_by(Entity.name)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def list_for_weekday(self, tenant_id: int, weekday: str) -> list[Entity]:
        """Возвращает сущности, у которых занятие в указанный день недели.

        weekday — трёхбуквенный код: mon, tue, wed, thu, fri, sat, sun.
        Фильтрация по schedule_days выполняется в Python, так как
        дни хранятся строкой через запятую (совместимо с SQLite и PostgreSQL).
        """
        entities = await self.list_by_tenant(tenant_id)
        weekday = weekday.strip().lower()
        return [entity for entity in entities if weekday in entity.schedule_days_list]

    async def update(self, tenant_id: int, entity_id: int, **fields: Any) -> Entity | None:
        """Обновляет поля сущности."""
        if not fields:
            return await self.get_by_id(tenant_id, entity_id)
        stmt = (
            update(Entity)
            .where(Entity.id == entity_id, Entity.tenant_id == tenant_id)
            .values(**fields)
        )
        await self.session.execute(stmt)
        await self.session.flush()
        return await self.get_by_id(tenant_id, entity_id)

    async def delete(self, tenant_id: int, entity_id: int) -> bool:
        """Удаляет сущность вместе с уроками и платежами (каскадно)."""
        stmt = delete(Entity).where(Entity.id == entity_id, Entity.tenant_id == tenant_id)
        result = await self.session.execute(stmt)
        await self.session.flush()
        return result.rowcount > 0


class LessonRepository:
    """Операции с уроками."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(
        self,
        tenant_id: int,
        entity_id: int,
        lesson_date: date,
        topic: str,
        homework: str | None = None,
        internal_notes: str | None = None,
        rating: str | None = None,
        next_step: str | None = None,
        attendance: str | None = None,
        raw_gemini_json: str | None = None,
        start_time: str | None = None,
    ) -> Lesson:
        """Создаёт запись об уроке."""
        lesson = Lesson(
            tenant_id=tenant_id,
            entity_id=entity_id,
            lesson_date=lesson_date,
            start_time=start_time,
            topic=topic,
            homework=homework,
            internal_notes=internal_notes,
            rating=rating,
            next_step=next_step,
            attendance=attendance,
            raw_gemini_json=raw_gemini_json,
        )
        self.session.add(lesson)
        await self.session.flush()
        logger.info(
            "Создан урок id=%s entity_id=%s date=%s",
            lesson.id,
            entity_id,
            lesson_date,
        )
        return lesson

    async def get_by_id(self, tenant_id: int, lesson_id: int) -> Lesson | None:
        """Возвращает урок по id в рамках тенанта."""
        stmt = select(Lesson).where(Lesson.id == lesson_id, Lesson.tenant_id == tenant_id)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_by_entity(
        self,
        tenant_id: int,
        entity_id: int,
        limit: int | None = None,
    ) -> list[Lesson]:
        """Возвращает историю уроков сущности, новые сверху."""
        stmt = (
            select(Lesson)
            .where(Lesson.tenant_id == tenant_id, Lesson.entity_id == entity_id)
            .order_by(
                Lesson.lesson_date.desc(), Lesson.start_time.desc().nullslast(), Lesson.id.desc()
            )
        )
        if limit:
            stmt = stmt.limit(limit)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def list_by_tenant(
        self,
        tenant_id: int,
        limit: int | None = None,
    ) -> list[Lesson]:
        """Возвращает все уроки тенанта, новые сверху.

        Используется журналом занятий в кабинете: тренер видит все свои
        уроки, а фильтрация по ученику и периоду выполняется в памяти.
        """
        stmt = (
            select(Lesson)
            .where(Lesson.tenant_id == tenant_id)
            .order_by(
                Lesson.lesson_date.desc(), Lesson.start_time.desc().nullslast(), Lesson.id.desc()
            )
        )
        if limit:
            stmt = stmt.limit(limit)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_last_lesson(self, tenant_id: int, entity_id: int) -> Lesson | None:
        """Возвращает последний урок сущности (для досье и next_step)."""
        lessons = await self.list_by_entity(tenant_id, entity_id, limit=1)
        return lessons[0] if lessons else None

    async def list_by_date(self, tenant_id: int, lesson_date: date) -> list[Lesson]:
        """Возвращает все уроки тенанта за конкретную дату."""
        stmt = (
            select(Lesson)
            .where(Lesson.tenant_id == tenant_id, Lesson.lesson_date == lesson_date)
            .order_by(Lesson.id)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def count_by_entity(self, tenant_id: int, entity_id: int) -> int:
        """Считает количество проведённых уроков у сущности."""
        stmt = select(func.count(Lesson.id)).where(
            Lesson.tenant_id == tenant_id, Lesson.entity_id == entity_id
        )
        result = await self.session.execute(stmt)
        return int(result.scalar_one())

    async def count_conducted_in_period(
        self,
        tenant_id: int,
        entity_id: int,
        date_from: date,
        date_to: date,
    ) -> int:
        """Считает проведённые занятия сущности за период.

        Проведённым считается занятие с заполненным полем ``attendance``
        (в кабинете оно называется «Примечание»): пустое значение означает,
        что занятие не состоялось и с баланса не списывается.

        Args:
            tenant_id: Идентификатор тенанта.
            entity_id: Идентификатор ученика или группы.
            date_from: Начало периода включительно.
            date_to: Конец периода включительно.

        Returns:
            Количество проведённых занятий за период.
        """
        stmt = select(func.count(Lesson.id)).where(
            Lesson.tenant_id == tenant_id,
            Lesson.entity_id == entity_id,
            Lesson.lesson_date >= date_from,
            Lesson.lesson_date <= date_to,
            Lesson.attendance.is_not(None),
            func.trim(Lesson.attendance) != "",
        )
        result = await self.session.execute(stmt)
        return int(result.scalar_one())

    async def conducted_counts_in_period(
        self,
        tenant_id: int,
        date_from: date,
        date_to: date,
    ) -> dict[int, int]:
        """Считает проведённые занятия всех сущностей тенанта за период.

        Один запрос вместо отдельного обращения на каждого ученика (N+1).
        Проведённым считается занятие с заполненным «Примечанием».

        Args:
            tenant_id: Идентификатор тенанта.
            date_from: Начало периода включительно.
            date_to: Конец периода включительно.

        Returns:
            Словарь ``entity_id`` → количество проведённых занятий.
            Сущности без проведённых занятий в словарь не попадают.
        """
        stmt = (
            select(Lesson.entity_id, func.count(Lesson.id))
            .where(
                Lesson.tenant_id == tenant_id,
                Lesson.lesson_date >= date_from,
                Lesson.lesson_date <= date_to,
                Lesson.attendance.is_not(None),
                func.trim(Lesson.attendance) != "",
            )
            .group_by(Lesson.entity_id)
        )
        result = await self.session.execute(stmt)
        return {int(entity_id): int(count) for entity_id, count in result.all()}

    async def update(self, tenant_id: int, lesson_id: int, **fields: Any) -> Lesson | None:
        """Обновляет поля урока."""
        if not fields:
            return await self.get_by_id(tenant_id, lesson_id)
        stmt = (
            update(Lesson)
            .where(Lesson.id == lesson_id, Lesson.tenant_id == tenant_id)
            .values(**fields)
        )
        await self.session.execute(stmt)
        await self.session.flush()
        return await self.get_by_id(tenant_id, lesson_id)

    async def delete(self, tenant_id: int, lesson_id: int) -> bool:
        """Удаляет урок."""
        stmt = delete(Lesson).where(Lesson.id == lesson_id, Lesson.tenant_id == tenant_id)
        result = await self.session.execute(stmt)
        await self.session.flush()
        return result.rowcount > 0


class PaymentRepository:
    """Операции с платежами."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(
        self,
        tenant_id: int,
        entity_id: int,
        payment_date: date,
        amount: float | None = None,
        currency: str = "ILS",
        lessons_added: int | None = None,
    ) -> Payment:
        """Создаёт запись о платеже."""
        payment = Payment(
            tenant_id=tenant_id,
            entity_id=entity_id,
            payment_date=payment_date,
            amount=amount,
            currency=currency,
            lessons_added=lessons_added,
        )
        self.session.add(payment)
        await self.session.flush()
        logger.info(
            "Создан платёж id=%s entity_id=%s amount=%s lessons_added=%s",
            payment.id,
            entity_id,
            amount,
            lessons_added,
        )
        return payment

    async def get_by_id(self, tenant_id: int, payment_id: int) -> Payment | None:
        """Возвращает платёж по id в рамках тенанта."""
        stmt = select(Payment).where(Payment.id == payment_id, Payment.tenant_id == tenant_id)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_by_entity(
        self,
        tenant_id: int,
        entity_id: int,
        limit: int | None = None,
    ) -> list[Payment]:
        """Возвращает историю платежей сущности, новые сверху."""
        stmt = (
            select(Payment)
            .where(Payment.tenant_id == tenant_id, Payment.entity_id == entity_id)
            .order_by(Payment.payment_date.desc(), Payment.id.desc())
        )
        if limit:
            stmt = stmt.limit(limit)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_last_payment(self, tenant_id: int, entity_id: int) -> Payment | None:
        """Возвращает последний платёж сущности (для квитанции на карточке)."""
        payments = await self.list_by_entity(tenant_id, entity_id, limit=1)
        return payments[0] if payments else None

    async def total_amount(self, tenant_id: int, entity_id: int) -> float:
        """Суммарная оплата по сущности."""
        stmt = select(func.coalesce(func.sum(Payment.amount), 0.0)).where(
            Payment.tenant_id == tenant_id, Payment.entity_id == entity_id
        )
        result = await self.session.execute(stmt)
        return float(result.scalar_one())

    async def total_amount_in_period(
        self,
        tenant_id: int,
        entity_id: int,
        date_from: date,
        date_to: date,
    ) -> float:
        """Суммарная оплата сущности за период (включительно).

        Используется для расчёта долга группы за календарный месяц.

        Args:
            tenant_id: Идентификатор тенанта.
            entity_id: Идентификатор ученика или группы.
            date_from: Начало периода включительно.
            date_to: Конец периода включительно.

        Returns:
            Сумма платежей за период (0.0, если платежей не было).
        """
        stmt = select(func.coalesce(func.sum(Payment.amount), 0.0)).where(
            Payment.tenant_id == tenant_id,
            Payment.entity_id == entity_id,
            Payment.payment_date >= date_from,
            Payment.payment_date <= date_to,
        )
        result = await self.session.execute(stmt)
        return float(result.scalar_one())

    async def totals_in_period(
        self,
        tenant_id: int,
        date_from: date,
        date_to: date,
    ) -> dict[int, float]:
        """Суммы платежей всех сущностей тенанта за период одним запросом.

        Используется списком учеников, чтобы не делать отдельный запрос
        на каждого ученика (N+1).

        Args:
            tenant_id: Идентификатор тенанта.
            date_from: Начало периода включительно.
            date_to: Конец периода включительно.

        Returns:
            Словарь ``entity_id`` → сумма платежей за период. Сущности без
            платежей в словарь не попадают.
        """
        stmt = (
            select(
                Payment.entity_id,
                func.coalesce(func.sum(Payment.amount), 0.0),
            )
            .where(
                Payment.tenant_id == tenant_id,
                Payment.payment_date >= date_from,
                Payment.payment_date <= date_to,
            )
            .group_by(Payment.entity_id)
        )
        result = await self.session.execute(stmt)
        return {int(entity_id): float(total) for entity_id, total in result.all()}

    async def delete(self, tenant_id: int, payment_id: int) -> bool:
        """Удаляет платёж."""
        stmt = delete(Payment).where(Payment.id == payment_id, Payment.tenant_id == tenant_id)
        result = await self.session.execute(stmt)
        await self.session.flush()
        return result.rowcount > 0


class ScheduleRepository:
    """Операции с регулярными слотами расписания."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(
        self,
        tenant_id: int,
        entity_id: int,
        weekday: str,
        start_time: str,
        duration_min: int = 60,
        location: str | None = None,
        is_active: bool = True,
    ) -> ScheduleSlot:
        """Создаёт регулярный слот расписания."""
        slot = ScheduleSlot(
            tenant_id=tenant_id,
            entity_id=entity_id,
            weekday=weekday,
            start_time=start_time,
            duration_min=duration_min,
            location=location,
            is_active=is_active,
        )
        self.session.add(slot)
        await self.session.flush()
        logger.info(
            "Создан слот id=%s entity_id=%s %s %s",
            slot.id,
            entity_id,
            weekday,
            start_time,
        )
        return slot

    async def get_by_id(self, tenant_id: int, slot_id: int) -> ScheduleSlot | None:
        """Возвращает слот по id в рамках тенанта."""
        stmt = select(ScheduleSlot).where(
            ScheduleSlot.id == slot_id, ScheduleSlot.tenant_id == tenant_id
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_by_tenant(
        self, tenant_id: int, *, only_active: bool = False
    ) -> list[ScheduleSlot]:
        """Возвращает все слоты тенанта, отсортированные по дню и времени."""
        stmt = select(ScheduleSlot).where(ScheduleSlot.tenant_id == tenant_id)
        if only_active:
            stmt = stmt.where(ScheduleSlot.is_active.is_(True))
        stmt = stmt.order_by(ScheduleSlot.weekday, ScheduleSlot.start_time, ScheduleSlot.id)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def list_for_weekday(
        self, tenant_id: int, weekday: str, *, only_active: bool = True
    ) -> list[ScheduleSlot]:
        """Возвращает слоты тенанта на конкретный день недели."""
        stmt = select(ScheduleSlot).where(
            ScheduleSlot.tenant_id == tenant_id,
            ScheduleSlot.weekday == weekday.lower(),
        )
        if only_active:
            stmt = stmt.where(ScheduleSlot.is_active.is_(True))
        stmt = stmt.order_by(ScheduleSlot.start_time, ScheduleSlot.id)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def list_by_entity(self, tenant_id: int, entity_id: int) -> list[ScheduleSlot]:
        """Возвращает слоты конкретного ученика или группы."""
        stmt = (
            select(ScheduleSlot)
            .where(
                ScheduleSlot.tenant_id == tenant_id,
                ScheduleSlot.entity_id == entity_id,
            )
            .order_by(ScheduleSlot.weekday, ScheduleSlot.start_time, ScheduleSlot.id)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def update(self, tenant_id: int, slot_id: int, **fields: Any) -> ScheduleSlot | None:
        """Обновляет поля слота."""
        allowed = {
            "entity_id",
            "weekday",
            "start_time",
            "duration_min",
            "location",
            "is_active",
        }
        payload = {key: value for key, value in fields.items() if key in allowed}
        if not payload:
            return await self.get_by_id(tenant_id, slot_id)
        stmt = (
            update(ScheduleSlot)
            .where(ScheduleSlot.id == slot_id, ScheduleSlot.tenant_id == tenant_id)
            .values(**payload)
        )
        await self.session.execute(stmt)
        await self.session.flush()
        return await self.get_by_id(tenant_id, slot_id)

    async def delete(self, tenant_id: int, slot_id: int) -> bool:
        """Удаляет слот расписания.

        История отклонений сохраняется: FK ``slot_id`` обнуляется на уровне
        БД (``ondelete="SET NULL"``). После удаления помечаем устаревшие
        объекты исключений как expired, чтобы ORM перечитала ``slot_id``
        из БД, а не отдавала значение из identity map.
        """
        stmt = delete(ScheduleSlot).where(
            ScheduleSlot.id == slot_id, ScheduleSlot.tenant_id == tenant_id
        )
        result = await self.session.execute(stmt)
        await self.session.flush()
        for obj in list(self.session.identity_map.values()):
            if isinstance(obj, ScheduleException):
                self.session.expire(obj, ["slot_id"])
        return result.rowcount > 0


class ScheduleExceptionRepository:
    """Операции с разовыми отклонениями от расписания."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(
        self,
        tenant_id: int,
        entity_id: int,
        exception_date: date,
        kind: str,
        slot_id: int | None = None,
        new_time: str | None = None,
        note: str | None = None,
    ) -> ScheduleException:
        """Создаёт разовое отклонение (перенос, отмена, доп. занятие)."""
        exception = ScheduleException(
            tenant_id=tenant_id,
            entity_id=entity_id,
            slot_id=slot_id,
            exception_date=exception_date,
            kind=kind,
            new_time=new_time,
            note=note,
        )
        self.session.add(exception)
        await self.session.flush()
        logger.info(
            "Создано отклонение id=%s entity_id=%s date=%s kind=%s",
            exception.id,
            entity_id,
            exception_date,
            kind,
        )
        return exception

    async def get_by_id(self, tenant_id: int, exception_id: int) -> ScheduleException | None:
        """Возвращает отклонение по id в рамках тенанта."""
        stmt = select(ScheduleException).where(
            ScheduleException.id == exception_id,
            ScheduleException.tenant_id == tenant_id,
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_by_date(self, tenant_id: int, target_date: date) -> list[ScheduleException]:
        """Возвращает все отклонения тенанта на конкретную дату."""
        stmt = (
            select(ScheduleException)
            .where(
                ScheduleException.tenant_id == tenant_id,
                ScheduleException.exception_date == target_date,
            )
            .order_by(ScheduleException.new_time, ScheduleException.id)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def list_by_date_range(
        self, tenant_id: int, start_date: date, end_date: date
    ) -> list[ScheduleException]:
        """Возвращает отклонения за период включительно (для недельной сетки)."""
        stmt = (
            select(ScheduleException)
            .where(
                ScheduleException.tenant_id == tenant_id,
                ScheduleException.exception_date >= start_date,
                ScheduleException.exception_date <= end_date,
            )
            .order_by(ScheduleException.exception_date, ScheduleException.id)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def list_by_entity(self, tenant_id: int, entity_id: int) -> list[ScheduleException]:
        """Возвращает отклонения конкретного ученика."""
        stmt = (
            select(ScheduleException)
            .where(
                ScheduleException.tenant_id == tenant_id,
                ScheduleException.entity_id == entity_id,
            )
            .order_by(ScheduleException.exception_date.desc(), ScheduleException.id.desc())
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def delete(self, tenant_id: int, exception_id: int) -> bool:
        """Удаляет отклонение."""
        stmt = delete(ScheduleException).where(
            ScheduleException.id == exception_id,
            ScheduleException.tenant_id == tenant_id,
        )
        result = await self.session.execute(stmt)
        await self.session.flush()
        return result.rowcount > 0


class ReminderRuleRepository:
    """Операции с правилами напоминаний."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(
        self,
        tenant_id: int,
        kind: str,
        time_of_day: str | None = None,
        offset_minutes: int | None = None,
        is_enabled: bool = True,
        channel: str = "telegram",
    ) -> ReminderRule:
        """Создаёт правило напоминания."""
        rule = ReminderRule(
            tenant_id=tenant_id,
            kind=kind,
            time_of_day=time_of_day,
            offset_minutes=offset_minutes,
            is_enabled=is_enabled,
            channel=channel,
        )
        self.session.add(rule)
        await self.session.flush()
        logger.info("Создано правило напоминания id=%s kind=%s", rule.id, kind)
        return rule

    async def get_by_id(self, tenant_id: int, rule_id: int) -> ReminderRule | None:
        """Возвращает правило по id в рамках тенанта."""
        stmt = select(ReminderRule).where(
            ReminderRule.id == rule_id, ReminderRule.tenant_id == tenant_id
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_kind(self, tenant_id: int, kind: str) -> ReminderRule | None:
        """Возвращает правило по типу (тип уникален в рамках тенанта)."""
        stmt = select(ReminderRule).where(
            ReminderRule.tenant_id == tenant_id, ReminderRule.kind == kind
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_by_tenant(
        self, tenant_id: int, *, only_enabled: bool = False
    ) -> list[ReminderRule]:
        """Возвращает все правила тенанта."""
        stmt = select(ReminderRule).where(ReminderRule.tenant_id == tenant_id)
        if only_enabled:
            stmt = stmt.where(ReminderRule.is_enabled.is_(True))
        stmt = stmt.order_by(ReminderRule.id)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def update(self, tenant_id: int, rule_id: int, **fields: Any) -> ReminderRule | None:
        """Обновляет поля правила."""
        allowed = {"time_of_day", "offset_minutes", "is_enabled", "channel"}
        payload = {key: value for key, value in fields.items() if key in allowed}
        if not payload:
            return await self.get_by_id(tenant_id, rule_id)
        stmt = (
            update(ReminderRule)
            .where(ReminderRule.id == rule_id, ReminderRule.tenant_id == tenant_id)
            .values(**payload)
        )
        await self.session.execute(stmt)
        await self.session.flush()
        return await self.get_by_id(tenant_id, rule_id)

    async def delete(self, tenant_id: int, rule_id: int) -> bool:
        """Удаляет правило."""
        stmt = delete(ReminderRule).where(
            ReminderRule.id == rule_id, ReminderRule.tenant_id == tenant_id
        )
        result = await self.session.execute(stmt)
        await self.session.flush()
        return result.rowcount > 0


class ShareLinkRepository:
    """Операции с публичными ссылками на конкретные занятия."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(
        self,
        tenant_id: int,
        lesson_id: int,
        expires_at: datetime | None = None,
        token: str | None = None,
    ) -> LessonShareLink:
        """Создаёт публичную ссылку на занятие."""
        link = LessonShareLink(
            tenant_id=tenant_id,
            lesson_id=lesson_id,
            token=token or generate_public_token(),
            expires_at=expires_at,
        )
        self.session.add(link)
        await self.session.flush()
        logger.info("Создана ссылка на урок id=%s lesson_id=%s", link.id, lesson_id)
        return link

    async def get_by_token(self, token: str) -> LessonShareLink | None:
        """Возвращает ссылку по токену (для страницы ``/lesson/{token}``).

        Токен глобально уникален, поэтому tenant_id не требуется.
        """
        stmt = select(LessonShareLink).where(LessonShareLink.token == token)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_id(self, tenant_id: int, link_id: int) -> LessonShareLink | None:
        """Возвращает ссылку по id в рамках тенанта."""
        stmt = select(LessonShareLink).where(
            LessonShareLink.id == link_id, LessonShareLink.tenant_id == tenant_id
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_by_lesson(self, tenant_id: int, lesson_id: int) -> list[LessonShareLink]:
        """Возвращает все ссылки конкретного занятия, новые сверху."""
        stmt = (
            select(LessonShareLink)
            .where(
                LessonShareLink.tenant_id == tenant_id,
                LessonShareLink.lesson_id == lesson_id,
            )
            .order_by(LessonShareLink.id.desc())
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_active_for_lesson(self, tenant_id: int, lesson_id: int) -> LessonShareLink | None:
        """Возвращает действующую (не отозванную и не истёкшую) ссылку занятия.

        SQLite хранит ``DateTime(timezone=True)`` без смещения, поэтому
        полученное значение может быть naive — приводим его к UTC вручную.
        """
        now = datetime.now(UTC)
        stmt = (
            select(LessonShareLink)
            .where(
                LessonShareLink.tenant_id == tenant_id,
                LessonShareLink.lesson_id == lesson_id,
                LessonShareLink.is_revoked.is_(False),
            )
            .order_by(LessonShareLink.id.desc())
        )
        result = await self.session.execute(stmt)
        for link in result.scalars().all():
            expires_at = link.expires_at
            if expires_at is None:
                return link
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=UTC)
            if expires_at > now:
                return link
        return None

    async def revoke(self, tenant_id: int, link_id: int) -> LessonShareLink | None:
        """Отзывает ссылку, не удаляя её из истории."""
        stmt = (
            update(LessonShareLink)
            .where(
                LessonShareLink.id == link_id,
                LessonShareLink.tenant_id == tenant_id,
            )
            .values(is_revoked=True)
        )
        await self.session.execute(stmt)
        await self.session.flush()
        logger.info("Отозвана ссылка id=%s", link_id)
        return await self.get_by_id(tenant_id, link_id)

    async def delete(self, tenant_id: int, link_id: int) -> bool:
        """Удаляет ссылку."""
        stmt = delete(LessonShareLink).where(
            LessonShareLink.id == link_id, LessonShareLink.tenant_id == tenant_id
        )
        result = await self.session.execute(stmt)
        await self.session.flush()
        return result.rowcount > 0


class AdminSessionRepository:
    """Операции с сессиями тренера в веб-кабинете."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(
        self,
        tenant_id: int,
        expires_at: datetime,
        token: str | None = None,
    ) -> AdminSession:
        """Создаёт сессию кабинета (cookie-токен)."""
        admin_session = AdminSession(
            tenant_id=tenant_id,
            token=token or generate_admin_token(),
            expires_at=expires_at,
        )
        self.session.add(admin_session)
        await self.session.flush()
        logger.info("Создана сессия кабинета id=%s tenant_id=%s", admin_session.id, tenant_id)
        return admin_session

    async def get_by_token(self, token: str) -> AdminSession | None:
        """Возвращает сессию по токену из cookie."""
        stmt = select(AdminSession).where(AdminSession.token == token)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_valid(self, token: str) -> AdminSession | None:
        """Возвращает сессию, только если она ещё не истекла.

        SQLite хранит ``DateTime(timezone=True)`` без смещения, поэтому
        полученное значение может быть naive — приводим его к UTC вручную.
        """
        admin_session = await self.get_by_token(token)
        if admin_session is None:
            return None
        expires_at = admin_session.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if expires_at <= datetime.now(UTC):
            return None
        return admin_session

    async def delete(self, token: str) -> bool:
        """Удаляет сессию (выход из кабинета)."""
        stmt = delete(AdminSession).where(AdminSession.token == token)
        result = await self.session.execute(stmt)
        await self.session.flush()
        return result.rowcount > 0

    async def delete_expired(self, *, now: datetime | None = None) -> int:
        """Удаляет все истёкшие сессии, возвращает их количество."""
        moment = now or datetime.now(UTC)
        stmt = delete(AdminSession).where(AdminSession.expires_at <= moment)
        result = await self.session.execute(stmt)
        await self.session.flush()
        return result.rowcount or 0

    async def delete_by_tenant(self, tenant_id: int) -> int:
        """Удаляет все сессии тенанта (например, при смене admin_token)."""
        stmt = delete(AdminSession).where(AdminSession.tenant_id == tenant_id)
        result = await self.session.execute(stmt)
        await self.session.flush()
        return result.rowcount or 0


class AuditLogRepository:
    """Операции с журналом действий тренера."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(
        self,
        tenant_id: int,
        action: str,
        payload: dict[str, Any] | str | None = None,
        actor: str = "trainer",
    ) -> AuditLog:
        """Записывает действие в журнал.

        Словарь ``payload`` сериализуется в JSON-строку: колонка в БД
        имеет тип ``Text``, поэтому SQLite не умеет связывать dict напрямую.
        """
        entry = AuditLog(
            tenant_id=tenant_id,
            actor=actor,
            action=action,
            payload=_dump_payload(payload),
        )
        self.session.add(entry)
        await self.session.flush()
        logger.info("Аудит: tenant_id=%s action=%s", tenant_id, action)
        return entry

    async def list_by_tenant(self, tenant_id: int, limit: int | None = None) -> list[AuditLog]:
        """Возвращает журнал тенанта, новые сверху."""
        stmt = (
            select(AuditLog)
            .where(AuditLog.tenant_id == tenant_id)
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        )
        if limit:
            stmt = stmt.limit(limit)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())


class DispatchLogRepository:
    """Операции с журналом отправленных напоминаний."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(
        self,
        tenant_id: int,
        rule_kind: str,
        dispatch_date: date,
        entity_id: int | None = None,
    ) -> DispatchLog:
        """Фиксирует факт отправки напоминания."""
        entry = DispatchLog(
            tenant_id=tenant_id,
            entity_id=entity_id,
            rule_kind=rule_kind,
            dispatch_date=dispatch_date,
        )
        self.session.add(entry)
        await self.session.flush()
        return entry

    async def was_sent(
        self,
        tenant_id: int,
        rule_kind: str,
        dispatch_date: date,
        entity_id: int | None = None,
    ) -> bool:
        """Проверяет, отправляли ли уже это напоминание.

        Защита от дублирования: одна и та же комбинация
        (тенант, ученик, тип правила, дата) отправляется один раз.
        """
        stmt = select(DispatchLog.id).where(
            DispatchLog.tenant_id == tenant_id,
            DispatchLog.rule_kind == rule_kind,
            DispatchLog.dispatch_date == dispatch_date,
        )
        if entity_id is None:
            stmt = stmt.where(DispatchLog.entity_id.is_(None))
        else:
            stmt = stmt.where(DispatchLog.entity_id == entity_id)
        result = await self.session.execute(stmt.limit(1))
        return result.scalar_one_or_none() is not None

    async def list_by_date(self, tenant_id: int, dispatch_date: date) -> list[DispatchLog]:
        """Возвращает все отправки тенанта за дату."""
        stmt = (
            select(DispatchLog)
            .where(
                DispatchLog.tenant_id == tenant_id,
                DispatchLog.dispatch_date == dispatch_date,
            )
            .order_by(DispatchLog.id)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def last_sent_date(self, tenant_id: int, rule_kind: str) -> date | None:
        """Возвращает дату последней успешной отправки правила.

        В отличие от :meth:`was_sent`, учитывает записи по всем ученикам:
        тренеру нужно видеть, что рассылка по правилу вообще работала.

        Args:
            tenant_id: Идентификатор тенанта.
            rule_kind: Вид правила (``day_before`` / ``morning_of``).

        Returns:
            Дата последней отправки или ``None``, если отправок не было.
        """
        stmt = select(func.max(DispatchLog.dispatch_date)).where(
            DispatchLog.tenant_id == tenant_id,
            DispatchLog.rule_kind == rule_kind,
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def delete_by_date(self, tenant_id: int, dispatch_date: date) -> int:
        """Сбрасывает журнал за дату (для повторной отправки вручную)."""
        stmt = delete(DispatchLog).where(
            DispatchLog.tenant_id == tenant_id,
            DispatchLog.dispatch_date == dispatch_date,
        )
        result = await self.session.execute(stmt)
        await self.session.flush()
        return result.rowcount or 0


class Repository:
    """Фасад над всеми репозиториями.

    Удобно передавать один объект вместо множества.

    Пример:
        repo = Repository(session)
        entity = await repo.entities.get_by_name(tenant_id, "Миша")
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.tenants = TenantRepository(session)
        self.entities = EntityRepository(session)
        self.lessons = LessonRepository(session)
        self.payments = PaymentRepository(session)
        self.schedule = ScheduleRepository(session)
        self.schedule_exceptions = ScheduleExceptionRepository(session)
        self.reminders = ReminderRuleRepository(session)
        self.share_links = ShareLinkRepository(session)
        self.admin_sessions = AdminSessionRepository(session)
        self.audit = AuditLogRepository(session)
        self.dispatch_log = DispatchLogRepository(session)
