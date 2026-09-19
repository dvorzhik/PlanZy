"""Тесты ScheduleService: слоты, отклонения, сборка расписания, конфликты.

Все проверки идут на изолированной in-memory SQLite (фикстуры из
``tests/conftest.py``), поэтому реальная база не затрагивается.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.db.repository import Repository
from app.services.schedule_service import (
    EXCEPTION_KINDS,
    WEEKDAY_CODES,
    Conflict,
    PlannedLesson,
    ScheduleService,
    WeekSchedule,
    _minutes,
    _normalize_kind,
    _normalize_weekday,
    _parse_time,
)

# Даты для тестов: 2026-09-14 — понедельник.
MONDAY = date(2026, 9, 14)
TUESDAY = date(2026, 9, 15)
WEDNESDAY = date(2026, 9, 16)
NEXT_MONDAY = date(2026, 9, 21)


async def _seed_entity(
    repo: Repository,
    tenant_id: int,
    *,
    name: str = "Миша",
    entity_type: str = "student",
) -> int:
    """Создаёт ученика или группу и возвращает его идентификатор."""
    entity = await repo.entities.create(
        tenant_id=tenant_id,
        name=name,
        entity_type=entity_type,
    )
    return entity.id


@pytest.fixture
def service(repo: Repository) -> ScheduleService:
    """Возвращает сервис расписания поверх тестового репозитория."""
    return ScheduleService(repo)


class TestHelpers:
    """Проверки вспомогательных функций модуля."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("09:30", (9, 30)),
            ("9:05", (9, 5)),
            (" 17:45 ", (17, 45)),
            ("08:00:00", (8, 0)),
        ],
    )
    def test_parse_time_accepts_valid_formats(self, raw: str, expected: tuple[int, int]) -> None:
        """Разбирает корректные строки времени."""
        parsed = _parse_time(raw)
        assert parsed is not None
        assert (parsed.hour, parsed.minute) == expected

    @pytest.mark.parametrize("raw", [None, "", "abc", "25:00", "12-30", "12"])
    def test_parse_time_rejects_invalid(self, raw: str | None) -> None:
        """Возвращает None для некорректных строк."""
        assert _parse_time(raw) is None

    def test_minutes_converts_to_day_offset(self) -> None:
        """Переводит время в минуты от начала суток."""
        assert _minutes("00:00") == 0
        assert _minutes("01:30") == 90
        assert _minutes("23:59") == 1439
        assert _minutes("bad") is None

    @pytest.mark.parametrize("code", WEEKDAY_CODES)
    def test_normalize_weekday_accepts_all_codes(self, code: str) -> None:
        """Принимает все допустимые коды дней недели."""
        assert _normalize_weekday(code) == code
        assert _normalize_weekday(code.upper()) == code
        assert _normalize_weekday(f"  {code}  ") == code

    def test_normalize_weekday_rejects_unknown(self) -> None:
        """Отклоняет неизвестный код дня недели."""
        with pytest.raises(ValueError, match="Неизвестный день недели"):
            _normalize_weekday("monday")

    @pytest.mark.parametrize("kind", EXCEPTION_KINDS)
    def test_normalize_kind_accepts_all_kinds(self, kind: str) -> None:
        """Принимает все допустимые виды отклонений."""
        assert _normalize_kind(kind) == kind
        assert _normalize_kind(kind.upper()) == kind

    def test_normalize_kind_rejects_unknown(self) -> None:
        """Отклоняет неизвестный вид отклонения."""
        with pytest.raises(ValueError, match="Неизвестный вид отклонения"):
            _normalize_kind("postponed")


