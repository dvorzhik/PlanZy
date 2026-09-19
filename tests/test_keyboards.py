"""Тесты клавиатур бота: фильтрация ссылок, пригодных для Telegram.

Telegram отклоняет inline-кнопки с локальными адресами
(``Bad Request: Wrong HTTP URL``), поэтому такие ссылки не должны
попадать в разметку — иначе падает отправка всего сообщения.
"""

from __future__ import annotations

import pytest

from app.bot.keyboards import (
    ENTITIES_PER_PAGE,
    admin_keyboard,
    dossier_keyboard,
    entity_list_keyboard,
    is_public_url,
    lesson_report_keyboard,
)
from app.db.models import Entity
from app.services.whatsapp_service import WhatsAppMessage


def _whatsapp(
    url: str,
    *,
    has_phone: bool = True,
    chat_url: str | None = None,
    text: str = "тест",
) -> WhatsAppMessage:
    """Создаёт заглушку сообщения WhatsApp."""
    return WhatsAppMessage(
        phone=None if chat_url else ("972501234567" if has_phone else None),
        text=text,
        url=url,
        has_phone=has_phone,
        chat_url=chat_url,
    )


class TestIsPublicUrl:
    """Проверки отбора ссылок, допустимых в inline-кнопках."""

    @pytest.mark.parametrize(
        "url",
        [
            "https://planzy.example.com/l/abc",
            "http://planzy.example.com/l/abc",
            "https://sub.domain.co.il/dossier/xyz",
            "https://wa.me/972501234567",
        ],
    )
    def test_accepts_public_urls(self, url: str) -> None:
        assert is_public_url(url) is True

    @pytest.mark.parametrize(
        "url",
        [
            None,
            "",
            "http://localhost:8010/l/abc",
            "http://127.0.0.1:8010/l/abc",
            "http://0.0.0.0:8000/",
            "http://[::1]:8000/",
            "http://192.168.1.10:8000/l/abc",
            "http://10.0.0.5:8000/l/abc",
            "http://172.16.0.1:8000/l/abc",
            "http://172.31.255.1:8000/l/abc",
            "http://169.254.1.1/l/abc",
            "http://printer.local/l/abc",
            "ftp://example.com/file",
            "not-a-url",
            "/l/abc",
        ],
    )
    def test_rejects_local_and_invalid_urls(self, url: str | None) -> None:
        assert is_public_url(url) is False

    def test_accepts_public_172_range(self) -> None:
        """172.32.x.x уже вне приватного диапазона."""
        assert is_public_url("http://172.32.0.1/l/abc") is True


class TestLessonReportKeyboard:
    """Проверки клавиатуры отчёта об уроке."""

    def test_builds_all_buttons_for_public_urls(self) -> None:
        keyboard = lesson_report_keyboard(
            whatsapp=_whatsapp("https://wa.me/972501234567"),
            card_url="https://planzy.example.com/l/abc",
            dossier_url="https://planzy.example.com/dossier/abc",
        )
        assert keyboard is not None
        assert len(keyboard.inline_keyboard) == 2
        assert len(keyboard.inline_keyboard[1]) == 2

    def test_skips_local_card_and_dossier(self) -> None:
        """Локальные ссылки отбрасываются, но WhatsApp-кнопка остаётся."""
        keyboard = lesson_report_keyboard(
            whatsapp=_whatsapp("https://wa.me/972501234567"),
            card_url="http://localhost:8010/l/abc",
            dossier_url="http://127.0.0.1:8010/dossier/abc",
        )
        assert keyboard is not None
        assert len(keyboard.inline_keyboard) == 1
        assert "wa.me" in keyboard.inline_keyboard[0][0].url

    def test_returns_none_when_all_urls_local(self) -> None:
        keyboard = lesson_report_keyboard(
            whatsapp=_whatsapp("http://localhost:8010/wa"),
            card_url="http://localhost:8010/l/abc",
            dossier_url=None,
        )
        assert keyboard is None

    def test_without_phone_uses_generic_whatsapp_button(self) -> None:
        keyboard = lesson_report_keyboard(
            whatsapp=_whatsapp("https://wa.me/", has_phone=False),
        )
        assert keyboard is not None
        assert "выбрать чат" in keyboard.inline_keyboard[0][0].text


