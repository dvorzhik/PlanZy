"""entities: индивидуальная цена занятия

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-09-18

Добавляет колонку ``lesson_price`` в таблицу ``entities``.

Цена занятия может отличаться для групп и частных учеников, поэтому
у каждой сущности появилась своя цена. Значение ``NULL`` означает
«использовать цену тенанта» — так поведение существующих записей
не меняется.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# Идентификаторы ревизии, используются Alembic.
revision: str = "e5f6a7b8c9d0"
down_revision: str | None = "d4e5f6a7b8c9"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Применение миграции: добавляем колонку индивидуальной цены занятия."""
    with op.batch_alter_table("entities", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "lesson_price",
                sa.Float(),
                nullable=True,
                comment="Индивидуальная цена занятия; NULL — использовать цену тенанта",
            )
        )


def downgrade() -> None:
    """Откат миграции: удаляем колонку индивидуальной цены занятия."""
    with op.batch_alter_table("entities", schema=None) as batch_op:
        batch_op.drop_column("lesson_price")
