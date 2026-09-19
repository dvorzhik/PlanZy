"""ORM-модели PlanZy.

Схема спроектирована с расчётом на мультитенантность (SaaS):
каждая таблица данных содержит tenant_id, что обеспечивает
логическую изоляцию данных разных тренеров.
"""

from __future__ import annotations

import json
import secrets
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


def utcnow() -> datetime:
    """Текущее время в UTC (timezone-aware)."""
    return datetime.now(UTC)


def generate_public_token() -> str:
    """Генерирует криптографически стойкий публичный токен для ссылки на карточку."""
    return secrets.token_urlsafe(16)


def generate_admin_token() -> str:
    """Генерирует секретный токен для входа тренера в веб-кабинет."""
    return secrets.token_urlsafe(24)


# Коды дней недели в порядке, совпадающем с ``date.weekday()``.
WEEKDAY_CODES: tuple[str, ...] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

# Допустимые типы разовых отклонений от расписания.
SCHEDULE_EXCEPTION_KINDS: tuple[str, ...] = ("moved", "cancelled", "extra")

# Допустимые типы правил напоминаний.
REMINDER_KINDS: tuple[str, ...] = ("day_before", "morning_of")


class Tenant(Base):
    """Тренер (владелец аккаунта).

    На старте тенант один — сам автор проекта. При выходе в SaaS
    каждый новый тренер получает свою запись и изолированные данные.
    """

    __tablename__ = "tenants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    telegram_user_id: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        unique=True,
        comment="Telegram user_id тренера — используется для whitelist бота",
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # --- Доступ в веб-кабинет ---
    admin_token: Mapped[str] = mapped_column(
        String(64),
        default=generate_admin_token,
        nullable=False,
        unique=True,
        comment="Секретный токен для входа в кабинет по ссылке /admin/{token}",
    )

    # --- Настройки тренера (персональные, живут в БД, а не в .env) ---
    timezone: Mapped[str] = mapped_column(
        String(64), default="Asia/Jerusalem", nullable=False, comment="Часовой пояс тренера"
    )
    lesson_price: Mapped[float] = mapped_column(
        Float, default=0.0, nullable=False, comment="Стоимость одного занятия"
    )
    currency: Mapped[str] = mapped_column(
        String(8), default="ILS", nullable=False, comment="Валюта платежей"
    )
    payment_instructions: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="Порядок оплаты: реквизиты, правила, скидки"
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    # Связи
    entities: Mapped[list[Entity]] = relationship(
        back_populates="tenant", cascade="all, delete-orphan", lazy="selectin"
    )
    lessons: Mapped[list[Lesson]] = relationship(
        back_populates="tenant", cascade="all, delete-orphan"
    )
    payments: Mapped[list[Payment]] = relationship(
        back_populates="tenant", cascade="all, delete-orphan"
    )
    schedule_slots: Mapped[list[ScheduleSlot]] = relationship(
        back_populates="tenant", cascade="all, delete-orphan"
    )
    schedule_exceptions: Mapped[list[ScheduleException]] = relationship(
        back_populates="tenant", cascade="all, delete-orphan"
    )
    reminder_rules: Mapped[list[ReminderRule]] = relationship(
        back_populates="tenant", cascade="all, delete-orphan"
    )
    lesson_share_links: Mapped[list[LessonShareLink]] = relationship(
        back_populates="tenant", cascade="all, delete-orphan"
    )
    admin_sessions: Mapped[list[AdminSession]] = relationship(
        back_populates="tenant", cascade="all, delete-orphan"
    )
    audit_logs: Mapped[list[AuditLog]] = relationship(
        back_populates="tenant", cascade="all, delete-orphan"
    )
    dispatch_logs: Mapped[list[DispatchLog]] = relationship(
        back_populates="tenant", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Tenant id={self.id} name={self.name!r}>"


class Entity(Base):
    """Ученик или группа.

    entity_type различает одиночного ученика и группу.
    public_token используется для публичной ссылки /l/{token} без пароля.
    """

    __tablename__ = "entities"
    __table_args__ = (
        CheckConstraint(
            "entity_type IN ('student', 'group')",
            name="ck_entities_entity_type",
        ),
        UniqueConstraint("public_token", name="uq_entities_public_token"),
        Index("ix_entities_tenant_name", "tenant_id", "name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    entity_type: Mapped[str] = mapped_column(
        String(20), nullable=False, default="student", comment="student | group"
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    subject: Mapped[str] = mapped_column(String(255), default="Шахматы", nullable=False)
    parent_phone: Mapped[str | None] = mapped_column(
        String(32), nullable=True, comment="Телефон родителя в формате 972XXXXXXXXX"
    )
    public_token: Mapped[str] = mapped_column(
        String(64),
        default=generate_public_token,
        nullable=False,
        comment="Токен для публичной ссылки /l/{token}",
    )
    members: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Участники группы: имена через запятую или с новой строки",
    )
    whatsapp_chat_url: Mapped[str | None] = mapped_column(
        String(512),
        nullable=True,
        comment="Ссылка на групповой чат WhatsApp (https://chat.whatsapp.com/...)",
    )
    lesson_price: Mapped[float | None] = mapped_column(
        Float,
        nullable=True,
        comment="Индивидуальная цена занятия; NULL — использовать цену тенанта",
    )
    schedule_days: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="Дни недели через запятую: mon,tue,wed,thu,fri,sat,sun",
    )
    schedule_time: Mapped[str | None] = mapped_column(
        String(8), nullable=True, comment="Время занятия в формате ЧЧ:ММ"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    # Связи
    tenant: Mapped[Tenant] = relationship(back_populates="entities")
    lessons: Mapped[list[Lesson]] = relationship(
        back_populates="entity", cascade="all, delete-orphan", lazy="selectin"
    )
    payments: Mapped[list[Payment]] = relationship(
        back_populates="entity", cascade="all, delete-orphan", lazy="selectin"
    )
    schedule_slots: Mapped[list[ScheduleSlot]] = relationship(
        back_populates="entity", cascade="all, delete-orphan", lazy="selectin"
    )
    schedule_exceptions: Mapped[list[ScheduleException]] = relationship(
        back_populates="entity", cascade="all, delete-orphan"
    )
    dispatch_logs: Mapped[list[DispatchLog]] = relationship(
        back_populates="entity", cascade="all, delete-orphan"
    )

    @property
    def is_group(self) -> bool:
        """Признак того, что сущность — группа."""
        return self.entity_type == "group"

    @property
    def members_list(self) -> list[str]:
        """Участники группы в виде списка имён.

        Имена могут быть разделены запятыми или переводами строк.
        """
        if not self.members:
            return []
        normalized = self.members.replace("\r\n", "\n").replace("\r", "\n")
        parts: list[str] = []
        for chunk in normalized.split("\n"):
            parts.extend(chunk.split(","))
        return [name.strip() for name in parts if name.strip()]

    @property
    def schedule_days_list(self) -> list[str]:
        """Дни недели расписания в виде списка."""
        if not self.schedule_days:
            return []
        return [day.strip().lower() for day in self.schedule_days.split(",") if day.strip()]

    def __repr__(self) -> str:
        return f"<Entity id={self.id} type={self.entity_type} name={self.name!r}>"


class Lesson(Base):
    """Проведённый урок.

    Публичные поля (topic, homework) показываются родителю на карточке.
    internal_notes — приватные заметки тренера, никогда не попадают
    на публичную страницу.
    """

    __tablename__ = "lessons"
    __table_args__ = (
        Index("ix_lessons_entity_date", "entity_id", "lesson_date"),
        Index("ix_lessons_tenant_date", "tenant_id", "lesson_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    entity_id: Mapped[int] = mapped_column(
        ForeignKey("entities.id", ondelete="CASCADE"), nullable=False, index=True
    )
    lesson_date: Mapped[date] = mapped_column(Date, nullable=False)
    start_time: Mapped[str | None] = mapped_column(
        String(5), nullable=True, comment="Время начала занятия в формате ЧЧ:ММ"
    )
    topic: Mapped[str] = mapped_column(Text, nullable=False, comment="Публичная тема урока")
    homework: Mapped[str | None] = mapped_column(Text, nullable=True, comment="Публичное ДЗ")
    internal_notes: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="Приватные заметки: психология, слабости. Не публикуется."
    )
    rating: Mapped[str | None] = mapped_column(
        String(32), nullable=True, comment="Оценка: 3+, 8/10 и т.п."
    )
    next_step: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="План на следующий урок"
    )
    attendance: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="Для групп: кто присутствовал"
    )
    raw_gemini_json: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="Сырой ответ Gemini для отладки и аудита"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    # Связи
    tenant: Mapped[Tenant] = relationship(back_populates="lessons")
    entity: Mapped[Entity] = relationship(back_populates="lessons")
    share_links: Mapped[list[LessonShareLink]] = relationship(
        back_populates="lesson", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Lesson id={self.id} entity_id={self.entity_id} date={self.lesson_date}>"


class Payment(Base):
    """Платёж за абонемент.

    Пополняет баланс занятий сущности на lessons_added.
    """

    __tablename__ = "payments"
    __table_args__ = (Index("ix_payments_entity_date", "entity_id", "payment_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    entity_id: Mapped[int] = mapped_column(
        ForeignKey("entities.id", ondelete="CASCADE"), nullable=False, index=True
    )
    amount: Mapped[float | None] = mapped_column(Float, nullable=True, comment="Сумма платежа")
    currency: Mapped[str] = mapped_column(String(8), default="ILS", nullable=False)
    lessons_added: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="Сколько занятий добавлено к балансу"
    )
    payment_date: Mapped[date] = mapped_column(Date, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    # Связи
    tenant: Mapped[Tenant] = relationship(back_populates="payments")
    entity: Mapped[Entity] = relationship(back_populates="payments")

    def __repr__(self) -> str:
        return f"<Payment id={self.id} entity_id={self.entity_id} amount={self.amount}>"


class ScheduleSlot(Base):
    """Регулярный слот расписания: «по вторникам в 17:30 у Ани».

    Слот описывает повторяющееся занятие. Разовые отклонения
    (перенос, отмена, дополнительное занятие) хранятся в
    :class:`ScheduleException`.
    """

    __tablename__ = "schedule_slots"
    __table_args__ = (
        Index("ix_schedule_slots_tenant_weekday", "tenant_id", "weekday"),
        Index("ix_schedule_slots_entity", "entity_id"),
        CheckConstraint(
            "weekday IN ('mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun')",
            name="ck_schedule_slots_weekday",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    entity_id: Mapped[int] = mapped_column(
        ForeignKey("entities.id", ondelete="CASCADE"), nullable=False
    )
    weekday: Mapped[str] = mapped_column(
        String(3), nullable=False, comment="Код дня недели: mon..sun"
    )
    start_time: Mapped[str] = mapped_column(
        String(5), nullable=False, comment="Время начала в формате ЧЧ:ММ"
    )
    duration_min: Mapped[int] = mapped_column(
        Integer, default=60, nullable=False, comment="Длительность занятия в минутах"
    )
    location: Mapped[str | None] = mapped_column(
        String(255), nullable=True, comment="Место проведения (адрес, зал, онлайн)"
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False, comment="Выключенный слот не попадает в расписание"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    # Связи
    tenant: Mapped[Tenant] = relationship(back_populates="schedule_slots")
    entity: Mapped[Entity] = relationship(back_populates="schedule_slots")
    # Без ORM-каскада: при удалении слота история переносов и отмен должна
    # сохраниться, поэтому FK обнуляется на уровне БД (ondelete="SET NULL").
    exceptions: Mapped[list[ScheduleException]] = relationship(
        back_populates="slot", passive_deletes=True
    )

    def __repr__(self) -> str:
        return (
            f"<ScheduleSlot id={self.id} entity_id={self.entity_id} "
            f"{self.weekday} {self.start_time}>"
        )


class ScheduleException(Base):
    """Разовое отклонение от регулярного расписания на конкретную дату.

    ``kind``:
      * ``moved`` — занятие перенесено на другое время (``new_time``);
      * ``cancelled`` — занятие отменено;
      * ``extra`` — дополнительное занятие, которого нет в регулярном слоте.
    """

    __tablename__ = "schedule_exceptions"
    __table_args__ = (
        Index("ix_schedule_exceptions_tenant_date", "tenant_id", "exception_date"),
        Index("ix_schedule_exceptions_entity", "entity_id"),
        CheckConstraint(
            "kind IN ('moved', 'cancelled', 'extra')",
            name="ck_schedule_exceptions_kind",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    entity_id: Mapped[int] = mapped_column(
        ForeignKey("entities.id", ondelete="CASCADE"), nullable=False
    )
    slot_id: Mapped[int | None] = mapped_column(
        ForeignKey("schedule_slots.id", ondelete="SET NULL"),
        nullable=True,
        comment="Слот, к которому относится отклонение (NULL для extra)",
    )
    exception_date: Mapped[date] = mapped_column(
        Date, nullable=False, comment="Дата, на которую действует отклонение"
    )
    kind: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="moved | cancelled | extra"
    )
    new_time: Mapped[str | None] = mapped_column(
        String(5), nullable=True, comment="Новое время начала для kind=moved"
    )
    note: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="Комментарий тренера к отклонению"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    # Связи
    tenant: Mapped[Tenant] = relationship(back_populates="schedule_exceptions")
    entity: Mapped[Entity] = relationship(back_populates="schedule_exceptions")
    slot: Mapped[ScheduleSlot | None] = relationship(back_populates="exceptions")

    def __repr__(self) -> str:
        return f"<ScheduleException id={self.id} date={self.exception_date} kind={self.kind}>"


class ReminderRule(Base):
    """Правило напоминания, настроенное тренером.

    ``kind``:
      * ``day_before`` — напоминание накануне занятия;
      * ``morning_of`` — напоминание утром в день занятия.
    """

    __tablename__ = "reminder_rules"
    __table_args__ = (
        UniqueConstraint("tenant_id", "kind", name="uq_reminder_rules_tenant_kind"),
        CheckConstraint(
            "kind IN ('day_before', 'morning_of')",
            name="ck_reminder_rules_kind",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(24), nullable=False, comment="day_before | morning_of")
    time_of_day: Mapped[str | None] = mapped_column(
        String(5), nullable=True, comment="Время отправки ЧЧ:ММ (локальное время тренера)"
    )
    offset_minutes: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="Сдвиг в минутах (например, за 120 минут до занятия)"
    )
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    channel: Mapped[str] = mapped_column(
        String(16), default="telegram", nullable=False, comment="Канал доставки напоминания"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    # Связи
    tenant: Mapped[Tenant] = relationship(back_populates="reminder_rules")

    def __repr__(self) -> str:
        return f"<ReminderRule id={self.id} kind={self.kind} enabled={self.is_enabled}>"


class LessonShareLink(Base):
    """Публичная ссылка на конкретное занятие (для родителя или чата).

    Ссылку можно отозвать (``is_revoked``) или ограничить по времени
    (``expires_at``). Токен уникален глобально.
    """

    __tablename__ = "lesson_share_links"
    __table_args__ = (Index("ix_lesson_share_links_lesson", "lesson_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    lesson_id: Mapped[int] = mapped_column(
        ForeignKey("lessons.id", ondelete="CASCADE"), nullable=False
    )
    token: Mapped[str] = mapped_column(
        String(64),
        default=generate_public_token,
        nullable=False,
        unique=True,
        comment="Токен для ссылки /lesson/{token}",
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="Срок действия ссылки (NULL — бессрочно)"
    )
    is_revoked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    # Связи
    tenant: Mapped[Tenant] = relationship(back_populates="lesson_share_links")
    lesson: Mapped[Lesson] = relationship(back_populates="share_links")

    def __repr__(self) -> str:
        return f"<LessonShareLink id={self.id} lesson_id={self.lesson_id}>"


class AdminSession(Base):
    """Сессия тренера в веб-кабинете (cookie-токен)."""

    __tablename__ = "admin_sessions"
    __table_args__ = (Index("ix_admin_sessions_expires", "expires_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token: Mapped[str] = mapped_column(
        String(64),
        default=generate_admin_token,
        nullable=False,
        unique=True,
        comment="Значение cookie-сессии кабинета",
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="Момент истечения сессии"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    # Связи
    tenant: Mapped[Tenant] = relationship(back_populates="admin_sessions")

    def __repr__(self) -> str:
        return f"<AdminSession id={self.id} tenant_id={self.tenant_id}>"


class AuditLog(Base):
    """Журнал действий тренера в кабинете (кто, что и когда изменил)."""

    __tablename__ = "audit_logs"
    __table_args__ = (Index("ix_audit_logs_tenant_created", "tenant_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    actor: Mapped[str] = mapped_column(
        String(32), default="trainer", nullable=False, comment="Кто выполнил действие"
    )
    action: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="Код действия, например student.create"
    )
    payload: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="JSON с деталями изменения"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    # Связи
    tenant: Mapped[Tenant] = relationship(back_populates="audit_logs")

    @property
    def payload_dict(self) -> dict[str, Any]:
        """Возвращает ``payload`` как словарь (пустой, если данных нет).

        В БД payload хранится JSON-строкой, поэтому для удобства
        потребителей (кабинет, тесты) он разбирается обратно.
        """
        if not self.payload:
            return {}
        try:
            parsed = json.loads(self.payload)
        except (TypeError, ValueError):
            return {"raw": self.payload}
        return parsed if isinstance(parsed, dict) else {"value": parsed}

    def __repr__(self) -> str:
        return f"<AuditLog id={self.id} action={self.action!r}>"


class DispatchLog(Base):
    """Журнал отправленных напоминаний — защита от дублирования.

    Уникальность (tenant, entity, rule_kind, dispatch_date) гарантирует,
    что одно и то же напоминание не уйдёт дважды за день.
    """

    __tablename__ = "dispatch_log"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "entity_id",
            "rule_kind",
            "dispatch_date",
            name="uq_dispatch_log_once_per_day",
        ),
        Index("ix_dispatch_log_tenant_date", "tenant_id", "dispatch_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    entity_id: Mapped[int | None] = mapped_column(
        ForeignKey("entities.id", ondelete="CASCADE"),
        nullable=True,
        comment="NULL для напоминаний уровня тренера",
    )
    rule_kind: Mapped[str] = mapped_column(
        String(24), nullable=False, comment="Тип правила, по которому ушло напоминание"
    )
    dispatch_date: Mapped[date] = mapped_column(Date, nullable=False)
    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    # Связи
    tenant: Mapped[Tenant] = relationship(back_populates="dispatch_logs")
    entity: Mapped[Entity | None] = relationship(back_populates="dispatch_logs")

    def __repr__(self) -> str:
        return f"<DispatchLog id={self.id} kind={self.rule_kind} date={self.dispatch_date}>"
