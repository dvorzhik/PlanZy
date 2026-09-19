"""lessons: отметка «оплачено»

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-09-18

Добавляет колонку ``paid`` в таблицу ``lessons``.

Для частных учеников родители обычно платят после занятия, поэтому
тренеру нужна простая отметка «оплата получена» прямо в отчёте о
занятии. Значение по умолчанию ``0`` (не оплачено) — поведение
существующих записей не меняется.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# Идентификаторы ревизии, используются Alembic.
revision: str = "f6a7b8c9d0e1"
down_revision: str | None = "e5f6a7b8c9d0"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Применение миграции: добавляем отметку «оплачено» у занятия."""
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


def downgrade() -> None:
    """Откат миграции: удаляем отметку «оплачено»."""
    with op.batch_alter_table("lessons", schema=None) as batch_op:
        batch_op.drop_column("paid")
