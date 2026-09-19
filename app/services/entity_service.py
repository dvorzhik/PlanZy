"""Сервис управления учениками и группами.

Отвечает за поиск/создание сущностей, генерацию публичных токенов
и настройку расписания.
"""

from __future__ import annotations

from app.core.logging import get_logger
from app.db.models import Entity
from app.db.repository import Repository
from app.parsers.lesson_parser import GROUP_MARKERS, LessonParser

logger = get_logger(__name__)

# Допустимые коды дней недели.
WEEKDAY_CODES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

# Русские названия дней недели для отображения.
WEEKDAY_LABELS = {
    "mon": "Пн",
    "tue": "Вт",
    "wed": "Ср",
    "thu": "Чт",
    "fri": "Пт",
    "sat": "Сб",
    "sun": "Вс",
}


class EntityService:
    """Бизнес-логика работы с учениками и группами."""

    def __init__(self, repo: Repository) -> None:
        """Инициализирует сервис.

        Args:
            repo: Фасад репозиториев.
        """
        self._repo = repo

    async def get_or_create(
        self,
        tenant_id: int,
        name: str,
        subject: str = "Шахматы",
    ) -> tuple[Entity, bool]:
        """Находит сущность по имени или создаёт новую.

        Тип сущности (ученик/группа) определяется эвристикой по имени.

        Args:
            tenant_id: Идентификатор тенанта.
            name: Имя ученика или название группы.
            subject: Предмет по умолчанию для новой сущности.

        Returns:
            Кортеж ``(сущность, была_ли_создана)``.
        """
        cleaned = name.strip()
        existing = await self._repo.entities.get_by_name(tenant_id, cleaned)
        if existing is not None:
            return existing, False

        entity_type = LessonParser.detect_entity_type(cleaned)
        entity = await self._repo.entities.create(
            tenant_id=tenant_id,
            name=cleaned,
            entity_type=entity_type,
            subject=subject,
        )
        logger.info(
            "Создана новая сущность %r (тип=%s) для тенанта %s",
            cleaned,
            entity_type,
            tenant_id,
        )
        return entity, True

    async def get_by_id(self, tenant_id: int, entity_id: int) -> Entity | None:
        """Возвращает сущность по id в рамках тенанта."""
        return await self._repo.entities.get_by_id(tenant_id, entity_id)

    async def get_by_token(self, public_token: str) -> Entity | None:
        """Возвращает сущность по публичному токену."""
        return await self._repo.entities.get_by_token(public_token)

    async def get_by_name(self, tenant_id: int, name: str) -> Entity | None:
        """Возвращает сущность по имени в рамках тенанта."""
        return await self._repo.entities.get_by_name(tenant_id, name)

    async def resolve(self, tenant_id: int, name: str) -> Entity | None:
        """Находит ученика или группу по имени из диктовки.

        Точного совпадения мало: тренер в диктовке называет группу коротко
        («группа Шпринцак 4»), а в кабинете она записана полностью
        («Шпринцак 4 - начинающие»). Поэтому поиск идёт по шагам:

        1. точное совпадение (без учёта регистра);
        2. если в имени есть слово «группа» — среди групп ищем ту, чьё
           название содержит остаток фразы («Шпринцак 4»);
        3. иначе — среди всех сущностей ищем ту, чьё название начинается
           с названного имени или содержит его целиком.

        Совпадение должно быть однозначным: если под условие подходят
        несколько сущностей, возвращается None, чтобы бот не записал урок
        не тому ученику и попросил уточнить имя.

        Args:
            tenant_id: Идентификатор тенанта.
            name: Имя из диктовки.

        Returns:
            Найденная сущность или None, если совпадение неоднозначно
            либо ничего не найдено.
        """
        target = name.strip()
        if not target:
            return None

        exact = await self._repo.entities.get_by_name(tenant_id, target)
        if exact is not None:
            return exact

        entities = await self._repo.entities.list_by_tenant(tenant_id)
        query = target.casefold()
        words = query.split()

        # Шаг 2: «группа Шпринцак 4» — отбрасываем слово «группа»
        # и ищем по остатку среди групп.
        if any(marker in query for marker in GROUP_MARKERS):
            remainder = " ".join(
                word for word in words if not any(marker in word for marker in GROUP_MARKERS)
            ).strip()
            groups = [entity for entity in entities if entity.is_group]

            if remainder:
                groups = [entity for entity in groups if remainder in entity.name.casefold()]

            if len(groups) == 1:
                return groups[0]
            if len(groups) > 1:
                logger.warning(
                    "Неоднозначное имя группы %r: подходят %s",
                    name,
                    [entity.name for entity in groups],
                )
                return None
            # Групп по остатку не нашли — проверим остальные варианты ниже.
            if not remainder:
                return None

        # Шаг 3: частичное совпадение по названию среди всех сущностей.
        candidates = [
            entity
            for entity in entities
            if query in entity.name.casefold() or entity.name.casefold().startswith(query)
        ]

        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1:
            logger.warning(
                "Неоднозначное имя %r: подходят %s",
                name,
                [entity.name for entity in candidates],
            )
        return None

    async def find_similar(self, tenant_id: int, name: str, *, limit: int = 5) -> list[Entity]:
        """Подбирает похожие названия для подсказки тренеру.

        Используется, когда :meth:`resolve` не нашёл однозначного
        совпадения: бот показывает варианты, чтобы тренер поправил текст.

        Служебные слова («группа», «команда») в сравнении не участвуют:
        иначе на запрос «Группа Новички» подсказка предлагала бы все группы
        подряд только из-за общего слова.

        Args:
            tenant_id: Идентификатор тенанта.
            name: Имя из диктовки.
            limit: Сколько вариантов вернуть.

        Returns:
            Список сущностей, наиболее похожих по названию. Пустой список,
            если значимых слов для сравнения не осталось.
        """
        entities = await self._repo.entities.list_by_tenant(tenant_id)
        query = name.strip().casefold()
        if not query:
            return []

        # Сравниваем по отдельным словам: «Шпринцак 4» найдёт
        # «Шпринцак 4 - начинающие», даже если порядок слов иной.
        words = [
            word
            for word in query.split()
            if len(word) > 1 and not any(marker in word for marker in GROUP_MARKERS)
        ]
        if not words:
            # В запросе только «группа» — сравнивать нечего, подсказки
            # из одних групп были бы шумом.
            return []

        scored: list[tuple[int, Entity]] = []
        for entity in entities:
            haystack = entity.name.casefold()
            score = sum(1 for word in words if word in haystack)
            if score:
                scored.append((score, entity))

        # Сначала больше совпавших слов, затем короткие названия, затем алфавит.
        scored.sort(key=lambda item: (-item[0], len(item[1].name), item[1].name))
        return [entity for _, entity in scored[:limit]]

    async def list_all(self, tenant_id: int) -> list[Entity]:
        """Возвращает всех учеников и группы тенанта."""
        return await self._repo.entities.list_by_tenant(tenant_id)

    async def list_students(self, tenant_id: int) -> list[Entity]:
        """Возвращает только учеников."""
        return await self._repo.entities.list_by_tenant(tenant_id, entity_type="student")

    async def list_groups(self, tenant_id: int) -> list[Entity]:
        """Возвращает только группы."""
        return await self._repo.entities.list_by_tenant(tenant_id, entity_type="group")

    async def list_for_weekday(self, tenant_id: int, weekday: str) -> list[Entity]:
        """Возвращает сущности с занятием в указанный день недели.

        Args:
            tenant_id: Идентификатор тенанта.
            weekday: Код дня недели (mon..sun).

        Returns:
            Список сущностей, отсортированный по времени занятия.
        """
        code = weekday.strip().lower()
        if code not in WEEKDAY_CODES:
            raise ValueError(
                f"Недопустимый день недели: {weekday!r}. "
                f"Ожидается один из: {', '.join(WEEKDAY_CODES)}"
            )
        entities = await self._repo.entities.list_for_weekday(tenant_id, code)
        return sorted(entities, key=lambda e: (e.schedule_time or "99:99", e.name))

    async def set_schedule(
        self,
        tenant_id: int,
        entity_id: int,
        days: list[str],
        time: str | None = None,
    ) -> Entity | None:
        """Задаёт расписание занятий сущности.

        Args:
            tenant_id: Идентификатор тенанта.
            entity_id: Идентификатор сущности.
            days: Список кодов дней недели (mon..sun).
            time: Время занятия в формате ЧЧ:ММ.

        Returns:
            Обновлённая сущность или None, если не найдена.

        Raises:
            ValueError: Если передан недопустимый код дня недели.
        """
        normalized = [day.strip().lower() for day in days if day.strip()]
        invalid = [day for day in normalized if day not in WEEKDAY_CODES]
        if invalid:
            raise ValueError(
                f"Недопустимые дни недели: {', '.join(invalid)}. "
                f"Ожидается один из: {', '.join(WEEKDAY_CODES)}"
            )
        return await self._repo.entities.update(
            tenant_id,
            entity_id,
            schedule_days=",".join(normalized) if normalized else None,
            schedule_time=time,
        )

    async def apply_schedule_from_dictation(
        self,
        tenant_id: int,
        entity_id: int,
        days: list[str],
        time: str | None = None,
    ) -> Entity | None:
        """Сохраняет расписание, названное в диктовке.

        Расписание из диктовки считается «мягким» источником: оно заполняет
        только те поля, которые ещё пусты. Уже заданное расписание не
        перезаписывается, чтобы случайная фраза не сломала утреннюю рассылку.

        Args:
            tenant_id: Идентификатор тенанта.
            entity_id: Идентификатор сущности.
            days: Дни недели кодами mon..sun (может быть пустым).
            time: Время занятия в формате ЧЧ:ММ.

        Returns:
            Обновлённая сущность или None, если не найдена.
        """
        entity = await self._repo.entities.get_by_id(tenant_id, entity_id)
        if entity is None:
            return None

        updates: dict[str, object] = {}
        if days and not entity.schedule_days:
            updates["schedule_days"] = ",".join(days)
        if time and not entity.schedule_time:
            updates["schedule_time"] = time

        if not updates:
            return entity

        updated = await self._repo.entities.update(tenant_id, entity_id, **updates)
        if updated is not None:
            logger.info(
                "Расписание entity_id=%s заполнено из диктовки: %s",
                entity_id,
                updates,
            )
        return updated

    async def set_parent_phone(
        self,
        tenant_id: int,
        entity_id: int,
        phone: str | None,
    ) -> Entity | None:
        """Задаёт телефон родителя для отправки отчётов в WhatsApp."""
        return await self._repo.entities.update(
            tenant_id,
            entity_id,
            parent_phone=phone.strip() if phone else None,
        )

    async def ensure_public_token(self, tenant_id: int, entity_id: int) -> str | None:
        """Гарантирует наличие публичного токена у сущности.

        Returns:
            Публичный токен или None, если сущность не найдена.
        """
        entity = await self._repo.entities.get_by_id(tenant_id, entity_id)
        if entity is None:
            return None
        if entity.public_token:
            return entity.public_token
        # Токен генерируется на уровне модели при создании, но на случай
        # legacy-данных предусмотрена явная установка.
        from app.db.models import generate_public_token

        token = generate_public_token()
        await self._repo.entities.update(tenant_id, entity_id, public_token=token)
        return token

    @staticmethod
    def format_schedule(entity: Entity) -> str:
        """Форматирует расписание сущности для отображения в чате.

        Args:
            entity: Сущность с расписанием.

        Returns:
            Строка вида «Пн, Ср в 16:00» или «расписание не задано».
        """
        days = entity.schedule_days_list
        if not days:
            return "расписание не задано"
        labels = ", ".join(WEEKDAY_LABELS.get(day, day) for day in days)
        if entity.schedule_time:
            return f"{labels} в {entity.schedule_time}"
        return labels
