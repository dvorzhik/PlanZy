"""Высокоуровневый парсер диктовки: текст → структурированный результат.

Слой поверх :class:`GeminiClient`, который добавляет бизнес-нормализацию:
- подставляет текущую дату, если Gemini не смог определить её из текста;
- определяет тип сущности (ученик/группа) по имени;
- приводит предмет к значению по умолчанию.
"""

from __future__ import annotations

from datetime import date

from app.core.logging import get_logger
from app.parsers.gemini_client import GeminiClient, GeminiParseError, gemini_client
from app.parsers.schemas import LessonParseResult

logger = get_logger(__name__)

# Слова-маркеры, по которым определяем группу в имени сущности.
GROUP_MARKERS = ("групп", "group", "команда")


class LessonParser:
    """Разбирает надиктованный текст в ``LessonParseResult`` с нормализацией."""

    def __init__(self, client: GeminiClient | None = None) -> None:
        """Инициализирует парсер.

        Args:
            client: Клиент Gemini. По умолчанию используется общий singleton.
        """
        self._client = client or gemini_client

    async def parse(self, text: str, *, today: date | None = None) -> LessonParseResult:
        """Разбирает текст диктовки и нормализует результат.

        Args:
            text: Сырой текст от тренера.
            today: Дата, используемая как «сегодня» (для тестов). По умолчанию
                берётся текущая дата.

        Returns:
            Нормализованный результат разбора.

        Raises:
            GeminiParseError: Если разбор не удался.
        """
        reference_date = today or date.today()
        result = await self._client.parse_lesson_text(text)
        return self._normalize(result, reference_date)

    @staticmethod
    def _normalize(result: LessonParseResult, reference_date: date) -> LessonParseResult:
        """Применяет бизнес-правила нормализации к результату Gemini.

        Дата занятия всегда равна сегодняшней: тренер диктует отчёт в день
        урока, а разбор «в среду» легко ошибается на неделю. Остальные поля
        берутся из ответа модели как есть.

        Args:
            result: Сырой результат от Gemini.
            reference_date: Дата, считающаяся «сегодня».

        Returns:
            Нормализованный результат.
        """
        updates: dict[str, object] = {"lesson_date": reference_date}

        # Предмет по умолчанию.
        if not result.subject.strip():
            updates["subject"] = "Шахматы"

        if result.lesson_date is not None and result.lesson_date != reference_date:
            logger.info(
                "Дата из текста (%s) заменена на сегодня (%s)",
                result.lesson_date,
                reference_date,
            )

        return result.model_copy(update=updates)

    @staticmethod
    def detect_entity_type(entity_name: str) -> str:
        """Определяет тип сущности по её имени.

        Args:
            entity_name: Имя ученика или название группы.

        Returns:
            ``"group"``, если в имени есть маркер группы, иначе ``"student"``.
        """
        lowered = entity_name.lower()
        if any(marker in lowered for marker in GROUP_MARKERS):
            return "group"
        return "student"


# Готовый к использованию singleton-парсер.
lesson_parser = LessonParser()

__all__ = ["LessonParser", "GeminiParseError", "lesson_parser"]
