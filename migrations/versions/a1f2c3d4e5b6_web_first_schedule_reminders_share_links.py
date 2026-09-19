"""web-first: расписание, напоминания, ссылки на уроки, сессии кабинета

Добавляет таблицы:
  * schedule_slots      — регулярные слоты расписания;
  * schedule_exceptions — разовые переносы/отмены/доп. занятия;
  * reminder_rules      — правила напоминаний;
  * lesson_share_links  — публичные ссылки на конкретный урок;
  * admin_sessions      — cookie-сессии веб-кабинета;
  * audit_logs          — журнал действий тренера;
  * dispatch_log        — журнал отправленных напоминаний.

Расширяет tenants полями: admin_token, timezone, lesson_price, currency,
payment_instructions, low_balance_threshold.

Переносит данные:
  * entities.schedule_days + schedule_time → schedule_slots;
  * lesson_price/currency из .env → tenants;
  * генерирует admin_token для существующих тенантов.

Revision ID: a1f2c3d4e5b6
Revises: ebb54b7015bc
Create Date: 2026-09-17 16:05:00.000000

"""

from __future__ import annotations

import secrets
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

# Идентификаторы ревизии
revision: str = "a1f2c3d4e5b6"
down_revision: str | None = "ebb54b7015bc"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Значения по умолчанию для переноса настроек из .env в БД.
DEFAULT_TIMEZONE = "Asia/Jerusalem"
DEFAULT_CURRENCY = "ILS"
DEFAULT_LESSON_PRICE = 0.0
DEFAULT_LOW_BALANCE_THRESHOLD = 1


def _generate_admin_token() -> str:
    """Генерирует секретный токен входа в кабинет (совпадает с моделью)."""
    return secrets.token_urlsafe(24)


