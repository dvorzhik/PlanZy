"""Сервис публичных ссылок на конкретное занятие.

Тренер может поделиться ссылкой ``/lesson/{token}`` с родителем: страница
показывает тему, домашнее задание, оценку и план на следующий урок, но
никогда — приватные заметки тренера (``internal_notes``).

Ссылку можно отозвать (``is_revoked``) или ограничить по времени
(``expires_at``). Проверка срока действия выполняется здесь, чтобы веб-слой
не дублировал логику работы с naive-датами SQLite.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.core.logging import get_logger
from app.db.models import Entity, Lesson, LessonShareLink
from app.db.repository import Repository

logger = get_logger(__name__)

# Срок действия ссылки по умолчанию — 30 дней.
DEFAULT_TTL_DAYS = 30

# Максимальный срок действия ссылки — 365 дней.
MAX_TTL_DAYS = 365


@dataclass(slots=True)
class SharedLesson:
    """Данные занятия для публичной страницы ``/lesson/{token}``.

    Attributes:
        link: Сама ссылка (для проверки срока и отзыва).
        lesson: Занятие, на которое указывает ссылка.
        entity: Ученик или группа, к которой относится занятие.
        is_expired: True, если срок действия ссылки истёк.
        is_revoked: True, если ссылку отозвали.
    """

    link: LessonShareLink
    lesson: Lesson
    entity: Entity
    is_expired: bool
    is_revoked: bool

    @property
    def is_valid(self) -> bool:
        """True, если ссылку ещё можно показывать."""
        return not self.is_expired and not self.is_revoked


class ShareLinkService:
    """Бизнес-логика публичных ссылок на занятия."""

    def __init__(self, repo: Repository) -> None:
        """Инициализирует сервис.

        Args:
            repo: Фасад репозиториев.
        """
        self._repo = repo

    async def create_link(
        self,
        tenant_id: int,
        lesson_id: int,
        *,
        ttl_days: int | None = DEFAULT_TTL_DAYS,
    ) -> LessonShareLink | None:
        """Создаёт публичную ссылку на занятие.

        Args:
            tenant_id: Идентификатор тенанта.
            lesson_id: Идентификатор занятия.
            ttl_days: Срок жизни ссылки в днях. ``None`` — бессрочно.

        Returns:
            Созданная ссылка или None, если занятие не найдено.
        """
        lesson = await self._repo.lessons.get_by_id(tenant_id, lesson_id)
        if lesson is None:
            logger.warning("Ссылка на урок: занятие id=%s не найдено", lesson_id)
            return None

        expires_at = self._resolve_expiry(ttl_days)
        link = await self._repo.share_links.create(
            tenant_id=tenant_id,
            lesson_id=lesson_id,
            expires_at=expires_at,
        )
        logger.info("Создана публичная ссылка на урок id=%s lesson_id=%s", link.id, lesson_id)
        return link

    async def get_active_link(self, tenant_id: int, lesson_id: int) -> LessonShareLink | None:
        """Возвращает действующую ссылку занятия, если она есть.

        Args:
            tenant_id: Идентификатор тенанта.
            lesson_id: Идентификатор занятия.

        Returns:
            Действующая ссылка или None.
        """
        return await self._repo.share_links.get_active_for_lesson(tenant_id, lesson_id)

    async def get_latest_link(self, tenant_id: int, lesson_id: int) -> LessonShareLink | None:
        """Возвращает последнюю неотозванную ссылку занятия, включая истёкшую.

        Нужна карточке занятия в кабинете: тренер должен видеть, что ссылка
        была создана, но уже истекла, и создать новую.

        Args:
            tenant_id: Идентификатор тенанта.
            lesson_id: Идентификатор занятия.

        Returns:
            Последняя неотозванная ссылка или None.
        """
        links = await self._repo.share_links.list_by_lesson(tenant_id, lesson_id)
        for link in links:
            if not link.is_revoked:
                return link
        return None

    async def ensure_link(
        self,
        tenant_id: int,
        lesson_id: int,
        *,
        ttl_days: int | None = DEFAULT_TTL_DAYS,
    ) -> LessonShareLink | None:
        """Возвращает действующую ссылку или создаёт новую.

        Args:
            tenant_id: Идентификатор тенанта.
            lesson_id: Идентификатор занятия.
            ttl_days: Срок жизни новой ссылки в днях.

        Returns:
            Действующая либо только что созданная ссылка, или None,
            если занятие не найдено.
        """
        existing = await self.get_active_link(tenant_id, lesson_id)
        if existing is not None:
            return existing
        return await self.create_link(tenant_id, lesson_id, ttl_days=ttl_days)

    async def revoke_link(self, tenant_id: int, link_id: int) -> LessonShareLink | None:
        """Отзывает ссылку, не удаляя её из истории.

        Args:
            tenant_id: Идентификатор тенанта.
            link_id: Идентификатор ссылки.

        Returns:
            Отозванная ссылка или None, если ссылка не найдена.
        """
        return await self._repo.share_links.revoke(tenant_id, link_id)

    async def resolve(self, token: str) -> SharedLesson | None:
        """Собирает данные занятия по токену публичной ссылки.

        Возвращает None, если токен неизвестен. Если ссылка отозвана или
        истекла, возвращает объект с соответствующими флагами — веб-слой
        сам решает, показать страницу «ссылка истекла» или 404.

        Args:
            token: Токен публичной ссылки.

        Returns:
            Данные занятия или None, если токен не найден.
        """
        link = await self._repo.share_links.get_by_token(token)
        if link is None:
            logger.info("Публичная ссылка на урок: неизвестный токен %r", token)
            return None

        lesson = await self._repo.lessons.get_by_id(link.tenant_id, link.lesson_id)
        if lesson is None:
            logger.warning("Публичная ссылка: занятие id=%s не найдено", link.lesson_id)
            return None

        entity = await self._repo.entities.get_by_id(link.tenant_id, lesson.entity_id)
        if entity is None:
            logger.warning("Публичная ссылка: ученик id=%s не найден", lesson.entity_id)
            return None

        return SharedLesson(
            link=link,
            lesson=lesson,
            entity=entity,
            is_expired=self._is_expired(link),
            is_revoked=bool(link.is_revoked),
        )

    # ------------------------------------------------------------------
    # Внутренние помощники
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_expiry(ttl_days: int | None) -> datetime | None:
        """Преобразует срок жизни в абсолютный момент истечения.

        Args:
            ttl_days: Срок жизни в днях. ``None`` — бессрочно.

        Returns:
            Момент истечения в UTC или None для бессрочной ссылки.
        """
        if ttl_days is None:
            return None
        days = max(1, min(int(ttl_days), MAX_TTL_DAYS))
        return datetime.now(UTC) + timedelta(days=days)

    @staticmethod
    def days_left(link: LessonShareLink) -> int | None:
        """Возвращает число полных дней до истечения ссылки.

        Args:
            link: Проверяемая ссылка.

        Returns:
            Количество оставшихся дней (0, если срок уже истёк) или None
            для бессрочной ссылки.
        """
        expires_at = link.expires_at
        if expires_at is None:
            return None
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        delta = expires_at - datetime.now(UTC)
        if delta.total_seconds() <= 0:
            return 0
        return delta.days

    @staticmethod
    def _is_expired(link: LessonShareLink) -> bool:
        """Проверяет, истёк ли срок действия ссылки.

        SQLite хранит ``DateTime(timezone=True)`` без смещения, поэтому
        полученное значение может быть naive — приводим его к UTC вручную.

        Args:
            link: Проверяемая ссылка.

        Returns:
            True, если срок действия истёк.
        """
        expires_at = link.expires_at
        if expires_at is None:
            return False
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        return expires_at <= datetime.now(UTC)
