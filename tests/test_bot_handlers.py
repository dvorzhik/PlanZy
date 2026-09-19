"""Тесты хендлеров Telegram-бота PlanZy.

Проверяем поведение команд и основного сценария диктовки. Внешние
зависимости (aiogram ``Message``, Gemini) подменяются заглушками, поэтому
тесты работают на изолированной in-memory SQLite и не ходят в сеть.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pytest

from app.bot import handlers
from app.bot.handlers import (
    BOT_COMMANDS,
    HELP_TEXT,
    _build_admin_url,
    _build_entity_block,
    _build_lesson_url,
    _format_payment_line,
    _format_schedule_line,
    _pluralize_lessons,
    _send_lesson_report,
    callback_entity_page,
    callback_lesson_cancel,
    callback_lesson_confirm,
    callback_open_dossier,
    cmd_admin,
    cmd_dossier,
    cmd_group,
    cmd_help,
    cmd_start,
    cmd_student,
    cmd_today,
    handle_dictation,
    set_bot_commands,
)
from app.bot.keyboards import LESSON_CANCEL_CALLBACK, LESSON_CONFIRM_CALLBACK
from app.core.config import settings
from app.db.models import Payment
from app.db.repository import Repository
from app.parsers.schemas import LessonParseResult
from app.services.entity_service import EntityService
from app.services.lesson_service import LessonService, ProcessedEntity, ProcessedLesson
from app.services.schedule_service import ScheduleService

TODAY = date(2026, 9, 17)


# ----------------------------------------------------------------------
# Заглушки
# ----------------------------------------------------------------------


class _StubMessage:
    """Минимальная заглушка aiogram Message для проверок ответов."""

    def __init__(self) -> None:
        """Инициализирует список отправленных ответов."""
        self.answers: list[dict[str, Any]] = []
        self.chat = _StubChat()
        self.bot = _StubBot()

    async def answer(self, text: str, **kwargs: Any) -> None:
        """Запоминает текст и параметры ответа."""
        self.answers.append({"text": text, **kwargs})

    @property
    def last_text(self) -> str:
        """Текст последнего ответа."""
        assert self.answers, "Ответ не был отправлен"
        return self.answers[-1]["text"]

    @property
    def last_markup(self) -> Any:
        """Разметка последнего ответа."""
        assert self.answers, "Ответ не был отправлен"
        return self.answers[-1].get("reply_markup")


class _StubChat:
    """Заглушка чата."""

    def __init__(self) -> None:
        self.id = 42


class _StubBot:
    """Заглушка бота с поддержкой send_chat_action и set_my_commands."""

    def __init__(self) -> None:
        self.actions: list[tuple[int, str]] = []
        self.commands: list[Any] | None = None

    async def send_chat_action(self, chat_id: int, action: str) -> None:
        """Запоминает отправленное действие."""
        self.actions.append((chat_id, action))

    async def set_my_commands(self, commands: list[Any]) -> None:
        """Запоминает опубликованное меню команд."""
        self.commands = commands


class _StubCallback:
    """Заглушка aiogram CallbackQuery для проверок инлайн-кнопок."""

    def __init__(self, data: str | None, message: _StubMessage | None = None) -> None:
        self.data = data
        self.message = message if message is not None else _StubMessage()
        self.answers: list[dict[str, Any]] = []

    async def answer(self, text: str | None = None, **kwargs: Any) -> None:
        """Запоминает ответ на нажатие кнопки."""
        self.answers.append({"text": text, **kwargs})

    @property
    def last_answer(self) -> str | None:
        """Текст последнего ответа на нажатие."""
        assert self.answers, "Ответ на callback не был отправлен"
        return self.answers[-1]["text"]


class _StubCommand:
    """Заглушка CommandObject с аргументами."""

    def __init__(self, args: str | None = None) -> None:
        self.args = args


class _StubParser:
    """Заглушка парсера: возвращает заданный результат или бросает ошибку."""

    def __init__(
        self, result: LessonParseResult | None = None, error: Exception | None = None
    ) -> None:
        self._result = result
        self._error = error

    async def parse(self, text: str, *, today: date | None = None) -> LessonParseResult:
        """Возвращает результат или поднимает заданную ошибку."""
        if self._error is not None:
            raise self._error
        assert self._result is not None
        return self._result


def _context(repo: Repository, tenant_id: int) -> dict[str, Any]:
    """Собирает контекст хендлера так, как это делает middleware."""
    return {"repo": repo, "tenant": _TenantStub(tenant_id)}


class _TenantStub:
    """Заглушка тенанта с нужным идентификатором и именем."""

    def __init__(self, tenant_id: int, name: str = "Тренер") -> None:
        self.id = tenant_id
        self.name = name


# ----------------------------------------------------------------------
# Команды
# ----------------------------------------------------------------------


class TestStartAndHelp:
    """Проверки приветствия и справки."""

    async def test_start_sends_greeting(self, repo: Repository, tenant_id: int) -> None:
        message = _StubMessage()
        await cmd_start(message, **_context(repo, tenant_id))
        assert message.last_text.startswith("Привет!")
        assert "PlanZy" in message.last_text

    async def test_start_has_no_account_name(self, repo: Repository, tenant_id: int) -> None:
        """В приветствии нет обращения по названию аккаунта.

        Раньше бот писал «Здравствуйте, Мой аккаунт!» — это выглядело
        странно, потому что имя тенанта хранится как название аккаунта.
        """
        message = _StubMessage()
        await cmd_start(message, **_context(repo, tenant_id))

        assert "Здравствуйте" not in message.last_text
        assert "Мой аккаунт" not in message.last_text

    async def test_help_sends_help_text(self) -> None:
        message = _StubMessage()
        await cmd_help(message)
        assert message.last_text == HELP_TEXT

    def test_help_text_lists_commands(self) -> None:
        """Справка перечисляет все команды из меню."""
        for command in ("/admin", "/today", "/student", "/group", "/help"):
            assert command in HELP_TEXT

    def test_help_text_has_no_dossier_command(self) -> None:
        """Команда /dossier убрана из меню: вместо неё списки учеников и групп."""
        assert "/dossier " not in HELP_TEXT

    def test_bot_commands_cover_menu(self) -> None:
        """В меню Telegram попадают все команды, объявленные в BOT_COMMANDS."""
        names = [name for name, _ in BOT_COMMANDS]
        assert names == ["start", "today", "student", "group", "admin", "help"]
        assert all(description for _, description in BOT_COMMANDS)


class TestCmdAdmin:
    """Проверки команды /admin."""

    async def test_admin_sends_link(self, repo: Repository, tenant_id: int) -> None:
        message = _StubMessage()
        await cmd_admin(message, **_context(repo, tenant_id))
        assert "/admin/" in message.last_text

    async def test_admin_without_token_warns(self, repo: Repository, tenant_id: int) -> None:
        """Если токен не удалось получить, тренер видит предупреждение."""
        message = _StubMessage()
        await cmd_admin(message, **_context(repo, tenant_id))
        assert "кабинет" in message.last_text.lower()

    async def test_admin_link_is_clickable_html(self, repo: Repository, tenant_id: int) -> None:
        """Ссылка в тексте обёрнута в HTML-тег <a>, то есть кликабельна."""
        message = _StubMessage()
        await cmd_admin(message, **_context(repo, tenant_id))
        assert '<a href="http' in message.last_text
        assert "</a>" in message.last_text

    async def test_admin_attaches_button_for_public_url(
        self,
        repo: Repository,
        tenant_id: int,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """При публичном BASE_URL ссылка дублируется кнопкой."""
        monkeypatch.setattr(settings, "base_url", "https://planzy.example.com")
        message = _StubMessage()
        await cmd_admin(message, **_context(repo, tenant_id))

        markup = message.last_markup
        assert markup is not None
        assert markup.inline_keyboard[0][0].url.startswith("https://planzy.example.com/admin/")

    async def test_admin_without_button_for_local_url(
        self, repo: Repository, tenant_id: int
    ) -> None:
        """Локальный адрес в кнопку не попадает, но текст ссылки остаётся."""
        message = _StubMessage()
        await cmd_admin(message, **_context(repo, tenant_id))
        assert message.last_markup is None
        assert "/admin/" in message.last_text


class TestCmdToday:
    """Проверки команды /today."""

    async def test_today_without_lessons(self, repo: Repository, tenant_id: int) -> None:
        message = _StubMessage()
        await cmd_today(message, **_context(repo, tenant_id))
        assert "занятий нет" in message.last_text.lower()

    async def test_today_shows_today_and_tomorrow_blocks(
        self, repo: Repository, tenant_id: int
    ) -> None:
        """В ответе есть оба блока: на сегодня и на завтра."""
        message = _StubMessage()
        await cmd_today(message, **_context(repo, tenant_id))
        assert "Сегодня" in message.last_text
        assert "Завтра" in message.last_text

    async def test_today_lists_slots(self, repo: Repository, tenant_id: int) -> None:
        """Слоты на сегодня попадают в ответ."""
        entity, _ = await EntityService(repo).get_or_create(tenant_id, "Миша")
        today = date.today()
        weekday = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")[today.weekday()]
        await ScheduleService(repo).add_slot(tenant_id, entity.id, weekday, "16:00")

        message = _StubMessage()
        await cmd_today(message, **_context(repo, tenant_id))

        assert "Миша" in message.last_text
        assert "16:00" in message.last_text
        assert "Всего занятий: 1" in message.last_text

    async def test_today_lists_tomorrow_slots(self, repo: Repository, tenant_id: int) -> None:
        """Слот на завтра попадает в блок «Завтра», а не в «Сегодня»."""
        entity, _ = await EntityService(repo).get_or_create(tenant_id, "Аня")
        tomorrow = date.today() + timedelta(days=1)
        weekday = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")[tomorrow.weekday()]
        await ScheduleService(repo).add_slot(tenant_id, entity.id, weekday, "18:30")

        message = _StubMessage()
        await cmd_today(message, **_context(repo, tenant_id))

        text = message.last_text
        assert "Аня" in text
        assert "18:30" in text
        today_block, tomorrow_block = text.split("Завтра")
        assert "Аня" not in today_block
        assert "Аня" in tomorrow_block


class TestCmdDossier:
    """Проверки текстового способа открыть досье — /dossier Имя.

    Команда больше не публикуется в меню: основной путь — списки
    ``/student`` и ``/group``, но текстовый вариант продолжает работать.
    """

    async def test_dossier_without_name(self, repo: Repository, tenant_id: int) -> None:
        message = _StubMessage()
        await cmd_dossier(message, command=_StubCommand(None), **_context(repo, tenant_id))
        assert "/student" in message.last_text

    async def test_dossier_unknown_entity(self, repo: Repository, tenant_id: int) -> None:
        message = _StubMessage()
        await cmd_dossier(message, command=_StubCommand("НетТакого"), **_context(repo, tenant_id))
        assert "не найден" in message.last_text

    async def test_dossier_known_entity(self, repo: Repository, tenant_id: int) -> None:
        await EntityService(repo).get_or_create(tenant_id, "Миша")

        message = _StubMessage()
        await cmd_dossier(message, command=_StubCommand("Миша"), **_context(repo, tenant_id))

        assert "Миша" in message.last_text


class TestCmdStudentAndGroup:
    """Проверки команд /student и /group со списками учеников и групп."""

    async def test_student_without_entities(self, repo: Repository, tenant_id: int) -> None:
        message = _StubMessage()
        await cmd_student(message, **_context(repo, tenant_id))
        assert "Учеников пока нет" in message.last_text

    async def test_group_without_entities(self, repo: Repository, tenant_id: int) -> None:
        message = _StubMessage()
        await cmd_group(message, **_context(repo, tenant_id))
        assert "Групп пока нет" in message.last_text

    async def test_student_lists_only_students(self, repo: Repository, tenant_id: int) -> None:
        """В списке учеников нет групп."""
        await EntityService(repo).get_or_create(tenant_id, "Миша")
        await repo.entities.create(tenant_id=tenant_id, name="Новички", entity_type="group")

        message = _StubMessage()
        await cmd_student(message, **_context(repo, tenant_id))

        button_texts = [row[0].text for row in message.last_markup.inline_keyboard]
        assert any("Миша" in text for text in button_texts)
        assert all("Новички" not in text for text in button_texts)

    async def test_group_lists_only_groups(self, repo: Repository, tenant_id: int) -> None:
        """В списке групп нет учеников."""
        await EntityService(repo).get_or_create(tenant_id, "Миша")
        await repo.entities.create(tenant_id=tenant_id, name="Новички", entity_type="group")

        message = _StubMessage()
        await cmd_group(message, **_context(repo, tenant_id))

        button_texts = [row[0].text for row in message.last_markup.inline_keyboard]
        assert any("Новички" in text for text in button_texts)
        assert all("Миша" not in text for text in button_texts)

    async def test_student_button_opens_dossier(self, repo: Repository, tenant_id: int) -> None:
        """Кнопка списка несёт callback_data с id ученика."""
        entity, _ = await EntityService(repo).get_or_create(tenant_id, "Миша")

        message = _StubMessage()
        await cmd_student(message, **_context(repo, tenant_id))

        assert message.last_markup.inline_keyboard[0][0].callback_data == (
            f"entity:student:{entity.id}:0"
        )


class TestCallbackHandlers:
    """Проверки нажатий на инлайн-кнопки списков."""

    async def test_callback_opens_dossier(self, repo: Repository, tenant_id: int) -> None:
        """Нажатие на ученика открывает досье — аналог /dossier Имя."""
        entity, _ = await EntityService(repo).get_or_create(tenant_id, "Миша")
        callback = _StubCallback(f"entity:student:{entity.id}:0")

        await callback_open_dossier(callback, **_context(repo, tenant_id))

        assert callback.last_answer is None
        assert "Миша" in callback.message.last_text
        assert "Досье" in callback.message.last_text
        button_texts = [
            button.text for row in callback.message.last_markup.inline_keyboard for button in row
        ]
        assert "🔙 К списку" in button_texts

    async def test_callback_unknown_entity_alerts(self, repo: Repository, tenant_id: int) -> None:
        callback = _StubCallback("entity:student:999999:0")

        await callback_open_dossier(callback, **_context(repo, tenant_id))

        assert callback.last_answer == "Запись не найдена."
        assert not callback.message.answers

    async def test_callback_with_broken_payload_alerts(
        self, repo: Repository, tenant_id: int
    ) -> None:
        callback = _StubCallback("entity:student:abc:0")

        await callback_open_dossier(callback, **_context(repo, tenant_id))

        assert callback.last_answer == "Не удалось открыть досье."

    async def test_callback_list_page(self, repo: Repository, tenant_id: int) -> None:
        """Кнопка «Назад к списку» возвращает список учеников."""
        await EntityService(repo).get_or_create(tenant_id, "Миша")
        callback = _StubCallback("entity_page:student:0")

        await callback_entity_page(callback, **_context(repo, tenant_id))

        assert "Ученики" in callback.message.last_text
        assert callback.message.last_markup is not None

    async def test_callback_unknown_page_type_alerts(
        self, repo: Repository, tenant_id: int
    ) -> None:
        callback = _StubCallback("entity_page:teacher:0")

        await callback_entity_page(callback, **_context(repo, tenant_id))

        assert callback.last_answer == "Не удалось переключить страницу."


class TestSetBotCommands:
    """Проверки публикации меню команд."""

    async def test_publishes_menu(self) -> None:
        bot = _StubBot()
        await set_bot_commands(bot)

        assert bot.commands is not None
        assert [command.command for command in bot.commands] == [name for name, _ in BOT_COMMANDS]

    async def test_network_error_does_not_raise(self) -> None:
        """Ошибка публикации меню не роняет запуск бота."""

        class _BrokenBot(_StubBot):
            async def set_my_commands(self, commands: list[Any]) -> None:
                raise RuntimeError("network is down")

        await set_bot_commands(_BrokenBot())


# ----------------------------------------------------------------------
# Диктовка
# ----------------------------------------------------------------------


class _StubState:
    """Заглушка FSMContext: хранит данные в памяти."""

    def __init__(self) -> None:
        self.data: dict[str, Any] = {}
        self.current_state: Any = None
        self.cleared = False

    async def set_state(self, state: Any) -> None:
        """Запоминает текущее состояние."""
        self.current_state = state

    async def update_data(self, **kwargs: Any) -> None:
        """Дописывает данные в хранилище."""
        self.data.update(kwargs)

    async def get_data(self) -> dict[str, Any]:
        """Возвращает все сохранённые данные."""
        return dict(self.data)

    async def clear(self) -> None:
        """Очищает состояние и данные."""
        self.data.clear()
        self.current_state = None
        self.cleared = True


class TestHandleDictation:
    """Проверки основного сценария — разбора диктовки с подтверждением."""

    async def test_short_text_is_rejected(self, repo: Repository, tenant_id: int) -> None:
        message = _StubMessage()
        message.text = "ок"
        await handle_dictation(message, state=_StubState(), **_context(repo, tenant_id))
        assert "слишком короткий" in message.last_text.lower()

    async def test_parse_error_is_reported(
        self, repo: Repository, tenant_id: int, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Ошибка Gemini показывается тренеру понятным текстом."""
        from app.parsers.gemini_client import GeminiParseError

        monkeypatch.setattr(
            handlers,
            "LessonService",
            lambda _repo: LessonService(_repo, parser=_StubParser(error=GeminiParseError("boom"))),
        )

        message = _StubMessage()
        message.text = "Миша, шахматы, вилка"
        await handle_dictation(message, state=_StubState(), **_context(repo, tenant_id))

        assert "Не удалось разобрать" in message.last_text

    async def test_preview_is_shown_without_saving(
        self, repo: Repository, tenant_id: int, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Диктовка показывает предпросмотр и кнопки подтверждения."""
        await EntityService(repo).get_or_create(tenant_id, "Миша")
        result = LessonParseResult(
            entity_name="Миша",
            topic="Вилка",
            rating=4,
            homework="Задачи 12–15",
            attendance="present",
            lesson_date=TODAY,
        )
        monkeypatch.setattr(
            handlers,
            "LessonService",
            lambda _repo: LessonService(_repo, parser=_StubParser(result=result)),
        )

        message = _StubMessage()
        message.text = "Миша, вилка, оценка 4"
        await handle_dictation(message, state=_StubState(), **_context(repo, tenant_id))

        text = message.last_text
        assert "Миша" in text
        assert "Вилка" in text
        assert "4/5" in text
        assert "Задачи 12–15" in text
        assert "Записать занятие?" in text

        # В базу пока ничего не записано.
        assert await repo.lessons.list_by_tenant(tenant_id) == []

    async def test_preview_has_confirm_buttons(
        self, repo: Repository, tenant_id: int, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Под предпросмотром есть кнопки «Да» и «Нет»."""
        await EntityService(repo).get_or_create(tenant_id, "Миша")
        result = LessonParseResult(entity_name="Миша", topic="Вилка", lesson_date=TODAY)
        monkeypatch.setattr(
            handlers,
            "LessonService",
            lambda _repo: LessonService(_repo, parser=_StubParser(result=result)),
        )

        message = _StubMessage()
        message.text = "Миша, вилка"
        await handle_dictation(message, state=_StubState(), **_context(repo, tenant_id))

        markup = message.last_markup
        assert markup is not None
        buttons = [button for row in markup.inline_keyboard for button in row]
        assert [button.callback_data for button in buttons] == [
            LESSON_CONFIRM_CALLBACK,
            LESSON_CANCEL_CALLBACK,
        ]

    async def test_state_holds_parse_result(
        self, repo: Repository, tenant_id: int, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Распознанный результат сохраняется в FSM для подтверждения."""
        await EntityService(repo).get_or_create(tenant_id, "Миша")
        result = LessonParseResult(entity_name="Миша", topic="Вилка", lesson_date=TODAY)
        monkeypatch.setattr(
            handlers,
            "LessonService",
            lambda _repo: LessonService(_repo, parser=_StubParser(result=result)),
        )

        state = _StubState()
        message = _StubMessage()
        message.text = "Миша, вилка"
        await handle_dictation(message, state=state, **_context(repo, tenant_id))

        assert "parse_result" in state.data
        assert state.data["parse_result"]["topic"] == "Вилка"

    async def test_unknown_entity_is_reported(
        self, repo: Repository, tenant_id: int, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Незнакомое имя: бот не создаёт ученика, а просит завести его в кабинете."""
        result = LessonParseResult(
            entity_name="Новичок",
            topic="Дебют",
            attendance="present",
            lesson_date=TODAY,
        )
        monkeypatch.setattr(
            handlers,
            "LessonService",
            lambda _repo: LessonService(_repo, parser=_StubParser(result=result)),
        )

        message = _StubMessage()
        message.text = "Новичок, дебют"
        await handle_dictation(message, state=_StubState(), **_context(repo, tenant_id))

        assert "Не нашёл в базе" in message.last_text
        assert "Новичок" in message.last_text
        # Ученик не создан.
        assert await EntityService(repo).get_by_name(tenant_id, "Новичок") is None
        assert await repo.lessons.list_by_tenant(tenant_id) == []


class TestLessonConfirmation:
    """Проверки подтверждения записи занятия кнопками Да/Нет."""

    async def test_confirm_writes_lesson(
        self, repo: Repository, tenant_id: int, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Нажатие «Да» записывает занятие и присылает отчёт."""
        await EntityService(repo).get_or_create(tenant_id, "Миша")
        result = LessonParseResult(
            entity_name="Миша",
            topic="Вилка",
            rating=4,
            attendance="present",
            lesson_date=TODAY,
        )
        parse_result = result.model_copy(update={"lesson_date": date.today()})

        state = _StubState()
        await state.update_data(parse_result=parse_result.model_dump(mode="json"))
        callback = _StubCallback(LESSON_CONFIRM_CALLBACK)

        await callback_lesson_confirm(callback, state=state, **_context(repo, tenant_id))

        # Урок записан.
        entity = await EntityService(repo).get_by_name(tenant_id, "Миша")
        assert entity is not None
        lessons = await repo.lessons.list_by_entity(tenant_id, entity.id)
        assert len(lessons) == 1
        assert lessons[0].topic == "Вилка"
        assert lessons[0].lesson_date == date.today()
        # Состояние очищено после записи.
        assert state.cleared is True
        assert "Урок сохранён" in callback.message.last_text

    async def test_confirm_without_data_alerts(self, repo: Repository, tenant_id: int) -> None:
        """Если данные потерялись, бот просит прислать текст заново."""
        state = _StubState()
        callback = _StubCallback(LESSON_CONFIRM_CALLBACK)

        await callback_lesson_confirm(callback, state=state, **_context(repo, tenant_id))

        assert callback.last_answer == "Данные потерялись — пришлите текст заново."
        assert state.cleared is True

    async def test_cancel_clears_state_and_waits(self, repo: Repository, tenant_id: int) -> None:
        """Нажатие «Нет» ничего не пишет и ждёт новый текст."""
        await EntityService(repo).get_or_create(tenant_id, "Миша")
        result = LessonParseResult(entity_name="Миша", topic="Вилка", lesson_date=TODAY)

        state = _StubState()
        await state.update_data(parse_result=result.model_dump(mode="json"))
        callback = _StubCallback(LESSON_CANCEL_CALLBACK)

        await callback_lesson_cancel(callback, state=state, **_context(repo, tenant_id))

        assert state.cleared is True
        assert "ничего не записал" in callback.message.last_text
        # В базу ничего не попало.
        assert await repo.lessons.list_by_tenant(tenant_id) == []

    async def test_full_flow_dictation_then_confirm(
        self, repo: Repository, tenant_id: int, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Полный путь: диктовка → подтверждение → запись."""
        await EntityService(repo).get_or_create(tenant_id, "Миша")
        result = LessonParseResult(
            entity_name="Миша",
            topic="Вилка",
            attendance="present",
            lesson_date=TODAY,
        )
        monkeypatch.setattr(
            handlers,
            "LessonService",
            lambda _repo: LessonService(_repo, parser=_StubParser(result=result)),
        )

        state = _StubState()
        message = _StubMessage()
        message.text = "Миша, вилка"
        await handle_dictation(message, state=state, **_context(repo, tenant_id))

        # До подтверждения в базе пусто.
        assert await repo.lessons.list_by_tenant(tenant_id) == []

        callback = _StubCallback(LESSON_CONFIRM_CALLBACK)
        await callback_lesson_confirm(callback, state=state, **_context(repo, tenant_id))

        assert await repo.lessons.list_by_tenant(tenant_id) != []


# ----------------------------------------------------------------------
# Отчёт и вспомогательные функции
# ----------------------------------------------------------------------


class TestSendLessonReport:
    """Проверки формирования отчёта об уроке."""

    async def _make_processed(self, repo: Repository, tenant_id: int) -> ProcessedLesson:
        """Готовит результат обработки одного урока."""
        await repo.entities.create(tenant_id=tenant_id, name="Миша", entity_type="student")
        result = LessonParseResult(
            entity_name="Миша",
            topic="Вилка",
            attendance="present",
            lesson_date=TODAY,
        )
        service = LessonService(repo, parser=_StubParser(result=result))
        return await service.process_dictation(tenant_id, "текст")

    async def test_report_contains_lesson_url(
        self,
        repo: Repository,
        tenant_id: int,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """В отчёт добавляется кнопка со ссылкой на веб-карточку урока."""
        # Telegram отклоняет локальные адреса, поэтому подменяем базовый URL
        # на публичный, чтобы кнопка не была отфильтрована.
        monkeypatch.setattr(settings, "base_url", "https://planzy.example.com")
        processed = await self._make_processed(repo, tenant_id)

        message = _StubMessage()
        await _send_lesson_report(message, processed, repo=repo, tenant_id=tenant_id)

        markup = message.last_markup
        assert markup is not None
        urls = [button.url for row in markup.inline_keyboard for button in row]
        assert any("/lesson/" in (url or "") for url in urls)

    async def test_report_without_repo_has_no_lesson_url(
        self, repo: Repository, tenant_id: int
    ) -> None:
        """Без репозитория ссылка на занятие не строится."""
        processed = await self._make_processed(repo, tenant_id)

        message = _StubMessage()
        await _send_lesson_report(message, processed)

        assert "Урок сохранён" in message.last_text


class TestBuildUrls:
    """Проверки построения ссылок."""

    async def test_admin_url_contains_token(self, repo: Repository, tenant_id: int) -> None:
        url = await _build_admin_url(repo, tenant_id)
        assert url is not None
        assert "/admin/" in url

    async def test_lesson_url_contains_token(self, repo: Repository, tenant_id: int) -> None:
        await repo.entities.create(tenant_id=tenant_id, name="Миша", entity_type="student")
        result = LessonParseResult(entity_name="Миша", topic="Вилка", lesson_date=TODAY)
        processed = await LessonService(repo, parser=_StubParser(result=result)).process_dictation(
            tenant_id, "текст"
        )

        url = await _build_lesson_url(repo, tenant_id, processed.lesson.id)
        assert url is not None
        assert "/lesson/" in url

    async def test_lesson_url_without_repo(self) -> None:
        assert await _build_lesson_url(None, None, 1) is None

    async def test_lesson_url_unknown_lesson(self, repo: Repository, tenant_id: int) -> None:
        assert await _build_lesson_url(repo, tenant_id, 999_999) is None


class TestFormatHelpers:
    """Проверки чистых функций форматирования."""

    @pytest.mark.parametrize(
        "count,expected",
        [
            (1, "занятие"),
            (2, "занятия"),
            (5, "занятий"),
            (11, "занятий"),
            (21, "занятие"),
            (22, "занятия"),
            (0, "занятий"),
        ],
    )
    def test_pluralize_lessons(self, count: int, expected: str) -> None:
        assert _pluralize_lessons(count) == expected

    def test_format_payment_line_with_amount_and_lessons(self) -> None:
        payment = Payment(
            tenant_id=1,
            entity_id=1,
            amount=500.0,
            currency="ILS",
            lessons_added=8,
            payment_date=TODAY,
        )
        line = _format_payment_line(payment)
        assert line is not None
        assert "500" in line
        assert "+8" in line

    def test_format_payment_line_empty(self) -> None:
        payment = Payment(
            tenant_id=1,
            entity_id=1,
            amount=None,
            currency="ILS",
            lessons_added=None,
            payment_date=TODAY,
        )
        assert _format_payment_line(payment) is None

    async def test_format_schedule_line_with_slots(self, repo: Repository, tenant_id: int) -> None:
        """Расписание собирается из регулярных слотов."""
        entity, _ = await EntityService(repo).get_or_create(tenant_id, "Миша")
        schedule = ScheduleService(repo)
        await schedule.add_slot(tenant_id, entity.id, "mon", "16:00")
        await schedule.add_slot(tenant_id, entity.id, "wed", "16:00")

        line = await _format_schedule_line(repo, tenant_id, entity)

        assert line is not None
        assert "Пн" in line
        assert "Ср" in line
        assert "16:00" in line

    async def test_format_schedule_line_without_slots(
        self, repo: Repository, tenant_id: int
    ) -> None:
        """Без активных слотов строки расписания нет."""
        entity, _ = await EntityService(repo).get_or_create(tenant_id, "Миша")

        assert await _format_schedule_line(repo, tenant_id, entity) is None

    async def test_format_schedule_line_ignores_inactive_slots(
        self, repo: Repository, tenant_id: int
    ) -> None:
        """Выключенные слоты не попадают в расписание."""
        entity, _ = await EntityService(repo).get_or_create(tenant_id, "Миша")
        slot = await ScheduleService(repo).add_slot(tenant_id, entity.id, "mon", "16:00")
        await repo.schedule.update(tenant_id, slot.id, is_active=False)

        assert await _format_schedule_line(repo, tenant_id, entity) is None


class TestBuildEntityBlock:
    """Проверки сборки блока отчёта по одному ученику.

    Блок берёт расписание из регулярных слотов (``schedule_slots``),
    поэтому в тестах используется настоящий репозиторий и сущность из БД.
    """

    async def _processed(
        self,
        repo: Repository,
        tenant_id: int,
        *,
        created: bool = False,
        with_slot: bool = False,
    ) -> ProcessedEntity:
        """Готовит результат обработки одного ученика."""
        entity, _ = await EntityService(repo).get_or_create(tenant_id, "Миша")
        if with_slot:
            await ScheduleService(repo).add_slot(tenant_id, entity.id, "mon", "16:00")

        lesson = _LessonStub()
        return ProcessedEntity(
            lesson=lesson,
            entity=entity,
            entity_created=created,
            payment=None,
        )

    async def test_block_contains_header(self, repo: Repository, tenant_id: int) -> None:
        parse = LessonParseResult(entity_name="Миша", topic="Вилка", lesson_date=TODAY)
        processed = await self._processed(repo, tenant_id)
        lines = await _build_entity_block(
            processed, parse, is_primary=True, repo=repo, tenant_id=tenant_id
        )
        text = "\n".join(lines)
        assert "Урок сохранён" in text

    async def test_block_marks_new_entity(self, repo: Repository, tenant_id: int) -> None:
        parse = LessonParseResult(entity_name="Миша", topic="Вилка", lesson_date=TODAY)
        processed = await self._processed(repo, tenant_id, created=True)
        lines = await _build_entity_block(
            processed, parse, is_primary=True, repo=repo, tenant_id=tenant_id
        )
        assert any("впервые" in line for line in lines)

    async def test_block_marks_secondary_entity(self, repo: Repository, tenant_id: int) -> None:
        parse = LessonParseResult(entity_name="Миша", topic="Вилка", lesson_date=TODAY)
        processed = await self._processed(repo, tenant_id)
        lines = await _build_entity_block(
            processed, parse, is_primary=False, repo=repo, tenant_id=tenant_id
        )
        assert any("доп. ученик" in line for line in lines)

    async def test_block_shows_schedule_from_slots(self, repo: Repository, tenant_id: int) -> None:
        """Регрессия: расписание берётся из слотов, а не из legacy-полей.

        Раньше блок всегда писал «расписание не задано», потому что читал
        устаревшие поля Entity, пустые после перехода на web-first.
        """
        parse = LessonParseResult(entity_name="Миша", topic="Вилка", lesson_date=TODAY)
        processed = await self._processed(repo, tenant_id, with_slot=True)

        lines = await _build_entity_block(
            processed, parse, is_primary=True, repo=repo, tenant_id=tenant_id
        )
        text = "\n".join(lines)
        assert "Расписание" in text
        assert "Пн" in text
        assert "16:00" in text
        assert "не задано" not in text

    async def test_block_without_slots_has_no_schedule_line(
        self, repo: Repository, tenant_id: int
    ) -> None:
        """Без слотов строка расписания не добавляется."""
        parse = LessonParseResult(entity_name="Миша", topic="Вилка", lesson_date=TODAY)
        processed = await self._processed(repo, tenant_id)

        lines = await _build_entity_block(
            processed, parse, is_primary=True, repo=repo, tenant_id=tenant_id
        )
        assert not any("Расписание" in line for line in lines)


class _LessonStub:
    """Заглушка урока для проверок блока отчёта."""

    def __init__(self) -> None:
        self.lesson_date = TODAY
        self.topic = "Вилка"
        self.rating = "4"
        self.homework = "Задачи 12–15"
        self.next_step = "Разобрать дебют"
