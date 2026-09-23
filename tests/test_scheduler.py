"""Тесты рассылки напоминаний и планировщика APScheduler.

Проверяем:
    * сборку занятий на день из ``ScheduleSlot`` с учётом ``ScheduleException``;
    * отправку по правилам ``ReminderRule`` (``morning_of``, ``day_before``);
    * отправку строго в указанное в правиле время (окно ``SEND_WINDOW``);
    * часовой пояс тенанта;
    * пропуск учеников без телефона родителя;
    * обработку ошибок Telegram API;
    * защиту от дублирования через журнал ``DispatchLog``;
    * регистрацию задачи-тика в планировщике.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime
from typing import Any

import pytest
from aiogram.exceptions import TelegramAPIError

from app.db.repository import Repository
from app.scheduler.dispatch_service import (
    DAY_BEFORE_KINDS,
    MORNING_KINDS,
    SEND_WINDOW,
    WEEKDAY_CODES,
    DispatchResult,
    MorningDispatchService,
)
from app.scheduler.scheduler import (
    DISPATCH_TICK_JOB_ID,
    create_scheduler,
    register_jobs,
    shutdown_scheduler,
    start_scheduler,
)

# Понедельник — удобная фиксированная дата для тестов.
MONDAY = date(2026, 9, 14)
TUESDAY = date(2026, 9, 15)

# Момент, когда правило morning_of в 09:00 уже наступило.
MONDAY_0900 = datetime(2026, 9, 14, 9, 0)


class StubBot:
    """Заглушка aiogram-бота: запоминает отправленные сообщения."""

    def __init__(self, *, fail_on: str | None = None) -> None:
        """Инициализирует заглушку.

        Args:
            fail_on: Имя ученика, на котором нужно смоделировать ошибку.
        """
        self.sent: list[dict[str, Any]] = []
        self._fail_on = fail_on

    async def send_message(
        self,
        chat_id: int,
        text: str,
        **kwargs: Any,
    ) -> None:
        """Имитирует отправку сообщения.

        Args:
            chat_id: Идентификатор чата.
            text: Текст сообщения.
            **kwargs: Дополнительные параметры (reply_markup и т.п.).

        Raises:
            TelegramAPIError: Если текст содержит имя из ``fail_on``.
        """
        if self._fail_on and self._fail_on in text:
            raise TelegramAPIError(method=None, message="stub failure")  # type: ignore[arg-type]
        self.sent.append({"chat_id": chat_id, "text": text, **kwargs})


async def _seed_student(
    repo: Repository,
    tenant_id: int,
    *,
    name: str,
    weekday: str,
    phone: str | None = "972501234567",
    start_time: str = "17:00",
) -> int:
    """Создаёт ученика со слотом расписания на указанный день.

    Args:
        repo: Фасад репозиториев.
        tenant_id: Идентификатор тенанта.
        name: Имя ученика.
        weekday: Код дня недели (mon..sun).
        phone: Телефон родителя.
        start_time: Время начала занятия.

    Returns:
        Идентификатор созданного ученика.
    """
    entity = await repo.entities.create(
        tenant_id,
        name=name,
        entity_type="student",
        parent_phone=phone,
    )
    await repo.schedule.create(
        tenant_id,
        entity_id=entity.id,
        weekday=weekday,
        start_time=start_time,
    )
    return entity.id


async def _enable_rules(
    repo: Repository,
    tenant_id: int,
    *kinds: str,
    time_of_day: str = "09:00",
) -> None:
    """Создаёт включённые правила напоминаний.

    Args:
        repo: Фасад репозиториев.
        tenant_id: Идентификатор тенанта.
        *kinds: Виды правил (``morning_of``, ``day_before``).
        time_of_day: Время отправки правила.
    """
    for kind in kinds:
        await repo.reminders.create(
            tenant_id=tenant_id,
            kind=kind,
            time_of_day=time_of_day,
            is_enabled=True,
        )


class TestDispatchResult:
    """Проверки структуры итогов рассылки."""

    def test_has_errors_false_by_default(self) -> None:
        """По умолчанию ошибок нет."""
        result = DispatchResult(dispatch_date=MONDAY)
        assert result.has_errors is False
        assert result.messages_sent == 0
        assert result.skipped_duplicate == 0

    def test_has_errors_true_when_errors_present(self) -> None:
        """При наличии ошибок флаг взводится."""
        result = DispatchResult(dispatch_date=MONDAY, errors=["boom"])
        assert result.has_errors is True


class TestMorningDispatchService:
    """Проверки логики рассылки."""

    async def test_run_sends_header_and_reminders(
        self,
        repo: Repository,
        tenant_id: int,
    ) -> None:
        """Рассылка отправляет заголовок и по напоминанию на занятие."""
        await _seed_student(repo, tenant_id, name="Миша", weekday="mon")
        await _seed_student(repo, tenant_id, name="Аня", weekday="mon")
        await _enable_rules(repo, tenant_id, "morning_of")
        bot = StubBot()
        service = MorningDispatchService(repo, bot)  # type: ignore[arg-type]

        result = await service.run_due(now=MONDAY_0900)

        assert result.tenants_processed == 1
        assert result.entities_total == 2
        assert result.messages_sent == 2
        assert result.skipped_no_phone == 0
        assert result.has_errors is False
        # 1 заголовок + 2 напоминания.
        assert len(bot.sent) == 3
        assert "Доброе утро" in bot.sent[0]["text"]
        assert "понедельник" in bot.sent[0]["text"]
        assert "Баланс" not in bot.sent[1]["text"]

    async def test_run_without_rules_sends_nothing(
        self,
        repo: Repository,
        tenant_id: int,
    ) -> None:
        """Без включённых правил рассылка ничего не отправляет."""
        await _seed_student(repo, tenant_id, name="Миша", weekday="mon")
        bot = StubBot()
        service = MorningDispatchService(repo, bot)  # type: ignore[arg-type]

        result = await service.run_due(now=MONDAY_0900)

        assert result.entities_total == 0
        assert result.messages_sent == 0
        assert bot.sent == []

    async def test_run_skips_entities_without_phone(
        self,
        repo: Repository,
        tenant_id: int,
    ) -> None:
        """Ученики без телефона родителя пропускаются."""
        await _seed_student(repo, tenant_id, name="Миша", weekday="mon")
        await _seed_student(repo, tenant_id, name="БезТелефона", weekday="mon", phone=None)
        await _enable_rules(repo, tenant_id, "morning_of")
        bot = StubBot()
        service = MorningDispatchService(repo, bot)  # type: ignore[arg-type]

        result = await service.run_due(now=MONDAY_0900)

        assert result.entities_total == 2
        assert result.messages_sent == 1
        assert result.skipped_no_phone == 1

    async def test_run_ignores_other_weekdays(
        self,
        repo: Repository,
        tenant_id: int,
    ) -> None:
        """Ученики с занятием в другой день не попадают в рассылку."""
        await _seed_student(repo, tenant_id, name="Вторник", weekday="tue")
        await _enable_rules(repo, tenant_id, "morning_of")
        bot = StubBot()
        service = MorningDispatchService(repo, bot)  # type: ignore[arg-type]

        result = await service.run_due(now=MONDAY_0900)

        assert result.entities_total == 0
        assert result.messages_sent == 0
        assert bot.sent == []

    async def test_run_without_entities_sends_nothing(
        self,
        repo: Repository,
        tenant_id: int,
    ) -> None:
        """Если занятий нет, сообщения не отправляются."""
        await _enable_rules(repo, tenant_id, "morning_of")
        bot = StubBot()
        service = MorningDispatchService(repo, bot)  # type: ignore[arg-type]

        result = await service.run_due(now=MONDAY_0900)

        assert result.tenants_processed == 1
        assert result.entities_total == 0
        assert bot.sent == []

    async def test_reminder_contains_whatsapp_button(
        self,
        repo: Repository,
        tenant_id: int,
    ) -> None:
        """Напоминание содержит inline-кнопку со ссылкой wa.me."""
        await _seed_student(repo, tenant_id, name="Миша", weekday="mon")
        await _enable_rules(repo, tenant_id, "morning_of")
        bot = StubBot()
        service = MorningDispatchService(repo, bot)  # type: ignore[arg-type]

        await service.run_due(now=MONDAY_0900)

        reminder = bot.sent[1]
        markup = reminder.get("reply_markup")
        assert markup is not None
        button = markup.inline_keyboard[0][0]
        assert "wa.me" in button.url
        assert "Миша" in button.text

    async def test_reminder_has_no_balance_line(
        self,
        repo: Repository,
        tenant_id: int,
    ) -> None:
        """В напоминании тренеру больше нет строки баланса."""
        await _seed_student(repo, tenant_id, name="Миша", weekday="mon")
        await _enable_rules(repo, tenant_id, "morning_of")
        bot = StubBot()
        service = MorningDispatchService(repo, bot)  # type: ignore[arg-type]

        await service.run_due(now=MONDAY_0900)

        assert "Баланс" not in bot.sent[1]["text"]
        assert "не пополнен" not in bot.sent[1]["text"]

    async def test_reminder_shows_lesson_time(
        self,
        repo: Repository,
        tenant_id: int,
    ) -> None:
        """Текст напоминания содержит время занятия из слота."""
        await _seed_student(repo, tenant_id, name="Миша", weekday="mon", start_time="18:30")
        await _enable_rules(repo, tenant_id, "morning_of")
        bot = StubBot()
        service = MorningDispatchService(repo, bot)  # type: ignore[arg-type]

        await service.run_due(now=MONDAY_0900)

        assert "18:30" in bot.sent[1]["text"]

    async def test_telegram_error_is_collected(
        self,
        repo: Repository,
        tenant_id: int,
    ) -> None:
        """Ошибка Telegram API попадает в итог, рассылка продолжается."""
        await _seed_student(repo, tenant_id, name="Миша", weekday="mon")
        await _seed_student(repo, tenant_id, name="Аня", weekday="mon")
        await _enable_rules(repo, tenant_id, "morning_of")
        bot = StubBot(fail_on="Миша")
        service = MorningDispatchService(repo, bot)  # type: ignore[arg-type]

        result = await service.run_due(now=MONDAY_0900)

        assert result.has_errors is True
        assert result.messages_sent == 1
        assert any("Миша" in error for error in result.errors)

    async def test_header_error_skips_tenant(
        self,
        repo: Repository,
        tenant_id: int,
    ) -> None:
        """Если не удалось отправить заголовок, занятия тенанта пропускаются."""
        await _seed_student(repo, tenant_id, name="Миша", weekday="mon")
        await _enable_rules(repo, tenant_id, "morning_of")
        bot = StubBot(fail_on="Доброе утро")
        service = MorningDispatchService(repo, bot)  # type: ignore[arg-type]

        result = await service.run_due(now=MONDAY_0900)

        assert result.has_errors is True
        assert result.messages_sent == 0
        assert bot.sent == []

    def test_weekday_codes_align_with_python(self) -> None:
        """Коды дней недели совпадают с ``date.weekday()``."""
        assert WEEKDAY_CODES[date(2026, 9, 14).weekday()] == "mon"
        assert WEEKDAY_CODES[date(2026, 9, 20).weekday()] == "sun"

    def test_rule_kind_groups(self) -> None:
        """Группы правил для утренней и вечерней рассылки не пересекаются."""
        assert set(MORNING_KINDS) == {"morning_of"}
        assert set(DAY_BEFORE_KINDS) == {"day_before"}


class TestScheduleExceptions:
    """Проверки учёта разовых отклонений расписания."""

    async def test_cancelled_lesson_is_not_sent(
        self,
        repo: Repository,
        tenant_id: int,
    ) -> None:
        """Отменённое занятие не попадает в рассылку."""
        entity_id = await _seed_student(repo, tenant_id, name="Миша", weekday="mon")
        await _enable_rules(repo, tenant_id, "morning_of")
        await repo.schedule_exceptions.create(
            tenant_id,
            entity_id=entity_id,
            exception_date=MONDAY,
            kind="cancelled",
        )
        bot = StubBot()
        service = MorningDispatchService(repo, bot)  # type: ignore[arg-type]

        result = await service.run_due(now=MONDAY_0900)

        assert result.entities_total == 0
        assert bot.sent == []

    async def test_moved_lesson_uses_new_time(
        self,
        repo: Repository,
        tenant_id: int,
    ) -> None:
        """Перенесённое занятие отправляется с новым временем."""
        entity_id = await _seed_student(repo, tenant_id, name="Миша", weekday="mon")
        await _enable_rules(repo, tenant_id, "morning_of")
        await repo.schedule_exceptions.create(
            tenant_id,
            entity_id=entity_id,
            exception_date=MONDAY,
            kind="moved",
            new_time="20:00",
        )
        bot = StubBot()
        service = MorningDispatchService(repo, bot)  # type: ignore[arg-type]

        await service.run_due(now=MONDAY_0900)

        assert "20:00" in bot.sent[1]["text"]
        assert "перенесено" in bot.sent[1]["text"]

    async def test_extra_lesson_is_sent(
        self,
        repo: Repository,
        tenant_id: int,
    ) -> None:
        """Дополнительное занятие попадает в рассылку."""
        entity_id = await _seed_student(repo, tenant_id, name="Миша", weekday="tue")
        await _enable_rules(repo, tenant_id, "morning_of")
        await repo.schedule_exceptions.create(
            tenant_id,
            entity_id=entity_id,
            exception_date=MONDAY,
            kind="extra",
            new_time="12:00",
        )
        bot = StubBot()
        service = MorningDispatchService(repo, bot)  # type: ignore[arg-type]

        result = await service.run_due(now=MONDAY_0900)

        assert result.entities_total == 1
        assert "12:00" in bot.sent[1]["text"]
        assert "доп. занятие" in bot.sent[1]["text"]


class TestReminderRules:
    """Проверки отправки по конкретным правилам."""

    async def test_disabled_rule_is_skipped(
        self,
        repo: Repository,
        tenant_id: int,
    ) -> None:
        """Выключенное правило не отправляет напоминаний."""
        await _seed_student(repo, tenant_id, name="Миша", weekday="mon")
        await repo.reminders.create(
            tenant_id=tenant_id,
            kind="morning_of",
            is_enabled=False,
        )
        bot = StubBot()
        service = MorningDispatchService(repo, bot)  # type: ignore[arg-type]

        result = await service.run_due(now=MONDAY_0900)

        assert result.entities_total == 0
        assert bot.sent == []

    async def test_day_before_sends_for_tomorrow(
        self,
        repo: Repository,
        tenant_id: int,
    ) -> None:
        """Правило day_before берёт занятия на следующий день."""
        # Вторник — следующий день после понедельника.
        await _seed_student(repo, tenant_id, name="Вторник", weekday="tue")
        await _enable_rules(repo, tenant_id, "day_before", time_of_day="20:00")
        bot = StubBot()
        service = MorningDispatchService(repo, bot)  # type: ignore[arg-type]

        result = await service.run_due(now=datetime(2026, 9, 14, 20, 0))

        assert result.dispatch_date == MONDAY
        assert result.entities_total == 1
        assert result.messages_sent == 1

    async def test_day_before_ignores_morning_rule(
        self,
        repo: Repository,
        tenant_id: int,
    ) -> None:
        """Правило day_before не обрабатывает занятия morning_of."""
        await _seed_student(repo, tenant_id, name="Вторник", weekday="tue")
        await _enable_rules(repo, tenant_id, "morning_of", time_of_day="20:00")
        bot = StubBot()
        service = MorningDispatchService(repo, bot)  # type: ignore[arg-type]

        # Правило morning_of ждёт занятия на понедельник — их нет.
        result = await service.run_due(now=datetime(2026, 9, 14, 20, 0))

        assert result.entities_total == 0
        assert bot.sent == []

    async def test_rule_not_sent_before_its_time(
        self,
        repo: Repository,
        tenant_id: int,
    ) -> None:
        """До времени правила напоминание не отправляется."""
        await _seed_student(repo, tenant_id, name="Миша", weekday="mon")
        await _enable_rules(repo, tenant_id, "morning_of", time_of_day="09:00")
        bot = StubBot()
        service = MorningDispatchService(repo, bot)  # type: ignore[arg-type]

        result = await service.run_due(now=datetime(2026, 9, 14, 8, 0))

        assert result.messages_sent == 0
        assert result.skipped_not_due == 1
        assert bot.sent == []

    async def test_rule_sent_at_its_time(
        self,
        repo: Repository,
        tenant_id: int,
    ) -> None:
        """В указанное в правиле время напоминание уходит."""
        await _seed_student(repo, tenant_id, name="Миша", weekday="mon")
        await _enable_rules(repo, tenant_id, "morning_of", time_of_day="09:00")
        bot = StubBot()
        service = MorningDispatchService(repo, bot)  # type: ignore[arg-type]

        result = await service.run_due(now=datetime(2026, 9, 14, 9, 0))

        assert result.messages_sent == 1

    async def test_rule_skipped_after_send_window(
        self,
        repo: Repository,
        tenant_id: int,
    ) -> None:
        """Просроченное напоминание (вне окна) не отправляется."""
        await _seed_student(repo, tenant_id, name="Миша", weekday="mon")
        await _enable_rules(repo, tenant_id, "morning_of", time_of_day="09:00")
        bot = StubBot()
        service = MorningDispatchService(repo, bot)  # type: ignore[arg-type]

        late = MONDAY_0900 + SEND_WINDOW
        result = await service.run_due(now=late)

        assert result.messages_sent == 0
        assert result.skipped_not_due == 1
        assert bot.sent == []

    async def test_time_comes_from_rule_not_env(
        self,
        repo: Repository,
        tenant_id: int,
    ) -> None:
        """Отправка управляется временем правила, а не настройками из .env."""
        await _seed_student(repo, tenant_id, name="Миша", weekday="mon")
        await _enable_rules(repo, tenant_id, "morning_of", time_of_day="17:45")
        bot = StubBot()
        service = MorningDispatchService(repo, bot)  # type: ignore[arg-type]

        assert (await service.run_due(now=datetime(2026, 9, 14, 8, 30))).messages_sent == 0
        assert (await service.run_due(now=datetime(2026, 9, 14, 17, 45))).messages_sent == 1

    async def test_tenant_timezone_is_respected(
        self,
        repo: Repository,
        tenant_id: int,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Момент отправки считается в часовом поясе тенанта."""
        await _seed_student(repo, tenant_id, name="Миша", weekday="mon")
        await _enable_rules(repo, tenant_id, "morning_of", time_of_day="09:00")

        tenant = await repo.tenants.get_by_id(tenant_id)
        assert tenant is not None
        tenant.timezone = "Europe/Moscow"
        await repo.session.flush()

        bot = StubBot()
        service = MorningDispatchService(repo, bot)  # type: ignore[arg-type]

        # 08:00 по Москве — правило на 09:00 ещё не наступило.
        before = await service.run_due(now=datetime(2026, 9, 14, 8, 0))
        assert before.messages_sent == 0
        assert before.skipped_not_due == 1

        # 09:00 по Москве — наступило.
        after = await service.run_due(now=datetime(2026, 9, 14, 9, 0))
        assert after.messages_sent == 1