class TestPlannedLesson:
    """Проверки dataclass-а занятия."""

    def test_end_time_adds_duration(self) -> None:
        """Считает время окончания по длительности."""
        lesson = PlannedLesson(
            entity_id=1,
            entity_name="Миша",
            lesson_date=MONDAY,
            start_time="17:30",
            duration_min=45,
        )
        assert lesson.end_time == "18:15"

    def test_end_time_wraps_over_midnight(self) -> None:
        """Корректно переходит через полночь."""
        lesson = PlannedLesson(
            entity_id=1,
            entity_name="Миша",
            lesson_date=MONDAY,
            start_time="23:30",
            duration_min=60,
        )
        assert lesson.end_time == "00:30"

    def test_end_time_returns_raw_on_bad_input(self) -> None:
        """Возвращает исходную строку, если время не разобрать."""
        lesson = PlannedLesson(
            entity_id=1,
            entity_name="Миша",
            lesson_date=MONDAY,
            start_time="bad",
        )
        assert lesson.end_time == "bad"

    def test_is_cancelled_flag(self) -> None:
        """Определяет отменённое занятие по виду."""
        regular = PlannedLesson(1, "Миша", MONDAY, "10:00")
        cancelled = PlannedLesson(1, "Миша", MONDAY, "10:00", kind="cancelled")
        assert regular.is_cancelled is False
        assert cancelled.is_cancelled is True


class TestWeekScheduleDataclass:
    """Проверки dataclass-а недельного расписания."""

    def test_end_date_is_sunday(self) -> None:
        """Воскресенье — шестой день от понедельника."""
        week = WeekSchedule(start_date=MONDAY)
        assert week.end_date == date(2026, 9, 20)

    def test_total_lessons_skips_cancelled(self) -> None:
        """Не считает отменённые занятия."""
        week = WeekSchedule(start_date=MONDAY)
        week.days[MONDAY] = [
            PlannedLesson(1, "Миша", MONDAY, "10:00"),
            PlannedLesson(2, "Аня", MONDAY, "11:00", kind="cancelled"),
        ]
        week.days[TUESDAY] = [PlannedLesson(3, "Группа", TUESDAY, "12:00")]
        assert week.total_lessons == 2


