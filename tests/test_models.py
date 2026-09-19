"""Тесты схемы БД: новые модели web-first архитектуры.

Проверяем:
    * регистрацию всех таблиц в ``Base.metadata``;
    * значения по умолчанию у новых полей ``Tenant``;
    * создание и связи ``ScheduleSlot`` / ``ScheduleException``;
    * каскадное удаление слотов и исключений вместе с учеником;
    * поведение ``ondelete="SET NULL"`` для ``ScheduleException.slot_id``;
    * ограничения CHECK на день недели и тип отклонения;
    * уникальность ``ReminderRule`` по паре (tenant, kind);
    * уникальность токенов ``LessonShareLink`` и ``AdminSession``;
    * каскадное удаление ссылок вместе с уроком;
    * защиту от дублирования в ``DispatchLog``;
    * каскадное удаление всех дочерних записей вместе с тенантом.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    AdminSession,
    AuditLog,
    Base,
    DispatchLog,
    Entity,
    Lesson,
    LessonShareLink,
    ReminderRule,
    ScheduleException,
    ScheduleSlot,
    Tenant,
)

TODAY = date(2026, 9, 17)
TOMORROW = date(2026, 9, 18)


async def _make_tenant(
    session: AsyncSession, *, name: str = "Тренер", telegram_user_id: int = 1001
) -> Tenant:
    """Создаёт тенанта и возвращает его.

    ``telegram_user_id`` уникален, поэтому при создании нескольких тенантов
    в одном тесте его нужно передавать явно.
    """
    tenant = Tenant(name=name, telegram_user_id=telegram_user_id)
    session.add(tenant)
    await session.flush()
    return tenant


async def _make_entity(session: AsyncSession, tenant: Tenant, *, name: str = "Миша") -> Entity:
    """Создаёт ученика и возвращает его."""
    entity = Entity(tenant_id=tenant.id, name=name, entity_type="student")
    session.add(entity)
    await session.flush()
    return entity


async def _make_lesson(session: AsyncSession, tenant: Tenant, entity: Entity) -> Lesson:
    """Создаёт урок и возвращает его."""
    lesson = Lesson(
        tenant_id=tenant.id,
        entity_id=entity.id,
        lesson_date=TODAY,
        topic="Дебюты",
    )
    session.add(lesson)
    await session.flush()
    return lesson


class TestSchemaRegistration:
    """Проверки регистрации таблиц в метаданных."""

    def test_all_tables_registered(self) -> None:
        """Все 11 таблиц присутствуют в метаданных."""
        expected = {
            "tenants",
            "entities",
            "lessons",
            "payments",
            "schedule_slots",
            "schedule_exceptions",
            "reminder_rules",
            "lesson_share_links",
            "admin_sessions",
            "audit_logs",
            "dispatch_log",
        }
        assert expected <= set(Base.metadata.tables)

    def test_new_tables_have_expected_columns(self) -> None:
        """Ключевые колонки новых таблиц объявлены."""
        assert "weekday" in Base.metadata.tables["schedule_slots"].columns
        assert "exception_date" in Base.metadata.tables["schedule_exceptions"].columns
        assert "time_of_day" in Base.metadata.tables["reminder_rules"].columns
        assert "expires_at" in Base.metadata.tables["lesson_share_links"].columns
        assert "expires_at" in Base.metadata.tables["admin_sessions"].columns
        assert "payload" in Base.metadata.tables["audit_logs"].columns
        assert "dispatch_date" in Base.metadata.tables["dispatch_log"].columns


class TestTenantSettings:
    """Проверки новых полей и значений по умолчанию у тенанта."""

    async def test_defaults_applied(self, session: AsyncSession) -> None:
        """Новый тенант получает настройки по умолчанию."""
        tenant = await _make_tenant(session)

        assert tenant.admin_token
        assert len(tenant.admin_token) >= 16
        assert tenant.timezone == "Asia/Jerusalem"
        assert tenant.lesson_price == 0.0
        assert tenant.currency == "ILS"
        assert tenant.payment_instructions is None

    async def test_admin_token_is_unique(self, session: AsyncSession) -> None:
        """Два тенанта не могут иметь одинаковый admin_token."""
        first = await _make_tenant(session, name="Первый", telegram_user_id=1001)
        second = await _make_tenant(session, name="Второй", telegram_user_id=1002)

        assert first.admin_token != second.admin_token

        second.admin_token = first.admin_token
        with pytest.raises(IntegrityError):
            await session.flush()

    async def test_settings_can_be_overridden(self, session: AsyncSession) -> None:
        """Настройки тренера можно изменить."""
        tenant = await _make_tenant(session)
        tenant.lesson_price = 150.0
        tenant.currency = "USD"
        tenant.timezone = "Europe/Berlin"
        tenant.payment_instructions = "Перевод на карту"
        await session.flush()

        assert tenant.lesson_price == 150.0
        assert tenant.currency == "USD"
        assert tenant.timezone == "Europe/Berlin"
        assert tenant.payment_instructions == "Перевод на карту"


class TestScheduleSlot:
    """Проверки регулярных слотов расписания."""

    async def test_create_with_defaults(self, session: AsyncSession) -> None:
        """Слот создаётся с длительностью 60 минут и активен."""
        tenant = await _make_tenant(session)
        entity = await _make_entity(session, tenant)

        slot = ScheduleSlot(
            tenant_id=tenant.id,
            entity_id=entity.id,
            weekday="mon",
            start_time="16:00",
        )
        session.add(slot)
        await session.flush()

        assert slot.id is not None
        assert slot.duration_min == 60
        assert slot.is_active is True
        assert slot.location is None
        assert slot.created_at is not None

    async def test_relationship_to_entity(self, session: AsyncSession) -> None:
        """Слот связан с учеником и попадает в его коллекцию."""
        tenant = await _make_tenant(session)
        entity = await _make_entity(session, tenant)

        slot = ScheduleSlot(
            tenant_id=tenant.id,
            entity_id=entity.id,
            weekday="tue",
            start_time="17:30",
            location="Онлайн",
        )
        session.add(slot)
        await session.flush()
        await session.refresh(entity, ["schedule_slots"])

        assert slot.entity.name == "Миша"
        assert slot.tenant.name == "Тренер"
        assert [s.id for s in entity.schedule_slots] == [slot.id]

    async def test_invalid_weekday_rejected(self, session: AsyncSession) -> None:
        """Недопустимый код дня недели нарушает CHECK-ограничение."""
        tenant = await _make_tenant(session)
        entity = await _make_entity(session, tenant)

        session.add(
            ScheduleSlot(
                tenant_id=tenant.id,
                entity_id=entity.id,
                weekday="monday",
                start_time="16:00",
            )
        )
        with pytest.raises(IntegrityError):
            await session.flush()

    async def test_cascade_delete_with_entity(self, session: AsyncSession) -> None:
        """Удаление ученика удаляет его слоты."""
        tenant = await _make_tenant(session)
        entity = await _make_entity(session, tenant)
        session.add(
            ScheduleSlot(
                tenant_id=tenant.id,
                entity_id=entity.id,
                weekday="mon",
                start_time="16:00",
            )
        )
        await session.flush()

        await session.delete(entity)
        await session.flush()

        remaining = await session.scalar(select(func.count()).select_from(ScheduleSlot))
        assert remaining == 0

    async def test_repr(self, session: AsyncSession) -> None:
        """repr содержит день и время."""
        tenant = await _make_tenant(session)
        entity = await _make_entity(session, tenant)
        slot = ScheduleSlot(
            tenant_id=tenant.id,
            entity_id=entity.id,
            weekday="wed",
            start_time="18:00",
        )
        session.add(slot)
        await session.flush()

        assert "wed" in repr(slot)
        assert "18:00" in repr(slot)


class TestScheduleException:
    """Проверки разовых отклонений от расписания."""

    async def test_create_moved_exception(self, session: AsyncSession) -> None:
        """Перенос ссылается на слот и хранит новое время."""
        tenant = await _make_tenant(session)
        entity = await _make_entity(session, tenant)
        slot = ScheduleSlot(
            tenant_id=tenant.id,
            entity_id=entity.id,
            weekday="mon",
            start_time="16:00",
        )
        session.add(slot)
        await session.flush()

        exception = ScheduleException(
            tenant_id=tenant.id,
            entity_id=entity.id,
            slot_id=slot.id,
            exception_date=TOMORROW,
            kind="moved",
            new_time="18:00",
            note="Перенос из-за турнира",
        )
        session.add(exception)
        await session.flush()
        await session.refresh(slot, ["exceptions"])

        assert exception.id is not None
        assert exception.slot.id == slot.id
        assert exception.new_time == "18:00"
        assert [e.id for e in slot.exceptions] == [exception.id]

    async def test_extra_exception_without_slot(self, session: AsyncSession) -> None:
        """Дополнительное занятие может не ссылаться на слот."""
        tenant = await _make_tenant(session)
        entity = await _make_entity(session, tenant)

        exception = ScheduleException(
            tenant_id=tenant.id,
            entity_id=entity.id,
            slot_id=None,
            exception_date=TOMORROW,
            kind="extra",
        )
        session.add(exception)
        await session.flush()

        assert exception.slot is None
        assert exception.new_time is None

    async def test_invalid_kind_rejected(self, session: AsyncSession) -> None:
        """Недопустимый тип отклонения нарушает CHECK-ограничение."""
        tenant = await _make_tenant(session)
        entity = await _make_entity(session, tenant)

        session.add(
            ScheduleException(
                tenant_id=tenant.id,
                entity_id=entity.id,
                exception_date=TOMORROW,
                kind="postponed",
            )
        )
        with pytest.raises(IntegrityError):
            await session.flush()

    async def test_slot_delete_sets_null(self, session: AsyncSession) -> None:
        """Удаление слота обнуляет ссылку, но сохраняет отклонение."""
        tenant = await _make_tenant(session)
        entity = await _make_entity(session, tenant)
        slot = ScheduleSlot(
            tenant_id=tenant.id,
            entity_id=entity.id,
            weekday="mon",
            start_time="16:00",
        )
        session.add(slot)
        await session.flush()

        exception = ScheduleException(
            tenant_id=tenant.id,
            entity_id=entity.id,
            slot_id=slot.id,
            exception_date=TOMORROW,
            kind="cancelled",
        )
        session.add(exception)
        await session.flush()
        exception_id = exception.id

        await session.delete(slot)
        await session.flush()
        session.expire_all()

        stored = await session.get(ScheduleException, exception_id)
        assert stored is not None
        assert stored.slot_id is None

    async def test_cascade_delete_with_entity(self, session: AsyncSession) -> None:
        """Удаление ученика удаляет его отклонения."""
        tenant = await _make_tenant(session)
        entity = await _make_entity(session, tenant)
        session.add(
            ScheduleException(
                tenant_id=tenant.id,
                entity_id=entity.id,
                exception_date=TOMORROW,
                kind="extra",
            )
        )
        await session.flush()

        await session.delete(entity)
        await session.flush()

        remaining = await session.scalar(select(func.count()).select_from(ScheduleException))
        assert remaining == 0


class TestReminderRule:
    """Проверки правил напоминаний."""

    async def test_create_with_defaults(self, session: AsyncSession) -> None:
        """Правило создаётся включённым и с каналом telegram."""
        tenant = await _make_tenant(session)

        rule = ReminderRule(tenant_id=tenant.id, kind="day_before", time_of_day="20:00")
        session.add(rule)
        await session.flush()

        assert rule.id is not None
        assert rule.is_enabled is True
        assert rule.channel == "telegram"
        assert rule.offset_minutes is None

    async def test_unique_per_tenant_and_kind(self, session: AsyncSession) -> None:
        """Нельзя создать два правила одного типа у одного тенанта."""
        tenant = await _make_tenant(session)
        session.add(ReminderRule(tenant_id=tenant.id, kind="morning_of", time_of_day="08:00"))
        await session.flush()

        session.add(ReminderRule(tenant_id=tenant.id, kind="morning_of", time_of_day="09:00"))
        with pytest.raises(IntegrityError):
            await session.flush()

    async def test_same_kind_allowed_for_different_tenants(self, session: AsyncSession) -> None:
        """Одинаковые типы правил у разных тенантов допустимы."""
        first = await _make_tenant(session, name="Первый", telegram_user_id=1001)
        second = await _make_tenant(session, name="Второй", telegram_user_id=1002)

        session.add(ReminderRule(tenant_id=first.id, kind="day_before", time_of_day="20:00"))
        session.add(ReminderRule(tenant_id=second.id, kind="day_before", time_of_day="20:00"))
        await session.flush()

        total = await session.scalar(select(func.count()).select_from(ReminderRule))
        assert total == 2

    async def test_invalid_kind_rejected(self, session: AsyncSession) -> None:
        """Недопустимый тип правила нарушает CHECK-ограничение."""
        tenant = await _make_tenant(session)
        session.add(ReminderRule(tenant_id=tenant.id, kind="weekly_digest"))
        with pytest.raises(IntegrityError):
            await session.flush()

    async def test_cascade_delete_with_tenant(self, session: AsyncSession) -> None:
        """Удаление тенанта удаляет его правила."""
        tenant = await _make_tenant(session)
        session.add(ReminderRule(tenant_id=tenant.id, kind="day_before", time_of_day="20:00"))
        await session.flush()

        await session.delete(tenant)
        await session.flush()

        remaining = await session.scalar(select(func.count()).select_from(ReminderRule))
        assert remaining == 0


class TestLessonShareLink:
    """Проверки публичных ссылок на урок."""

    async def test_create_generates_token(self, session: AsyncSession) -> None:
        """Ссылка получает уникальный токен и не отозвана."""
        tenant = await _make_tenant(session)
        entity = await _make_entity(session, tenant)
        lesson = await _make_lesson(session, tenant, entity)

        link = LessonShareLink(tenant_id=tenant.id, lesson_id=lesson.id)
        session.add(link)
        await session.flush()

        assert link.id is not None
        assert link.token
        assert len(link.token) >= 16
        assert link.is_revoked is False
        assert link.expires_at is None

    async def test_relationship_to_lesson(self, session: AsyncSession) -> None:
        """Ссылка связана с уроком и попадает в его коллекцию."""
        tenant = await _make_tenant(session)
        entity = await _make_entity(session, tenant)
        lesson = await _make_lesson(session, tenant, entity)

        link = LessonShareLink(tenant_id=tenant.id, lesson_id=lesson.id)
        session.add(link)
        await session.flush()
        await session.refresh(lesson, ["share_links"])

        assert link.lesson.id == lesson.id
        assert [item.id for item in lesson.share_links] == [link.id]

    async def test_token_is_unique(self, session: AsyncSession) -> None:
        """Два токена не могут совпадать."""
        tenant = await _make_tenant(session)
        entity = await _make_entity(session, tenant)
        lesson = await _make_lesson(session, tenant, entity)

        first = LessonShareLink(tenant_id=tenant.id, lesson_id=lesson.id)
        session.add(first)
        await session.flush()

        session.add(LessonShareLink(tenant_id=tenant.id, lesson_id=lesson.id, token=first.token))
        with pytest.raises(IntegrityError):
            await session.flush()

    async def test_expiry_and_revocation(self, session: AsyncSession) -> None:
        """Ссылку можно ограничить по времени и отозвать."""
        tenant = await _make_tenant(session)
        entity = await _make_entity(session, tenant)
        lesson = await _make_lesson(session, tenant, entity)

        link = LessonShareLink(
            tenant_id=tenant.id,
            lesson_id=lesson.id,
            expires_at=datetime.now(UTC) + timedelta(days=7),
            is_revoked=True,
        )
        session.add(link)
        await session.flush()

        assert link.expires_at is not None
        assert link.is_revoked is True

    async def test_cascade_delete_with_lesson(self, session: AsyncSession) -> None:
        """Удаление урока удаляет его ссылки."""
        tenant = await _make_tenant(session)
        entity = await _make_entity(session, tenant)
        lesson = await _make_lesson(session, tenant, entity)
        session.add(LessonShareLink(tenant_id=tenant.id, lesson_id=lesson.id))
        await session.flush()

        await session.delete(lesson)
        await session.flush()

        remaining = await session.scalar(select(func.count()).select_from(LessonShareLink))
        assert remaining == 0


class TestAdminSession:
    """Проверки сессий веб-кабинета."""

    async def test_create_generates_token(self, session: AsyncSession) -> None:
        """Сессия получает уникальный токен."""
        tenant = await _make_tenant(session)

        admin_session = AdminSession(
            tenant_id=tenant.id,
            expires_at=datetime.now(UTC) + timedelta(days=30),
        )
        session.add(admin_session)
        await session.flush()

        assert admin_session.id is not None
        assert admin_session.token
        assert len(admin_session.token) >= 16

    async def test_token_is_unique(self, session: AsyncSession) -> None:
        """Две сессии не могут иметь одинаковый токен."""
        tenant = await _make_tenant(session)
        expires = datetime.now(UTC) + timedelta(days=30)

        first = AdminSession(tenant_id=tenant.id, expires_at=expires)
        session.add(first)
        await session.flush()

        session.add(AdminSession(tenant_id=tenant.id, expires_at=expires, token=first.token))
        with pytest.raises(IntegrityError):
            await session.flush()

    async def test_cascade_delete_with_tenant(self, session: AsyncSession) -> None:
        """Удаление тенанта удаляет его сессии."""
        tenant = await _make_tenant(session)
        session.add(
            AdminSession(
                tenant_id=tenant.id,
                expires_at=datetime.now(UTC) + timedelta(days=30),
            )
        )
        await session.flush()

        await session.delete(tenant)
        await session.flush()

        remaining = await session.scalar(select(func.count()).select_from(AdminSession))
        assert remaining == 0


class TestAuditLog:
    """Проверки журнала действий тренера."""

    async def test_create_with_defaults(self, session: AsyncSession) -> None:
        """Запись журнала создаётся с actor=trainer."""
        tenant = await _make_tenant(session)

        entry = AuditLog(tenant_id=tenant.id, action="student.create")
        session.add(entry)
        await session.flush()

        assert entry.id is not None
        assert entry.actor == "trainer"
        assert entry.payload is None
        assert entry.created_at is not None

    async def test_payload_is_stored(self, session: AsyncSession) -> None:
        """JSON-детали действия сохраняются."""
        tenant = await _make_tenant(session)

        entry = AuditLog(
            tenant_id=tenant.id,
            action="balance.adjust",
            payload='{"delta": 5}',
        )
        session.add(entry)
        await session.flush()

        assert entry.payload == '{"delta": 5}'

    async def test_cascade_delete_with_tenant(self, session: AsyncSession) -> None:
        """Удаление тенанта удаляет его журнал."""
        tenant = await _make_tenant(session)
        session.add(AuditLog(tenant_id=tenant.id, action="student.create"))
        await session.flush()

        await session.delete(tenant)
        await session.flush()

        remaining = await session.scalar(select(func.count()).select_from(AuditLog))
        assert remaining == 0


class TestDispatchLog:
    """Проверки журнала отправленных напоминаний."""

    async def test_create_for_entity(self, session: AsyncSession) -> None:
        """Запись журнала создаётся для ученика."""
        tenant = await _make_tenant(session)
        entity = await _make_entity(session, tenant)

        entry = DispatchLog(
            tenant_id=tenant.id,
            entity_id=entity.id,
            rule_kind="day_before",
            dispatch_date=TODAY,
        )
        session.add(entry)
        await session.flush()

        assert entry.id is not None
        assert entry.sent_at is not None
        assert entry.entity.name == "Миша"

    async def test_create_for_tenant_level(self, session: AsyncSession) -> None:
        """Напоминание уровня тренера хранится с entity_id=None."""
        tenant = await _make_tenant(session)

        entry = DispatchLog(
            tenant_id=tenant.id,
            entity_id=None,
            rule_kind="morning_of",
            dispatch_date=TODAY,
        )
        session.add(entry)
        await session.flush()

        assert entry.entity_id is None
        assert entry.entity is None

    async def test_duplicate_dispatch_rejected(self, session: AsyncSession) -> None:
        """Одно напоминание не может уйти дважды за день."""
        tenant = await _make_tenant(session)
        entity = await _make_entity(session, tenant)

        session.add(
            DispatchLog(
                tenant_id=tenant.id,
                entity_id=entity.id,
                rule_kind="day_before",
                dispatch_date=TODAY,
            )
        )
        await session.flush()

        session.add(
            DispatchLog(
                tenant_id=tenant.id,
                entity_id=entity.id,
                rule_kind="day_before",
                dispatch_date=TODAY,
            )
        )
        with pytest.raises(IntegrityError):
            await session.flush()

    async def test_different_dates_allowed(self, session: AsyncSession) -> None:
        """На следующий день то же напоминание допустимо."""
        tenant = await _make_tenant(session)
        entity = await _make_entity(session, tenant)

        session.add(
            DispatchLog(
                tenant_id=tenant.id,
                entity_id=entity.id,
                rule_kind="day_before",
                dispatch_date=TODAY,
            )
        )
        session.add(
            DispatchLog(
                tenant_id=tenant.id,
                entity_id=entity.id,
                rule_kind="day_before",
                dispatch_date=TOMORROW,
            )
        )
        await session.flush()

        total = await session.scalar(select(func.count()).select_from(DispatchLog))
        assert total == 2

    async def test_different_kinds_allowed(self, session: AsyncSession) -> None:
        """Разные типы напоминаний в один день допустимы."""
        tenant = await _make_tenant(session)
        entity = await _make_entity(session, tenant)

        session.add(
            DispatchLog(
                tenant_id=tenant.id,
                entity_id=entity.id,
                rule_kind="day_before",
                dispatch_date=TODAY,
            )
        )
        session.add(
            DispatchLog(
                tenant_id=tenant.id,
                entity_id=entity.id,
                rule_kind="morning_of",
                dispatch_date=TODAY,
            )
        )
        await session.flush()

        total = await session.scalar(select(func.count()).select_from(DispatchLog))
        assert total == 2

    async def test_cascade_delete_with_entity(self, session: AsyncSession) -> None:
        """Удаление ученика удаляет его записи журнала."""
        tenant = await _make_tenant(session)
        entity = await _make_entity(session, tenant)
        session.add(
            DispatchLog(
                tenant_id=tenant.id,
                entity_id=entity.id,
                rule_kind="day_before",
                dispatch_date=TODAY,
            )
        )
        await session.flush()

        await session.delete(entity)
        await session.flush()

        remaining = await session.scalar(select(func.count()).select_from(DispatchLog))
        assert remaining == 0


class TestTenantCascade:
    """Проверки каскадного удаления всех дочерних записей тенанта."""

    async def test_delete_tenant_removes_everything(self, session: AsyncSession) -> None:
        """Удаление тенанта очищает все связанные таблицы."""
        tenant = await _make_tenant(session)
        entity = await _make_entity(session, tenant)
        lesson = await _make_lesson(session, tenant, entity)
        slot = ScheduleSlot(
            tenant_id=tenant.id,
            entity_id=entity.id,
            weekday="mon",
            start_time="16:00",
        )
        session.add(slot)
        await session.flush()

        session.add_all(
            [
                ScheduleException(
                    tenant_id=tenant.id,
                    entity_id=entity.id,
                    slot_id=slot.id,
                    exception_date=TOMORROW,
                    kind="cancelled",
                ),
                ReminderRule(tenant_id=tenant.id, kind="day_before", time_of_day="20:00"),
                LessonShareLink(tenant_id=tenant.id, lesson_id=lesson.id),
                AdminSession(
                    tenant_id=tenant.id,
                    expires_at=datetime.now(UTC) + timedelta(days=30),
                ),
                AuditLog(tenant_id=tenant.id, action="student.create"),
                DispatchLog(
                    tenant_id=tenant.id,
                    entity_id=entity.id,
                    rule_kind="day_before",
                    dispatch_date=TODAY,
                ),
            ]
        )
        await session.flush()

        await session.delete(tenant)
        await session.flush()

        for model in (
            Entity,
            Lesson,
            ScheduleSlot,
            ScheduleException,
            ReminderRule,
            LessonShareLink,
            AdminSession,
            AuditLog,
            DispatchLog,
        ):
            remaining = await session.scalar(select(func.count()).select_from(model))
            assert remaining == 0, f"Остались записи в {model.__tablename__}"