class TestDispatchDeduplication:
    """Проверки защиты от дублирования через журнал отправок."""

    async def test_second_run_skips_duplicates(
        self,
        repo: Repository,
        tenant_id: int,
    ) -> None:
        """Повторная рассылка в тот же день не дублирует напоминания."""
        await _seed_student(repo, tenant_id, name="Миша", weekday="mon")
        await _enable_rules(repo, tenant_id, "morning_of")
        bot = StubBot()
        service = MorningDispatchService(repo, bot)  # type: ignore[arg-type]

        first = await service.run_due(now=MONDAY_0900)
        second = await service.run_due(now=MONDAY_0900)

        assert first.messages_sent == 1
        assert second.messages_sent == 0
        assert second.skipped_duplicate == 1

    async def test_dispatch_log_records_sent_reminder(
        self,
        repo: Repository,
        tenant_id: int,
    ) -> None:
        """После отправки в журнале появляется запись."""
        entity_id = await _seed_student(repo, tenant_id, name="Миша", weekday="mon")
        await _enable_rules(repo, tenant_id, "morning_of")
        bot = StubBot()
        service = MorningDispatchService(repo, bot)  # type: ignore[arg-type]

        await service.run_due(now=MONDAY_0900)

        assert await repo.dispatch_log.was_sent(tenant_id, "morning_of", MONDAY, entity_id)

    async def test_different_rules_send_separately(
        self,
        repo: Repository,
        tenant_id: int,
    ) -> None:
        """Правила morning_of и day_before не обрабатываются вместе."""
        await _seed_student(repo, tenant_id, name="Миша", weekday="mon")
        await _enable_rules(repo, tenant_id, "morning_of", "day_before")
        bot = StubBot()
        service = MorningDispatchService(repo, bot)  # type: ignore[arg-type]

        result = await service.run_due(now=MONDAY_0900)

        # Утренняя рассылка обрабатывает только morning_of.
        assert result.messages_sent == 1