class TestDossierKeyboard:
    """Проверки клавиатуры досье."""

    def test_builds_buttons_for_public_urls(self) -> None:
        keyboard = dossier_keyboard(
            card_url="https://planzy.example.com/l/abc",
            whatsapp=_whatsapp("https://wa.me/972501234567"),
        )
        assert keyboard is not None
        assert len(keyboard.inline_keyboard) == 2

    def test_skips_local_card_url(self) -> None:
        keyboard = dossier_keyboard(
            card_url="http://localhost:8010/l/abc",
            whatsapp=_whatsapp("https://wa.me/972501234567"),
        )
        assert keyboard is not None
        assert len(keyboard.inline_keyboard) == 1
        assert "Написать родителю" in keyboard.inline_keyboard[0][0].text

    def test_returns_none_without_valid_urls(self) -> None:
        keyboard = dossier_keyboard(
            card_url="http://localhost:8010/l/abc",
            whatsapp=None,
        )
        assert keyboard is None

    def test_group_chat_button_is_shown(self) -> None:
        """Для группы кнопка ведёт в чат, даже если телефона нет.

        Регрессия: раньше кнопка появлялась только при ``has_phone``,
        поэтому у групп её не было вовсе, хотя ссылка на чат задана.
        """
        chat = "https://chat.whatsapp.com/AbCdEf123"
        keyboard = dossier_keyboard(whatsapp=_whatsapp(chat, chat_url=chat, text="Привет"))

        assert keyboard is not None
        assert keyboard.inline_keyboard[0][0].text == "📲 Написать в чат группы"
        assert keyboard.inline_keyboard[0][0].url == chat

    def test_group_chat_has_copy_text_button(self) -> None:
        """Для чата добавляется кнопка копирования текста.

        WhatsApp игнорирует ``?text=`` у ссылок-приглашений, поэтому текст
        передаётся отдельной кнопкой copy_text.
        """
        chat = "https://chat.whatsapp.com/AbCdEf123"
        keyboard = dossier_keyboard(whatsapp=_whatsapp(chat, chat_url=chat, text="Текст досье"))

        assert keyboard is not None
        copy_buttons = [row[0] for row in keyboard.inline_keyboard if row[0].copy_text is not None]
        assert len(copy_buttons) == 1
        assert copy_buttons[0].copy_text is not None
        assert copy_buttons[0].copy_text.text == "Текст досье"

    def test_personal_chat_has_no_copy_button(self) -> None:
        """У личного сообщения родителю кнопки копирования нет."""
        keyboard = dossier_keyboard(whatsapp=_whatsapp("https://wa.me/972501234567"))

        assert keyboard is not None
        assert all(button.copy_text is None for row in keyboard.inline_keyboard for button in row)

    def test_chat_without_text_has_no_copy_button(self) -> None:
        """Пустой текст не создаёт кнопку копирования."""
        chat = "https://chat.whatsapp.com/AbCdEf123"
        keyboard = dossier_keyboard(whatsapp=_whatsapp(chat, chat_url=chat, text=""))

        assert keyboard is not None
        assert all(button.copy_text is None for row in keyboard.inline_keyboard for button in row)

    def test_back_button_still_last(self) -> None:
        """Кнопка «К списку» остаётся последней строкой."""
        chat = "https://chat.whatsapp.com/AbCdEf123"
        keyboard = dossier_keyboard(
            whatsapp=_whatsapp(chat, chat_url=chat, text="Текст"),
            back_callback="entity_page:group:0",
        )

        assert keyboard is not None
        assert keyboard.inline_keyboard[-1][0].callback_data == "entity_page:group:0"

    def test_back_button_added_for_list_navigation(self) -> None:
        """Кнопка «К списку» появляется, если досье открыто из списка."""
        keyboard = dossier_keyboard(back_callback="entity_page:student:0")
        assert keyboard is not None
        assert keyboard.inline_keyboard[-1][0].callback_data == "entity_page:student:0"

    def test_without_back_callback_no_back_button(self) -> None:
        keyboard = dossier_keyboard(admin_url="https://planzy.example.com/admin/t")
        assert keyboard is not None
        assert all(
            button.callback_data is None for row in keyboard.inline_keyboard for button in row
        )


