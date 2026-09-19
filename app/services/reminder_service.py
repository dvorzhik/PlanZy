"""Сервис правил напоминаний: CRUD и расчёт момента отправки.

Правила хранятся в ``reminder_rules`` и описывают, *что* и *когда*
отправлять тренеру в Telegram:

* ``day_before`` — вечером накануне занятия (``time_of_day``);
* ``morning_of`` — утром в день занятия (``time_of_day``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from app.core.logging import get_logger
from app.db.models import ReminderRule
from app.db.repository import Repository

logger = get_logger(__name__)

# Допустимые виды правил напоминаний.
REMINDER_KINDS = ("day_before", "morning_of")

# Человекочитаемые названия правил.
KIND_LABELS = {
    "day_before": "Вечером накануне занятия",
    "morning_of": "Утром в день занятия",
}

# Допустимые каналы доставки.
REMINDER_CHANNELS = ("telegram", "whatsapp")

# Значения по умолчанию для новых правил.
DEFAULT_TIME_OF_DAY = {
    "day_before": "19:00",
    "morning_of": "08:00",
}

# Границы смещения правила (в минутах).
MIN_OFFSET_MINUTES = 0
MAX_OFFSET_MINUTES = 24 * 60


@dataclass(slots=True)
class ReminderSchedule:
    """Расчёт момента отправки напоминания.

    Attributes:
        rule_kind: Вид правила.
        send_at: Момент, когда напоминание должно быть отправлено.
        target_date: Дата, к которой относится напоминание (дата занятия).
        description: Человекочитаемое описание.
    """

    rule_kind: str
    send_at: datetime
    target_date: date
    description: str


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


def _normalize_kind(kind: str) -> str:
    """Проверяет вид правила напоминания.

    Args:
        kind: Вид правила.

    Returns:
        Нормализованный вид.

    Raises:
        ValueError: Если вид правила неизвестен.
    """
    code = (kind or "").strip().lower()
    if code not in REMINDER_KINDS:
        raise ValueError(
            f"Неизвестный вид напоминания: {kind!r}. Допустимо: {', '.join(REMINDER_KINDS)}."
        )
    return code


class ReminderService:
    """Бизнес-логика правил напоминаний."""

    def __init__(self, repo: Repository) -> None:
        """Инициализирует сервис.

        Args:
            repo: Фасад репозиториев.
        """
        self._repo = repo

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    async def list_rules(self, tenant_id: int, *, only_enabled: bool = False) -> list[ReminderRule]:
        """Возвращает правила напоминаний тенанта.

        Args:
            tenant_id: Идентификатор тенанта.
            only_enabled: Вернуть только включённые правила.

        Returns:
            Список правил.
        """
        return await self._repo.reminders.list_by_tenant(tenant_id, only_enabled=only_enabled)

    async def get_rule(self, tenant_id: int, kind: str) -> ReminderRule | None:
        """Возвращает правило по виду.

        Args:
            tenant_id: Идентификатор тенанта.
            kind: Вид правила.

        Returns:
            Правило или ``None``.
        """
        return await self._repo.reminders.get_by_kind(tenant_id, _normalize_kind(kind))

    async def upsert_rule(
        self,
        tenant_id: int,
        kind: str,
        *,
        time_of_day: str | None = None,
        offset_minutes: int | None = None,
        is_enabled: bool = True,
        channel: str = "telegram",
    ) -> ReminderRule:
        """Создаёт или обновляет правило напоминания.

        Args:
            tenant_id: Идентификатор тенанта.
            kind: Вид правила.
            time_of_day: Время отправки в формате ``ЧЧ:ММ``.
            offset_minutes: Смещение в минутах.
            is_enabled: Включено ли правило.
            channel: Канал доставки.

        Returns:
            Созданное или обновлённое правило.

        Raises:
            ValueError: Если параметры правила некорректны.
        """
        code = _normalize_kind(kind)
        payload = self._validate(
            code,
            time_of_day=time_of_day,
            offset_minutes=offset_minutes,
            channel=channel,
        )

        existing = await self._repo.reminders.get_by_kind(tenant_id, code)
        if existing is not None:
            updated = await self._repo.reminders.update(
                tenant_id,
                existing.id,
                is_enabled=is_enabled,
                **payload,
            )
            logger.info("Правило напоминания %s обновлено для тенанта %s", code, tenant_id)
            return updated if updated is not None else existing

        rule = await self._repo.reminders.create(
            tenant_id=tenant_id,
            kind=code,
            is_enabled=is_enabled,
            **payload,
        )
        logger.info("Создано правило напоминания %s для тенанта %s", code, tenant_id)
        return rule

    async def update_rule(
        self, tenant_id: int, rule_id: int, **fields: object
    ) -> ReminderRule | None:
        """Обновляет правило по идентификатору.

        Args:
            tenant_id: Идентификатор тенанта.
            rule_id: Идентификатор правила.
            **fields: Изменяемые поля (``time_of_day``, ``offset_minutes``,
                ``is_enabled``, ``channel``).

        Returns:
            Обновлённое правило или ``None``.

        Raises:
            ValueError: Если значение поля некорректно.
        """
        payload: dict[str, object] = {}

        if "time_of_day" in fields and fields["time_of_day"] is not None:
            value = str(fields["time_of_day"]).strip()
            if _parse_time(value) is None:
                raise ValueError(f"Некорректное время напоминания: {value!r}. Ожидается ЧЧ:ММ.")
            payload["time_of_day"] = value

        if "offset_minutes" in fields and fields["offset_minutes"] is not None:
            offset = int(fields["offset_minutes"])  # type: ignore[arg-type]
            if not MIN_OFFSET_MINUTES <= offset <= MAX_OFFSET_MINUTES:
                raise ValueError(
                    f"Смещение должно быть в диапазоне "
                    f"{MIN_OFFSET_MINUTES}…{MAX_OFFSET_MINUTES} минут."
                )
            payload["offset_minutes"] = offset

        if "channel" in fields and fields["channel"] is not None:
            channel = str(fields["channel"]).strip().lower()
            if channel not in REMINDER_CHANNELS:
                raise ValueError(
                    f"Неизвестный канал: {channel!r}. Допустимо: {', '.join(REMINDER_CHANNELS)}."
                )
            payload["channel"] = channel

        if "is_enabled" in fields and fields["is_enabled"] is not None:
            payload["is_enabled"] = bool(fields["is_enabled"])

        if not payload:
            return await self._repo.reminders.get_by_id(tenant_id, rule_id)

        return await self._repo.reminders.update(tenant_id, rule_id, **payload)

    async def delete_rule(self, tenant_id: int, rule_id: int) -> bool:
        """Удаляет правило напоминания.

        Args:
            tenant_id: Идентификатор тенанта.
            rule_id: Идентификатор правила.

        Returns:
            ``True``, если правило было удалено.
        """
        return await self._repo.reminders.delete(tenant_id, rule_id)

    async def ensure_defaults(self, tenant_id: int) -> list[ReminderRule]:
        """Создаёт набор правил по умолчанию, если их ещё нет.

        Args:
            tenant_id: Идентификатор тенанта.

        Returns:
            Список всех правил тенанта после создания.
        """
        existing = {rule.kind for rule in await self._repo.reminders.list_by_tenant(tenant_id)}

        if "day_before" not in existing:
            await self._repo.reminders.create(
                tenant_id=tenant_id,
                kind="day_before",
                time_of_day=DEFAULT_TIME_OF_DAY["day_before"],
                is_enabled=True,
            )
        if "morning_of" not in existing:
            await self._repo.reminders.create(
                tenant_id=tenant_id,
                kind="morning_of",
                time_of_day=DEFAULT_TIME_OF_DAY["morning_of"],
                is_enabled=True,
            )

        return await self._repo.reminders.list_by_tenant(tenant_id)

    # ------------------------------------------------------------------
    # Расчёт момента отправки
    # ------------------------------------------------------------------

    @staticmethod
    def resolve_send_at(
        rule: ReminderRule,
        lesson_date: date,
        lesson_time: str,
        *,
        now: datetime | None = None,
    ) -> ReminderSchedule | None:
        """Вычисляет, когда нужно отправить напоминание по правилу.

        Args:
            rule: Правило напоминания.
            lesson_date: Дата занятия.
            lesson_time: Время начала занятия в формате ``ЧЧ:ММ``.
            now: Текущий момент (для тестов).

        Returns:
            Расчёт момента отправки или ``None``, если правило выключено
            либо параметры некорректны.
        """
        if not rule.is_enabled:
            return None

        if rule.kind == "day_before":
            send_time = _parse_time(rule.time_of_day) or time(19, 0)
            send_at = datetime.combine(lesson_date - timedelta(days=1), send_time)
            return ReminderSchedule(
                rule_kind=rule.kind,
                send_at=send_at,
                target_date=lesson_date,
                description=f"Напоминание о занятии {lesson_date:%d.%m.%Y}",
            )

        if rule.kind == "morning_of":
            send_time = _parse_time(rule.time_of_day) or time(8, 0)
            send_at = datetime.combine(lesson_date, send_time)
            return ReminderSchedule(
                rule_kind=rule.kind,
                send_at=send_at,
                target_date=lesson_date,
                description=f"Занятие сегодня в {lesson_time}",
            )

        return None

    @staticmethod
    def describe(rule: ReminderRule) -> str:
        """Формирует человекочитаемое описание правила.

        Args:
            rule: Правило напоминания.

        Returns:
            Описание для интерфейса кабинета.
        """
        label = KIND_LABELS.get(rule.kind, rule.kind)
        return f"{label}: в {rule.time_of_day or '—'}"

    @staticmethod
    def _validate(
        kind: str,
        *,
        time_of_day: str | None,
        offset_minutes: int | None,
        channel: str,
    ) -> dict[str, object]:
        """Проверяет параметры правила и приводит их к нужным типам.

        Args:
            kind: Нормализованный вид правила.
            time_of_day: Время отправки.
            offset_minutes: Смещение в минутах.
            channel: Канал доставки.

        Returns:
            Словарь с полями для записи в БД.

        Raises:
            ValueError: Если параметры некорректны.
        """
        payload: dict[str, object] = {}

        if channel not in REMINDER_CHANNELS:
            raise ValueError(
                f"Неизвестный канал: {channel!r}. Допустимо: {', '.join(REMINDER_CHANNELS)}."
            )
        payload["channel"] = channel

        value = time_of_day or DEFAULT_TIME_OF_DAY.get(kind)
        if value is None or _parse_time(value) is None:
            raise ValueError(f"Некорректное время напоминания: {value!r}. Ожидается ЧЧ:ММ.")
        payload["time_of_day"] = value
        payload["offset_minutes"] = None
        return payload
