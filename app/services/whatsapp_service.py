"""Сервис генерации ссылок WhatsApp (wa.me).

Формирует готовые ссылки для отправки родителю отчёта об уроке и ссылки
на публичную смарт-карточку ученика. Тренеру остаётся только нажать кнопку
в Telegram — сообщение уже подставлено в WhatsApp.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from urllib.parse import quote, urlparse

from app.core.config import settings
from app.core.logging import get_logger
from app.db.models import Entity, Lesson, Payment

logger = get_logger(__name__)

# Базовый адрес WhatsApp для отправки сообщения конкретному номеру.
WHATSAPP_BASE_URL = "https://wa.me"

# Максимальная длина текста сообщения, которую принимает wa.me.
# Официальный лимит — около 2000 символов, оставляем запас.
MAX_MESSAGE_LENGTH = 1800


@dataclass(slots=True)
class WhatsAppMessage:
    """Готовое сообщение для WhatsApp.

    Attributes:
        phone: Номер получателя в международном формате без «+».
        text: Текст сообщения.
        url: Ссылка wa.me или ссылка на чат с уже закодированным текстом.
        has_phone: True, если адресат известен и ссылка адресная.
        chat_url: Ссылка на групповой чат, если адресат — чат, а не номер.
    """

    phone: str | None
    text: str
    url: str
    has_phone: bool
    chat_url: str | None = None


class WhatsAppLinkService:
    """Бизнес-логика формирования ссылок и текстов для WhatsApp."""

    def __init__(self, base_url: str | None = None) -> None:
        """Инициализирует сервис.

        Args:
            base_url: Базовый адрес сайта для ссылок на карточку.
                По умолчанию берётся из настроек приложения.
        """
        self._base_url = (base_url or settings.base_url).rstrip("/")

    # ------------------------------------------------------------------
    # Публичные методы
    # ------------------------------------------------------------------

    def build_lesson_report(
        self,
        entity: Entity,
        lesson: Lesson,
        *,
        payment: Payment | None = None,
    ) -> WhatsAppMessage:
        """Формирует отчёт родителю об уроке.

        Args:
            entity: Ученик или группа.
            lesson: Сохранённый урок.
            payment: Платёж, если он был в этой диктовке.

        Returns:
            Готовое сообщение со ссылкой wa.me.
        """
        lines: list[str] = [
            f"Доброго дня! Отчёт о занятии — {entity.name}.",
            "",
            f"📅 Дата: {self._format_date(lesson.lesson_date)}",
        ]

        if lesson.topic:
            lines.append(f"📚 Тема: {lesson.topic}")
        if lesson.rating:
            lines.append(f"⭐ Оценка: {lesson.rating}/5")
        if lesson.homework:
            lines.append(f"📝 Домашнее задание: {lesson.homework}")
        if lesson.next_step:
            lines.append(f"➡️ На следующем занятии: {lesson.next_step}")

        if payment is not None:
            lines.append("")
            lines.append(f"💰 Оплата: {self._format_payment(payment)}")

        card_url = self.build_card_url(entity)
        if card_url:
            lines.append("")
            lines.append(f"Подробнее: {card_url}")

        text = "\n".join(lines)
        return self._build_message(entity.parent_phone, text)

    def build_reminder(
        self,
        entity: Entity,
        *,
        lesson_date: date | None = None,
        lesson_time: str | None = None,
    ) -> WhatsAppMessage:
        """Формирует напоминание о занятии (для рассылки и досье).

        Получатель выбирается по типу сущности:

        * у группы (или любого ученика со ссылкой на чат) — групповой чат
          ``whatsapp_chat_url``, ссылка ведёт прямо в чат;
        * у ученика — личный номер родителя ``parent_phone``;
        * если ничего не задано — ссылка без адресата, WhatsApp предложит
          выбрать чат вручную.

        Время начала занятия берётся из переданного ``lesson_time``
        (фактическое время из расписания, включая переносы и доп. занятия),
        а не из устаревшего поля ``Entity.schedule_time``: после перехода
        на web-first оно пустое у всех.

        Args:
            entity: Ученик или группа.
            lesson_date: Дата занятия. По умолчанию — сегодня.
            lesson_time: Время начала занятия в формате ``ЧЧ:ММ``.

        Returns:
            Готовое сообщение со ссылкой wa.me.
        """
        when = self._format_date(lesson_date or date.today())
        time_part = f" в {lesson_time}" if lesson_time else ""
        lines = [
            f"Доброго дня! Напоминаю о занятии — {entity.name}.",
            "",
            f"📅 {when}{time_part}",
        ]

        text = "\n".join(lines)
        return self._build_message(entity.parent_phone, text, chat_url=entity.whatsapp_chat_url)

    def build_chat_message(self, entity: Entity, text: str) -> WhatsAppMessage:
        """Формирует произвольное сообщение в чат сущности.

        Используется для кнопки «Написать в чат» в досье группы: адресат —
        групповой чат WhatsApp, если он задан, иначе личный номер родителя.

        Args:
            entity: Ученик или группа.
            text: Текст сообщения.

        Returns:
            Готовое сообщение со ссылкой.
        """
        return self._build_message(entity.parent_phone, text, chat_url=entity.whatsapp_chat_url)

    def build_dossier_message(self, entity: Entity, dossier_text: str) -> WhatsAppMessage:
        """Формирует сообщение для кнопки WhatsApp в досье.

        Для группы адресат — её чат WhatsApp, для ученика — номер родителя.
        Текст подбирается по адресату: в чат уходит сводка по группе,
        родителю — сводка по ребёнку.

        Args:
            entity: Ученик или группа.
            dossier_text: Готовое текстовое досье.

        Returns:
            Готовое сообщение со ссылкой.
        """
        if entity.is_group:
            header = f"Доброго дня! Информация по группе «{entity.name}»."
        else:
            header = f"Доброго дня! Информация по занятиям — {entity.name}."
        return self._build_message(
            entity.parent_phone,
            f"{header}\n\n{dossier_text}",
            chat_url=entity.whatsapp_chat_url,
        )

    def build_card_url(self, entity: Entity) -> str | None:
        """Возвращает публичную ссылку на карточку ученика.

        Args:
            entity: Сущность с публичным токеном.

        Returns:
            Абсолютный URL карточки или None, если токена нет.
        """
        if not entity.public_token:
            return None
        return f"{self._base_url}/l/{entity.public_token}"

    def build_dossier_url(self, entity: Entity) -> str | None:
        """Возвращает ссылку на досье ученика (для преподавателя).

        Args:
            entity: Сущность с публичным токеном.

        Returns:
            Абсолютный URL досье или None, если токена нет.
        """
        if not entity.public_token:
            return None
        return f"{self._base_url}/dossier/{entity.public_token}"

    def build_plain_link(self, phone: str | None, text: str) -> str:
        """Строит ссылку wa.me без привязки к сущности.

        Args:
            phone: Номер получателя (может быть None).
            text: Текст сообщения.

        Returns:
            Ссылка wa.me с закодированным текстом.
        """
        return self._build_message(phone, text).url

    # ------------------------------------------------------------------
    # Внутренние помощники
    # ------------------------------------------------------------------

    def _build_message(
        self,
        phone: str | None,
        text: str,
        *,
        chat_url: str | None = None,
    ) -> WhatsAppMessage:
        """Собирает объект сообщения WhatsApp.

        Приоритет адресата:

        1. ``chat_url`` — ссылка на групповой чат: телефон не нужен,
           WhatsApp откроет чат с уже подставленным текстом;
        2. ``phone`` — личный номер получателя;
        3. без адресата — WhatsApp предложит выбрать чат вручную.

        Args:
            phone: Телефон получателя в произвольном формате.
            text: Текст сообщения.
            chat_url: Ссылка на групповой чат WhatsApp.

        Returns:
            Готовое сообщение со ссылкой.
        """
        trimmed = self._truncate(text)
        encoded = quote(trimmed, safe="")

        chat = self.normalize_chat_url(chat_url)
        if chat:
            # Ссылка-приглашение ведёт в чат. WhatsApp игнорирует ?text=
            # у таких ссылок, поэтому текст не приклеиваем: его тренер
            # скопирует отдельной кнопкой в интерфейсе.
            return WhatsAppMessage(
                phone=None,
                text=trimmed,
                url=chat,
                has_phone=True,
                chat_url=chat,
            )

        normalized = self.normalize_phone(phone)

        if normalized:
            url = f"{WHATSAPP_BASE_URL}/{normalized}?text={encoded}"
        else:
            # Без адресата WhatsApp предложит выбрать получателя вручную.
            url = f"{WHATSAPP_BASE_URL}/?text={encoded}"

        return WhatsAppMessage(
            phone=normalized,
            text=trimmed,
            url=url,
            has_phone=normalized is not None,
        )

    @staticmethod
    def normalize_chat_url(chat_url: str | None) -> str | None:
        """Проверяет и нормализует ссылку на групповой чат WhatsApp.

        Принимает только ссылки-приглашения ``chat.whatsapp.com``: прочие
        адреса отбрасываются, чтобы кнопка не вела на сторонний ресурс.

        Args:
            chat_url: Ссылка из карточки группы.

        Returns:
            Нормализованная ссылка без параметров или None, если ссылка
            пуста либо ведёт не на чат WhatsApp.
        """
        if not chat_url:
            return None

        cleaned = chat_url.strip()
        if not cleaned:
            return None

        if not cleaned.startswith(("http://", "https://")):
            cleaned = f"https://{cleaned}"

        parsed = urlparse(cleaned)
        host = (parsed.hostname or "").lower()
        if host not in {"chat.whatsapp.com", "www.chat.whatsapp.com"}:
            logger.warning("Ссылка на чат отклонена (не chat.whatsapp.com): %r", chat_url)
            return None

        path = parsed.path.rstrip("/")
        if not path or path == "/":
            logger.warning("Ссылка на чат без идентификатора: %r", chat_url)
            return None

        return f"https://chat.whatsapp.com{path}"

    @staticmethod
    def normalize_phone(phone: str | None) -> str | None:
        """Приводит номер телефона к формату wa.me (только цифры).

        Убирает пробелы, дефисы, скобки и ведущий «+».

        Args:
            phone: Исходный номер телефона.

        Returns:
            Номер из одних цифр или None, если номер пуст/некорректен.
        """
        if not phone:
            return None
        digits = "".join(ch for ch in phone if ch.isdigit())
        if len(digits) < 8:
            logger.warning("Некорректный номер телефона: %r", phone)
            return None
        return digits

    @staticmethod
    def _truncate(text: str) -> str:
        """Обрезает слишком длинный текст сообщения.

        Args:
            text: Исходный текст.

        Returns:
            Текст, укладывающийся в лимит wa.me.
        """
        if len(text) <= MAX_MESSAGE_LENGTH:
            return text
        return text[: MAX_MESSAGE_LENGTH - 1].rstrip() + "…"

    @staticmethod
    def _format_date(value: date | None) -> str:
        """Форматирует дату в русском формате ДД.ММ.ГГГГ."""
        if value is None:
            return "—"
        return value.strftime("%d.%m.%Y")

    @staticmethod
    def _format_payment(payment: Payment) -> str:
        """Форматирует платёж для отчёта родителю.

        Args:
            payment: Запись о платеже.

        Returns:
            Строка вида «500 ILS, +8 занятий».
        """
        parts: list[str] = []
        if payment.amount is not None:
            parts.append(f"{payment.amount:g} {payment.currency}")
        if payment.lessons_added:
            parts.append(f"+{payment.lessons_added} занятий")
        return ", ".join(parts) if parts else "получена"
