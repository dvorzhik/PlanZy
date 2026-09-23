"""Проверка расписания задач APScheduler: когда реально сработают рассылки.

Запускается вручную::

    .venv\\Scripts\\python.exe scripts\\diag_scheduler.py

Планировщик просыпается по тику, а фактическое время отправки берётся из
правил тренера (``reminder_rules.time_of_day``) — скрипт печатает и то, и
другое, чтобы это было видно.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.db.base import get_session
from app.db.repository import Repository
from app.scheduler.scheduler import DISPATCH_TICK_JOB_ID, DISPATCH_TICK_MINUTES


async def _print_rules() -> None:
    """Печатает правила напоминаний активных тенантов."""
    async with get_session() as session:
        repo = Repository(session)
        tenants = await repo.tenants.list_active()
        print(f"\nАктивных тенантов: {len(tenants)}")
        for tenant in tenants:
            print(f"\n= Тенант {tenant.id} {tenant.name!r} (tz={tenant.timezone})")
            rules = await repo.reminders.list_by_tenant(tenant.id)
            if not rules:
                print("  правила не созданы")
            for rule in rules:
                print(
                    f"  {rule.kind}: отправка в {rule.time_of_day or '—'} enabled={rule.is_enabled}"
                )


def main() -> None:
    """Печатает ближайшие времена запуска задач."""
    now = datetime.now()
    print(f"Локальное время машины: {now:%Y-%m-%d %H:%M:%S %Z%z}")
    print(f"settings.timezone      : {settings.timezone}")

    trigger = CronTrigger(minute=f"*/{DISPATCH_TICK_MINUTES}", timezone=settings.timezone)
    nxt = trigger.get_next_fire_time(None, now)
    print(f"\n{DISPATCH_TICK_JOB_ID}: проверка каждые {DISPATCH_TICK_MINUTES} мин")
    print(f"  ближайший запуск: {nxt}")
    print("  фактическое время отправки задаётся в правилах ниже")

    asyncio.run(_print_rules())


if __name__ == "__main__":
    main()