class TestSlotCrud:
    """Проверки CRUD регулярных слотов."""

    async def test_add_slot_creates_record(
        self, service: ScheduleService, repo: Repository, tenant_id: int
    ) -> None:
        """Создаёт слот с нормализованным днём недели."""
        entity_id = await _seed_entity(repo, tenant_id)
        slot = await service.add_slot(tenant_id, entity_id, "MON", "17:30", 45, "Дом")

        assert slot.id is not None
        assert slot.weekday == "mon"
        assert slot.start_time == "17:30"
        assert slot.duration_min == 45
        assert slot.location == "Дом"
        assert slot.is_active is True

    async def test_add_slot_rejects_bad_weekday(
        self, service: ScheduleService, repo: Repository, tenant_id: int
    ) -> None:
        """Отклоняет неизвестный день недели."""
        entity_id = await _seed_entity(repo, tenant_id)
        with pytest.raises(ValueError, match="Неизвестный день недели"):
            await service.add_slot(tenant_id, entity_id, "monday", "17:30")

    async def test_add_slot_rejects_bad_time(
        self, service: ScheduleService, repo: Repository, tenant_id: int
    ) -> None:
        """Отклоняет некорректное время начала."""
        entity_id = await _seed_entity(repo, tenant_id)
        with pytest.raises(ValueError, match="Некорректное время начала"):
            await service.add_slot(tenant_id, entity_id, "mon", "25:99")

    async def test_add_slot_rejects_non_positive_duration(
        self, service: ScheduleService, repo: Repository, tenant_id: int
    ) -> None:
        """Отклоняет нулевую и отрицательную длительность."""
        entity_id = await _seed_entity(repo, tenant_id)
        with pytest.raises(ValueError, match="Длительность занятия"):
            await service.add_slot(tenant_id, entity_id, "mon", "17:30", 0)

    async def test_update_slot_changes_fields(
        self, service: ScheduleService, repo: Repository, tenant_id: int
    ) -> None:
        """Обновляет поля слота."""
        entity_id = await _seed_entity(repo, tenant_id)
        slot = await service.add_slot(tenant_id, entity_id, "mon", "17:30")

        updated = await service.update_slot(
            tenant_id, slot.id, start_time="18:00", duration_min=90, location="Онлайн"
        )

        assert updated is not None
        assert updated.start_time == "18:00"
        assert updated.duration_min == 90
        assert updated.location == "Онлайн"

    async def test_update_slot_normalizes_weekday(
        self, service: ScheduleService, repo: Repository, tenant_id: int
    ) -> None:
        """Нормализует день недели при обновлении."""
        entity_id = await _seed_entity(repo, tenant_id)
        slot = await service.add_slot(tenant_id, entity_id, "mon", "17:30")

        updated = await service.update_slot(tenant_id, slot.id, weekday="WED")

        assert updated is not None
        assert updated.weekday == "wed"

    async def test_update_slot_rejects_bad_time(
        self, service: ScheduleService, repo: Repository, tenant_id: int
    ) -> None:
        """Отклоняет некорректное время при обновлении."""
        entity_id = await _seed_entity(repo, tenant_id)
        slot = await service.add_slot(tenant_id, entity_id, "mon", "17:30")

        with pytest.raises(ValueError, match="Некорректное время начала"):
            await service.update_slot(tenant_id, slot.id, start_time="abc")

    async def test_update_slot_rejects_bad_duration(
        self, service: ScheduleService, repo: Repository, tenant_id: int
    ) -> None:
        """Отклоняет некорректную длительность при обновлении."""
        entity_id = await _seed_entity(repo, tenant_id)
        slot = await service.add_slot(tenant_id, entity_id, "mon", "17:30")

        with pytest.raises(ValueError, match="Длительность занятия"):
            await service.update_slot(tenant_id, slot.id, duration_min=-10)

    async def test_update_slot_unknown_returns_none(
        self, service: ScheduleService, tenant_id: int
    ) -> None:
        """Возвращает None для несуществующего слота."""
        assert await service.update_slot(tenant_id, 999, start_time="10:00") is None

    async def test_move_slot_changes_day_and_time(
        self, service: ScheduleService, repo: Repository, tenant_id: int
    ) -> None:
        """Переносит слот на другой день и время."""
        entity_id = await _seed_entity(repo, tenant_id)
        slot = await service.add_slot(tenant_id, entity_id, "mon", "17:30")

        moved = await service.move_slot(tenant_id, slot.id, "thu", "19:00")

        assert moved is not None
        assert moved.weekday == "thu"
        assert moved.start_time == "19:00"

    async def test_delete_slot_removes_record(
        self, service: ScheduleService, repo: Repository, tenant_id: int
    ) -> None:
        """Удаляет слот."""
        entity_id = await _seed_entity(repo, tenant_id)
        slot = await service.add_slot(tenant_id, entity_id, "mon", "17:30")

        assert await service.delete_slot(tenant_id, slot.id) is True
        assert await service.list_slots(tenant_id) == []

    async def test_delete_slot_unknown_returns_false(
        self, service: ScheduleService, tenant_id: int
    ) -> None:
        """Возвращает False для несуществующего слота."""
        assert await service.delete_slot(tenant_id, 999) is False

    async def test_list_slots_sorted_by_weekday_and_time(
        self, service: ScheduleService, repo: Repository, tenant_id: int
    ) -> None:
        """Сортирует слоты по дню недели и времени."""
        entity_id = await _seed_entity(repo, tenant_id)
        await service.add_slot(tenant_id, entity_id, "wed", "10:00")
        await service.add_slot(tenant_id, entity_id, "mon", "18:00")
        await service.add_slot(tenant_id, entity_id, "mon", "09:00")

        slots = await service.list_slots(tenant_id)

        assert [(slot.weekday, slot.start_time) for slot in slots] == [
            ("mon", "09:00"),
            ("mon", "18:00"),
            ("wed", "10:00"),
        ]

    async def test_list_slots_only_active(
        self, service: ScheduleService, repo: Repository, tenant_id: int
    ) -> None:
        """Фильтрует неактивные слоты по флагу."""
        entity_id = await _seed_entity(repo, tenant_id)
        active = await service.add_slot(tenant_id, entity_id, "mon", "09:00")
        inactive = await service.add_slot(tenant_id, entity_id, "mon", "10:00")
        await service.update_slot(tenant_id, inactive.id, is_active=False)

        assert [slot.id for slot in await service.list_slots(tenant_id)] == [
            active.id,
            inactive.id,
        ]
        assert [slot.id for slot in await service.list_slots(tenant_id, only_active=True)] == [
            active.id
        ]

    async def test_list_slots_for_weekday_normalizes_code(
        self, service: ScheduleService, repo: Repository, tenant_id: int
    ) -> None:
        """Принимает код дня недели в любом регистре."""
        entity_id = await _seed_entity(repo, tenant_id)
        await service.add_slot(tenant_id, entity_id, "mon", "09:00")

        assert len(await service.list_slots_for_weekday(tenant_id, "MON")) == 1

    async def test_list_slots_for_weekday_rejects_bad_code(
        self, service: ScheduleService, tenant_id: int
    ) -> None:
        """Отклоняет неизвестный день недели."""
        with pytest.raises(ValueError, match="Неизвестный день недели"):
            await service.list_slots_for_weekday(tenant_id, "nope")


