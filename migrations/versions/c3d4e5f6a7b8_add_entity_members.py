"""entities: участники группы

Добавляет колонку ``entities.members`` — список имён участников группы
(через запятую или с новой строки). Поле необязательное: у одиночных
учеников и старых групп остаётся пустым.

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-09-18 13:20:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# Идентификаторы ревизии
revision: str = "c3d4e5f6a7b8"
down_revision: str | None = "b2c3d4e5f6a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Применение миграции: добавляем колонку участников группы."""
    with op.batch_alter_table("entities", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "members",
                sa.Text(),
                nullable=True,
                comment="Участники группы: имена через запятую или с новой строки",
            )
        )


def downgrade() -> None:
    """Откат миграции: удаляем колонку участников группы."""
    with op.batch_alter_table("entities", schema=None) as batch_op:
        batch_op.drop_column("members")
