"""Асинхронный клиент Gemini Flash для разбора надиктованного текста.

Клиент инкапсулирует работу с актуальным SDK ``google-genai``:
- формирует системный промпт на русском;
- передаёт ``response_schema`` (Pydantic-модель), чтобы Gemini вернул строгий JSON;
- выполняет ретраи с экспоненциальной задержкой при временных сбоях.

SDK ``google-genai`` предоставляет нативный async-клиент (``client.aio``),
поэтому блокирующих вызовов в event loop нет.
"""

from __future__ import annotations

import asyncio
from typing import Any

from google import genai
from google.genai import errors as genai_errors
from google.genai import types

from app.core.config import settings
from app.core.logging import get_logger
from app.parsers.schemas import LessonParseResult

logger = get_logger(__name__)

# Системный промпт: задаёт роль, правила извлечения и формат ответа.
SYSTEM_PROMPT = """\
Ты — ассистент тренера по шахматам. Тренер надиктовывает голосом короткий отчёт
о прошедшем занятии, а ты превращаешь этот текст в строгий JSON.

ПРАВИЛА ИЗВЛЕЧЕНИЯ:
1. entity_name — имя основного ученика или название группы. Обязательное поле.
2. extra_entities — список ОСТАЛЬНЫХ учеников, названных в диктовке.
   «Рон и Мия» → entity_name = "Рон", extra_entities = ["Мия"].
   «Миша, Аня и Группа Новички» → entity_name = "Миша",
   extra_entities = ["Аня", "Группа Новички"].
   Если ученик один — верни пустой список [].
   НЕ включай основного ученика в extra_entities повторно.
3. subject — предмет. По умолчанию «Шахматы», если не сказано иное.
4. topic — что разбирали на уроке (тема). Формулируй кратко, 3–10 слов.
5. homework — домашнее задание, если тренер его назвал.
6. internal_notes — приватные заметки тренера: поведение, проблемы, личные
   наблюдения. Всё, что помечено словами «заметка», «для себя», «приватно»,
   «не для родителей», идёт сюда.
7. rating — оценка РАБОТЫ УЧЕНИКА от 1 до 5. Если тренер говорит «отлично» — 5,
   «хорошо» — 4, «средне»/«нормально» — 3, «слабо» — 2, «плохо» — 1.
   ВНИМАНИЕ: числа, которые описывают НЕ оценку ученика, сюда не попадают.
   Например, «играли против бота 3 уровня», «решали задачи уровня 2»,
   «сделали 15 задач» — это НЕ оценка. Если явной оценки нет — верни null.
8. next_step — план на следующий урок.
9. attendance — одно из: present (был), absent (не пришёл), cancelled (отменили),
   rescheduled (перенесли). По умолчанию present.
10. attendance_note — КТО БЫЛ на занятии, если тренер это назвал. Пиши коротко,
    как сказано: «были все, кроме Леонида» → "все, кроме Леонида";
    «пришли Миша и Аня» → "Миша и Аня". Если состав не упоминался — null.
11. lesson_date — ВСЕГДА верни null: дата занятия проставляется автоматически
    текущим днём. Не вычисляй дату из текста.
12. schedule_days — дни недели, в которые ученик занимается, кодами:
    mon, tue, wed, thu, fri, sat, sun. «занимаемся по средам и субботам» →
    ["wed", "sat"]. Если расписание не названо — пустой список [].
13. schedule_time — время занятия в формате ЧЧ:ММ (24 часа).
    «среда 15:15» → "15:15", «в 5 вечера» → "17:00". Если не названо — null.
14. payment — блок про оплату:
    - has_payment = true, если упомянута оплата, перевод, «заплатили»,
      «оплатили», «пополнил», «оплачено».
    - amount — сумма (число), если названа. «120 шекелей» → 120.
    - lessons_added — сколько ЗАНЯТИЙ добавилось на баланс, если названо ЧИСЛО
      занятий. «оплатил 400 за 8 занятий» → 8. «+4 занятия» → 4.
      Если названа только сумма без числа занятий («оплата 120 шекелей»,
      «занятия оплачено») — верни null. НЕ выдумывай количество.

ВАЖНО:
- Не выдумывай данные. Если чего-то нет в тексте — верни null (или [] для списков).
- Числа возвращай числами, а не строками.
- Отвечай ТОЛЬКО JSON, без пояснений и markdown-обёрток.
"""


class GeminiParseError(RuntimeError):
    """Ошибка разбора текста через Gemini (после исчерпания ретраев)."""