class TestExceptions:
    """Проверки разовых отклонений."""

    async def test_add_exception_moved(
        self, service: ScheduleService, repo: Repository, tenant_id: int
    ) -> None:
        """Создаёт перенос с новым временем."""
        entity_id = await _seed_entity(repo, tenant_id)
        slot = await service.add_slot(tenant_id, entity_id, "mon", "17:30")

        exception = await service.add_exception(
            tenant_id, entity_id, MONDAY, "moved", slot_id=slot.id, new_time="19:00"
        )

        assert exception.kind == "moved"
        assert exception.new_time == "19:00"
        assert exception.slot_id == slot.id

    async def test_add_exception_moved_requires_time(
        self, service: ScheduleService, repo: Repository, tenant_id: int
    ) -> None:
        """Требует новое время для переноса."""
        entity_id = await _seed_entity(repo, tenant_id)
        with pytest.raises(ValueError, match="нужно указать новое время"):
            await service.add_exception(tenant_id, entity_id, MONDAY, "moved")

    async def test_add_exception_rejects_bad_kind(
        self, service: ScheduleService, repo: Repository, tenant_id: int
    ) -> None:
        """Отклоняет неизвестный вид отклонения."""
        entity_id = await _seed_entity(repo, tenant_id)
        with pytest.raises(ValueError, match="Неизвестный вид отклонения"):
            await service.add_exception(tenant_id, entity_id, MONDAY, "postponed")

    async def test_cancel_lesson_creates_cancelled_exception(
        self, service: ScheduleService, repo: Repository, tenant_id: int
    ) -> None:
        """Отмена создаёт отклонение вида cancelled."""
        entity_id = await _seed_entity(repo, tenant_id)
        slot = await service.add_slot(tenant_id, entity_id, "mon", "17:30")

        exception = await service.cancel_lesson(
            tenant_id, entity_id, MONDAY, slot_id=slot.id, note="Болезнь"
        )

        assert exception.kind == "cancelled"
        assert exception.note == "Болезнь"

    async def test_move_lesson_creates_moved_exception(
        self, service: ScheduleService, repo: Repository, tenant_id: int
    ) -> None:
        """Перенос создаёт отклонение вида moved."""
        entity_id = await _seed_entity(repo, tenant_id)
        slot = await service.add_slot(tenant_id, entity_id, "mon", "17:30")

        exception = await service.move_lesson(
            tenant_id, entity_id, MONDAY, "20:00", slot_id=slot.id
        )

        assert exception.kind == "moved"
        assert exception.new_time == "20:00"

    async def test_add_extra_lesson_has_no_slot(
        self, service: ScheduleService, repo: Repository, tenant_id: int
    ) -> None:
        """Дополнительное занятие не привязано к слоту."""
        entity_id = await _seed_entity(repo, tenant_id)

        exception = await service.add_extra_lesson(tenant_id, entity_id, WEDNESDAY, "15:00")

        assert exception.kind == "extra"
        assert exception.slot_id is None
        assert exception.new_time == "15:00"

    async def test_delete_exception(
        self, service: ScheduleService, repo: Repository, tenant_id: int
    ) -> None:
        """Удаляет отклонение."""
        entity_id = await _seed_entity(repo, tenant_id)
        exception = await service.add_extra_lesson(tenant_id, entity_id, WEDNESDAY, "15:00")

        assert await service.delete_exception(tenant_id, exception.id) is True
        assert await service.list_exceptions(tenant_id, WEDNESDAY) == []

    async def test_list_exceptions_by_date(
        self, service: ScheduleService, repo: Repository, tenant_id: int
    ) -> None:
        """Возвращает только отклонения указанной даты."""
        entity_id = await _seed_entity(repo, tenant_id)
        await service.add_extra_lesson(tenant_id, entity_id, MONDAY, "15:00")
        await service.add_extra_lesson(tenant_id, entity_id, TUESDAY, "16:00")

        exceptions = await service.list_exceptions(tenant_id, MONDAY)

        assert len(exceptions) == 1
        assert exceptions[0].exception_date == MONDAY


