"""Клавиатуры бота PlanZy.

Inline-кнопки ведут на внешние ссылки (wa.me и публичную карточку),
поэтому используются ``InlineKeyboardButton`` с ``url``.

Telegram принимает в кнопках только абсолютные публичные URL. Локальные
адреса (``localhost``, ``127.0.0.1``) он отклоняет с ошибкой
``Bad Request: Wrong HTTP URL``, поэтому такие ссылки отфильтровываются —
иначе падала бы отправка всего сообщения.

Кнопки-действия (открыть досье из списка) используют ``callback_data``:
её обрабатывают callback-хендлеры в ``app/bot/handlers.py``.
"""

from __future__ import annotations

from collections.abc import Sequence
from urllib.parse import urlparse

from aiogram.types import (
    CopyTextButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from app.db.models import Entity
from app.services.entity_service import EntityService
from app.services.whatsapp_service import WhatsAppMessage

# Хосты, которые Telegram гарантированно не примет в inline-кнопке.
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "0.0.0.0", "::1"})

# Префиксы callback_data для списков учеников и групп.
ENTITY_CALLBACK_PREFIX = "entity"
ENTITY_PAGE_CALLBACK_PREFIX = "entity_page"

# Callback-кнопки подтверждения записи урока.
LESSON_CONFIRM_CALLBACK = "lesson_save"
LESSON_CANCEL_CALLBACK = "lesson_cancel"

# Сколько сущностей показывать на одной странице списка.
ENTITIES_PER_PAGE = 8


def is_public_url(url: str | None) -> bool:
    """Проверяет, пригодна ли ссылка для inline-кнопки Telegram.

    Args:
        url: Проверяемый адрес.

    Returns:
        True, если это абсолютный http(s)-URL с публичным хостом.
    """
    if not url:
        return False
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return False
    host = (parsed.hostname or "").lower()
    if not host or host in _LOCAL_HOSTS:
        return False
    # Приватные диапазоны и .local тоже недоступны извне.
    if host.endswith(".local"):
        return False
    if host.startswith(("10.", "192.168.", "169.254.")):
        return False
    if host.startswith("172."):
        try:
            second = int(host.split(".")[1])
        except (IndexError, ValueError):
            return False
        if 16 <= second <= 31:
            return False
    return True


def _button(text: str, url: str | None) -> InlineKeyboardButton | None:
    """Создаёт кнопку-ссылку, если URL пригоден для Telegram.

    Args:
        text: Подпись кнопки.
        url: Адрес перехода.

    Returns:
        Кнопка или None, если ссылку нельзя использовать.
    """
    if not is_public_url(url):
        return None
    return InlineKeyboardButton(text=text, url=url)  # type: ignore[arg-type]


def _whatsapp_button(
    whatsapp: WhatsAppMessage, *, dossier: bool = False
) -> InlineKeyboardButton | None:
    """Собирает кнопку WhatsApp с подписью, зависящей от адресата.

    Args:
        whatsapp: Готовое сообщение WhatsApp.
        dossier: True для клавиатуры досье (другая подпись).

    Returns:
        Кнопка или None, если адресат неизвестен и ссылка не пригодна.
    """
    if whatsapp.chat_url:
        text = "📲 Написать в чат группы" if dossier else "📲 Отправить в чат группы"
        return _button(text, whatsapp.url)
    if whatsapp.has_phone:
        text = "📲 Написать родителю" if dossier else "📲 Отправить родителю в WhatsApp"
        return _button(text, whatsapp.url)
    # Без адресата WhatsApp предложит выбрать получателя вручную.
    return _button("📲 Открыть WhatsApp (выбрать чат)", whatsapp.url)


def lesson_report_keyboard(
    whatsapp: WhatsAppMessage,
    card_url: str | None = None,
    dossier_url: str | None = None,
    lesson_url: str | None = None,
) -> InlineKeyboardMarkup | None:
    """Собирает клавиатуру к отчёту об уроке.

    Args:
        whatsapp: Готовое сообщение WhatsApp (содержит ссылку wa.me).
        card_url: Публичная ссылка на карточку ученика.
        dossier_url: Ссылка на досье ученика.
        lesson_url: Публичная ссылка на веб-карточку этого занятия.

    Returns:
        Клавиатура или None, если ни одной валидной ссылки нет.
    """
    rows: list[list[InlineKeyboardButton]] = []

    whatsapp_button = _whatsapp_button(whatsapp)
    if whatsapp_button is not None:
        rows.append([whatsapp_button])

    lesson_button = _button("🔗 Карточка урока", lesson_url)
    if lesson_button is not None:
        rows.append([lesson_button])

    secondary: list[InlineKeyboardButton] = []
    card_button = _button("🎫 Карточка ученика", card_url)
    if card_button is not None:
        secondary.append(card_button)
    dossier_button = _button("📁 Досье", dossier_url)
    if dossier_button is not None:
        secondary.append(dossier_button)
    if secondary:
        rows.append(secondary)

    if not rows:
        return None
    return InlineKeyboardMarkup(inline_keyboard=rows)