class GeminiClient:
    """Обёртка над google-genai с async-интерфейсом и ретраями."""

    def __init__(
        self,
        api_key: str | None = None,
        model_name: str | None = None,
        max_retries: int = 3,
        base_delay: float = 1.0,
    ) -> None:
        """Инициализирует клиент.

        Args:
            api_key: Ключ Gemini API. По умолчанию берётся из настроек.
            model_name: Имя модели. По умолчанию берётся из настроек.
            max_retries: Максимальное число попыток при временных сбоях.
            base_delay: Базовая задержка между попытками (экспоненциально растёт).
        """
        self._api_key = api_key or settings.gemini_api_key
        self._model_name = model_name or settings.gemini_model
        self._max_retries = max_retries
        self._base_delay = base_delay
        self._client: genai.Client | None = None

    def _ensure_configured(self) -> genai.Client:
        """Ленивая инициализация SDK: создаём клиент только при первом вызове.

        Returns:
            Готовый клиент google-genai.

        Raises:
            GeminiParseError: Если API-ключ не задан.
        """
        if self._client is not None:
            return self._client
        if not self._api_key:
            raise GeminiParseError(
                "Не задан GEMINI_API_KEY. Укажите ключ в .env или передайте api_key."
            )
        self._client = genai.Client(api_key=self._api_key)
        return self._client

    def _build_config(self) -> types.GenerateContentConfig:
        """Собирает конфигурацию генерации со строгой схемой ответа."""
        return types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            response_mime_type="application/json",
            response_schema=LessonParseResult,
            temperature=0.1,
        )

    async def parse_lesson_text(self, text: str) -> LessonParseResult:
        """Разбирает надиктованный текст в ``LessonParseResult``.

        Args:
            text: Сырой текст диктовки от тренера.

        Returns:
            Валидированный результат разбора.

        Raises:
            GeminiParseError: Если текст пуст, ключ не задан или все попытки
                завершились ошибкой.
        """
        cleaned = text.strip()
        if not cleaned:
            raise GeminiParseError("Пустой текст: нечего разбирать.")

        client = self._ensure_configured()
        config = self._build_config()

        last_error: Exception | None = None
        for attempt in range(1, self._max_retries + 1):
            try:
                response = await client.aio.models.generate_content(
                    model=self._model_name,
                    contents=cleaned,
                    config=config,
                )
                return self._parse_response(response)
            except genai_errors.ClientError as exc:
                # 4xx: неверный ключ, недоступная модель, исчерпанная квота.
                # Ретраить бессмысленно — запрос не станет валидным.
                logger.error("Gemini: ошибка клиента (%s): %s", exc.code, exc.message)
                raise GeminiParseError(self._describe_client_error(exc)) from exc
            except GeminiParseError:
                # Ответ получен, но не соответствует схеме. Это не временный
                # сбой — повторный запрос с тем же текстом даст тот же результат.
                raise
            except (
                genai_errors.ServerError,
                genai_errors.APIError,
                TimeoutError,
                ConnectionError,
            ) as exc:
                last_error = exc
                delay = self._base_delay * (2 ** (attempt - 1))
                logger.warning(
                    "Gemini: временный сбой (попытка %d/%d): %s. Пауза %.1f с",
                    attempt,
                    self._max_retries,
                    exc,
                    delay,
                )
                if attempt < self._max_retries:
                    await asyncio.sleep(delay)
            except Exception as exc:  # noqa: BLE001 — нужен общий фолбэк для SDK
                last_error = exc
                delay = self._base_delay * (2 ** (attempt - 1))
                logger.warning(
                    "Gemini: непредвиденная ошибка (попытка %d/%d): %s",
                    attempt,
                    self._max_retries,
                    exc,
                )
                if attempt < self._max_retries:
                    await asyncio.sleep(delay)

        raise GeminiParseError(
            f"Не удалось разобрать текст после {self._max_retries} попыток: {last_error}"
        ) from last_error

    @staticmethod
    def _describe_client_error(exc: genai_errors.ClientError) -> str:
        """Формирует понятное пользователю сообщение об ошибке 4xx.

        Args:
            exc: Исключение клиента google-genai.

        Returns:
            Текст ошибки на русском языке.
        """
        code = getattr(exc, "code", None)
        message = getattr(exc, "message", "") or str(exc)
        if code == 429:
            return "Превышена квота Gemini API. Попробуйте позже."
        if code in (401, 403):
            return "Gemini отклонил ключ API. Проверьте GEMINI_API_KEY в .env."
        if code == 404:
            return (
                f"Модель {settings.gemini_model!r} недоступна. "
                "Укажите актуальную модель в GEMINI_MODEL."
            )
        return f"Ошибка запроса к Gemini ({code}): {message}"

    @staticmethod
    def _parse_response(response: Any) -> LessonParseResult:
        """Извлекает и валидирует JSON из ответа Gemini.

        Args:
            response: Объект ответа от SDK.

        Returns:
            Валидированный ``LessonParseResult``.

        Raises:
            GeminiParseError: Если ответ пуст или не соответствует схеме.
        """
        raw_text = getattr(response, "text", None)
        if not raw_text:
            raise GeminiParseError("Gemini вернул пустой ответ.")

        try:
            return LessonParseResult.model_validate_json(raw_text)
        except Exception as exc:  # noqa: BLE001 — pydantic ValidationError + JSONDecodeError
            logger.error("Gemini вернул невалидный JSON: %s", raw_text[:500])
            raise GeminiParseError(f"Gemini вернул невалидный JSON: {exc}") from exc


# Готовый к использованию singleton-клиент.
gemini_client = GeminiClient()