def upgrade() -> None:
    """Применение миграции."""
    # --- 1. Расширяем tenants настройками тренера и токеном кабинета ---
    with op.batch_alter_table("tenants", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "admin_token",
                sa.String(length=64),
                nullable=True,
                comment="Секретный токен для входа в кабинет по ссылке /admin/{token}",
            )
        )
        batch_op.add_column(
            sa.Column(
                "timezone",
                sa.String(length=64),
                nullable=False,
                server_default=DEFAULT_TIMEZONE,
                comment="Часовой пояс тренера",
            )
        )
        batch_op.add_column(
            sa.Column(
                "lesson_price",
                sa.Float(),
                nullable=False,
                server_default=str(DEFAULT_LESSON_PRICE),
                comment="Стоимость одного занятия",
            )
        )
        batch_op.add_column(
            sa.Column(
                "currency",
                sa.String(length=8),
                nullable=False,
                server_default=DEFAULT_CURRENCY,
                comment="Валюта платежей",
            )
        )
        batch_op.add_column(
            sa.Column(
                "payment_instructions",
                sa.Text(),
                nullable=True,
                comment="Порядок оплаты: реквизиты, правила, скидки",
            )
        )
        batch_op.add_column(
            sa.Column(
                "low_balance_threshold",
                sa.Integer(),
                nullable=False,
                server_default=str(DEFAULT_LOW_BALANCE_THRESHOLD),
                comment="Порог «пора напомнить об оплате»",
            )
        )

    # Заполняем admin_token для уже существующих тенантов.
    connection = op.get_bind()
    # Единая метка времени для всех вставок данных (SQLite не умеет биндить
    # SQL-функции вроде CURRENT_TIMESTAMP как параметры запроса).
    now = datetime.now(UTC)
    tenant_rows = connection.execute(sa.text("SELECT id FROM tenants")).fetchall()
    for (tenant_id,) in tenant_rows:
        connection.execute(
            sa.text("UPDATE tenants SET admin_token = :token WHERE id = :tid"),
            {"token": _generate_admin_token(), "tid": tenant_id},
        )

    # Теперь, когда значения заполнены, включаем NOT NULL и UNIQUE.
    with op.batch_alter_table("tenants", schema=None) as batch_op:
        batch_op.alter_column("admin_token", existing_type=sa.String(length=64), nullable=False)
        batch_op.create_unique_constraint("uq_tenants_admin_token", ["admin_token"])

    # --- 2. Регулярные слоты расписания ---
    op.create_table(
        "schedule_slots",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("entity_id", sa.Integer(), nullable=False),
        sa.Column(
            "weekday", sa.String(length=3), nullable=False, comment="Код дня недели: mon..sun"
        ),
        sa.Column(
            "start_time",
            sa.String(length=5),
            nullable=False,
            comment="Время начала в формате ЧЧ:ММ",
        ),
        sa.Column(
            "duration_min",
            sa.Integer(),
            nullable=False,
            server_default="60",
            comment="Длительность занятия в минутах",
        ),
        sa.Column(
            "location",
            sa.String(length=255),
            nullable=True,
            comment="Место проведения (адрес, зал, онлайн)",
        ),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
            comment="Выключенный слот не попадает в расписание",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "weekday IN ('mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun')",
            name="ck_schedule_slots_weekday",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["entity_id"], ["entities.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("schedule_slots", schema=None) as batch_op:
        batch_op.create_index("ix_schedule_slots_tenant_id", ["tenant_id"], unique=False)
        batch_op.create_index(
            "ix_schedule_slots_tenant_weekday", ["tenant_id", "weekday"], unique=False
        )
        batch_op.create_index("ix_schedule_slots_entity", ["entity_id"], unique=False)

    # --- 3. Разовые отклонения от расписания ---
    op.create_table(
        "schedule_exceptions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("entity_id", sa.Integer(), nullable=False),
        sa.Column(
            "slot_id",
            sa.Integer(),
            nullable=True,
            comment="Слот, к которому относится отклонение (NULL для extra)",
        ),
        sa.Column(
            "exception_date",
            sa.Date(),
            nullable=False,
            comment="Дата, на которую действует отклонение",
        ),
        sa.Column(
            "kind", sa.String(length=16), nullable=False, comment="moved | cancelled | extra"
        ),
        sa.Column(
            "new_time",
            sa.String(length=5),
            nullable=True,
            comment="Новое время начала для kind=moved",
        ),
        sa.Column("note", sa.Text(), nullable=True, comment="Комментарий тренера к отклонению"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "kind IN ('moved', 'cancelled', 'extra')", name="ck_schedule_exceptions_kind"
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["entity_id"], ["entities.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["slot_id"], ["schedule_slots.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("schedule_exceptions", schema=None) as batch_op:
        batch_op.create_index("ix_schedule_exceptions_tenant_id", ["tenant_id"], unique=False)
        batch_op.create_index(
            "ix_schedule_exceptions_tenant_date", ["tenant_id", "exception_date"], unique=False
        )
        batch_op.create_index("ix_schedule_exceptions_entity", ["entity_id"], unique=False)

    # --- 4. Правила напоминаний ---
    op.create_table(
        "reminder_rules",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column(
            "kind",
            sa.String(length=24),
            nullable=False,
            comment="day_before | morning_of | low_balance",
        ),
        sa.Column(
            "time_of_day",
            sa.String(length=5),
            nullable=True,
            comment="Время отправки ЧЧ:ММ (локальное время тренера)",
        ),
        sa.Column(
            "offset_minutes",
            sa.Integer(),
            nullable=True,
            comment="Сдвиг в минутах (например, за 120 минут до занятия)",
        ),
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "channel",
            sa.String(length=16),
            nullable=False,
            server_default="telegram",
            comment="Канал доставки напоминания",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "kind IN ('day_before', 'morning_of', 'low_balance')", name="ck_reminder_rules_kind"
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "kind", name="uq_reminder_rules_tenant_kind"),
    )
    with op.batch_alter_table("reminder_rules", schema=None) as batch_op:
        batch_op.create_index("ix_reminder_rules_tenant_id", ["tenant_id"], unique=False)

    # --- 5. Публичные ссылки на конкретный урок ---
    op.create_table(
        "lesson_share_links",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("lesson_id", sa.Integer(), nullable=False),
        sa.Column(
            "token",
            sa.String(length=64),
            nullable=False,
            comment="Токен для ссылки /lesson/{token}",
        ),
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Срок действия ссылки (NULL — бессрочно)",
        ),
        sa.Column("is_revoked", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["lesson_id"], ["lessons.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token"),
    )
    with op.batch_alter_table("lesson_share_links", schema=None) as batch_op:
        batch_op.create_index("ix_lesson_share_links_tenant_id", ["tenant_id"], unique=False)
        batch_op.create_index("ix_lesson_share_links_lesson", ["lesson_id"], unique=False)

    # --- 6. Сессии веб-кабинета ---
    op.create_table(
        "admin_sessions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column(
            "token",
            sa.String(length=64),
            nullable=False,
            comment="Значение cookie-сессии кабинета",
        ),
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="Момент истечения сессии",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token"),
    )
    with op.batch_alter_table("admin_sessions", schema=None) as batch_op:
        batch_op.create_index("ix_admin_sessions_tenant_id", ["tenant_id"], unique=False)
        batch_op.create_index("ix_admin_sessions_expires", ["expires_at"], unique=False)

    # --- 7. Журнал действий тренера ---
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column(
            "actor",
            sa.String(length=32),
            nullable=False,
            server_default="trainer",
            comment="Кто выполнил действие",
        ),
        sa.Column(
            "action",
            sa.String(length=64),
            nullable=False,
            comment="Код действия, например student.create",
        ),
        sa.Column("payload", sa.Text(), nullable=True, comment="JSON с деталями изменения"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("audit_logs", schema=None) as batch_op:
        batch_op.create_index("ix_audit_logs_tenant_id", ["tenant_id"], unique=False)
        batch_op.create_index(
            "ix_audit_logs_tenant_created", ["tenant_id", "created_at"], unique=False
        )

    # --- 8. Журнал отправленных напоминаний ---
    op.create_table(
        "dispatch_log",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column(
            "entity_id",
            sa.Integer(),
            nullable=True,
            comment="NULL для напоминаний уровня тренера",
        ),
        sa.Column(
            "rule_kind",
            sa.String(length=24),
            nullable=False,
            comment="Тип правила, по которому ушло напоминание",
        ),
        sa.Column("dispatch_date", sa.Date(), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["entity_id"], ["entities.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "entity_id",
            "rule_kind",
            "dispatch_date",
            name="uq_dispatch_log_once_per_day",
        ),
    )
    with op.batch_alter_table("dispatch_log", schema=None) as batch_op:
        batch_op.create_index("ix_dispatch_log_tenant_id", ["tenant_id"], unique=False)
        batch_op.create_index(
            "ix_dispatch_log_tenant_date", ["tenant_id", "dispatch_date"], unique=False
        )

    # --- 9. Перенос расписания из entities в schedule_slots ---
    entity_rows = connection.execute(
        sa.text(
            "SELECT id, tenant_id, schedule_days, schedule_time "
            "FROM entities "
            "WHERE schedule_days IS NOT NULL AND schedule_days != ''"
        )
    ).fetchall()

    for entity_id, tenant_id, schedule_days, schedule_time in entity_rows:
        days = [day.strip().lower() for day in (schedule_days or "").split(",") if day.strip()]
        start_time = (schedule_time or "").strip() or "00:00"
        for day in days:
            connection.execute(
                sa.text(
                    "INSERT INTO schedule_slots "
                    "(tenant_id, entity_id, weekday, start_time, duration_min, is_active, created_at) "
                    "VALUES (:tenant_id, :entity_id, :weekday, :start_time, 60, 1, :created_at)"
                ),
                {
                    "tenant_id": tenant_id,
                    "entity_id": entity_id,
                    "weekday": day,
                    "start_time": start_time,
                    "created_at": now,
                },
            )

    # --- 10. Правила напоминаний по умолчанию для существующих тенантов ---
    for (tenant_id,) in tenant_rows:
        for kind, time_of_day in (
            ("day_before", "20:00"),
            ("morning_of", "08:00"),
            ("low_balance", None),
        ):
            connection.execute(
                sa.text(
                    "INSERT INTO reminder_rules "
                    "(tenant_id, kind, time_of_day, is_enabled, channel, created_at) "
                    "VALUES (:tenant_id, :kind, :time_of_day, 1, 'telegram', :created_at)"
                ),
                {
                    "tenant_id": tenant_id,
                    "kind": kind,
                    "time_of_day": time_of_day,
                    "created_at": now,
                },
            )


def downgrade() -> None:
    """Откат миграции."""
    op.drop_table("dispatch_log")
    op.drop_table("audit_logs")
    op.drop_table("admin_sessions")
    op.drop_table("lesson_share_links")
    op.drop_table("reminder_rules")
    op.drop_table("schedule_exceptions")
    op.drop_table("schedule_slots")

    with op.batch_alter_table("tenants", schema=None) as batch_op:
        batch_op.drop_constraint("uq_tenants_admin_token", type_="unique")
        batch_op.drop_column("low_balance_threshold")
        batch_op.drop_column("payment_instructions")
        batch_op.drop_column("currency")
        batch_op.drop_column("lesson_price")
        batch_op.drop_column("timezone")
        batch_op.drop_column("admin_token")
