"""Показывает названия групп в читаемом виде для отладки поиска."""

from __future__ import annotations

import asyncio
import sys

from sqlalchemy import select

from app.core.logging import setup_logging
from app.db.base import dispose_engine, get_session
from app.db.models import Entity

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


async def main() -> None:
    """Печатает все названия групп и учеников."""
    setup_logging()
    async with get_session() as session:
        for kind in ("group", "student"):
            rows = (
                (
                    await session.execute(
                        select(Entity.name).where(Entity.entity_type == kind).order_by(Entity.id)
                    )
                )
                .scalars()
                .all()
            )
            print(f"=== {kind} ({len(rows)}) ===")
            for name in rows:
                print(f"  {name!r}")
    await dispose_engine()


if __name__ == "__main__":
    asyncio.run(main())
