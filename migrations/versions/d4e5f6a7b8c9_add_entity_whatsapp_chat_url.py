"""entities: ссылка на групповой чат WhatsApp

Добавляет колонку ``entities.whatsapp_chat_url`` — ссылку на групповой
чат WhatsApp (``https://chat.whatsapp.com/...``). Поле необязательное:
у одиночных учеников и групп без чата остаётся пустым.

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-09-18 14:45:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# Идентификаторы ревизии
revision: str = "d4e5f6a7b8c9"
down_revision: str | None = "c3d4e5f6a7b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Применение миграции: добавляем колонку ссылки на чат WhatsApp."""
    with op.batch_alter_table("entities", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "whatsapp_chat_url",
                sa.String(length=512),
                nullable=True,
                comment="Ссылка на групповой чат WhatsApp (https://chat.whatsapp.com/...)",
            )
        )


def downgrade() -> None:
    """Откат миграции: удаляем колонку ссылки на чат WhatsApp."""
    with op.batch_alter_table("entities", schema=None) as batch_op:
        batch_op.drop_column("whatsapp_chat_url")