def dossier_keyboard(
    card_url: str | None = None,
    whatsapp: WhatsAppMessage | None = None,
    admin_url: str | None = None,
    *,
    back_callback: str | None = None,
) -> InlineKeyboardMarkup | None:
    """Собирает клавиатуру к досье ученика.

    Args:
        card_url: Публичная ссылка на карточку ученика.
        whatsapp: Готовое сообщение WhatsApp для напоминания родителю.
        admin_url: Ссылка на веб-кабинет тренера (для правок и расписания).
        back_callback: Данные callback-кнопки «Назад к списку» (если досье
            открыто из меню ``/student`` или ``/group``).

    Returns:
        Клавиатура или None, если валидных ссылок нет.
    """
    rows: list[list[InlineKeyboardButton]] = []

    if whatsapp is not None:
        whatsapp_button = _whatsapp_button(whatsapp, dossier=True)
        if whatsapp_button is not None:
            rows.append([whatsapp_button])

        # В ссылке-приглашении группы WhatsApp игнорирует ?text=, поэтому
        # даём отдельную кнопку: тренер копирует текст и вставляет в чат.
        if whatsapp.chat_url and whatsapp.text:
            rows.append(
                [
                    InlineKeyboardButton(
                        text="📋 Скопировать текст",
                        copy_text=CopyTextButton(text=whatsapp.text[:256]),
                    )
                ]
            )

    card_button = _button("🎫 Карточка ученика", card_url)
    if card_button is not None:
        rows.append([card_button])

    admin_button = _button("🖥 Веб-кабинет", admin_url)
    if admin_button is not None:
        rows.append([admin_button])

    if back_callback:
        rows.append([InlineKeyboardButton(text="🔙 К списку", callback_data=back_callback)])

    if not rows:
        return None
    return InlineKeyboardMarkup(inline_keyboard=rows)


def lesson_confirm_keyboard() -> InlineKeyboardMarkup:
    """Собирает клавиатуру подтверждения записи занятия.

    Returns:
        Клавиатура с кнопками «Да, записать» и «Нет».
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Да, записать",
                    callback_data=LESSON_CONFIRM_CALLBACK,
                ),
                InlineKeyboardButton(
                    text="❌ Нет",
                    callback_data=LESSON_CANCEL_CALLBACK,
                ),
            ]
        ]
    )


def admin_keyboard(admin_url: str | None) -> InlineKeyboardMarkup | None:
    """Собирает клавиатуру со ссылкой на веб-кабинет.

    Args:
        admin_url: Секретная ссылка входа в кабинет.

    Returns:
        Клавиатура или None, если ссылку нельзя использовать в кнопке.
    """
    button = _button("🖥 Открыть веб-кабинет", admin_url)
    if button is None:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[button]])


def entity_list_keyboard(
    entities: Sequence[Entity],
    *,
    entity_type: str,
    page: int = 0,
    page_size: int = ENTITIES_PER_PAGE,
) -> InlineKeyboardMarkup | None:
    """Собирает клавиатуру со списком учеников или групп.

    Одна кнопка — одна сущность: нажатие открывает досье (аналог
    ``/dossier Имя``). Длинные списки разбиваются на страницы, чтобы
    сообщение не упиралось в лимит Telegram на количество кнопок.

    Args:
        entities: Полный список учеников или групп тенанта.
        entity_type: Тип сущностей: ``"student"`` или ``"group"``.
        page: Номер страницы (с нуля).
        page_size: Сколько кнопок помещается на одну страницу.

    Returns:
        Клавиатура или None, если список пуст.
    """
    if not entities:
        return None

    total_pages = max(1, (len(entities) + page_size - 1) // page_size)
    current_page = max(0, min(page, total_pages - 1))
    start = current_page * page_size
    chunk = entities[start : start + page_size]

    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text=_entity_button_label(entity),
                callback_data=f"{ENTITY_CALLBACK_PREFIX}:{entity_type}:{entity.id}:{current_page}",
            )
        ]
        for entity in chunk
    ]

    if total_pages > 1:
        navigation: list[InlineKeyboardButton] = []
        if current_page > 0:
            navigation.append(
                InlineKeyboardButton(
                    text="◀️",
                    callback_data=f"{ENTITY_PAGE_CALLBACK_PREFIX}:{entity_type}:{current_page - 1}",
                )
            )
        navigation.append(
            InlineKeyboardButton(
                text=f"{current_page + 1}/{total_pages}",
                callback_data=f"{ENTITY_PAGE_CALLBACK_PREFIX}:{entity_type}:{current_page}",
            )
        )
        if current_page < total_pages - 1:
            navigation.append(
                InlineKeyboardButton(
                    text="▶️",
                    callback_data=f"{ENTITY_PAGE_CALLBACK_PREFIX}:{entity_type}:{current_page + 1}",
                )
            )
        rows.append(navigation)

    return InlineKeyboardMarkup(inline_keyboard=rows)


def _entity_button_label(entity: Entity) -> str:
    """Собирает подпись кнопки со сущностью.

    Args:
        entity: Ученик или группа.

    Returns:
        Подпись вида «Миша · Пн, Ср в 16:00» или просто имя.
    """
    schedule = EntityService.format_schedule(entity)
    if schedule == "расписание не задано":
        return entity.name
    return f"{entity.name} · {schedule}"