class TestAdminKeyboard:
    """Проверки клавиатуры со ссылкой на веб-кабинет."""

    def test_public_admin_url_becomes_button(self) -> None:
        keyboard = admin_keyboard("https://planzy.example.com/admin/token")
        assert keyboard is not None
        assert keyboard.inline_keyboard[0][0].url == "https://planzy.example.com/admin/token"

    def test_local_admin_url_is_skipped(self) -> None:
        """Локальный адрес Telegram в кнопке не примет — клавиатуры нет."""
        assert admin_keyboard("http://localhost:8010/admin/token") is None
        assert admin_keyboard(None) is None


class TestEntityListKeyboard:
    """Проверки клавиатуры со списком учеников и групп."""

    def _entity(self, name: str, entity_id: int, *, days: str | None = None) -> Entity:
        """Создаёт сущность с заданным именем и расписанием."""
        return Entity(
            id=entity_id,
            tenant_id=1,
            name=name,
            entity_type="student",
            subject="Шахматы",
            schedule_days=days,
        )

    def test_empty_list_returns_none(self) -> None:
        assert entity_list_keyboard([], entity_type="student") is None

    def test_button_per_entity_with_callback(self) -> None:
        keyboard = entity_list_keyboard(
            [self._entity("Миша", 7), self._entity("Аня", 9)],
            entity_type="student",
        )
        assert keyboard is not None
        assert [row[0].text for row in keyboard.inline_keyboard] == ["Миша", "Аня"]
        assert [row[0].callback_data for row in keyboard.inline_keyboard] == [
            "entity:student:7:0",
            "entity:student:9:0",
        ]

    def test_button_label_contains_schedule(self) -> None:
        keyboard = entity_list_keyboard(
            [self._entity("Миша", 7, days="mon,wed")],
            entity_type="student",
        )
        assert keyboard is not None
        assert keyboard.inline_keyboard[0][0].text == "Миша · Пн, Ср"

    def test_long_list_is_paginated(self) -> None:
        entities = [
            self._entity(f"Ученик {index}", index) for index in range(1, ENTITIES_PER_PAGE + 3)
        ]
        keyboard = entity_list_keyboard(entities, entity_type="student")
        assert keyboard is not None
        # Первая страница: кнопки сущностей + строка навигации.
        assert len(keyboard.inline_keyboard) == ENTITIES_PER_PAGE + 1
        navigation = keyboard.inline_keyboard[-1]
        # На первой странице «назад» некуда: только счётчик и «вперёд».
        assert [button.text for button in navigation] == ["1/2", "▶️"]
        assert navigation[1].callback_data == "entity_page:student:1"

    def test_second_page_marks_selected_page(self) -> None:
        entities = [
            self._entity(f"Ученик {index}", index) for index in range(1, ENTITIES_PER_PAGE + 3)
        ]
        keyboard = entity_list_keyboard(entities, entity_type="student", page=1)
        assert keyboard is not None
        navigation = keyboard.inline_keyboard[-1]
        # На последней странице «вперёд» уже нет.
        assert [button.text for button in navigation] == ["◀️", "2/2"]
        assert navigation[0].callback_data == "entity_page:student:0"

    def test_out_of_range_page_is_clamped(self) -> None:
        entities = [
            self._entity(f"Ученик {index}", index) for index in range(1, ENTITIES_PER_PAGE + 3)
        ]
        keyboard = entity_list_keyboard(entities, entity_type="student", page=99)
        assert keyboard is not None
        assert keyboard.inline_keyboard[-1][1].text == "2/2"
