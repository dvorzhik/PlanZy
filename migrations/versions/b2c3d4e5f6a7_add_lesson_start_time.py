"""lessons: время начала занятия

Добавляет колонку ``lessons.start_time`` (``ЧЧ:ММ``) — время начала
занятия в журнале учёта. Поле необязательное: старые записи остаются
без времени.

Revision ID: b2c3d4e5f6a7
Revises: a1f2c3d4e5b6
Create Date: 2026-09-18 12:52:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# Идентификаторы ревизии
revision: str = "b2c3d4e5f6a7"
down_revision: str | None = "a1f2c3d4e5b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Применение миграции: добавляем колонку времени начала занятия."""
    with op.batch_alter_table("lessons", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "start_time",
                sa.String(length=5),
                nullable=True,
                comment="Время начала занятия в формате ЧЧ:ММ",
            )
        )


def downgrade() -> None:
    """Откат миграции: удаляем колонку времени начала занятия."""
    with op.batch_alter_table("lessons", schema=None) as batch_op:
        batch_op.drop_column("start_time")
