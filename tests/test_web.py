"""Интеграционные тесты веб-роутов PlanZy.

Проверяют публичную карточку ``/l/{token}``, досье ``/dossier/{token}``,
health-check ``/health`` и обработку неизвестных токенов.

Тестовая БД подменяется через ``dependency_overrides``: приложение
получает ту же in-memory сессию, что и фикстуры pytest.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repository import Repository
from app.web.app import create_app
from app.web.dependencies import get_db_session


@pytest_asyncio.fixture
async def client(session: AsyncSession) -> AsyncIterator[AsyncClient]:
    """HTTP-клиент поверх приложения с подменённой сессией БД.

    Args:
        session: Тестовая in-memory сессия из conftest.

    Yields:
        Асинхронный httpx-клиент, работающий напрямую с ASGI-приложением.
    """
    app = create_app()

    async def _override_session() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_db_session] = _override_session

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http_client:
        yield http_client

    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def student(repo: Repository, tenant_id: int):
    """Создаёт ученика с уроками и платежом.

    Returns:
        Кортеж ``(entity, token)``.
    """
    entity = await repo.entities.create(
        tenant_id=tenant_id,
        name="Миша",
        entity_type="student",
        subject="Шахматы",
        parent_phone="972541234567",
    )
    await repo.lessons.create(
        tenant_id=tenant_id,
        entity_id=entity.id,
        lesson_date=date(2026, 9, 15),
        topic="Итальянская партия",
        homework="Решить 10 задач на мат в 2 хода",
        internal_notes="Секретная заметка тренера",
        rating="4",
        next_step="Разобрать дебютные ловушки",
        attendance="present",
    )
    await repo.lessons.create(
        tenant_id=tenant_id,
        entity_id=entity.id,
        lesson_date=date(2026, 9, 10),
        topic="Двойной удар",
        homework="Повторить вилки",
        rating="3",
        attendance="present",
    )
    await repo.payments.create(
        tenant_id=tenant_id,
        entity_id=entity.id,
        amount=1200.0,
        currency="ILS",
        lessons_added=8,
        payment_date=date(2026, 9, 1),
    )
    await repo.session.flush()
    return entity, entity.public_token


class TestHealthRoute:
    """Проверки health-check."""

    async def test_health_returns_ok(self, client: AsyncClient) -> None:
        """Health-check отвечает 200 и статусом ok."""
        response = await client.get("/health")

        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "ok"
        assert payload["app"] == "PlanZy"
        assert payload["database"] == "ok"


class TestIndexRoute:
    """Проверки корневой страницы."""

    async def test_index_renders(self, client: AsyncClient) -> None:
        """Корневая страница отдаёт HTML с названием сервиса."""
        response = await client.get("/")

        assert response.status_code == 200
        assert "PlanZy" in response.text


class TestPublicCard:
    """Проверки публичной карточки ученика."""

    async def test_card_shows_name_and_homework(
        self,
        client: AsyncClient,
        student,
    ) -> None:
        """Карточка показывает имя и домашнее задание."""
        entity, token = student

        response = await client.get(f"/l/{token}")

        assert response.status_code == 200
        assert entity.name in response.text
        assert "Решить 10 задач на мат в 2 хода" in response.text
        assert "Итальянская партия" in response.text

    async def test_card_hides_internal_notes(
        self,
        client: AsyncClient,
        student,
    ) -> None:
        """Приватные заметки тренера не попадают на публичную страницу."""
        _entity, token = student

        response = await client.get(f"/l/{token}")

        assert response.status_code == 200
        assert "Секретная заметка тренера" not in response.text

    async def test_card_unknown_token_returns_404(self, client: AsyncClient) -> None:
        """Неизвестный токен отдаёт стилизованную 404."""
        response = await client.get("/l/does-not-exist")

        assert response.status_code == 404
        assert "не найдена" in response.text.lower()

    async def test_card_shows_schedule_from_slots(
        self,
        client: AsyncClient,
        repo: Repository,
        tenant_id: int,
        student,
    ) -> None:
        """Расписание берётся из schedule_slots, а не из устаревших полей."""
        entity, token = student
        await repo.schedule.create(
            tenant_id=tenant_id,
            entity_id=entity.id,
            weekday="tue",
            start_time="17:30",
            duration_min=45,
            location="Кабинет 3",
        )
        await repo.session.flush()

        response = await client.get(f"/l/{token}")

        assert response.status_code == 200
        assert "Вторник" in response.text
        assert "17:30" in response.text
        assert "45 мин" in response.text
        assert "Кабинет 3" in response.text

    async def test_card_hides_inactive_slots(
        self,
        client: AsyncClient,
        repo: Repository,
        tenant_id: int,
        student,
    ) -> None:
        """Неактивные слоты не показываются родителю."""
        entity, token = student
        await repo.schedule.create(
            tenant_id=tenant_id,
            entity_id=entity.id,
            weekday="wed",
            start_time="10:00",
            is_active=False,
        )
        await repo.session.flush()

        response = await client.get(f"/l/{token}")

        assert response.status_code == 200
        assert "10:00" not in response.text

    async def test_card_hides_price_and_payment_instructions(
        self,
        client: AsyncClient,
        repo: Repository,
        tenant_id: int,
        student,
    ) -> None:
        """Карточка родителя не показывает цену, валюту и порядок оплаты."""
        _entity, token = student
        await repo.tenants.update_settings(
            tenant_id,
            lesson_price=150.0,
            currency="ILS",
            payment_instructions="Перевод на карту 1234",
        )
        await repo.session.flush()

        response = await client.get(f"/l/{token}")

        assert response.status_code == 200
        assert "Оплата занятий" not in response.text
        assert "Перевод на карту 1234" not in response.text
        assert "Стоимость занятия" not in response.text

    async def test_card_hides_lesson_history(
        self,
        client: AsyncClient,
        student,
    ) -> None:
        """Раздел «История занятий» убран из карточки родителя."""
        _entity, token = student

        response = await client.get(f"/l/{token}")

        assert response.status_code == 200
        assert "История занятий" not in response.text

    async def test_card_shows_note_from_last_lesson(
        self,
        client: AsyncClient,
        repo: Repository,
        tenant_id: int,
        student,
    ) -> None:
        """Примечание последнего занятия показывается родителю как есть."""
        entity, token = student
        await repo.lessons.create(
            tenant_id=tenant_id,
            entity_id=entity.id,
            lesson_date=date(2026, 9, 17),
            topic="Ладейные окончания",
            attendance="Миша, Аня",
        )
        await repo.session.flush()

        response = await client.get(f"/l/{token}")

        assert response.status_code == 200
        assert "Примечание" in response.text
        assert "Миша, Аня" in response.text

    async def test_card_has_no_note_block_without_note(
        self,
        client: AsyncClient,
        repo: Repository,
        tenant_id: int,
    ) -> None:
        """Без занятий с примечанием раздел «Примечание» не рисуется."""
        entity = await repo.entities.create(
            tenant_id=tenant_id,
            name="Лев",
            entity_type="student",
            subject="Шахматы",
        )
        await repo.session.flush()

        response = await client.get(f"/l/{entity.public_token}")

        assert response.status_code == 200
        assert "Примечание" not in response.text

    async def test_card_has_no_balance_block(
        self,
        client: AsyncClient,
        student,
    ) -> None:
        """Карточка родителя больше не показывает баланс занятий."""
        _entity, token = student

        response = await client.get(f"/l/{token}")

        assert response.status_code == 200
        assert "Баланс" not in response.text
        assert "пополнить баланс" not in response.text


class TestDossier:
    """Проверки страницы досье для тренера."""

    async def test_dossier_shows_history_and_metrics(
        self,
        client: AsyncClient,
        student,
    ) -> None:
        """Досье содержит историю тем, метрики и план."""
        entity, token = student

        response = await client.get(f"/dossier/{token}")

        assert response.status_code == 200
        assert entity.name in response.text
        assert "Итальянская партия" in response.text
        assert "Двойной удар" in response.text
        assert "Разобрать дебютные ловушки" in response.text

    async def test_dossier_unknown_token_returns_404(self, client: AsyncClient) -> None:
        """Неизвестный токен досье отдаёт 404."""
        response = await client.get("/dossier/nope")

        assert response.status_code == 404
        assert "не найдено" in response.text.lower()


class TestSharedLesson:
    """Проверки публичной страницы одного занятия ``/lesson/{token}``."""

    async def test_lesson_shows_homework_and_plan(
        self,
        client: AsyncClient,
        repo: Repository,
        tenant_id: int,
        student,
    ) -> None:
        """Страница занятия показывает тему, ДЗ, оценку и план."""
        entity, _ = student
        lesson = await repo.lessons.get_last_lesson(tenant_id, entity.id)
        assert lesson is not None
        link = await repo.share_links.create(tenant_id=tenant_id, lesson_id=lesson.id)
        await repo.session.flush()

        response = await client.get(f"/lesson/{link.token}")

        assert response.status_code == 200
        assert entity.name in response.text
        assert "Итальянская партия" in response.text
        assert "Решить 10 задач на мат в 2 хода" in response.text
        assert "Разобрать дебютные ловушки" in response.text

    async def test_lesson_hides_internal_notes(
        self,
        client: AsyncClient,
        repo: Repository,
        tenant_id: int,
        student,
    ) -> None:
        """Приватные заметки тренера не попадают на публичную страницу."""
        entity, _ = student
        lesson = await repo.lessons.get_last_lesson(tenant_id, entity.id)
        assert lesson is not None
        link = await repo.share_links.create(tenant_id=tenant_id, lesson_id=lesson.id)
        await repo.session.flush()

        response = await client.get(f"/lesson/{link.token}")

        assert response.status_code == 200
        assert "Секретная заметка тренера" not in response.text

    async def test_lesson_unknown_token_returns_404(self, client: AsyncClient) -> None:
        """Неизвестный токен ссылки на занятие отдаёт 404."""
        response = await client.get("/lesson/nope")

        assert response.status_code == 404
        assert "не найдено" in response.text.lower()

    async def test_lesson_revoked_link_returns_410(
        self,
        client: AsyncClient,
        repo: Repository,
        tenant_id: int,
        student,
    ) -> None:
        """Отозванная ссылка показывает страницу «ссылка недействительна»."""
        entity, _ = student
        lesson = await repo.lessons.get_last_lesson(tenant_id, entity.id)
        assert lesson is not None
        link = await repo.share_links.create(tenant_id=tenant_id, lesson_id=lesson.id)
        await repo.session.flush()
        await repo.share_links.revoke(tenant_id, link.id)
        await repo.session.flush()

        response = await client.get(f"/lesson/{link.token}")

        assert response.status_code == 410
        assert "отозвана" in response.text.lower()

    async def test_lesson_expired_link_returns_410(
        self,
        client: AsyncClient,
        repo: Repository,
        tenant_id: int,
        student,
    ) -> None:
        """Истёкшая ссылка показывает страницу «ссылка недействительна»."""
        entity, _ = student
        lesson = await repo.lessons.get_last_lesson(tenant_id, entity.id)
        assert lesson is not None
        link = await repo.share_links.create(
            tenant_id=tenant_id,
            lesson_id=lesson.id,
            expires_at=datetime(2020, 1, 1, tzinfo=UTC),
        )
        await repo.session.flush()

        response = await client.get(f"/lesson/{link.token}")

        assert response.status_code == 410
        assert "истёк" in response.text.lower()

    async def test_lesson_expired_page_shows_expiry_date(
        self,
        client: AsyncClient,
        repo: Repository,
        tenant_id: int,
        student,
    ) -> None:
        """Страница «ссылка истекла» показывает дату окончания действия."""
        entity, _ = student
        lesson = await repo.lessons.get_last_lesson(tenant_id, entity.id)
        assert lesson is not None
        link = await repo.share_links.create(
            tenant_id=tenant_id,
            lesson_id=lesson.id,
            expires_at=datetime(2020, 1, 1, tzinfo=UTC),
        )
        await repo.session.flush()

        response = await client.get(f"/lesson/{link.token}")

        assert response.status_code == 410
        assert "1 января 2020" in response.text

    async def test_lesson_scoped_to_tenant(
        self,
        client: AsyncClient,
        repo: Repository,
        tenant_id: int,
        student,
    ) -> None:
        """Ссылка на занятие другого тенанта недоступна."""
        entity, _ = student
        lesson = await repo.lessons.get_last_lesson(tenant_id, entity.id)
        assert lesson is not None
        await repo.share_links.create(tenant_id=tenant_id, lesson_id=lesson.id)
        await repo.session.flush()

        other = await repo.tenants.create(name="Другой тренер", telegram_user_id=2002)
        await repo.session.flush()
        other_entity = await repo.entities.create(
            tenant_id=other.id,
            name="Чужой ученик",
            entity_type="student",
        )
        other_lesson = await repo.lessons.create(
            tenant_id=other.id,
            entity_id=other_entity.id,
            lesson_date=date(2026, 9, 15),
            topic="Чужая тема",
        )
        await repo.session.flush()
        other_link = await repo.share_links.create(
            tenant_id=other.id,
            lesson_id=other_lesson.id,
        )
        await repo.session.flush()

        response = await client.get(f"/lesson/{other_link.token}")

        assert response.status_code == 200
        assert "Чужая тема" in response.text
        assert "Итальянская партия" not in response.text


class TestUnknownRoute:
    """Проверки обработки несуществующих маршрутов."""

    async def test_unknown_path_returns_404(self, client: AsyncClient) -> None:
        """Любой неизвестный путь отдаёт страницу 404."""
        response = await client.get("/totally/unknown")

        assert response.status_code == 404
        assert "не найдена" in response.text.lower()


@pytest.mark.parametrize("token", ["", " ", "x" * 200])
async def test_card_rejects_bad_tokens(client: AsyncClient, token: str) -> None:
    """Некорректные токены не приводят к 500."""
    response = await client.get(f"/l/{token}")

    assert response.status_code in (404, 422)