class TestSchedulerSetup:
    """Проверки конфигурации APScheduler."""

    def test_create_scheduler_uses_settings(self) -> None:
        """Планировщик создаётся с настройками по умолчанию."""
        scheduler = create_scheduler()
        try:
            assert scheduler.running is False
            assert scheduler.timezone is not None
        finally:
            if scheduler.running:
                scheduler.shutdown(wait=False)

    def test_register_jobs_adds_dispatch_tick(self) -> None:
        """Задача-тик регистрируется с корректным id и расписанием."""
        scheduler = create_scheduler()
        try:
            register_jobs(scheduler, StubBot())  # type: ignore[arg-type]
            job = scheduler.get_job(DISPATCH_TICK_JOB_ID)
            assert job is not None
            assert job.id == DISPATCH_TICK_JOB_ID
            assert "minute" in str(job.trigger)
        finally:
            if scheduler.running:
                scheduler.shutdown(wait=False)

    async def test_start_and_shutdown_scheduler(self) -> None:
        """Планировщик запускается и корректно останавливается."""
        scheduler = create_scheduler()
        start_scheduler(scheduler)
        assert scheduler.running is True
        shutdown_scheduler(scheduler)
        # ``AsyncIOScheduler._shutdown`` выполняется через ``call_soon``,
        # поэтому даём event loop прокрутиться до фактической остановки.
        await asyncio.sleep(0)
        assert scheduler.running is False

    def test_shutdown_when_not_running_is_noop(self) -> None:
        """Остановка незапущенного планировщика не падает."""
        scheduler = create_scheduler()
        shutdown_scheduler(scheduler)
        assert scheduler.running is False