class TestFormatEntitySchedule:
    """Проверки текстовой сводки расписания ученика из регулярных слотов.

    Регрессия: раньше расписание читалось из устаревших полей Entity
    (``schedule_days``/``schedule_time``), которые после перехода на
    web-first пусты — все ученики выглядели без расписания.
    """

    async def test_single_slot(
        self, service: ScheduleService, repo: Repository, tenant_id: int
    ) -> None:
        entity_id = await _seed_entity(repo, tenant_id)
        await service.add_slot(tenant_id, entity_id, "thu", "15:00")

        assert await service.format_entity_schedule(tenant_id, entity_id) == "Чт в 15:00"

    async def test_slots_sorted_by_weekday(
        self, service: ScheduleService, repo: Repository, tenant_id: int
    ) -> None:
        """Дни идут в порядке недели, независимо от порядка создания."""
        entity_id = await _seed_entity(repo, tenant_id)
        await service.add_slot(tenant_id, entity_id, "sat", "11:00")
        await service.add_slot(tenant_id, entity_id, "tue", "17:30")

        schedule = await service.format_entity_schedule(tenant_id, entity_id)

        assert schedule == "Вт, Сб в 17:30, 11:00"

    async def test_same_day_slots_share_label(
        self, service: ScheduleService, repo: Repository, tenant_id: int
    ) -> None:
        """Повторяющийся день недели не дублируется в подписи."""
        entity_id = await _seed_entity(repo, tenant_id)
        await service.add_slot(tenant_id, entity_id, "mon", "16:00")
        await service.add_slot(tenant_id, entity_id, "mon", "18:00")

        schedule = await service.format_entity_schedule(tenant_id, entity_id)

        assert schedule == "Пн в 16:00, 18:00"

    async def test_without_slots(
        self, service: ScheduleService, repo: Repository, tenant_id: int
    ) -> None:
        entity_id = await _seed_entity(repo, tenant_id)

        assert await service.format_entity_schedule(tenant_id, entity_id) == (
            "расписание не задано"
        )

    async def test_inactive_slots_ignored(
        self, service: ScheduleService, repo: Repository, tenant_id: int
    ) -> None:
        entity_id = await _seed_entity(repo, tenant_id)
        slot = await service.add_slot(tenant_id, entity_id, "mon", "16:00")
        await repo.schedule.update(tenant_id, slot.id, is_active=False)

        assert await service.format_entity_schedule(tenant_id, entity_id) == (
            "расписание не задано"
        )

    async def test_other_entity_slots_not_leaked(
        self, service: ScheduleService, repo: Repository, tenant_id: int
    ) -> None:
        """Расписание одного ученика не подмешивается к другому."""
        misha = await _seed_entity(repo, tenant_id, name="Миша")
        anya = await _seed_entity(repo, tenant_id, name="Аня")
        await service.add_slot(tenant_id, misha, "mon", "16:00")
        await service.add_slot(tenant_id, anya, "tue", "18:30")

        assert await service.format_entity_schedule(tenant_id, misha) == "Пн в 16:00"
        assert await service.format_entity_schedule(tenant_id, anya) == "Вт в 18:30"

    async def test_group_schedule(
        self, service: ScheduleService, repo: Repository, tenant_id: int
    ) -> None:
        """Метод работает и для групп."""
        group_id = await _seed_entity(repo, tenant_id, name="Новички", entity_type="group")
        await service.add_slot(tenant_id, group_id, "fri", "13:00")

        assert await service.format_entity_schedule(tenant_id, group_id) == "Пт в 13:00"


