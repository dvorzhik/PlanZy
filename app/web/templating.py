"""Настройка окружения Jinja2 для веб-приложения.

Здесь создаётся единый экземпляр ``Jinja2Templates`` и регистрируются
глобальные фильтры/переменные, доступные во всех шаблонах.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any

from fastapi.templating import Jinja2Templates

from app.core.config import BASE_DIR, settings

# Директория с HTML-шаблонами.
TEMPLATES_DIR: Path = BASE_DIR / "app" / "web" / "templates"

# Директория со статикой (CSS/JS/иконки).
STATIC_DIR: Path = BASE_DIR / "app" / "web" / "static"

# Русские названия месяцев в родительном падеже для формата «17 сентября 2026».
MONTHS_GENITIVE = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)

# Русские названия дней недели (0 = понедельник).
WEEKDAY_LABELS = (
    "Понедельник",
    "Вторник",
    "Среда",
    "Четверг",
    "Пятница",
    "Суббота",
    "Воскресенье",
)

# Человекочитаемые подписи для оценок динамики.
TREND_LABELS = {
    "up": "Растёт",
    "down": "Снижается",
    "flat": "Стабильно",
}

# Подписи для статуса посещаемости.
ATTENDANCE_LABELS = {
    "present": "Присутствовал",
    "absent": "Отсутствовал",
    "cancelled": "Отменён",
    "rescheduled": "Перенесён",
}


def format_date_ru(value: date | datetime | None) -> str:
    """Форматирует дату по-русски: «17 сентября 2026».

    Args:
        value: Дата или datetime. None возвращает прочерк.

    Returns:
        Строка с датой на русском языке.
    """
    if value is None:
        return "—"
    if isinstance(value, datetime):
        value = value.date()
    return f"{value.day} {MONTHS_GENITIVE[value.month - 1]} {value.year}"


def format_weekday_ru(value: date | datetime | None) -> str:
    """Возвращает название дня недели по-русски.

    Args:
        value: Дата или datetime.

    Returns:
        Название дня недели или пустая строка.
    """
    if value is None:
        return ""
    if isinstance(value, datetime):
        value = value.date()
    return WEEKDAY_LABELS[value.weekday()]


def format_money(value: float | None, currency: str = "ILS") -> str:
    """Форматирует сумму с символом валюты.

    Args:
        value: Сумма или None.
        currency: Код валюты (ILS, RUB, USD).

    Returns:
        Строка вида «₪ 1 200» или прочерк.
    """
    if value is None:
        return "—"
    symbols = {"ILS": "₪", "RUB": "₽", "USD": "$", "EUR": "€"}
    symbol = symbols.get(currency.upper(), currency)
    formatted = f"{value:,.0f}".replace(",", " ")
    return f"{symbol} {formatted}"


def pluralize_lessons(count: int) -> str:
    """Склоняет слово «занятие» по числу.

    Args:
        count: Количество занятий.

    Returns:
        Правильная форма: «занятие», «занятия» или «занятий».
    """
    remainder_100 = abs(count) % 100
    remainder_10 = abs(count) % 10
    if 11 <= remainder_100 <= 19:
        return "занятий"
    if remainder_10 == 1:
        return "занятие"
    if 2 <= remainder_10 <= 4:
        return "занятия"
    return "занятий"


def create_templates() -> Jinja2Templates:
    """Создаёт и настраивает окружение шаблонов Jinja2.

    Returns:
        Готовый к использованию экземпляр ``Jinja2Templates``.
    """
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    globals_map: dict[str, Any] = {
        "app_name": "PlanZy",
        "base_url": settings.base_url,
        "trend_labels": TREND_LABELS,
        "attendance_labels": ATTENDANCE_LABELS,
    }
    templates.env.globals.update(globals_map)

    templates.env.filters["date_ru"] = format_date_ru
    templates.env.filters["weekday_ru"] = format_weekday_ru
    templates.env.filters["money"] = format_money
    templates.env.filters["pluralize_lessons"] = pluralize_lessons

    return templates


# Единый экземпляр шаблонов для всего приложения.
templates = create_templates()
