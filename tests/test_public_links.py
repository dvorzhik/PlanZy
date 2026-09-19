"""Тесты сервиса публичных ссылок на занятие (``ShareLinkService``).

Проверяют полный жизненный цикл ссылки: создание с разным сроком действия,
переиспользование действующей ссылки, отзыв, разрешение токена и корректную
классификацию «истекла / отозвана / действует».

Интеграционные проверки веб-страниц ``/lesson/{token}`` живут в
``tests/test_web.py``; здесь тестируется именно бизнес-логика сервиса.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from app.db.repository import Repository
from app.services.share_link_service import (
    DEFAULT_TTL_DAYS,
    MAX_TTL_DAYS,
    SharedLesson,
    ShareLinkService,
)

LESSON_DATE = date(2026, 9, 14)


async def _make_lesson(
    repo: Repository,
    tenant_id: int,
    entity_id: int,
    *,
    topic: str = "Итальянская партия",
    homework: str | None = "Решить 10 задач",
    internal_notes: str | None = "Секретная заметка тренера",
    rating: str | None = "4",
    next_step: str | None = "Разобрать дебютные ловушки",
    attendance: str | None = "present",
):
    """Создаёт занятие с типовым набором полей.

    Returns:
        Созданный объект занятия.
    """
    lesson = await repo.lessons.create(
        tenant_id=tenant_id,
        entity_id=entity_id,
        lesson_date=LESSON_DATE,
        topic=topic,
        homework=homework,
        internal_notes=internal_notes,
        rating=rating,
        next_step=next_step,
        attendance=attendance,
    )
    await repo.session.flush()
    return lesson


@pytest.fixture
def service(repo: Repository) -> ShareLinkService:
    """Возвращает сервис публичных ссылок поверх тестового репозитория."""
    return ShareLinkService(repo)


class TestCreateLink:
    """Проверки создания публичной ссылки."""

    async def test_create_returns_link_with_token(
        self,
        repo: Repository,
        tenant_id: int,
        service: ShareLinkService,
    ) -> None:
        """Созданная ссылка получает уникальный токен и привязана к занятию."""
        entity = await repo.entities.create(tenant_id=tenant_id, name="Аня", entity_type="student")
        lesson = await _make_lesson(repo, tenant_id, entity.id)

        link = await service.create_link(tenant_id, lesson.id)

        assert link is not None
        assert link.lesson_id == lesson.id
        assert link.tenant_id == tenant_id
        assert link.token
        assert link.is_revoked is False

    async def test_create_sets_default_expiry(
        self,
        repo: Repository,
        tenant_id: int,
        service: ShareLinkService,
    ) -> None:
        """По умолчанию ссылка живёт ``DEFAULT_TTL_DAYS`` дней."""
        entity = await repo.entities.create(tenant_id=tenant_id, name="Аня", entity_type="student")
        lesson = await _make_lesson(repo, tenant_id, entity.id)

        link = await service.create_link(tenant_id, lesson.id)

        assert link is not None
        assert link.expires_at is not None
        days_left = service.days_left(link)
        assert days_left is not None
        assert days_left == DEFAULT_TTL_DAYS - 1 or days_left == DEFAULT_TTL_DAYS

    async def test_create_without_ttl_is_indefinite(
        self,
        repo: Repository,
        tenant_id: int,
        service: ShareLinkService,
    ) -> None:
        """``ttl_days=None`` создаёт бессрочную ссылку."""
        entity = await repo.entities.create(tenant_id=tenant_id, name="Аня", entity_type="student")
        lesson = await _make_lesson(repo, tenant_id, entity.id)

        link = await service.create_link(tenant_id, lesson.id, ttl_days=None)

        assert link is not None
        assert link.expires_at is None
        assert service.days_left(link) is None

    async def test_create_clamps_ttl_to_maximum(
        self,
        repo: Repository,
        tenant_id: int,
        service: ShareLinkService,
    ) -> None:
        """Слишком большой срок обрезается до ``MAX_TTL_DAYS``."""
        entity = await repo.entities.create(tenant_id=tenant_id, name="Аня", entity_type="student")
        lesson = await _make_lesson(repo, tenant_id, entity.id)

        link = await service.create_link(tenant_id, lesson.id, ttl_days=10_000)

        assert link is not None
        assert link.expires_at is not None
        days_left = service.days_left(link)
        assert days_left is not None
        assert days_left <= MAX_TTL_DAYS

    async def test_create_unknown_lesson_returns_none(
        self,
        tenant_id: int,
        service: ShareLinkService,
    ) -> None:
        """Для несуществующего занятия ссылка не создаётся."""
        link = await service.create_link(tenant_id, 999_999)

        assert link is None

    async def test_create_generates_distinct_tokens(
        self,
        repo: Repository,
        tenant_id: int,
        service: ShareLinkService,
    ) -> None:
        """Каждая новая ссылка получает собственный токен."""
        entity = await repo.entities.create(tenant_id=tenant_id, name="Аня", entity_type="student")
        lesson = await _make_lesson(repo, tenant_id, entity.id)

        first = await service.create_link(tenant_id, lesson.id)
        second = await service.create_link(tenant_id, lesson.id)

        assert first is not None
        assert second is not None
        assert first.token != second.token


class TestEnsureLink:
    """Проверки переиспользования действующей ссылки."""

    async def test_ensure_creates_when_absent(
        self,
        repo: Repository,
        tenant_id: int,
        service: ShareLinkService,
    ) -> None:
        """Если действующей ссылки нет — создаётся новая."""
        entity = await repo.entities.create(tenant_id=tenant_id, name="Аня", entity_type="student")
        lesson = await _make_lesson(repo, tenant_id, entity.id)

        link = await service.ensure_link(tenant_id, lesson.id)

        assert link is not None
        assert link.lesson_id == lesson.id

    async def test_ensure_reuses_active_link(
        self,
        repo: Repository,
        tenant_id: int,
        service: ShareLinkService,
    ) -> None:
        """Повторный вызов возвращает ту же действующую ссылку."""
        entity = await repo.entities.create(tenant_id=tenant_id, name="Аня", entity_type="student")
        lesson = await _make_lesson(repo, tenant_id, entity.id)

        first = await service.ensure_link(tenant_id, lesson.id)
        second = await service.ensure_link(tenant_id, lesson.id)

        assert first is not None
        assert second is not None
        assert first.id == second.id
        assert first.token == second.token

    async def test_ensure_creates_new_after_revoke(
        self,
        repo: Repository,
        tenant_id: int,
        service: ShareLinkService,
    ) -> None:
        """После отзыва создаётся новая ссылка с другим токеном."""
        entity = await repo.entities.create(tenant_id=tenant_id, name="Аня", entity_type="student")
        lesson = await _make_lesson(repo, tenant_id, entity.id)

        first = await service.ensure_link(tenant_id, lesson.id)
        assert first is not None
        await service.revoke_link(tenant_id, first.id)

        second = await service.ensure_link(tenant_id, lesson.id)

        assert second is not None
        assert second.id != first.id
        assert second.token != first.token

    async def test_ensure_creates_new_after_expiry(
        self,
        repo: Repository,
        tenant_id: int,
        service: ShareLinkService,
    ) -> None:
        """Истёкшая ссылка не переиспользуется — создаётся новая."""
        entity = await repo.entities.create(tenant_id=tenant_id, name="Аня", entity_type="student")
        lesson = await _make_lesson(repo, tenant_id, entity.id)
        expired = await repo.share_links.create(
            tenant_id=tenant_id,
            lesson_id=lesson.id,
            expires_at=datetime(2020, 1, 1, tzinfo=UTC),
        )
        await repo.session.flush()

        link = await service.ensure_link(tenant_id, lesson.id)

        assert link is not None
        assert link.id != expired.id

    async def test_ensure_unknown_lesson_returns_none(
        self,
        tenant_id: int,
        service: ShareLinkService,
    ) -> None:
        """Для несуществующего занятия ссылка не создаётся."""
        link = await service.ensure_link(tenant_id, 999_999)

        assert link is None


class TestRevokeLink:
    """Проверки отзыва ссылки."""

    async def test_revoke_marks_link(
        self,
        repo: Repository,
        tenant_id: int,
        service: ShareLinkService,
    ) -> None:
        """Отзыв выставляет флаг ``is_revoked``."""
        entity = await repo.entities.create(tenant_id=tenant_id, name="Аня", entity_type="student")
        lesson = await _make_lesson(repo, tenant_id, entity.id)
        link = await service.create_link(tenant_id, lesson.id)
        assert link is not None

        revoked = await service.revoke_link(tenant_id, link.id)

        assert revoked is not None
        assert revoked.is_revoked is True

    async def test_revoke_unknown_returns_none(
        self,
        tenant_id: int,
        service: ShareLinkService,
    ) -> None:
        """Отзыв несуществующей ссылки возвращает None."""
        revoked = await service.revoke_link(tenant_id, 999_999)

        assert revoked is None

    async def test_revoked_link_not_active(
        self,
        repo: Repository,
        tenant_id: int,
        service: ShareLinkService,
    ) -> None:
        """Отозванная ссылка больше не считается действующей."""
        entity = await repo.entities.create(tenant_id=tenant_id, name="Аня", entity_type="student")
        lesson = await _make_lesson(repo, tenant_id, entity.id)
        link = await service.create_link(tenant_id, lesson.id)
        assert link is not None
        await service.revoke_link(tenant_id, link.id)

        active = await service.get_active_link(tenant_id, lesson.id)

        assert active is None


class TestGetLatestLink:
    """Проверки выборки последней неотозванной ссылки."""

    async def test_latest_returns_none_without_links(
        self,
        repo: Repository,
        tenant_id: int,
        service: ShareLinkService,
    ) -> None:
        """Без ссылок метод возвращает None."""
        entity = await repo.entities.create(tenant_id=tenant_id, name="Аня", entity_type="student")
        lesson = await _make_lesson(repo, tenant_id, entity.id)

        latest = await service.get_latest_link(tenant_id, lesson.id)

        assert latest is None

    async def test_latest_returns_expired_link(
        self,
        repo: Repository,
        tenant_id: int,
        service: ShareLinkService,
    ) -> None:
        """Истёкшая ссылка всё равно возвращается как последняя."""
        entity = await repo.entities.create(tenant_id=tenant_id, name="Аня", entity_type="student")
        lesson = await _make_lesson(repo, tenant_id, entity.id)
        expired = await repo.share_links.create(
            tenant_id=tenant_id,
            lesson_id=lesson.id,
            expires_at=datetime(2020, 1, 1, tzinfo=UTC),
        )
        await repo.session.flush()

        latest = await service.get_latest_link(tenant_id, lesson.id)

        assert latest is not None
        assert latest.id == expired.id

    async def test_latest_skips_revoked_link(
        self,
        repo: Repository,
        tenant_id: int,
        service: ShareLinkService,
    ) -> None:
        """Отозванная ссылка не считается последней."""
        entity = await repo.entities.create(tenant_id=tenant_id, name="Аня", entity_type="student")
        lesson = await _make_lesson(repo, tenant_id, entity.id)
        link = await service.create_link(tenant_id, lesson.id)
        assert link is not None
        await service.revoke_link(tenant_id, link.id)

        latest = await service.get_latest_link(tenant_id, lesson.id)

        assert latest is None


class TestResolve:
    """Проверки разрешения токена публичной ссылки."""

    async def test_resolve_returns_shared_lesson(
        self,
        repo: Repository,
        tenant_id: int,
        service: ShareLinkService,
    ) -> None:
        """Действующий токен возвращает занятие, ученика и флаги."""
        entity = await repo.entities.create(tenant_id=tenant_id, name="Аня", entity_type="student")
        lesson = await _make_lesson(repo, tenant_id, entity.id)
        link = await service.create_link(tenant_id, lesson.id)
        assert link is not None

        shared = await service.resolve(link.token)

        assert isinstance(shared, SharedLesson)
        assert shared.lesson.id == lesson.id
        assert shared.entity.id == entity.id
        assert shared.is_expired is False
        assert shared.is_revoked is False
        assert shared.is_valid is True

    async def test_resolve_unknown_token_returns_none(
        self,
        service: ShareLinkService,
    ) -> None:
        """Неизвестный токен возвращает None."""
        shared = await service.resolve("нет-такого-токена")

        assert shared is None

    async def test_resolve_marks_revoked(
        self,
        repo: Repository,
        tenant_id: int,
        service: ShareLinkService,
    ) -> None:
        """Отозванная ссылка помечается флагом ``is_revoked``."""
        entity = await repo.entities.create(tenant_id=tenant_id, name="Аня", entity_type="student")
        lesson = await _make_lesson(repo, tenant_id, entity.id)
        link = await service.create_link(tenant_id, lesson.id)
        assert link is not None
        await service.revoke_link(tenant_id, link.id)

        shared = await service.resolve(link.token)

        assert shared is not None
        assert shared.is_revoked is True
        assert shared.is_valid is False

    async def test_resolve_marks_expired(
        self,
        repo: Repository,
        tenant_id: int,
        service: ShareLinkService,
    ) -> None:
        """Истёкшая ссылка помечается флагом ``is_expired``."""
        entity = await repo.entities.create(tenant_id=tenant_id, name="Аня", entity_type="student")
        lesson = await _make_lesson(repo, tenant_id, entity.id)
        link = await repo.share_links.create(
            tenant_id=tenant_id,
            lesson_id=lesson.id,
            expires_at=datetime(2020, 1, 1, tzinfo=UTC),
        )
        await repo.session.flush()

        shared = await service.resolve(link.token)

        assert shared is not None
        assert shared.is_expired is True
        assert shared.is_valid is False

    async def test_resolve_keeps_internal_notes_in_model(
        self,
        repo: Repository,
        tenant_id: int,
        service: ShareLinkService,
    ) -> None:
        """Сервис отдаёт занятие целиком — фильтрация заметок на веб-слое."""
        entity = await repo.entities.create(tenant_id=tenant_id, name="Аня", entity_type="student")
        lesson = await _make_lesson(repo, tenant_id, entity.id)
        link = await service.create_link(tenant_id, lesson.id)
        assert link is not None

        shared = await service.resolve(link.token)

        assert shared is not None
        assert shared.lesson.internal_notes == "Секретная заметка тренера"

    async def test_resolve_scoped_by_token(
        self,
        repo: Repository,
        tenant_id: int,
        service: ShareLinkService,
    ) -> None:
        """Токен одного занятия не открывает другое."""
        entity = await repo.entities.create(tenant_id=tenant_id, name="Аня", entity_type="student")
        first = await _make_lesson(repo, tenant_id, entity.id, topic="Первая тема")
        second = await _make_lesson(repo, tenant_id, entity.id, topic="Вторая тема")
        link = await service.create_link(tenant_id, first.id)
        assert link is not None

        shared = await service.resolve(link.token)

        assert shared is not None
        assert shared.lesson.id == first.id
        assert shared.lesson.id != second.id


class TestDaysLeft:
    """Проверки расчёта оставшихся дней."""

    async def test_days_left_none_for_indefinite(
        self,
        repo: Repository,
        tenant_id: int,
        service: ShareLinkService,
    ) -> None:
        """Бессрочная ссылка возвращает None."""
        entity = await repo.entities.create(tenant_id=tenant_id, name="Аня", entity_type="student")
        lesson = await _make_lesson(repo, tenant_id, entity.id)
        link = await service.create_link(tenant_id, lesson.id, ttl_days=None)
        assert link is not None

        assert service.days_left(link) is None

    async def test_days_left_zero_for_expired(
        self,
        repo: Repository,
        tenant_id: int,
        service: ShareLinkService,
    ) -> None:
        """Истёкшая ссылка возвращает 0."""
        entity = await repo.entities.create(tenant_id=tenant_id, name="Аня", entity_type="student")
        lesson = await _make_lesson(repo, tenant_id, entity.id)
        link = await repo.share_links.create(
            tenant_id=tenant_id,
            lesson_id=lesson.id,
            expires_at=datetime(2020, 1, 1, tzinfo=UTC),
        )
        await repo.session.flush()

        assert service.days_left(link) == 0

    async def test_days_left_counts_remaining(
        self,
        repo: Repository,
        tenant_id: int,
        service: ShareLinkService,
    ) -> None:
        """Для будущей даты возвращается число полных дней."""
        entity = await repo.entities.create(tenant_id=tenant_id, name="Аня", entity_type="student")
        lesson = await _make_lesson(repo, tenant_id, entity.id)
        link = await repo.share_links.create(
            tenant_id=tenant_id,
            lesson_id=lesson.id,
            expires_at=datetime.now(UTC) + timedelta(days=5),
        )
        await repo.session.flush()

        days_left = service.days_left(link)

        assert days_left is not None
        assert 4 <= days_left <= 5

    async def test_days_left_handles_naive_datetime(
        self,
        repo: Repository,
        tenant_id: int,
        service: ShareLinkService,
    ) -> None:
        """Naive-дата из SQLite трактуется как UTC."""
        entity = await repo.entities.create(tenant_id=tenant_id, name="Аня", entity_type="student")
        lesson = await _make_lesson(repo, tenant_id, entity.id)
        link = await repo.share_links.create(
            tenant_id=tenant_id,
            lesson_id=lesson.id,
            expires_at=datetime(2020, 1, 1),
        )
        await repo.session.flush()

        assert service.days_left(link) == 0


class TestResolveExpiry:
    """Проверки преобразования срока жизни в момент истечения."""

    def test_resolve_expiry_none_for_indefinite(self) -> None:
        """``None`` означает бессрочную ссылку."""
        assert ShareLinkService._resolve_expiry(None) is None

    def test_resolve_expiry_clamps_low(self) -> None:
        """Срок меньше одного дня поднимается до одного дня."""
        expires_at = ShareLinkService._resolve_expiry(0)

        assert expires_at is not None
        assert expires_at > datetime.now(UTC)

    def test_resolve_expiry_clamps_high(self) -> None:
        """Срок больше максимума обрезается до ``MAX_TTL_DAYS``."""
        expires_at = ShareLinkService._resolve_expiry(10_000)

        assert expires_at is not None
        delta = expires_at - datetime.now(UTC)
        assert delta.days <= MAX_TTL_DAYS
