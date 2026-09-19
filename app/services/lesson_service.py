"""Сервис уроков: обработка диктовки от начала до записи в БД.

Это ключевой сценарий PlanZy: тренер надиктовывает текст → Gemini разбирает
его → сервис находит нужного ученика или группу, сохраняет урок и при
необходимости регистрирует платёж.

Новые сущности бот не создаёт: их заводит тренер в веб-кабинете. Если имя
не найдено, поднимается :class:`UnknownEntityError` с подсказкой.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from app.core.logging import get_logger
from app.db.models import Entity, Lesson, Payment, ScheduleSlot
from app.db.repository import Repository
from app.parsers.lesson_parser import LessonParser, lesson_parser
from app.parsers.schemas import LessonParseResult
from app.services.entity_service import EntityService
from app.services.payment_service import PaymentService
from app.services.schedule_service import ScheduleService

logger = get_logger(__name__)

# Человекочитаемые подписи посещаемости для примечания.
ATTENDANCE_LABELS: dict[str, str] = {
    "present": "был",
    "absent": "не пришёл",
    "cancelled": "отменили",
    "rescheduled": "перенесли",
}


def merge_internal_notes(parse_result: LessonParseResult) -> str | None:
    """Собирает приватные заметки, включая пометку о посещаемости.

    Тренер в диктовке называет, кто был на занятии («были все, кроме
    Леонида»). Бот сохраняет эту фразу в примечании — на публичную
    страницу родителя оно не попадает.

    Args:
        parse_result: Результат разбора диктовки.

    Returns:
        Объединённый текст заметок или None, если писать нечего.
    """
    parts: list[str] = []

    if parse_result.attendance != "present":
        label = ATTENDANCE_LABELS.get(parse_result.attendance, parse_result.attendance)
        parts.append(f"Посещаемость: {label}")

    if parse_result.attendance_note:
        parts.append(f"Кто был: {parse_result.attendance_note}")

    if parse_result.internal_notes:
        parts.append(parse_result.internal_notes)

    if not parts:
        return None
    return "\n".join(parts)


class UnknownEntityError(Exception):
    """Диктовка ссылается на ученика или группу, которых нет в базе.

    Бот не создаёт сущности сам: сначала тренер заводит ученика в кабинете
    (или тот появляется из другого источника). Исключение несёт список
    ненайденных имён и похожие варианты, чтобы бот показал понятную
    подсказку.

    Attributes:
        names: Имена, которых не оказалось в базе.
        suggestions: Похожие названия из базы — по одному списку на имя.
            Пустой список, если ничего похожего не нашлось.
    """

    def __init__(
        self,
        names: list[str],
        suggestions: dict[str, list[str]] | None = None,
    ) -> None:
        """Инициализирует исключение.

        Args:
            names: Ненайденные имена учеников или групп.
            suggestions: Подсказки ``{имя: [похожие названия]}``.
        """
        self.names = list(names)
        self.suggestions = suggestions or {}
        joined = ", ".join(self.names)
        super().__init__(f"Не найдены в базе: {joined}")


@dataclass(slots=True)
class PreparedEntity:
    """Распознанная по диктовке сущность без записи в БД.

    Attributes:
        name: Имя, как оно распознано в тексте.
        entity: Найденный ученик или группа.
    """

    entity: Entity

    @property
    def name(self) -> str:
        """Имя ученика или название группы."""
        return self.entity.name

    @property
    def is_group(self) -> bool:
        """True, если это группа."""
        return self.entity.is_group


@dataclass(slots=True)
class PreparedLesson:
    """Предпросмотр диктовки: что будет записано после подтверждения.

    Диктовка разбирается, но ничего не пишется в БД. Тренер видит
    распознанные поля и подтверждает запись кнопкой.

    Attributes:
        parse_result: Структурированный результат разбора.
        entities: Найденные ученики и группы в порядке упоминания.
    """

    parse_result: LessonParseResult
    entities: list[PreparedEntity]

    @property
    def primary(self) -> PreparedEntity:
        """Основная сущность — та, что названа первой."""
        return self.entities[0]

    @property
    def names(self) -> list[str]:
        """Имена всех названных сущностей."""
        return [item.name for item in self.entities]

    @property
    def is_multi(self) -> bool:
        """Признак того, что в диктовке названо несколько сущностей."""
        return len(self.entities) > 1


@dataclass(slots=True)
class ProcessedEntity:
    """Результат обработки одной сущности (ученика или группы) из диктовки.

    Attributes:
        lesson: Сохранённая запись об уроке.
        entity: Ученик или группа, к которой отнесён урок.
        entity_created: True, если сущность была создана впервые.
        payment: Зарегистрированный платёж, если он относился к этой сущности.
    """

    lesson: Lesson
    entity: Entity
    entity_created: bool
    payment: Payment | None


@dataclass(slots=True)
class ProcessedLesson:
    """Результат обработки одной диктовки.

    Диктовка может упоминать нескольких учеников («Рон и Мия»). В этом случае
    урок создаётся для каждого, а ``primary`` указывает на основного —
    того, чьё имя названо первым.

    Attributes:
        primary: Результат по основному ученику.
        others: Результаты по остальным названным ученикам.
        parse_result: Исходный структурированный результат разбора.
    """

    primary: ProcessedEntity
    others: list[ProcessedEntity]
    parse_result: LessonParseResult

    @property
    def lesson(self) -> Lesson:
        """Урок основного ученика (для обратной совместимости)."""
        return self.primary.lesson

    @property
    def entity(self) -> Entity:
        """Основной ученик или группа."""
        return self.primary.entity

    @property
    def entity_created(self) -> bool:
        """True, если основной ученик был создан впервые."""
        return self.primary.entity_created

    @property
    def payment(self) -> Payment | None:
        """Платёж основного ученика, если он был."""
        return self.primary.payment

    @property
    def all_entities(self) -> list[ProcessedEntity]:
        """Все обработанные сущности: основной ученик и остальные."""
        return [self.primary, *self.others]

    @property
    def is_multi(self) -> bool:
        """Признак того, что в диктовке было несколько учеников."""
        return bool(self.others)


class LessonService:
    """Бизнес-логика обработки уроков."""

    def __init__(
        self,
        repo: Repository,
        parser: LessonParser | None = None,
    ) -> None:
        """Инициализирует сервис.

        Args:
            repo: Фасад репозиториев.
            parser: Парсер диктовки. По умолчанию используется общий singleton.
        """
        self._repo = repo
        self._parser = parser or lesson_parser
        self._entities = EntityService(repo)
        self._payments = PaymentService(repo)
        self._schedule = ScheduleService(repo)

    async def prepare(
        self,
        tenant_id: int,
        text: str,
        *,
        today: date | None = None,
    ) -> PreparedLesson:
        """Разбирает диктовку без записи в БД.

        Используется ботом, чтобы показать тренеру распознанные поля и
        спросить подтверждение. Новые ученики и группы не создаются:
        если имя не найдено, поднимается :class:`UnknownEntityError`.

        Args:
            tenant_id: Идентификатор тенанта (тренера).
            text: Сырой текст диктовки.
            today: Дата, считающаяся «сегодня» (для тестов).

        Returns:
            Предпросмотр с распознанными полями и найденными сущностями.

        Raises:
            GeminiParseError: Если текст не удалось разобрать.
            UnknownEntityError: Если какое-то имя не найдено в базе.
        """
        parse_result = await self._parser.parse(text, today=today)
        return await self.prepare_from_parse_result(tenant_id, parse_result)

    async def prepare_from_parse_result(
        self,
        tenant_id: int,
        parse_result: LessonParseResult,
    ) -> PreparedLesson:
        """Находит сущности для уже разобранного результата.

        Поиск неточный: тренер может назвать группу коротко («группа
        Шпринцак 4») — :meth:`EntityService.resolve` подберёт её среди
        заведённых в кабинете.

        Args:
            tenant_id: Идентификатор тенанта.
            parse_result: Структурированный результат разбора.

        Returns:
            Предпросмотр с найденными сущностями.

        Raises:
            UnknownEntityError: Если хотя бы одно имя не удалось сопоставить
                однозначно. В исключении есть подсказки с похожими
                названиями.
        """
        prepared: list[PreparedEntity] = []
        missing: list[str] = []
        suggestions: dict[str, list[str]] = {}

        for name in parse_result.all_entity_names:
            entity = await self._entities.resolve(tenant_id, name)
            if entity is None:
                missing.append(name)
                similar = await self._entities.find_similar(tenant_id, name)
                if similar:
                    suggestions[name] = [item.name for item in similar]
                continue
            prepared.append(PreparedEntity(entity=entity))

        if missing:
            raise UnknownEntityError(missing, suggestions)

        return PreparedLesson(parse_result=parse_result, entities=prepared)

    async def process_dictation(
        self,
        tenant_id: int,
        text: str,
        *,
        today: date | None = None,
    ) -> ProcessedLesson:
        """Разбирает диктовку и сразу записывает урок в БД.

        Метод оставлен для служебных сценариев. Интерактивный путь бота
        сначала вызывает :meth:`prepare`, показывает предпросмотр и лишь
        после подтверждения — :meth:`apply_parse_result`.

        Args:
            tenant_id: Идентификатор тенанта (тренера).
            text: Сырой текст диктовки.
            today: Дата, считающаяся «сегодня» (для тестов).

        Returns:
            Результат обработки.

        Raises:
            GeminiParseError: Если текст не удалось разобрать.
            UnknownEntityError: Если какое-то имя не найдено в базе.
        """
        prepared = await self.prepare(tenant_id, text, today=today)
        await self.prepare_from_parse_result(tenant_id, prepared.parse_result)
        return await self.apply_parse_result(tenant_id, prepared.parse_result)

    async def apply_parse_result(
        self,
        tenant_id: int,
        parse_result: LessonParseResult,
    ) -> ProcessedLesson:
        """Применяет уже разобранный результат: сохраняет уроки и обновляет балансы.

        Если в диктовке названо несколько учеников («Рон и Мия»), урок
        создаётся для каждого. Платёж относится только к основному ученику —
        тому, чьё имя названо первым.

        Отдельный метод позволяет повторно применить результат без
        повторного обращения к Gemini (например, после подтверждения тренером).

        Args:
            tenant_id: Идентификатор тенанта.
            parse_result: Структурированный результат разбора.

        Returns:
            Результат обработки с основным учеником и списком остальных.
        """
        names = parse_result.all_entity_names
        processed: list[ProcessedEntity] = []

        for index, name in enumerate(names):
            # Платёж регистрируем только для основного ученика: в диктовке
            # «оплата 120» относится к тому, о ком идёт речь в первую очередь.
            is_primary = index == 0
            processed.append(
                await self._apply_for_entity(
                    tenant_id,
                    parse_result,
                    name,
                    with_payment=is_primary,
                )
            )

        primary, others = processed[0], processed[1:]

        logger.info(
            "Обработана диктовка: сущностей=%s, основной=%s (создана=%s)",
            len(processed),
            primary.entity.name,
            primary.entity_created,
        )

        return ProcessedLesson(
            primary=primary,
            others=others,
            parse_result=parse_result,
        )

    async def _apply_for_entity(
        self,
        tenant_id: int,
        parse_result: LessonParseResult,
        name: str,
        *,
        with_payment: bool,
    ) -> ProcessedEntity:
        """Сохраняет урок для одной сущности.

        Args:
            tenant_id: Идентификатор тенанта.
            parse_result: Структурированный результат разбора.
            name: Имя ученика или группы.
            with_payment: Регистрировать ли платёж из диктовки.

        Returns:
            Результат обработки по этой сущности.

        Raises:
            UnknownEntityError: Если имя не удалось сопоставить с базой.
        """
        entity = await self._entities.resolve(tenant_id, name)
        if entity is None:
            # Бот не создаёт учеников и группы: их заводит тренер в кабинете.
            similar = await self._entities.find_similar(tenant_id, name)
            suggestions = {name: [item.name for item in similar]} if similar else {}
            raise UnknownEntityError([name], suggestions)
        created = False

        # Расписание из диктовки пишем в те же таблицы, что и кабинет:
        # регулярные слоты ``schedule_slots``. Legacy-поля сущности
        # (``schedule_days``/``schedule_time``) заполняем как «мягкий» источник
        # только у пустых значений — для обратной совместимости.
        if parse_result.has_schedule:
            await self._sync_schedule_slots(
                tenant_id,
                entity.id,
                parse_result.schedule_days,
                parse_result.schedule_time,
            )
            updated_entity = await self._entities.apply_schedule_from_dictation(
                tenant_id,
                entity.id,
                parse_result.schedule_days,
                parse_result.schedule_time,
            )
            if updated_entity is not None:
                entity = updated_entity

        lesson = await self._repo.lessons.create(
            tenant_id=tenant_id,
            entity_id=entity.id,
            lesson_date=date.today(),
            topic=parse_result.topic or "—",
            homework=parse_result.homework,
            internal_notes=merge_internal_notes(parse_result),
            rating=str(parse_result.rating) if parse_result.rating is not None else None,
            next_step=parse_result.next_step,
            attendance=parse_result.attendance,
            raw_gemini_json=parse_result.model_dump_json(),
        )

        payment: Payment | None = None
        if with_payment and parse_result.payment.has_payment:
            payment = await self._payments.register_payment(
                tenant_id=tenant_id,
                entity_id=entity.id,
                amount=parse_result.payment.amount,
                lessons_added=parse_result.payment.lessons_added,
                payment_date=date.today(),
            )

        logger.info(
            "Обработан урок id=%s entity_id=%s (создана=%s)",
            lesson.id,
            entity.id,
            created,
        )

        return ProcessedEntity(
            lesson=lesson,
            entity=entity,
            entity_created=created,
            payment=payment,
        )

    async def _sync_schedule_slots(
        self,
        tenant_id: int,
        entity_id: int,
        days: list[str],
        time: str | None,
    ) -> list[ScheduleSlot]:
        """Создаёт регулярные слоты расписания из диктовки.

        Диктовка пишет в ту же таблицу ``schedule_slots``, что и веб-кабинет,
        поэтому расписание остаётся единым источником правды. Дубликаты не
        создаются: если слот на этот день и время уже есть, он пропускается.

        Время обязательно: без него слот создать нельзя (``start_time`` —
        обязательное поле). Если время не названо, слоты не создаются.

        Args:
            tenant_id: Идентификатор тенанта.
            entity_id: Идентификатор ученика или группы.
            days: Дни недели кодами ``mon``…``sun``.
            time: Время начала в формате ``ЧЧ:ММ``.

        Returns:
            Список созданных слотов (может быть пустым).
        """
        if not days or not time:
            return []

        existing = await self._schedule.list_slots_for_entity(
            tenant_id,
            entity_id,
            only_active=False,
        )
        known = {(slot.weekday, slot.start_time) for slot in existing}

        created: list[ScheduleSlot] = []
        for weekday in days:
            if (weekday, time) in known:
                continue
            try:
                slot = await self._schedule.add_slot(
                    tenant_id,
                    entity_id,
                    weekday,
                    time,
                )
            except ValueError:
                logger.warning(
                    "Пропущен некорректный слот из диктовки: entity_id=%s, %s %s",
                    entity_id,
                    weekday,
                    time,
                )
                continue
            known.add((slot.weekday, slot.start_time))
            created.append(slot)

        if created:
            logger.info(
                "Из диктовки создано слотов=%s для entity_id=%s",
                len(created),
                entity_id,
            )
        return created

    async def list_history(
        self,
        tenant_id: int,
        entity_id: int,
        limit: int | None = None,
    ) -> list[Lesson]:
        """Возвращает историю уроков сущности (новые сверху)."""
        return await self._repo.lessons.list_by_entity(tenant_id, entity_id, limit=limit)

    async def get_last_lesson(self, tenant_id: int, entity_id: int) -> Lesson | None:
        """Возвращает последний урок сущности."""
        return await self._repo.lessons.get_last_lesson(tenant_id, entity_id)

    async def count_lessons(self, tenant_id: int, entity_id: int) -> int:
        """Считает количество проведённых уроков у сущности."""
        return await self._repo.lessons.count_by_entity(tenant_id, entity_id)

    async def list_by_date(self, tenant_id: int, lesson_date: date) -> list[Lesson]:
        """Возвращает все уроки тенанта за конкретную дату."""
        return await self._repo.lessons.list_by_date(tenant_id, lesson_date)

    async def delete_lesson(self, tenant_id: int, lesson_id: int) -> bool:
        """Удаляет урок."""
        return await self._repo.lessons.delete(tenant_id, lesson_id)
