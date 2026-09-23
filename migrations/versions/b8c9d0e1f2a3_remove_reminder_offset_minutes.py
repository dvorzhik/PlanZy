"""Удаление неиспользуемого поля offset_minutes из правил напоминаний

Поле ``reminder_rules.offset_minutes`` было задумано как «сдвиг отправки в
минутах», но планировщик его никогда не читал: день отправки задаётся типом
правила (``day_before`` — накануне, ``morning_of`` — в день занятия), а время —
полем ``time_of_day``. В кабинете поле отображалось как «Сдвиг, дней», из-за
чего тренер менял значение без какого-либо эффекта.

Поле удалено вместе с формой ввода.

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
Create Date: 2026-09-23 10:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# Идентификаторы ревизии
revision: str = "b8c9d0e1f2a3"
down_revision: str | None = "a7b8c9d0e1f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Применение миграции."""
    with op.batch_alter_table("reminder_rules", schema=None) as batch_op:
        batch_op.drop_column("offset_minutes")


def downgrade() -> None:
    """Откат миграции."""
    with op.batch_alter_table("reminder_rules", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "offset_minutes",
                sa.Integer(),
                nullable=True,
                comment="Сдвиг в минутах (например, за 120 минут до занятия)",
            )
        )
