"""Диагностика рассылки напоминаний на реальной БД.

Запускается вручную::

    .venv\\Scripts\\python.exe scripts\\diag_reminders.py

Печатает, что видит планировщик: тенанты, включённые правила, занятия на
завтра/сегодня и итог «сухой» рассылки (без отправки в Telegram).
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

from app.db.base import get_session
from app.db.repository import Repository
from app.scheduler.dispatch_service import MorningDispatchService
from app.services.schedule_service import ScheduleService


class DryRunBot:
    """Заглушка бота: печатает то, что было бы отправлено."""

    async def send_message(self, chat_id: int, text: str, **kwargs: object) -> None:
        """Печатает сообщение вместо отправки."""
        print(f"  → chat_id={chat_id}: {text.splitlines()[0]} ...")


async def main() -> None:
    """Выполняет диагностику."""
    async with get_session() as session:
        repo = Repository(session)
        service = MorningDispatchService(repo, DryRunBot())  # type: ignore[arg-type]
        today = service.today()

        tenants = await repo.tenants.list_active()
        print(f"Сегодня: {today} ({today.strftime('%a')})")
        print(f"Активных тенантов: {len(tenants)}")

        for tenant in tenants:
            print(f"\n= Тенант {tenant.id} {tenant.name!r} chat={tenant.telegram_user_id}")
            rules = await repo.reminders.list_by_tenant(tenant.id)
            for rule in rules:
                print(
                    f"  правило {rule.kind}: enabled={rule.is_enabled} "
                    f"time={rule.time_of_day} channel={rule.channel}"
                )

            schedule = ScheduleService(repo)
            for offset, label in ((0, "сегодня"), (1, "завтра")):
                target = today + timedelta(days=offset)
                lessons = await schedule.get_day_schedule(tenant.id, target)
                print(f"  занятия {label} ({target}): {len(lessons)}")
                for lesson in lessons:
                    entity = await repo.entities.get_by_id(tenant.id, lesson.entity_id)
                    phone = entity.parent_phone if entity else None
                    print(
                        f"    - {lesson.start_time} {lesson.entity_name} "
                        f"phone={phone!r} kind={lesson.kind}"
                    )

        for label, runner in (
            ("УТРЕННЯЯ (morning_of)", service.run),
            ("ВЕЧЕРНЯЯ (day_before)", service.run_day_before),
        ):
            result = await runner()
            print(
                f"\n{label}: дата={result.dispatch_date} тенантов={result.tenants_processed} "
                f"занятий={result.entities_total} отправлено={result.messages_sent} "
                f"без_телефона={result.skipped_no_phone} дублей={result.skipped_duplicate} "
                f"ошибок={len(result.errors)}"
            )
            for error in result.errors:
                print(f"  ошибка: {error}")


if __name__ == "__main__":
    asyncio.run(main())