class TestDaySchedule:
    """Проверки сборки расписания на день."""

    async def test_regular_slots_are_returned_sorted(
        self, service: ScheduleService, repo: Repository, tenant_id: int
    ) -> None:
        """Собирает регулярные слоты дня по возрастанию времени."""
        misha = await _seed_entity(repo, tenant_id, name="Миша")
        anya = await _seed_entity(repo, tenant_id, name="Аня")
        await service.add_slot(tenant_id, misha, "mon", "18:00")
        await service.add_slot(tenant_id, anya, "mon", "09:00")

        lessons = await service.get_day_schedule(tenant_id, MONDAY)

        assert [lesson.entity_name for lesson in lessons] == ["Аня", "Миша"]
        assert all(lesson.kind == "regular" for lesson in lessons)

    async def test_other_weekday_slots_are_ignored(
        self, service: ScheduleService, repo: Repository, tenant_id: int
    ) -> None:
        """Не берёт слоты другого дня недели."""
        entity_id = await _seed_entity(repo, tenant_id)
        await service.add_slot(tenant_id, entity_id, "tue", "10:00")

        assert await service.get_day_schedule(tenant_id, MONDAY) == []

    async def test_cancelled_slot_is_hidden_by_default(
        self, service: ScheduleService, repo: Repository, tenant_id: int
    ) -> None:
        """Отменённое занятие не попадает в расписание."""
        entity_id = await _seed_entity(repo, tenant_id)
        slot = await service.add_slot(tenant_id, entity_id, "mon", "17:30")
        await service.cancel_lesson(tenant_id, entity_id, MONDAY, slot_id=slot.id)

        assert await service.get_day_schedule(tenant_id, MONDAY) == []

    async def test_cancelled_slot_can_be_included(
        self, service: ScheduleService, repo: Repository, tenant_id: int
    ) -> None:
        """По флагу отменённое занятие остаётся в списке."""
        entity_id = await _seed_entity(repo, tenant_id)
        slot = await service.add_slot(tenant_id, entity_id, "mon", "17:30")
        await service.cancel_lesson(tenant_id, entity_id, MONDAY, slot_id=slot.id)

        lessons = await service.get_day_schedule(tenant_id, MONDAY, include_cancelled=True)

        assert len(lessons) == 1
        assert lessons[0].is_cancelled is True

    async def test_moved_slot_changes_time(
        self, service: ScheduleService, repo: Repository, tenant_id: int
    ) -> None:
        """Перенос меняет время занятия и помечает его как moved."""
        entity_id = await _seed_entity(repo, tenant_id)
        slot = await service.add_slot(tenant_id, entity_id, "mon", "17:30")
        await service.move_lesson(tenant_id, entity_id, MONDAY, "20:00", slot_id=slot.id)

        lessons = await service.get_day_schedule(tenant_id, MONDAY)

        assert len(lessons) == 1
        assert lessons[0].start_time == "20:00"
        assert lessons[0].kind == "moved"
        assert lessons[0].exception_id is not None

    async def test_extra_lesson_is_appended(
        self, service: ScheduleService, repo: Repository, tenant_id: int
    ) -> None:
        """Дополнительное занятие добавляется в расписание дня."""
        entity_id = await _seed_entity(repo, tenant_id)
        await service.add_slot(tenant_id, entity_id, "mon", "17:30")
        await service.add_extra_lesson(tenant_id, entity_id, MONDAY, "12:00")

        lessons = await service.get_day_schedule(tenant_id, MONDAY)

        assert [lesson.start_time for lesson in lessons] == ["12:00", "17:30"]
        assert lessons[0].kind == "extra"
        assert lessons[0].slot_id is None

    async def test_extra_lesson_for_other_date_is_ignored(
        self, service: ScheduleService, repo: Repository, tenant_id: int
    ) -> None:
        """Дополнительное занятие другой даты не попадает в день."""
        entity_id = await _seed_entity(repo, tenant_id)
        await service.add_extra_lesson(tenant_id, entity_id, TUESDAY, "12:00")

        assert await service.get_day_schedule(tenant_id, MONDAY) == []

    async def test_slot_of_deleted_entity_is_skipped(
        self, service: ScheduleService, repo: Repository, tenant_id: int
    ) -> None:
        """Слот без найденного ученика не ломает сборку."""
        entity_id = await _seed_entity(repo, tenant_id)
        await service.add_slot(tenant_id, entity_id, "mon", "17:30")
        await repo.entities.delete(tenant_id, entity_id)

        assert await service.get_day_schedule(tenant_id, MONDAY) == []

    async def test_format_day_renders_lines(
        self, service: ScheduleService, repo: Repository, tenant_id: int
    ) -> None:
        """Формирует текстовое представление дня."""
        entity_id = await _seed_entity(repo, tenant_id, name="Миша")
        await service.add_slot(tenant_id, entity_id, "mon", "17:30", 45, "Дом")

        lessons = await service.get_day_schedule(tenant_id, MONDAY)
        text = service.format_day(lessons)

        assert text == "17:30–18:15 — Миша, Дом"

    async def test_format_day_empty(self, service: ScheduleService) -> None:
        """Возвращает пустую строку для пустого дня."""
        assert service.format_day([]) == ""


