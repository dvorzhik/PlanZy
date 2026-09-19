"""Pydantic-схемы для структурированного результата парсинга диктовки.

Эти схемы используются двумя способами:
1. Как ``response_schema`` для Gemini Flash — модель гарантирует валидный JSON.
2. Как контракт между парсером и Service Layer.
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field, ValidationInfo, field_validator

# Допустимые значения посещаемости
ATTENDANCE_VALUES = ("present", "absent", "cancelled", "rescheduled")

# Допустимые коды дней недели (совпадают с Entity.schedule_days)
WEEKDAY_CODES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

# Русские названия дней → коды. Нужны для нормализации расписания.
WEEKDAY_ALIASES: dict[str, str] = {
    "понедельник": "mon",
    "пн": "mon",
    "вторник": "tue",
    "вт": "tue",
    "среда": "wed",
    "среду": "wed",
    "ср": "wed",
    "четверг": "thu",
    "чт": "thu",
    "пятница": "fri",
    "пятницу": "fri",
    "пт": "fri",
    "суббота": "sat",
    "субботу": "sat",
    "сб": "sat",
    "воскресенье": "sun",
    "вс": "sun",
}


class PaymentInfo(BaseModel):
    """Информация о платеже, извлечённая из текста диктовки."""

    has_payment: bool = Field(
        default=False,
        description="True, если в тексте упоминается факт оплаты или пополнения баланса.",
    )
    amount: float | None = Field(
        default=None,
        description="Сумма платежа в валюте тенанта (например, шекели). None, если не указана.",
    )
    lessons_added: int | None = Field(
        default=None,
        description="Количество уроков, добавленных на баланс. None, если не указано.",
    )

    @field_validator("amount")
    @classmethod
    def _validate_amount(cls, value: float | None) -> float | None:
        """Сумма платежа не может быть отрицательной."""
        if value is not None and value < 0:
            raise ValueError("Сумма платежа не может быть отрицательной")
        return value

    @field_validator("lessons_added")
    @classmethod
    def _validate_lessons_added(cls, value: int | None) -> int | None:
        """Количество добавленных уроков не может быть отрицательным."""
        if value is not None and value < 0:
            raise ValueError("Количество добавленных уроков не может быть отрицательным")
        return value


class LessonParseResult(BaseModel):
    """Результат разбора надиктованного текста об уроке.

    Все поля, кроме ``entity_name``, опциональны: тренер может надиктовать
    минимум («Миша, шахматы, тема — вилка»), а остальное дозаполнить позже.
    """

    entity_name: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="Имя основного ученика или название группы, о которой идёт речь.",
    )
    extra_entities: list[str] = Field(
        default_factory=list,
        description=(
            "Остальные ученики, названные в диктовке (кроме основного). "
            "Например, для «Рон и Мия» — ['Мия']. Пустой список, если ученик один."
        ),
    )
    subject: str = Field(
        default="Шахматы",
        max_length=100,
        description="Предмет/направление занятия. По умолчанию «Шахматы».",
    )
    topic: str | None = Field(
        default=None,
        max_length=500,
        description="Тема урока (что разбирали).",
    )
    homework: str | None = Field(
        default=None,
        max_length=1000,
        description="Домашнее задание, выданное ученику.",
    )
    internal_notes: str | None = Field(
        default=None,
        max_length=2000,
        description=(
            "Приватные заметки тренера. НИКОГДА не показываются родителю на публичной странице."
        ),
    )
    rating: int | None = Field(
        default=None,
        ge=1,
        le=5,
        description="Оценка работы ученика на уроке по шкале от 1 до 5.",
    )
    next_step: str | None = Field(
        default=None,
        max_length=500,
        description="План на следующий урок (что делать дальше).",
    )
    attendance: str = Field(
        default="present",
        description=(
            "Посещаемость: present (был), absent (не пришёл), "
            "cancelled (отменили), rescheduled (перенесли)."
        ),
    )
    attendance_note: str | None = Field(
        default=None,
        max_length=500,
        description=(
            "Кто именно был на занятии, если тренер это назвал. Например, "
            "«были все, кроме Леонида» → 'все, кроме Леонида'. "
            "None, если состав не упоминался."
        ),
    )
    lesson_date: date | None = Field(
        default=None,
        description=(
            "Дата урока в формате YYYY-MM-DD. Если в тексте дата не названа — None "
            "(тогда используется текущая дата)."
        ),
    )
    schedule_days: list[str] = Field(
        default_factory=list,
        description=(
            "Дни недели, в которые занимается ученик, кодами: mon, tue, wed, thu, "
            "fri, sat, sun. Например, для «среда 15:15» — ['wed']. Пустой список, "
            "если расписание не названо."
        ),
    )
    schedule_time: str | None = Field(
        default=None,
        description=(
            "Время занятия в формате ЧЧ:ММ (24 часа). Например, «15:15» → '15:15'. "
            "None, если время не названо."
        ),
    )
    payment: PaymentInfo = Field(
        default_factory=PaymentInfo,
        description="Информация о платеже, если он упоминался в тексте.",
    )

    @field_validator("entity_name", "subject")
    @classmethod
    def _strip_required(cls, value: str) -> str:
        """Убираем лишние пробелы и проверяем, что строка не пустая."""
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Поле не может быть пустым")
        return cleaned

    @field_validator("topic", "homework", "internal_notes", "next_step", "attendance_note")
    @classmethod
    def _strip_optional(cls, value: str | None) -> str | None:
        """Нормализуем опциональные строковые поля."""
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None

    @field_validator("attendance")
    @classmethod
    def _validate_attendance(cls, value: str) -> str:
        """Приводим посещаемость к нижнему регистру и проверяем допустимость."""
        normalized = value.strip().lower()
        if normalized not in ATTENDANCE_VALUES:
            raise ValueError(
                f"Недопустимое значение посещаемости: {value!r}. "
                f"Ожидается одно из: {', '.join(ATTENDANCE_VALUES)}"
            )
        return normalized

    @field_validator("extra_entities")
    @classmethod
    def _clean_extra_entities(
        cls,
        value: list[str],
        info: ValidationInfo,
    ) -> list[str]:
        """Убираем пустые имена и дубликаты, сохраняя порядок.

        Основной ученик (``entity_name``) тоже участвует в проверке на
        дубликаты: «Рон и Рон» не должен превратиться в два урока.
        """
        cleaned: list[str] = []
        seen: set[str] = set()
        primary = info.data.get("entity_name")
        if isinstance(primary, str) and primary.strip():
            seen.add(primary.strip().lower())
        for raw in value:
            name = raw.strip()
            if not name:
                continue
            key = name.lower()
            if key in seen:
                continue
            seen.add(key)
            cleaned.append(name)
        return cleaned

    @field_validator("schedule_days")
    @classmethod
    def _normalize_schedule_days(cls, value: list[str]) -> list[str]:
        """Приводит дни недели к кодам mon..sun, отбрасывая непонятные."""
        normalized: list[str] = []
        for raw in value:
            token = raw.strip().lower()
            if not token:
                continue
            code = token if token in WEEKDAY_CODES else WEEKDAY_ALIASES.get(token)
            if code is None:
                continue
            if code not in normalized:
                normalized.append(code)
        return normalized

    @field_validator("schedule_time")
    @classmethod
    def _normalize_schedule_time(cls, value: str | None) -> str | None:
        """Приводит время к формату ЧЧ:ММ, отбрасывая некорректные значения.

        Понимает «15:15», «9.05», «9-05» и слитную запись «1500»/«915».
        """
        if value is None:
            return None
        cleaned = value.strip().replace(".", ":").replace("-", ":")
        if not cleaned:
            return None

        if ":" in cleaned:
            parts = cleaned.split(":")
            if len(parts) != 2:
                return None
            hours, minutes = parts
        elif cleaned.isdigit() and 3 <= len(cleaned) <= 4:
            # Слитная запись: «1500» → 15:00, «915» → 09:15.
            hours, minutes = cleaned[:-2], cleaned[-2:]
        else:
            return None

        if not (hours.isdigit() and minutes.isdigit()):
            return None
        hour_value, minute_value = int(hours), int(minutes)
        if not (0 <= hour_value <= 23 and 0 <= minute_value <= 59):
            return None
        return f"{hour_value:02d}:{minute_value:02d}"

    @property
    def is_group(self) -> bool:
        """Эвристика: считаем запись группой, если в имени есть слово «группа»."""
        return "групп" in self.entity_name.lower()

    @property
    def all_entity_names(self) -> list[str]:
        """Все названные в диктовке сущности: основной ученик + остальные.

        Порядок сохраняется, дубликаты (без учёта регистра) убираются.
        """
        names: list[str] = [self.entity_name]
        seen = {self.entity_name.lower()}
        for name in self.extra_entities:
            key = name.lower()
            if key in seen:
                continue
            seen.add(key)
            names.append(name)
        return names

    @property
    def has_schedule(self) -> bool:
        """Признак того, что в диктовке было названо расписание."""
        return bool(self.schedule_days) or self.schedule_time is not None
