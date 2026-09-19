"""Удаление учёта баланса занятий, порога низкого баланса и оплаты занятия

Больше не ведём учёт должников и остатков оплаченных занятий:

  * ``tenants.low_balance_threshold``   — порог низкого баланса;
  * ``entities.balance_lessons``        — остаток оплаченных занятий;
  * ``lessons.paid``                    — отметка «занятие оплачено»;
  * ``reminder_rules.kind = 'low_balance'`` — правило напоминания.

Правило ``low_balance`` удаляется из таблицы: уникальность типа правила в
рамках тенанта (``uq_reminder_rules_tenant_kind``) не позволяет оставлять
его при новых ограничениях ``CHECK``. Ограничение ``ck_reminder_rules_kind``
пересоздаётся без ``low_balance``.

Revision ID: a7b8c9d0e1f2
Revises: f6a7b8c9d0e1
Create Date: 2026-09-18 22:05:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# Идентификаторы ревизии
revision: str = "a7b8c9d0e1f2"
down_revision: str | None = "f6a7b8c9d0e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Применение миграции."""
    # --- 1. Убираем правило напоминания low_balance ---
    connection = op.get_bind()
    connection.execute(sa.text("DELETE FROM reminder_rules WHERE kind = 'low_balance'"))
    connection.execute(sa.text("DELETE FROM dispatch_log WHERE rule_kind = 'low_balance'"))

    # --- 2. Пересоздаём CHECK-ограничение без low_balance ---
    with op.batch_alter_table("reminder_rules", schema=None) as batch_op:
        batch_op.drop_constraint("ck_reminder_rules_kind", type_="check")
        batch_op.create_check_constraint(
            "ck_reminder_rules_kind",
            "kind IN ('day_before', 'morning_of')",
        )

    # --- 3. Удаляем колонки баланса, порога и оплаты занятия ---
    with op.batch_alter_table("tenants", schema=None) as batch_op:
        batch_op.drop_column("low_balance_threshold")

    with op.batch_alter_table("entities", schema=None) as batch_op:
        batch_op.drop_column("balance_lessons")

    with op.batch_alter_table("lessons", schema=None) as batch_op:
        batch_op.drop_column("paid")


def downgrade() -> None:
    """Откат миграции."""
    with op.batch_alter_table("lessons", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "paid",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("0"),
                comment="Для частных учеников: оплата за это занятие получена",
            )
        )

    with op.batch_alter_table("entities", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "balance_lessons",
                sa.Integer(),
                nullable=False,
                server_default="0",
                comment="Остаток оплаченных занятий",
            )
        )

    with op.batch_alter_table("tenants", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "low_balance_threshold",
                sa.Integer(),
                nullable=False,
                server_default="1",
                comment="Порог «пора напомнить об оплате»",
            )
        )

    with op.batch_alter_table("reminder_rules", schema=None) as batch_op:
        batch_op.drop_constraint("ck_reminder_rules_kind", type_="check")
        batch_op.create_check_constraint(
            "ck_reminder_rules_kind",
            "kind IN ('day_before', 'morning_of', 'low_balance')",
        )