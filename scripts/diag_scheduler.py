"""Проверка расписания задач APScheduler: когда реально сработают рассылки.

Запускается вручную::

    .venv\\Scripts\\python.exe scripts\\diag_scheduler.py
"""

from __future__ import annotations

from datetime import datetime

from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings


def main() -> None:
    """Печатает ближайшие времена запуска задач."""
    now = datetime.now()
    print(f"Локальное время машины: {now:%Y-%m-%d %H:%M:%S %Z%z}")
    print(f"settings.timezone      : {settings.timezone}")

    for name, hour, minute in (
        ("morning_dispatch", settings.dispatch_hour, settings.dispatch_minute),
        ("day_before_dispatch", settings.day_before_hour, settings.day_before_minute),
    ):
        trigger = CronTrigger(hour=hour, minute=minute, timezone=settings.timezone)
        nxt = trigger.get_next_fire_time(None, now)
        print(f"\n{name}: задано {hour:02d}:{minute:02d} ({settings.timezone})")
        print(f"  ближайший запуск: {nxt}")


if __name__ == "__main__":
    main()