class TestWeekSchedule:
    """Проверки сборки расписания на неделю."""

    async def test_week_starts_on_monday(
        self, service: ScheduleService, repo: Repository, tenant_id: int
    ) -> None:
        """Приводит любую дату недели к понедельнику."""
        entity_id = await _seed_entity(repo, tenant_id)
        await service.add_slot(tenant_id, entity_id, "wed", "10:00")

        week = await service.get_week_schedule(tenant_id, WEDNESDAY)

        assert week.start_date == MONDAY
        assert week.end_date == date(2026, 9, 20)
        assert len(week.days) == 7

    async def test_week_collects_lessons_by_day(
        self, service: ScheduleService, repo: Repository, tenant_id: int
    ) -> None:
        """Раскладывает занятия по датам недели."""
        entity_id = await _seed_entity(repo, tenant_id)
        await service.add_slot(tenant_id, entity_id, "mon", "10:00")
        await service.add_slot(tenant_id, entity_id, "wed", "11:00")

        week = await service.get_week_schedule(tenant_id, MONDAY)

        assert len(week.days[MONDAY]) == 1
        assert len(week.days[WEDNESDAY]) == 1
        assert week.days[TUESDAY] == []
        assert week.total_lessons == 2

    async def test_week_respects_exceptions(
        self, service: ScheduleService, repo: Repository, tenant_id: int
    ) -> None:
        """Учитывает отмену занятия внутри недели."""
        entity_id = await _seed_entity(repo, tenant_id)
        slot = await service.add_slot(tenant_id, entity_id, "mon", "10:00")
        await service.cancel_lesson(tenant_id, entity_id, MONDAY, slot_id=slot.id)

        week = await service.get_week_schedule(tenant_id, MONDAY)

        assert week.days[MONDAY] == []
        assert week.total_lessons == 0

    async def test_week_for_entity_filters_lessons(
        self, service: ScheduleService, repo: Repository, tenant_id: int
    ) -> None:
        """Оставляет занятия только выбранного ученика."""
        misha = await _seed_entity(repo, tenant_id, name="Миша")
        anya = await _seed_entity(repo, tenant_id, name="Аня")
        await service.add_slot(tenant_id, misha, "mon", "10:00")
        await service.add_slot(tenant_id, anya, "mon", "11:00")

        days = await service.get_week_schedule_for_entity(tenant_id, misha, MONDAY)

        assert [lesson.entity_name for lesson in days[MONDAY]] == ["Миша"]

    async def test_week_next_monday_is_different_week(
        self, service: ScheduleService, repo: Repository, tenant_id: int
    ) -> None:
        """Следующий понедельник даёт другую неделю."""
        entity_id = await _seed_entity(repo, tenant_id)
        await service.add_slot(tenant_id, entity_id, "mon", "10:00")

        week = await service.get_week_schedule(tenant_id, NEXT_MONDAY)

        assert week.start_date == NEXT_MONDAY
        assert len(week.days[NEXT_MONDAY]) == 1


