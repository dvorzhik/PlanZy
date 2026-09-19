"""Сервис досье ученика.

Собирает разрозненные данные (уроки, платежи) в единую структуру,
пригодную для отображения в чате Telegram и на веб-странице досье.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from app.core.logging import get_logger
from app.db.models import Entity, Lesson, Payment
from app.db.repository import Repository
from app.services.entity_service import EntityService
from app.services.payment_service import PaymentService
from app.services.schedule_service import ScheduleService

logger = get_logger(__name__)

# Сколько последних уроков показывать в досье по умолчанию.
DEFAULT_HISTORY_LIMIT = 10


@dataclass(slots=True)
class TopicEntry:
    """Одна запись в истории тем.

    Attributes:
        lesson_date: Дата занятия.
        topic: Тема занятия.
        rating: Оценка (1–5) или None.
        homework: Домашнее задание.
        next_step: План на следующее занятие.
        attendance: Посещаемость.
    """

    lesson_date: date
    topic: str
    rating: int | None
    homework: str | None
    next_step: str | None
    attendance: str


@dataclass(slots=True)
class StudentDossier:
    """Агрегированное досье ученика.

    Attributes:
        entity: Ученик или группа.
        lessons_count: Общее количество проведённых уроков.
        total_paid: Суммарно оплаченная сумма.
        last_payment: Последний платёж.
        history: История тем (новые сверху).
        average_rating: Средняя оценка по урокам с оценкой.
        rating_trend: Динамика оценок: «up», «down», «flat» или None.
        last_next_step: План с прошлого занятия (что задавали вперёд).
        last_homework: Домашнее задание с прошлого занятия.
        weak_topics: Темы с низкой оценкой (≤ 3).
        schedule: Текстовая сводка регулярного расписания из слотов.
    """

    entity: Entity
    lessons_count: int
    total_paid: float
    last_payment: Payment | None
    history: list[TopicEntry] = field(default_factory=list)
    average_rating: float | None = None
    rating_trend: str | None = None
    last_next_step: str | None = None
    last_homework: str | None = None
    weak_topics: list[str] = field(default_factory=list)
    schedule: str = "расписание не задано"

    @property
    def name(self) -> str:
        """Имя ученика или название группы."""
        return self.entity.name

    @property
    def is_group(self) -> bool:
        """True, если досье относится к группе."""
        return self.entity.is_group


class DossierService:
    """Бизнес-логика сборки досье ученика."""

    def __init__(self, repo: Repository) -> None:
        """Инициализирует сервис.

        Args:
            repo: Фасад репозиториев.
        """
        self._repo = repo
        self._entities = EntityService(repo)
        self._payments = PaymentService(repo)

    async def build(
        self,
        tenant_id: int,
        entity_id: int,
        *,
        history_limit: int = DEFAULT_HISTORY_LIMIT,
    ) -> StudentDossier | None:
        """Собирает досье ученика по его идентификатору.

        Args:
            tenant_id: Идентификатор тенанта.
            entity_id: Идентификатор ученика/группы.
            history_limit: Сколько последних уроков включить в историю.

        Returns:
            Досье или None, если сущность не найдена.
        """
        entity = await self._entities.get_by_id(tenant_id, entity_id)
        if entity is None:
            logger.warning("Досье: сущность id=%s не найдена", entity_id)
            return None
        return await self._build_for_entity(
            tenant_id,
            entity,
            history_limit=history_limit,
        )

    async def build_by_token(
        self,
        public_token: str,
        *,
        history_limit: int = DEFAULT_HISTORY_LIMIT,
    ) -> StudentDossier | None:
        """Собирает досье по публичному токену.

        Args:
            public_token: Публичный токен ученика.
            history_limit: Сколько последних уроков включить в историю.

        Returns:
            Досье или None, если токен не найден.
        """
        entity = await self._entities.get_by_token(public_token)
        if entity is None:
            logger.warning("Досье: токен %r не найден", public_token)
            return None
        return await self._build_for_entity(
            entity.tenant_id,
            entity,
            history_limit=history_limit,
        )

    async def build_by_name(
        self,
        tenant_id: int,
        name: str,
        *,
        history_limit: int = DEFAULT_HISTORY_LIMIT,
    ) -> StudentDossier | None:
        """Собирает досье по имени ученика.

        Args:
            tenant_id: Идентификатор тенанта.
            name: Имя ученика или название группы.
            history_limit: Сколько последних уроков включить в историю.

        Returns:
            Досье или None, если ученик не найден.
        """
        entity = await self._entities.get_by_name(tenant_id, name)
        if entity is None:
            return None
        return await self._build_for_entity(
            tenant_id,
            entity,
            history_limit=history_limit,
        )

    # ------------------------------------------------------------------
    # Внутренние помощники
    # ------------------------------------------------------------------

    async def _build_for_entity(
        self,
        tenant_id: int,
        entity: Entity,
        *,
        history_limit: int,
    ) -> StudentDossier:
        """Собирает досье для уже найденной сущности.

        Args:
            tenant_id: Идентификатор тенанта.
            entity: Ученик или группа.
            history_limit: Сколько последних уроков включить в историю.

        Returns:
            Заполненное досье.
        """
        lessons = await self._repo.lessons.list_by_entity(
            tenant_id,
            entity.id,
            limit=history_limit,
        )
        lessons_count = await self._repo.lessons.count_by_entity(tenant_id, entity.id)
        total_paid = await self._payments.total_paid(tenant_id, entity.id)
        last_payment = await self._payments.get_last_payment(tenant_id, entity.id)

        history = [self._to_topic_entry(lesson) for lesson in lessons]
        ratings = [entry.rating for entry in history if entry.rating is not None]

        # Расписание берём из регулярных слотов — того же источника, что
        # использует веб-кабинет. Устаревшие поля Entity пусты после
        # перехода на web-first.
        schedule = await ScheduleService(self._repo).format_entity_schedule(tenant_id, entity.id)

        dossier = StudentDossier(
            entity=entity,
            lessons_count=lessons_count,
            total_paid=total_paid,
            last_payment=last_payment,
            history=history,
            average_rating=round(sum(ratings) / len(ratings), 2) if ratings else None,
            rating_trend=self._compute_trend(ratings),
            last_next_step=history[0].next_step if history else None,
            last_homework=history[0].homework if history else None,
            weak_topics=self._collect_weak_topics(history),
            schedule=schedule,
        )
        return dossier

    @staticmethod
    def _to_topic_entry(lesson: Lesson) -> TopicEntry:
        """Преобразует модель урока в запись истории тем.

        Args:
            lesson: Запись об уроке.

        Returns:
            Запись истории тем.
        """
        return TopicEntry(
            lesson_date=lesson.lesson_date,
            topic=lesson.topic or "—",
            rating=DossierService._parse_rating(lesson.rating),
            homework=lesson.homework,
            next_step=lesson.next_step,
            attendance=lesson.attendance or "present",
        )

    @staticmethod
    def _parse_rating(raw: str | None) -> int | None:
        """Разбирает оценку из строкового поля БД.

        Args:
            raw: Строковое представление оценки.

        Returns:
            Оценка 1–5 или None, если значение отсутствует/некорректно.
        """
        if not raw:
            return None
        try:
            value = int(str(raw).strip())
        except (TypeError, ValueError):
            logger.debug("Не удалось разобрать оценку %r", raw)
            return None
        if 1 <= value <= 5:
            return value
        return None

    @staticmethod
    def _compute_trend(ratings: list[int]) -> str | None:
        """Определяет динамику оценок по последним урокам.

        Сравнивает среднее первой половины списка со второй. Список приходит
        в порядке «новые сверху», поэтому первая половина — более свежие уроки.

        Args:
            ratings: Оценки в порядке «новые сверху».

        Returns:
            «up», «down», «flat» или None, если данных мало.
        """
        if len(ratings) < 2:
            return None
        half = len(ratings) // 2
        recent = ratings[:half]
        older = ratings[half:]
        if not recent or not older:
            return None
        recent_avg = sum(recent) / len(recent)
        older_avg = sum(older) / len(older)
        delta = recent_avg - older_avg
        if delta > 0.3:
            return "up"
        if delta < -0.3:
            return "down"
        return "flat"

    @staticmethod
    def _collect_weak_topics(history: list[TopicEntry], threshold: int = 3) -> list[str]:
        """Собирает темы с низкой оценкой.

        Args:
            history: История тем.
            threshold: Оценка, при которой тема считается слабой.

        Returns:
            Список тем с оценкой не выше порога (без повторов).
        """
        weak: list[str] = []
        for entry in history:
            if entry.rating is not None and entry.rating <= threshold and entry.topic not in weak:
                weak.append(entry.topic)
        return weak

    # ------------------------------------------------------------------
    # Форматирование для чата
    # ------------------------------------------------------------------

    @staticmethod
    def format_for_chat(dossier: StudentDossier) -> str:
        """Формирует текстовое представление досье для Telegram.

        Args:
            dossier: Собранное досье.

        Returns:
            Текст, готовый к отправке в чат.
        """
        entity = dossier.entity
        lines: list[str] = [
            f"📁 Досье: {entity.name}",
            f"Тип: {'группа' if entity.is_group else 'ученик'} · предмет: {entity.subject}",
            "",
            f"📚 Проведено занятий: {dossier.lessons_count}",
        ]

        if dossier.total_paid:
            lines.append(f"💰 Всего оплачено: {dossier.total_paid:g}")

        if dossier.average_rating is not None:
            trend_label = {
                "up": "📈 растёт",
                "down": "📉 снижается",
                "flat": "➡️ стабильно",
            }.get(dossier.rating_trend or "", "")
            suffix = f" ({trend_label})" if trend_label else ""
            lines.append(f"⭐ Средняя оценка: {dossier.average_rating}{suffix}")

        schedule = dossier.schedule
        lines.append(f"🗓 Расписание: {schedule}")

        if dossier.last_next_step:
            lines.append("")
            lines.append(f"➡️ План с прошлого занятия: {dossier.last_next_step}")

        if dossier.last_homework:
            lines.append(f"📝 Прошлое ДЗ: {dossier.last_homework}")

        if dossier.weak_topics:
            lines.append("")
            lines.append(f"⚠️ Слабые темы: {', '.join(dossier.weak_topics)}")

        if dossier.history:
            lines.append("")
            lines.append("📖 История тем:")
            for entry in dossier.history:
                rating = f" — {entry.rating}/5" if entry.rating is not None else ""
                mark = "" if entry.attendance == "present" else f" [{entry.attendance}]"
                lines.append(
                    f"• {entry.lesson_date.strftime('%d.%m.%Y')}: {entry.topic}{rating}{mark}"
                )
        else:
            lines.append("")
            lines.append("История занятий пока пуста.")

        return "\n".join(lines)
