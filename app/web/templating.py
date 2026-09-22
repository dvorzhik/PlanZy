"""Настройка окружения Jinja2 для веб-приложения.

Здесь создаётся единый экземпляр ``Jinja2Templates`` и регистрируются
глобальные фильтры/переменные, доступные во всех шаблонах.
"""

from __future__ import annotations

import html
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any

from fastapi.templating import Jinja2Templates
from markupsafe import Markup

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

# Схема URL, после которой адрес считается полным («https://lichess.org/puzzles»).
LINK_SCHEME_PATTERN = r"(?:https?://|www\.)"

# «Голые» домены без схемы («lichess.org/puzzles») распознаём только по
# знакомым зонам: иначе слова с точкой («стр. 24», «10.5 задач») стали бы
# ссылками. Список намеренно короткий — самые частые ресурсы для занятий.
KNOWN_DOMAIN_PATTERN = (
    r"(?:"
    r"[a-z0-9-]+\.(?:com|org|net|edu|io|ru|me|app|dev|tv|info|site|online|xyz)"
    r"|[a-z0-9-]+\.(?:рф)"
    r")"
)

# Хвост домена: путь, параметры и якорь без пробелов и кавычек.
LINK_TAIL_PATTERN = r"/[^\s<>\"']*"

# Итоговое регулярное выражение для автолинковки:
#   1. полный адрес со схемой — «https://…», «http://…», «www.…»;
#   2. домен из белого списка зон, обязательно с путём: «lichess.org/abc».
URL_PATTERN = re.compile(
    rf"(?P<url>"
    rf"{LINK_SCHEME_PATTERN}[^\s<>\"']+"
    rf"|{KNOWN_DOMAIN_PATTERN}{LINK_TAIL_PATTERN}"
    rf")",
    re.IGNORECASE,
)

# Знаки, которые часто ставят вплотную к ссылке и которые ссылкой не являются.
TRAILING_PUNCTUATION = ".,;:!?…»”')]}"


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


def _trim_link(url: str) -> tuple[str, str]:
    """Отделяет от ссылки прилипшую пунктуацию.

    Тренер часто пишет «задачи тут: lichess.org/puzzles.» — завершающая
    точка относится к предложению, а не к адресу, и в href попадать
    не должна.

    Args:
        url: Найденный адрес.

    Returns:
        Кортеж ``(чистый адрес, хвост из знаков)``.
    """
    tail = ""
    while url and url[-1] in TRAILING_PUNCTUATION:
        # Закрывающую скобку оставляем в адресе, если внутри ссылки есть
        # открывающая (типично для ссылок вида «…/abc_(def)») или если
        # после неё идёт ещё одна закрывающая — «(см. example.com/a)».
        if url[-1] == ")" and url.count("(") >= url.count(")"):
            break
        tail = url[-1] + tail
        url = url[:-1]
    return url, tail


def _to_href(url: str) -> str:
    """Приводит распознанный адрес к абсолютному виду.

    Args:
        url: Адрес в том виде, как его написал тренер.

    Returns:
        Адрес со схемой: «www.lichess.org» → «https://www.lichess.org».
    """
    lowered = url.lower()
    if lowered.startswith(("http://", "https://")):
        return url
    return f"https://{url}"


def linkify(value: Any) -> Markup:
    """Превращает ссылки в тексте в активные, сохраняя переносы строк.

    Используется в шаблонах для публичных полей (ДЗ, план, примечание):
    тренер пишет «lichess.org/puzzles» или «https://www.youtube.com/…»,
    а родитель получает кликабельную ссылку.

    Текст предварительно экранируется через ``html.escape``, поэтому
    вставка HTML из данных по-прежнему невозможна (XSS не появится):
    единственные теги в результате — ``<a>``, которые построил сам фильтр.
    Переносы строк не заменяются на ``<br>`` — шаблоны уже используют
    CSS-класс ``whitespace-pre-line``.

    Args:
        value: Исходный текст (обычно поле занятия). None даёт пустую строку.

    Returns:
        Безопасный HTML-фрагмент со ссылками ``<a target="_blank">``.
    """
    if value is None:
        return Markup("")

    text = value if isinstance(value, str) else str(value)
    if not text:
        return Markup("")

    parts: list[str] = []
    cursor = 0
    for match in URL_PATTERN.finditer(text):
        start, end = match.span("url")
        found = match.group("url")
        link, tail = _trim_link(found)
        if not link:
            continue

        parts.append(html.escape(text[cursor:start]))
        href = html.escape(_to_href(link), quote=True)
        parts.append(
            f'<a href="{href}" target="_blank" rel="noopener noreferrer"'
            f' class="break-all text-brand-600 underline">{html.escape(link)}</a>'
        )
        cursor = end - len(tail)

    if not parts:
        return Markup(html.escape(text))

    parts.append(html.escape(text[cursor:]))
    return Markup("".join(parts))


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
    templates.env.filters["linkify"] = linkify

    return templates


# Единый экземпляр шаблонов для всего приложения.
templates = create_templates()