class TestConflicts:
    """Проверки поиска конфликтов расписания."""

    def test_overlapping_lessons_are_detected(self) -> None:
        """Находит два занятия в одно время."""
        first = PlannedLesson(1, "Миша", MONDAY, "10:00", duration_min=60)
        second = PlannedLesson(2, "Аня", MONDAY, "10:30", duration_min=60)

        conflicts = ScheduleService.find_conflicts([first, second], MONDAY)

        assert len(conflicts) == 1
        assert isinstance(conflicts[0], Conflict)
        assert conflicts[0].lesson_date == MONDAY

    def test_adjacent_lessons_do_not_conflict(self) -> None:
        """Смежные занятия без пересечения конфликтом не считаются."""
        first = PlannedLesson(1, "Миша", MONDAY, "10:00", duration_min=60)
        second = PlannedLesson(2, "Аня", MONDAY, "11:00", duration_min=60)

        assert ScheduleService.find_conflicts([first, second], MONDAY) == []

    def test_identical_times_conflict(self) -> None:
        """Одинаковое время — конфликт."""
        first = PlannedLesson(1, "Миша", MONDAY, "10:00")
        second = PlannedLesson(2, "Аня", MONDAY, "10:00")

        assert len(ScheduleService.find_conflicts([first, second], MONDAY)) == 1

    def test_cancelled_lessons_are_ignored(self) -> None:
        """Отменённые занятия не участвуют в конфликтах."""
        first = PlannedLesson(1, "Миша", MONDAY, "10:00", kind="cancelled")
        second = PlannedLesson(2, "Аня", MONDAY, "10:00")

        assert ScheduleService.find_conflicts([first, second], MONDAY) == []

    def test_three_overlaps_produce_three_pairs(self) -> None:
        """Три пересекающихся занятия дают три пары."""
        lessons = [
            PlannedLesson(1, "Миша", MONDAY, "10:00"),
            PlannedLesson(2, "Аня", MONDAY, "10:15"),
            PlannedLesson(3, "Группа", MONDAY, "10:30"),
        ]

        assert len(ScheduleService.find_conflicts(lessons, MONDAY)) == 3

    def test_conflict_description_contains_names(self) -> None:
        """Описание конфликта содержит имена и время."""
        first = PlannedLesson(1, "Миша", MONDAY, "10:00")
        second = PlannedLesson(2, "Аня", MONDAY, "10:30")
        conflict = Conflict(lesson_date=MONDAY, first=first, second=second)

        description = conflict.description

        assert "Миша" in description
        assert "Аня" in description
        assert "14.09.2026" in description

    async def test_find_conflicts_for_date(
        self, service: ScheduleService, repo: Repository, tenant_id: int
    ) -> None:
        """Находит конфликты по данным из БД."""
        misha = await _seed_entity(repo, tenant_id, name="Миша")
        anya = await _seed_entity(repo, tenant_id, name="Аня")
        await service.add_slot(tenant_id, misha, "mon", "10:00", 60)
        await service.add_slot(tenant_id, anya, "mon", "10:30", 60)

        conflicts = await service.find_conflicts_for_date(tenant_id, MONDAY)

        assert len(conflicts) == 1

    async def test_week_schedule_reports_conflicts(
        self, service: ScheduleService, repo: Repository, tenant_id: int
    ) -> None:
        """Недельное расписание содержит найденные конфликты."""
        misha = await _seed_entity(repo, tenant_id, name="Миша")
        anya = await _seed_entity(repo, tenant_id, name="Аня")
        await service.add_slot(tenant_id, misha, "mon", "10:00", 60)
        await service.add_slot(tenant_id, anya, "mon", "10:30", 60)

        week = await service.get_week_schedule(tenant_id, MONDAY)

        assert len(week.conflicts) == 1
        assert week.conflicts[0].lesson_date == MONDAY

    async def test_no_conflicts_for_different_days(
        self, service: ScheduleService, repo: Repository, tenant_id: int
    ) -> None:
        """Занятия в разные дни не конфликтуют."""
        misha = await _seed_entity(repo, tenant_id, name="Миша")
        anya = await _seed_entity(repo, tenant_id, name="Аня")
        await service.add_slot(tenant_id, misha, "mon", "10:00")
        await service.add_slot(tenant_id, anya, "tue", "10:00")

        week = await service.get_week_schedule(tenant_id, MONDAY)

        assert week.conflicts == []
