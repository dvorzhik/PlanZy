"""Тесты парсера диктовки и Pydantic-схем.

Тесты не обращаются к реальному Gemini API: клиент подменяется моком,
который возвращает заранее подготовленный ``LessonParseResult``.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from pydantic import ValidationError

from app.parsers.gemini_client import GeminiClient, GeminiParseError
from app.parsers.lesson_parser import LessonParser
from app.parsers.schemas import LessonParseResult, PaymentInfo


class FakeResponse:
    """Минимальная заглушка ответа Gemini с атрибутом ``text``."""

    def __init__(self, text: str) -> None:
        self.text = text


class FakeModels:
    """Заглушка ``client.aio.models``: возвращает заданные ответы по очереди."""

    def __init__(self, responses: list[Any]) -> None:
        self._responses = responses
        self.calls: list[str] = []

    async def generate_content(self, *, model: str, contents: str, config: Any) -> Any:
        """Имитирует async-вызов генерации контента."""
        self.calls.append(contents)
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class FakeAio:
    """Заглушка ``client.aio``."""

    def __init__(self, models: FakeModels) -> None:
        self.models = models


class FakeClient:
    """Заглушка ``genai.Client`` с async-фасадом ``aio``."""

    def __init__(self, models: FakeModels) -> None:
        self.aio = FakeAio(models)


class FakeGeminiClient(GeminiClient):
    """Клиент Gemini с подменённым SDK-клиентом (без обращения к сети)."""

    def __init__(self, responses: list[Any]) -> None:
        super().__init__(api_key="test-key", model_name="test-model", base_delay=0.0)
        self._fake_models = FakeModels(responses)

    def _ensure_configured(self) -> Any:  # noqa: D102 — переопределяем для тестов
        return FakeClient(self._fake_models)


# ---------------------------------------------------------------------------
# Схемы
# ---------------------------------------------------------------------------


class TestLessonParseResultSchema:
    """Проверки валидации Pydantic-схемы."""

    def test_minimal_payload(self) -> None:
        """Минимальный набор: только имя сущности."""
        result = LessonParseResult.model_validate({"entity_name": "Миша"})
        assert result.entity_name == "Миша"
        assert result.subject == "Шахматы"
        assert result.attendance == "present"
        assert result.payment.has_payment is False
        assert result.rating is None

    def test_strips_whitespace(self) -> None:
        """Пробелы по краям обрезаются, пустые опциональные поля → None."""
        result = LessonParseResult.model_validate(
            {"entity_name": "  Аня  ", "topic": "   ", "homework": "  Задачи  "}
        )
        assert result.entity_name == "Аня"
        assert result.topic is None
        assert result.homework == "Задачи"

    def test_empty_entity_name_rejected(self) -> None:
        """Пустое имя сущности недопустимо."""
        with pytest.raises(ValidationError):
            LessonParseResult.model_validate({"entity_name": "   "})

    def test_rating_bounds(self) -> None:
        """Оценка должна быть в диапазоне 1..5."""
        with pytest.raises(ValidationError):
            LessonParseResult.model_validate({"entity_name": "Миша", "rating": 0})
        with pytest.raises(ValidationError):
            LessonParseResult.model_validate({"entity_name": "Миша", "rating": 6})

    def test_attendance_normalized(self) -> None:
        """Посещаемость приводится к нижнему регистру."""
        result = LessonParseResult.model_validate({"entity_name": "Миша", "attendance": "ABSENT"})
        assert result.attendance == "absent"

    def test_invalid_attendance_rejected(self) -> None:
        """Недопустимое значение посещаемости отклоняется."""
        with pytest.raises(ValidationError):
            LessonParseResult.model_validate({"entity_name": "Миша", "attendance": "опоздал"})

    def test_payment_negative_amount_rejected(self) -> None:
        """Отрицательная сумма платежа недопустима."""
        with pytest.raises(ValidationError):
            PaymentInfo(has_payment=True, amount=-100)

    def test_extra_entities_cleaned(self) -> None:
        """Лишние пробелы и дубликаты в extra_entities убираются."""
        result = LessonParseResult(
            entity_name="Рон",
            extra_entities=["  Мия  ", "", "рон", "Мия"],
        )

        assert result.extra_entities == ["Мия"]

    def test_all_entity_names_combines_primary_and_extra(self) -> None:
        """all_entity_names возвращает основного ученика и остальных."""
        result = LessonParseResult(entity_name="Рон", extra_entities=["Мия"])

        assert result.all_entity_names == ["Рон", "Мия"]

    def test_all_entity_names_single_student(self) -> None:
        """Без extra_entities список содержит только основного ученика."""
        result = LessonParseResult(entity_name="Рон")

        assert result.all_entity_names == ["Рон"]

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (["среда"], ["wed"]),
            (["среду"], ["wed"]),
            (["ср"], ["wed"]),
            (["понедельник", "пятница"], ["mon", "fri"]),
            (["wed", "WED"], ["wed"]),
            (["неизвестный"], []),
            ([], []),
        ],
    )
    def test_schedule_days_normalized(self, raw: list[str], expected: list[str]) -> None:
        """Названия дней недели нормализуются в коды mon..sun."""
        result = LessonParseResult(entity_name="Мия", schedule_days=raw)

        assert result.schedule_days == expected

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("15:15", "15:15"),
            ("9:05", "09:05"),
            ("9.05", "09:05"),
            ("9-05", "09:05"),
            ("1500", "15:00"),
            ("25:00", None),
            ("abc", None),
            (None, None),
        ],
    )
    def test_schedule_time_normalized(self, raw: str | None, expected: str | None) -> None:
        """Время занятия приводится к формату ЧЧ:ММ."""
        result = LessonParseResult(entity_name="Мия", schedule_time=raw)

        assert result.schedule_time == expected

    def test_has_schedule_flag(self) -> None:
        """has_schedule отражает наличие расписания в диктовке."""
        assert LessonParseResult(entity_name="Мия").has_schedule is False
        assert LessonParseResult(entity_name="Мия", schedule_days=["wed"]).has_schedule is True
        assert LessonParseResult(entity_name="Мия", schedule_time="15:15").has_schedule is True

    def test_is_group_heuristic(self) -> None:
        """Эвристика определения группы по имени."""
        assert LessonParseResult(entity_name="Группа Новички").is_group is True
        assert LessonParseResult(entity_name="Миша").is_group is False


# ---------------------------------------------------------------------------
# Клиент Gemini
# ---------------------------------------------------------------------------


class TestGeminiClient:
    """Проверки клиента: разбор ответа, ретраи, ошибки."""

    @pytest.mark.asyncio
    async def test_parses_valid_json(self) -> None:
        """Корректный JSON от Gemini превращается в модель."""
        payload = (
            '{"entity_name": "Миша", "topic": "Вилка", "rating": 4, '
            '"payment": {"has_payment": true, "amount": 400, "lessons_added": 4}}'
        )
        client = FakeGeminiClient([FakeResponse(payload)])
        result = await client.parse_lesson_text("Миша, вилка, четыре")
        assert result.entity_name == "Миша"
        assert result.topic == "Вилка"
        assert result.rating == 4
        assert result.payment.has_payment is True
        assert result.payment.amount == 400
        assert result.payment.lessons_added == 4

    @pytest.mark.asyncio
    async def test_empty_text_raises(self) -> None:
        """Пустой текст не отправляется в API."""
        client = FakeGeminiClient([])
        with pytest.raises(GeminiParseError, match="Пустой текст"):
            await client.parse_lesson_text("   ")

    @pytest.mark.asyncio
    async def test_invalid_json_raises(self) -> None:
        """Невалидный JSON приводит к GeminiParseError."""
        client = FakeGeminiClient([FakeResponse("не json")])
        with pytest.raises(GeminiParseError, match="невалидный JSON"):
            await client.parse_lesson_text("Миша")

    @pytest.mark.asyncio
    async def test_empty_response_raises(self) -> None:
        """Пустой ответ от Gemini приводит к GeminiParseError."""
        client = FakeGeminiClient([FakeResponse("")])
        with pytest.raises(GeminiParseError, match="пустой ответ"):
            await client.parse_lesson_text("Миша")

    @pytest.mark.asyncio
    async def test_retries_on_transient_error(self) -> None:
        """При временном сбое клиент повторяет запрос и в итоге succeeds."""
        payload = '{"entity_name": "Аня"}'
        client = FakeGeminiClient([RuntimeError("temporary"), FakeResponse(payload)])
        result = await client.parse_lesson_text("Аня")
        assert result.entity_name == "Аня"
        assert len(client._fake_models.calls) == 2

    @pytest.mark.asyncio
    async def test_gives_up_after_max_retries(self) -> None:
        """После исчерпания попыток выбрасывается GeminiParseError."""
        client = FakeGeminiClient(
            [RuntimeError("fail"), RuntimeError("fail"), RuntimeError("fail")]
        )
        with pytest.raises(GeminiParseError, match="после 3 попыток"):
            await client.parse_lesson_text("Миша")


# ---------------------------------------------------------------------------
# LessonParser (нормализация)
# ---------------------------------------------------------------------------


class TestLessonParser:
    """Проверки нормализации поверх клиента."""

    @pytest.mark.asyncio
    async def test_fills_today_when_date_missing(self) -> None:
        """Если дата не определена — подставляется переданное «сегодня»."""
        payload = '{"entity_name": "Миша", "lesson_date": null}'
        parser = LessonParser(client=FakeGeminiClient([FakeResponse(payload)]))
        result = await parser.parse("Миша", today=date(2026, 9, 17))
        assert result.lesson_date == date(2026, 9, 17)

    @pytest.mark.asyncio
    async def test_replaces_any_date_with_today(self) -> None:
        """Любая дата из текста перезаписывается сегодняшней.

        Дата занятия всегда сегодняшняя: тренер диктует отчёт в день урока,
        а разбор «в среду» легко ошибается на неделю.
        """
        payload = '{"entity_name": "Миша", "lesson_date": "2026-09-10"}'
        parser = LessonParser(client=FakeGeminiClient([FakeResponse(payload)]))
        result = await parser.parse("Миша", today=date(2026, 9, 17))
        assert result.lesson_date == date(2026, 9, 17)

    @pytest.mark.asyncio
    async def test_replaces_future_date(self) -> None:
        """Дата в будущем тоже заменяется на «сегодня»."""
        payload = '{"entity_name": "Миша", "lesson_date": "2027-01-01"}'
        parser = LessonParser(client=FakeGeminiClient([FakeResponse(payload)]))
        result = await parser.parse("Миша", today=date(2026, 9, 17))
        assert result.lesson_date == date(2026, 9, 17)

    def test_detect_entity_type(self) -> None:
        """Определение типа сущности по имени."""
        assert LessonParser.detect_entity_type("Группа Новички") == "group"
        assert LessonParser.detect_entity_type("Миша") == "student"
        assert LessonParser.detect_entity_type("Команда А") == "group"
