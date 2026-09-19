"""Сервис расписания: регулярные слоты, разовые отклонения, конфликты.

Слой бизнес-логики между веб-кабинетом/ботом и репозиториями расписания.
Отвечает за:

* CRUD регулярных слотов (``schedule_slots``);
* разовые отклонения на дату (``schedule_exceptions``): перенос, отмена,
  дополнительное занятие;
* сборку расписания на конкретный день и на неделю с учётом отклонений;
* поиск конфликтов — двух занятий в одно и то же время у тренера.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from app.core.logging import get_logger
from app.db.models import Entity, ScheduleException, ScheduleSlot
from app.db.repository import Repository

logger = get_logger(__name__)

# Допустимые коды дней недели (совместимы с EntityService).
WEEKDAY_CODES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

# Русские названия дней недели для отображения.
WEEKDAY_LABELS = {
    "mon": "Пн",
    "tue": "Вт",
    "wed": "Ср",
    "thu": "Чт",
    "fri": "Пт",
    "sat": "Сб",
    "sun": "Вс",
}

# Соответствие кода дня недели номеру в ``date.weekday()`` (0 = понедельник).
_WEEKDAY_INDEX = {code: index for index, code in enumerate(WEEKDAY_CODES)}

# Допустимые виды отклонений.
EXCEPTION_KINDS = ("moved", "cancelled", "extra")


@dataclass(slots=True)
class PlannedLesson:
    """Одно занятие в собранном расписании на конкретную дату.

    Attributes:
        entity_id: Идентификатор ученика или группы.
        entity_name: Имя ученика или название группы.
        lesson_date: Дата занятия.
        start_time: Время начала в формате ``ЧЧ:ММ``.
        duration_min: Длительность занятия в минутах.
        location: Место проведения (если задано).
        slot_id: Идентификатор регулярного слота, если занятие из него.
        exception_id: Идентификатор отклонения, если занятие разовое.
        kind: Вид занятия: ``regular``, ``moved`` или ``extra``.
        note: Комментарий к отклонению.
    """

    entity_id: int
    entity_name: str
    lesson_date: date
    start_time: str
    duration_min: int = 60
    location: str | None = None
    slot_id: int | None = None
    exception_id: int | None = None
    kind: str = "regular"
    note: str | None = None

    @property
    def end_time(self) -> str:
        """Возвращает время окончания занятия в формате ``ЧЧ:ММ``."""
        start = _parse_time(self.start_time)
        if start is None:
            return self.start_time
        total = start.hour * 60 + start.minute + self.duration_min
        return f"{(total // 60) % 24:02d}:{total % 60:02d}"

    @property
    def is_cancelled(self) -> bool:
        """Показывает, отменено ли занятие."""
        return self.kind == "cancelled"


@dataclass(slots=True)
class Conflict:
    """Конфликт расписания: два занятия пересекаются по времени.

    Attributes:
        lesson_date: Дата, на которую обнаружен конфликт.
        first: Первое занятие.
        second: Второе занятие.
    """

    lesson_date: date
    first: PlannedLesson
    second: PlannedLesson

    @property
    def description(self) -> str:
        """Человекочитаемое описание конфликта."""
        return (
            f"{self.lesson_date:%d.%m.%Y}: "
            f"{self.first.entity_name} ({self.first.start_time}–{self.first.end_time}) "
            f"пересекается с {self.second.entity_name} "
            f"({self.second.start_time}–{self.second.end_time})"
        )


@dataclass(slots=True)
class WeekSchedule:
    """Расписание на неделю: занятия, сгруппированные по датам.

    Attributes:
        start_date: Понедельник недели.
        days: Словарь «дата → список занятий», отсортированный по дате.
        conflicts: Найденные конфликты за неделю.
    """

    start_date: date
    days: dict[date, list[PlannedLesson]] = field(default_factory=dict)
    conflicts: list[Conflict] = field(default_factory=list)

    @property
    def end_date(self) -> date:
        """Возвращает воскресенье недели."""
        return self.start_date + timedelta(days=6)

    @property
    def total_lessons(self) -> int:
        """Общее число занятий за неделю (без отменённых)."""
        return sum(
            1 for lessons in self.days.values() for lesson in lessons if not lesson.is_cancelled
        )


def _parse_time(value: str | None) -> time | None:
    """Разбирает строку ``ЧЧ:ММ`` в :class:`datetime.time`.

    Args:
        value: Строка времени или ``None``.

    Returns:
        Время или ``None``, если строку разобрать не удалось.
    """
    if not value:
        return None
    cleaned = value.strip()
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            return datetime.strptime(cleaned, fmt).time()
        except ValueError:
            continue
    return None


def _normalize_weekday(weekday: str) -> str:
    """Приводит код дня недели к нижнему регистру и проверяет его.

    Args:
        weekday: Код дня недели (``mon``…``sun``).

    Returns:
        Нормализованный код.

    Raises:
        ValueError: Если код дня недели неизвестен.
    """
    code = (weekday or "").strip().lower()
    if code not in WEEKDAY_CODES:
        raise ValueError(
            f"Неизвестный день недели: {weekday!r}. Допустимо: {', '.join(WEEKDAY_CODES)}."
        )
    return code


def _normalize_kind(kind: str) -> str:
    """Проверяет вид отклонения.

    Args:
        kind: Вид отклонения (``moved``, ``cancelled``, ``extra``).

    Returns:
        Нормализованный вид.

    Raises:
        ValueError: Если вид отклонения неизвестен.
    """
    code = (kind or "").strip().lower()
    if code not in EXCEPTION_KINDS:
        raise ValueError(
            f"Неизвестный вид отклонения: {kind!r}. Допустимо: {', '.join(EXCEPTION_KINDS)}."
        )
    return code


def _minutes(value: str | None) -> int | None:
    """Переводит строку ``ЧЧ:ММ`` в минуты от начала суток.

    Args:
        value: Строка времени.

    Returns:
        Количество минут или ``None``, если строку разобрать не удалось.
    """
    parsed = _parse_time(value)
    if parsed is None:
        return None
    return parsed.hour * 60 + parsed.minute


class ScheduleService:
    """Бизнес-логика расписания тренера."""

    def __init__(self, repo: Repository) -> None:
        """Инициализирует сервис.

        Args:
            repo: Фасад репозиториев.
        """
        self._repo = repo

    # ------------------------------------------------------------------
    # Регулярные слоты
    # ------------------------------------------------------------------

    async def add_slot(
        self,
        tenant_id: int,
        entity_id: int,
        weekday: str,
        start_time: str,
        duration_min: int = 60,
        location: str | None = None,
    ) -> ScheduleSlot:
        """Добавляет регулярный слот расписания.

        Args:
            tenant_id: Идентификатор тенанта.
            entity_id: Идентификатор ученика или группы.
            weekday: Код дня недели (``mon``…``sun``).
            start_time: Время начала в формате ``ЧЧ:ММ``.
            duration_min: Длительность занятия в минутах.
            location: Место проведения.

        Returns:
            Созданный слот.

        Raises:
            ValueError: Если день недели или время заданы неверно.
        """
        code = _normalize_weekday(weekday)
        if _parse_time(start_time) is None:
            raise ValueError(f"Некорректное время начала: {start_time!r}. Ожидается ЧЧ:ММ.")
        if duration_min <= 0:
            raise ValueError("Длительность занятия должна быть положительной.")

        slot = await self._repo.schedule.create(
            tenant_id=tenant_id,
            entity_id=entity_id,
            weekday=code,
            start_time=start_time,
            duration_min=duration_min,
            location=location,
        )
        logger.info(
            "Добавлен слот id=%s: entity_id=%s, %s %s",
            slot.id,
            entity_id,
            code,
            start_time,
        )
        return slot

    async def update_slot(
        self, tenant_id: int, slot_id: int, **fields: object
    ) -> ScheduleSlot | None:
        """Обновляет регулярный слот.

        Args:
            tenant_id: Идентификатор тенанта.
            slot_id: Идентификатор слота.
            **fields: Изменяемые поля (``weekday``, ``start_time``,
                ``duration_min``, ``location``, ``is_active``, ``entity_id``).

        Returns:
            Обновлённый слот или ``None``, если слот не найден.

        Raises:
            ValueError: Если передан некорректный день недели или время.
        """
        payload = dict(fields)
        if "weekday" in payload and payload["weekday"] is not None:
            payload["weekday"] = _normalize_weekday(str(payload["weekday"]))
        if (
            "start_time" in payload
            and payload["start_time"] is not None
            and _parse_time(str(payload["start_time"])) is None
        ):
            raise ValueError(
                f"Некорректное время начала: {payload['start_time']!r}. Ожидается ЧЧ:ММ."
            )
        if (
            "duration_min" in payload
            and payload["duration_min"] is not None
            and int(payload["duration_min"]) <= 0  # type: ignore[arg-type]
        ):
            raise ValueError("Длительность занятия должна быть положительной.")
        return await self._repo.schedule.update(tenant_id, slot_id, **payload)

    async def delete_slot(self, tenant_id: int, slot_id: int) -> bool:
        """Удаляет регулярный слот.

        История разовых отклонений сохраняется: ссылка ``slot_id`` в них
        обнуляется на уровне БД.

        Args:
            tenant_id: Идентификатор тенанта.
            slot_id: Идентификатор слота.

        Returns:
            ``True``, если слот был удалён.
        """
        return await self._repo.schedule.delete(tenant_id, slot_id)

    async def move_slot(
        self, tenant_id: int, slot_id: int, weekday: str, start_time: str
    ) -> ScheduleSlot | None:
        """Переносит регулярный слот на другой день и/или время.

        Args:
            tenant_id: Идентификатор тенанта.
            slot_id: Идентификатор слота.
            weekday: Новый день недели.
            start_time: Новое время начала.

        Returns:
            Обновлённый слот или ``None``, если слот не найден.
        """
        return await self.update_slot(
            tenant_id,
            slot_id,
            weekday=weekday,
            start_time=start_time,
        )

    async def list_slots(self, tenant_id: int, *, only_active: bool = False) -> list[ScheduleSlot]:
        """Возвращает все слоты тенанта.

        Args:
            tenant_id: Идентификатор тенанта.
            only_active: Вернуть только активные слоты.

        Returns:
            Список слотов, отсортированный по дню недели и времени.
        """
        return await self._repo.schedule.list_by_tenant(tenant_id, only_active=only_active)

    async def list_slots_for_weekday(
        self, tenant_id: int, weekday: str, *, only_active: bool = True
    ) -> list[ScheduleSlot]:
        """Возвращает слоты на указанный день недели.

        Args:
            tenant_id: Идентификатор тенанта.
            weekday: Код дня недели.
            only_active: Учитывать только активные слоты.

        Returns:
            Список слотов.

        Raises:
            ValueError: Если день недели задан неверно.
        """
        code = _normalize_weekday(weekday)
        return await self._repo.schedule.list_for_weekday(tenant_id, code, only_active=only_active)

    async def list_slots_for_entity(
        self, tenant_id: int, entity_id: int, *, only_active: bool = True
    ) -> list[ScheduleSlot]:
        """Возвращает регулярные слоты одного ученика или группы.

        Используется публичной карточкой родителя: показывает актуальное
        расписание из таблицы ``schedule_slots`` вместо устаревших полей
        ``entities.schedule_days``/``schedule_time``.

        Args:
            tenant_id: Идентификатор тенанта.
            entity_id: Идентификатор ученика или группы.
            only_active: Учитывать только активные слоты.

        Returns:
            Список слотов, отсортированный по дню недели и времени.
        """
        slots = await self._repo.schedule.list_by_entity(tenant_id, entity_id)
        if only_active:
            slots = [slot for slot in slots if slot.is_active]
        return slots

    # ------------------------------------------------------------------
    # Разовые отклонения
    # ------------------------------------------------------------------

    async def add_exception(
        self,
        tenant_id: int,
        entity_id: int,
        exception_date: date,
        kind: str,
        slot_id: int | None = None,
        new_time: str | None = None,
        note: str | None = None,
    ) -> ScheduleException:
        """Добавляет разовое отклонение от расписания.

        Args:
            tenant_id: Идентификатор тенанта.
            entity_id: Идентификатор ученика или группы.
            exception_date: Дата, к которой относится отклонение.
            kind: Вид отклонения: ``moved``, ``cancelled`` или ``extra``.
            slot_id: Идентификатор регулярного слота (для ``moved``/``cancelled``).
            new_time: Новое время начала (для ``moved``).
            note: Комментарий.

        Returns:
            Созданное отклонение.

        Raises:
            ValueError: Если вид отклонения неизвестен или не задано новое
                время для переноса.
        """
        code = _normalize_kind(kind)
        if code == "moved" and _parse_time(new_time) is None:
            raise ValueError("Для переноса занятия нужно указать новое время в формате ЧЧ:ММ.")

        exception = await self._repo.schedule_exceptions.create(
            tenant_id=tenant_id,
            entity_id=entity_id,
            exception_date=exception_date,
            kind=code,
            slot_id=slot_id,
            new_time=new_time,
            note=note,
        )
        logger.info(
            "Добавлено отклонение id=%s (%s) на %s",
            exception.id,
            code,
            exception_date,
        )
        return exception

    async def cancel_lesson(
        self,
        tenant_id: int,
        entity_id: int,
        exception_date: date,
        slot_id: int | None = None,
        note: str | None = None,
    ) -> ScheduleException:
        """Отменяет занятие на конкретную дату.

        Args:
            tenant_id: Идентификатор тенанта.
            entity_id: Идентификатор ученика или группы.
            exception_date: Дата отмены.
            slot_id: Идентификатор регулярного слота.
            note: Причина отмены.

        Returns:
            Созданное отклонение.
        """
        return await self.add_exception(
            tenant_id,
            entity_id,
            exception_date,
            "cancelled",
            slot_id=slot_id,
            note=note,
        )

    async def move_lesson(
        self,
        tenant_id: int,
        entity_id: int,
        exception_date: date,
        new_time: str,
        slot_id: int | None = None,
        note: str | None = None,
    ) -> ScheduleException:
        """Переносит занятие на другое время в ту же дату.

        Args:
            tenant_id: Идентификатор тенанта.
            entity_id: Идентификатор ученика или группы.
            exception_date: Дата занятия.
            new_time: Новое время начала.
            slot_id: Идентификатор регулярного слота.
            note: Комментарий.

        Returns:
            Созданное отклонение.
        """
        return await self.add_exception(
            tenant_id,
            entity_id,
            exception_date,
            "moved",
            slot_id=slot_id,
            new_time=new_time,
            note=note,
        )

    async def add_extra_lesson(
        self,
        tenant_id: int,
        entity_id: int,
        exception_date: date,
        new_time: str,
        note: str | None = None,
    ) -> ScheduleException:
        """Добавляет дополнительное занятие вне регулярного расписания.

        Args:
            tenant_id: Идентификатор тенанта.
            entity_id: Идентификатор ученика или группы.
            exception_date: Дата дополнительного занятия.
            new_time: Время начала.
            note: Комментарий.

        Returns:
            Созданное отклонение.
        """
        return await self.add_exception(
            tenant_id,
            entity_id,
            exception_date,
            "extra",
            new_time=new_time,
            note=note,
        )

    async def delete_exception(self, tenant_id: int, exception_id: int) -> bool:
        """Удаляет разовое отклонение.

        Args:
            tenant_id: Идентификатор тенанта.
            exception_id: Идентификатор отклонения.

        Returns:
            ``True``, если отклонение было удалено.
        """
        return await self._repo.schedule_exceptions.delete(tenant_id, exception_id)

    async def list_exceptions(self, tenant_id: int, target_date: date) -> list[ScheduleException]:
        """Возвращает отклонения на конкретную дату.

        Args:
            tenant_id: Идентификатор тенанта.
            target_date: Дата.

        Returns:
            Список отклонений.
        """
        return await self._repo.schedule_exceptions.list_by_date(tenant_id, target_date)

    # ------------------------------------------------------------------
    # Сборка расписания
    # ------------------------------------------------------------------

    async def get_day_schedule(
        self,
        tenant_id: int,
        target_date: date,
        *,
        include_cancelled: bool = False,
    ) -> list[PlannedLesson]:
        """Собирает расписание на конкретный день с учётом отклонений.

        Алгоритм:

        1. Берём регулярные слоты на день недели целевой даты.
        2. Применяем отклонения: ``cancelled`` убирает занятие,
           ``moved`` меняет время, ``extra`` добавляет новое занятие.
        3. Сортируем результат по времени начала.

        Args:
            tenant_id: Идентификатор тенанта.
            target_date: Дата, на которую нужно расписание.
            include_cancelled: Оставлять ли отменённые занятия в списке.

        Returns:
            Список занятий, отсортированный по времени начала.
        """
        weekday = WEEKDAY_CODES[target_date.weekday()]
        slots = await self._repo.schedule.list_for_weekday(tenant_id, weekday)
        exceptions = await self._repo.schedule_exceptions.list_by_date(tenant_id, target_date)

        entities = {
            entity.id: entity for entity in await self._repo.entities.list_by_tenant(tenant_id)
        }

        cancelled_by_slot = {
            exc.slot_id: exc
            for exc in exceptions
            if exc.kind == "cancelled" and exc.slot_id is not None
        }
        cancelled_slot_ids = set(cancelled_by_slot)
        moved_by_slot = {
            exc.slot_id: exc
            for exc in exceptions
            if exc.kind == "moved" and exc.slot_id is not None
        }
        # Фолбэк: тренер мог отменить/перенести занятие, не зная id слота —
        # тогда отклонение привязано только к ученику. Сопоставляем по entity_id.
        cancelled_by_entity = {
            exc.entity_id: exc
            for exc in exceptions
            if exc.kind == "cancelled" and exc.slot_id is None
        }
        cancelled_entity_ids = set(cancelled_by_entity)
        moved_by_entity = {
            exc.entity_id: exc for exc in exceptions if exc.kind == "moved" and exc.slot_id is None
        }

        planned: list[PlannedLesson] = []

        for slot in slots:
            entity = entities.get(slot.entity_id)
            if entity is None:
                continue
            if slot.id in cancelled_slot_ids or slot.entity_id in cancelled_entity_ids:
                if include_cancelled:
                    cancel_exc = cancelled_by_slot.get(slot.id) or cancelled_by_entity.get(
                        slot.entity_id
                    )
                    planned.append(
                        self._from_slot(
                            slot,
                            entity,
                            target_date,
                            kind="cancelled",
                            exception_id=cancel_exc.id if cancel_exc else None,
                            note=cancel_exc.note if cancel_exc else None,
                        )
                    )
                continue
            moved = moved_by_slot.get(slot.id) or moved_by_entity.get(slot.entity_id)
            if moved is not None:
                planned.append(
                    self._from_slot(
                        slot,
                        entity,
                        target_date,
                        kind="moved",
                        start_time=moved.new_time or slot.start_time,
                        exception_id=moved.id,
                        note=moved.note,
                    )
                )
                continue
            planned.append(self._from_slot(slot, entity, target_date))

        for exc in exceptions:
            if exc.kind != "extra":
                continue
            entity = entities.get(exc.entity_id)
            if entity is None:
                continue
            planned.append(
                PlannedLesson(
                    entity_id=entity.id,
                    entity_name=entity.name,
                    lesson_date=target_date,
                    start_time=exc.new_time or "00:00",
                    duration_min=60,
                    location=None,
                    slot_id=None,
                    exception_id=exc.id,
                    kind="extra",
                    note=exc.note,
                )
            )

        planned.sort(key=lambda lesson: (_minutes(lesson.start_time) or 0, lesson.entity_name))
        return planned

    async def format_entity_schedule(self, tenant_id: int, entity_id: int) -> str:
        """Собирает читаемую строку расписания ученика или группы.

        Источник — регулярные слоты (таблица ``schedule_slots``), то же
        место, откуда расписание берёт веб-кабинет и рассылка. Устаревшие
        поля ``Entity.schedule_days``/``schedule_time`` не используются:
        после перехода на web-first они всегда пусты, из-за чего все
        ученики выглядели как «без расписания».

        Args:
            tenant_id: Идентификатор тенанта.
            entity_id: Идентификатор ученика или группы.

        Returns:
            Строка вида ``«Пн, Ср в 16:00»`` или ``«расписание не задано»``.
        """
        slots = await self._repo.schedule.list_by_entity(tenant_id, entity_id)
        active = [slot for slot in slots if slot.is_active]
        if not active:
            return "расписание не задано"

        ordered = sorted(
            active,
            key=lambda slot: (_WEEKDAY_INDEX.get(slot.weekday, 99), slot.start_time or ""),
        )

        days: list[str] = []
        for slot in ordered:
            label = WEEKDAY_LABELS.get(slot.weekday, slot.weekday)
            if label not in days:
                days.append(label)

        times: list[str] = []
        for slot in ordered:
            if slot.start_time and slot.start_time not in times:
                times.append(slot.start_time)

        if times:
            return f"{', '.join(days)} в {', '.join(times)}"
        return ", ".join(days)

    async def get_week_schedule(
        self,
        tenant_id: int,
        start_date: date,
        *,
        include_cancelled: bool = False,
    ) -> WeekSchedule:
        """Собирает расписание на неделю, начиная с указанной даты.

        Args:
            tenant_id: Идентификатор тенанта.
            start_date: Любая дата недели; приводится к понедельнику.
            include_cancelled: Оставлять ли отменённые занятия.

        Returns:
            Расписание недели с найденными конфликтами.
        """
        monday = start_date - timedelta(days=start_date.weekday())
        week = WeekSchedule(start_date=monday)

        for offset in range(7):
            current = monday + timedelta(days=offset)
            lessons = await self.get_day_schedule(
                tenant_id, current, include_cancelled=include_cancelled
            )
            week.days[current] = lessons
            week.conflicts.extend(self.find_conflicts(lessons, current))

        return week

    async def get_week_schedule_for_entity(
        self, tenant_id: int, entity_id: int, start_date: date
    ) -> dict[date, list[PlannedLesson]]:
        """Возвращает расписание недели только для одного ученика или группы.

        Args:
            tenant_id: Идентификатор тенанта.
            entity_id: Идентификатор ученика или группы.
            start_date: Любая дата недели.

        Returns:
            Словарь «дата → занятия этого ученика».
        """
        week = await self.get_week_schedule(tenant_id, start_date)
        return {
            day: [lesson for lesson in lessons if lesson.entity_id == entity_id]
            for day, lessons in week.days.items()
        }

    # ------------------------------------------------------------------
    # Конфликты
    # ------------------------------------------------------------------

    @staticmethod
    def find_conflicts(
        lessons: list[PlannedLesson], target_date: date | None = None
    ) -> list[Conflict]:
        """Находит пересечения занятий по времени.

        Args:
            lessons: Занятия одного дня.
            target_date: Дата для подписи конфликта. Если не задана,
                берётся из первого занятия.

        Returns:
            Список конфликтов.
        """
        active = [lesson for lesson in lessons if not lesson.is_cancelled]
        conflicts: list[Conflict] = []

        for index, first in enumerate(active):
            first_start = _minutes(first.start_time)
            first_end = _minutes(first.end_time)
            if first_start is None or first_end is None:
                continue
            for second in active[index + 1 :]:
                second_start = _minutes(second.start_time)
                second_end = _minutes(second.end_time)
                if second_start is None or second_end is None:
                    continue
                if first_start < second_end and second_start < first_end:
                    conflicts.append(
                        Conflict(
                            lesson_date=target_date or first.lesson_date,
                            first=first,
                            second=second,
                        )
                    )
        return conflicts

    async def find_conflicts_for_date(self, tenant_id: int, target_date: date) -> list[Conflict]:
        """Ищет конфликты расписания на конкретную дату.

        Args:
            tenant_id: Идентификатор тенанта.
            target_date: Дата проверки.

        Returns:
            Список конфликтов.
        """
        lessons = await self.get_day_schedule(tenant_id, target_date)
        return self.find_conflicts(lessons, target_date)

    # ------------------------------------------------------------------
    # Вспомогательное
    # ------------------------------------------------------------------

    @staticmethod
    def _from_slot(
        slot: ScheduleSlot,
        entity: Entity,
        target_date: date,
        *,
        kind: str = "regular",
        start_time: str | None = None,
        exception_id: int | None = None,
        note: str | None = None,
    ) -> PlannedLesson:
        """Создаёт :class:`PlannedLesson` из регулярного слота.

        Args:
            slot: Регулярный слот.
            entity: Ученик или группа слота.
            target_date: Дата занятия.
            kind: Вид занятия.
            start_time: Переопределённое время начала.
            exception_id: Идентификатор связанного отклонения.
            note: Комментарий.

        Returns:
            Собранное занятие.
        """
        return PlannedLesson(
            entity_id=entity.id,
            entity_name=entity.name,
            lesson_date=target_date,
            start_time=start_time or slot.start_time,
            duration_min=slot.duration_min,
            location=slot.location,
            slot_id=slot.id,
            exception_id=exception_id,
            kind=kind,
            note=note,
        )

    @staticmethod
    def format_day(lessons: list[PlannedLesson]) -> str:
        """Формирует текстовое представление расписания на день.

        Args:
            lessons: Занятия дня.

        Returns:
            Многострочный текст; пустая строка, если занятий нет.
        """
        if not lessons:
            return ""
        lines = []
        for lesson in lessons:
            mark = " (отменено)" if lesson.is_cancelled else ""
            place = f", {lesson.location}" if lesson.location else ""
            lines.append(
                f"{lesson.start_time}–{lesson.end_time} — {lesson.entity_name}{place}{mark}"
            )
        return "\n".join(lines)