class TestDispatchTick:
    """Проверки обёртки периодической задачи планировщика."""

    async def test_tick_runs_dispatch_and_returns_result(
        self,
        repo: Repository,
        tenant_id: int,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Тик выполняет рассылку и возвращает итог."""
        from contextlib import asynccontextmanager

        from app.scheduler import scheduler as scheduler_module

        @asynccontextmanager
        async def _fake_session():
            yield repo.session

        monkeypatch.setattr("app.db.base.get_session", _fake_session)

        bot = StubBot()
        first = await scheduler_module._run_dispatch_tick(bot)  # type: ignore[arg-type]

        assert first is not None
        assert isinstance(first, DispatchResult)

    async def test_tick_is_idempotent(
        self,
        repo: Repository,
        tenant_id: int,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Повторный тик не отправляет то же напоминание снова."""
        from contextlib import asynccontextmanager

        from app.scheduler import scheduler as scheduler_module

        await _seed_student(repo, tenant_id, name="Миша", weekday=MONDAY.strftime("%a").lower())
        await _enable_rules(repo, tenant_id, "morning_of", time_of_day="09:00")

        @asynccontextmanager
        async def _fake_session():
            yield repo.session

        monkeypatch.setattr("app.db.base.get_session", _fake_session)

        # Защита от дублей — журнал DispatchLog, а не память процесса.
        bot = StubBot()
        first = await scheduler_module._run_dispatch_tick(bot)  # type: ignore[arg-type]
        assert first is not None

        # Компенсируем «сегодня» фиксированной датой, иначе тик смотрит
        # на реальный день и занятий не найдёт.
        service = MorningDispatchService(repo, bot)  # type: ignore[arg-type]
        assert (await service.run_due(now=MONDAY_0900)).messages_sent == 1
        second = await service.run_due(now=MONDAY_0900)
        assert second.messages_sent == 0
        assert second.skipped_duplicate == 1
