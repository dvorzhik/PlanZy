"""Тесты раздела кабинета «Занятия» (``/admin/lessons``).

Проверяют журнал занятий с фильтрами по ученику и периоду, ручное
создание занятия, карточку с отчётом родителю, редактирование полей
и удаление занятия.

Тестовая БД подменяется через ``dependency_overrides``: приложение
получает ту же in-memory сессию, что и фикстуры pytest. Вход в кабинет
выполняется «как в жизни» — через ``GET /admin/{admin_token}``, чтобы
cookie-сессию выставило само приложение.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repository import Repository
from app.web.admin_lessons_routes import (
    DEFAULT_PERIOD_DAYS,
    MAX_PERIOD_DAYS,
    PERIOD_OPTIONS,
    _build_report,
    _clean,
    _filter_lessons,
    _parse_date,
    _parse_int,
    _parse_time,
    _resolve_period,
)
from app.web.app import create_app
from app.web.dependencies import get_db_session

# Даты занятий для проверок (сентябрь 2026).
LESSON_DATE = date(2026, 9, 14)
SECOND_LESSON_DATE = date(2026, 9, 16)
OLD_LESSON_DATE = date(2026, 1, 5)


@pytest_asyncio.fixture
async def cabinet_token(repo: Repository, tenant_id: int) -> str:
    """Секретный токен кабинета для тестового тенанта.

    Имя фикстуры намеренно отличается от ``admin_token`` — иначе оно
    конфликтует с именем path-параметра роута ``/admin/{admin_token}``.

    Args:
        repo: Фасад репозиториев.
        tenant_id: Идентификатор тестового тенанта.

    Returns:
        Строка-токен кабинета.
    """
    token = await repo.tenants.ensure_admin_token(tenant_id)
    assert token is not None
    return token


@pytest_asyncio.fixture
async def client(session: AsyncSession, cabinet_token: str) -> AsyncIterator[AsyncClient]:
    """HTTP-клиент с уже установленной cookie-сессией тренера.

    Вход выполняется через реальный роут ``GET /admin/{cabinet_token}``,
    поэтому cookie выставляет само приложение.

    Args:
        session: Тестовая in-memory сессия из conftest.
        cabinet_token: Секретный токен кабинета.

    Yields:
        Асинхронный httpx-клиент, авторизованный в кабинете.
    """
    app = create_app()

    async def _override_session() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_db_session] = _override_session

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http_client:
        login = await http_client.get(f"/admin/{cabinet_token}", follow_redirects=False)
        assert login.status_code in {302, 303, 307}
        yield http_client

    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def anon_client(session: AsyncSession) -> AsyncIterator[AsyncClient]:
    """HTTP-клиент без cookie-сессии (анонимный тренер).

    Args:
        session: Тестовая in-memory сессия из conftest.

    Yields:
        Асинхронный httpx-клиент без авторизации.
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
    """Ученик с ненулевым балансом для проверок журнала.

    Args:
        repo: Фасад репозиториев.
        tenant_id: Идентификатор тестового тенанта.

    Returns:
        Созданный ученик.
    """
    entity = await repo.entities.create(
        tenant_id=tenant_id,
        name="Аня",
        entity_type="student",
    )
    await repo.session.flush()
    return entity


@pytest_asyncio.fixture
async def second_student(repo: Repository, tenant_id: int):
    """Второй ученик — для проверки фильтра по ученику.

    Args:
        repo: Фасад репозиториев.
        tenant_id: Идентификатор тестового тенанта.

    Returns:
        Созданный ученик.
    """
    entity = await repo.entities.create(
        tenant_id=tenant_id,
        name="Боря",
        entity_type="student",
    )
    await repo.session.flush()
    return entity


async def _make_lesson(
    repo: Repository,
    tenant_id: int,
    entity_id: int,
    *,
    lesson_date: date = LESSON_DATE,
    topic: str = "Дроби",
    attendance: str | None = "present",
    homework: str | None = None,
    rating: str | None = None,
    next_step: str | None = None,
    internal_notes: str | None = None,
):
    """Создаёт занятие напрямую через репозиторий.

    Args:
        repo: Фасад репозиториев.
        tenant_id: Идентификатор тенанта.
        entity_id: Идентификатор ученика.
        lesson_date: Дата занятия.
        topic: Тема занятия.
        attendance: Посещаемость.
        homework: Домашнее задание.
        rating: Оценка.
        next_step: План на следующий урок.
        internal_notes: Приватные заметки тренера.

    Returns:
        Созданное занятие.
    """
    lesson = await repo.lessons.create(
        tenant_id=tenant_id,
        entity_id=entity_id,
        lesson_date=lesson_date,
        topic=topic,
        homework=homework,
        rating=rating,
        next_step=next_step,
        internal_notes=internal_notes,
        attendance=attendance,
    )
    await repo.session.flush()
    return lesson


class TestLessonsList:
    """Проверки журнала занятий и фильтров."""

    async def test_list_renders_empty_state(self, client: AsyncClient) -> None:
        """Пустой журнал показывает заглушку."""
        response = await client.get("/admin/lessons")
        assert response.status_code == 200
        assert "Занятия" in response.text

    async def test_list_shows_created_lessons(
        self, client: AsyncClient, repo: Repository, tenant_id: int, student
    ) -> None:
        """Созданные занятия попадают в журнал."""
        await _make_lesson(repo, tenant_id, student.id, topic="Дроби")
        response = await client.get("/admin/lessons")
        assert response.status_code == 200
        assert "Дроби" in response.text
        assert "Аня" in response.text

    async def test_list_shows_total(
        self, client: AsyncClient, repo: Repository, tenant_id: int, student
    ) -> None:
        """Журнал показывает количество найденных занятий."""
        await _make_lesson(repo, tenant_id, student.id)
        await _make_lesson(
            repo, tenant_id, student.id, lesson_date=SECOND_LESSON_DATE, topic="Уравнения"
        )
        response = await client.get("/admin/lessons")
        assert response.status_code == 200
        assert "Уравнения" in response.text

    async def test_list_filters_by_entity(
        self,
        client: AsyncClient,
        repo: Repository,
        tenant_id: int,
        student,
        second_student,
    ) -> None:
        """Фильтр по ученику скрывает чужие занятия."""
        await _make_lesson(repo, tenant_id, student.id, topic="Дроби")
        await _make_lesson(repo, tenant_id, second_student.id, topic="Физика")
        response = await client.get(f"/admin/lessons?entity_id={student.id}")
        assert response.status_code == 200
        assert "Дроби" in response.text
        assert "Физика" not in response.text

    async def test_list_ignores_unknown_entity(
        self, client: AsyncClient, repo: Repository, tenant_id: int, student
    ) -> None:
        """Несуществующий ученик сбрасывает фильтр, а не ломает страницу."""
        await _make_lesson(repo, tenant_id, student.id, topic="Дроби")
        response = await client.get("/admin/lessons?entity_id=999999")
        assert response.status_code == 200
        assert "Дроби" in response.text

    async def test_list_filters_by_period(
        self, client: AsyncClient, repo: Repository, tenant_id: int, student
    ) -> None:
        """Старое занятие не попадает в период «за неделю»."""
        await _make_lesson(repo, tenant_id, student.id, lesson_date=OLD_LESSON_DATE, topic="Старое")
        response = await client.get("/admin/lessons?period=week")
        assert response.status_code == 200
        assert "Старое" not in response.text

    async def test_list_accepts_custom_dates(
        self, client: AsyncClient, repo: Repository, tenant_id: int, student
    ) -> None:
        """Явные даты задают произвольный период."""
        await _make_lesson(repo, tenant_id, student.id, lesson_date=OLD_LESSON_DATE, topic="Старое")
        response = await client.get("/admin/lessons?date_from=2026-01-01&date_to=2026-01-31")
        assert response.status_code == 200
        assert "Старое" in response.text

    async def test_list_swaps_reversed_dates(
        self, client: AsyncClient, repo: Repository, tenant_id: int, student
    ) -> None:
        """Перепутанные местами даты не ломают фильтр."""
        await _make_lesson(repo, tenant_id, student.id, lesson_date=OLD_LESSON_DATE, topic="Старое")
        response = await client.get("/admin/lessons?date_from=2026-01-31&date_to=2026-01-01")
        assert response.status_code == 200
        assert "Старое" in response.text

    async def test_list_shows_saved_banner(self, client: AsyncClient) -> None:
        """Флаг ``saved`` показывает баннер об успехе."""
        response = await client.get("/admin/lessons?saved=1")
        assert response.status_code == 200
        assert "Сохранено" in response.text or "сохран" in response.text.lower()

    async def test_list_shows_error_banner(self, client: AsyncClient) -> None:
        """Текст ошибки выводится в баннере."""
        response = await client.get("/admin/lessons?error=Что-то+пошло+не+так")
        assert response.status_code == 200
        assert "Что-то пошло не так" in response.text

    async def test_list_scoped_to_tenant(
        self, client: AsyncClient, repo: Repository, student
    ) -> None:
        """Журнал не показывает занятия чужого тенанта."""
        other = await repo.tenants.create(name="Чужой", telegram_user_id=4001)
        await repo.session.flush()
        foreign = await repo.entities.create(
            tenant_id=other.id, name="Чужой ученик", entity_type="student"
        )
        await repo.session.flush()
        await _make_lesson(repo, other.id, foreign.id, topic="Чужое занятие")

        response = await client.get("/admin/lessons")
        assert response.status_code == 200
        assert "Чужое занятие" not in response.text

    async def test_list_requires_session(self, anon_client: AsyncClient) -> None:
        """Без cookie-сессии журнал недоступен."""
        response = await anon_client.get("/admin/lessons", follow_redirects=False)
        assert response.status_code == 401


class TestLessonCreate:
    """Проверки ручного создания занятия."""

    async def test_new_form_renders(self, client: AsyncClient) -> None:
        """Форма создания занятия открывается."""
        response = await client.get("/admin/lessons/new")
        assert response.status_code == 200
        assert "Новое занятие" in response.text

    async def test_new_form_labels_attendance_as_note(self, client: AsyncClient) -> None:
        """Поле посещаемости подписано как «Примечание»."""
        response = await client.get("/admin/lessons/new")

        assert response.status_code == 200
        assert "Примечание" in response.text
        assert "Посещаемость" not in response.text

    async def test_new_form_embeds_group_members(
        self, client: AsyncClient, repo: Repository, tenant_id: int
    ) -> None:
        """Форма занятия отдаёт участников группы для автоподстановки."""
        group = await repo.entities.create(
            tenant_id=tenant_id,
            name="Группа А",
            entity_type="group",
            members="Аня, Марк",
        )
        await repo.session.flush()

        response = await client.get("/admin/lessons/new")
        assert response.status_code == 200
        # ``tojson`` экранирует кириллицу в \\uXXXX, поэтому проверяем ключ и данные.
        assert f'"{group.id}"' in response.text
        assert "\\u0410\\u043d\\u044f" in response.text
        assert "\\u041c\\u0430\\u0440\\u043a" in response.text

    async def test_create_lesson_redirects(
        self, client: AsyncClient, repo: Repository, tenant_id: int, student
    ) -> None:
        """Успешное создание ведёт в журнал с флагом ``saved``."""
        response = await client.post(
            "/admin/lessons",
            data={
                "entity_id": str(student.id),
                "lesson_date": LESSON_DATE.isoformat(),
                "topic": "Дроби",
                "attendance": "present",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/admin/lessons?saved=1"

        lessons = await repo.lessons.list_by_entity(tenant_id, student.id)
        assert len(lessons) == 1
        assert lessons[0].topic == "Дроби"

    async def test_create_without_entity_returns_error(self, client: AsyncClient) -> None:
        """Без выбранного ученика форма возвращает ошибку."""
        response = await client.post(
            "/admin/lessons",
            data={"topic": "Дроби"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert "error=" in response.headers["location"]

    async def test_create_unknown_entity_returns_error(self, client: AsyncClient) -> None:
        """Несуществующий ученик возвращает ошибку."""
        response = await client.post(
            "/admin/lessons",
            data={"entity_id": "999999", "topic": "Дроби"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert "error=" in response.headers["location"]

    async def test_create_defaults_topic(
        self, client: AsyncClient, repo: Repository, tenant_id: int, student
    ) -> None:
        """Пустая тема заменяется прочерком."""
        await client.post(
            "/admin/lessons",
            data={"entity_id": str(student.id), "topic": "   "},
            follow_redirects=False,
        )
        lessons = await repo.lessons.list_by_entity(tenant_id, student.id)
        assert lessons[0].topic == "—"

    async def test_create_stores_free_text_attendance(
        self, client: AsyncClient, repo: Repository, tenant_id: int, student
    ) -> None:
        """Посещаемость — свободный текст и сохраняется как есть."""
        await client.post(
            "/admin/lessons",
            data={
                "entity_id": str(student.id),
                "topic": "Дроби",
                "attendance": "Аня, Марк",
            },
            follow_redirects=False,
        )
        lessons = await repo.lessons.list_by_entity(tenant_id, student.id)
        assert lessons[0].attendance == "Аня, Марк"

    async def test_create_saves_start_time(
        self, client: AsyncClient, repo: Repository, tenant_id: int, student
    ) -> None:
        """Время начала занятия сохраняется из формы."""
        await client.post(
            "/admin/lessons",
            data={
                "entity_id": str(student.id),
                "lesson_date": LESSON_DATE.isoformat(),
                "start_time": "17:30",
                "topic": "Дроби",
                "attendance": "present",
            },
            follow_redirects=False,
        )
        lessons = await repo.lessons.list_by_entity(tenant_id, student.id)
        assert lessons[0].start_time == "17:30"

    async def test_create_blank_start_time_is_none(
        self, client: AsyncClient, repo: Repository, tenant_id: int, student
    ) -> None:
        """Пустое время сохраняется как ``None``."""
        await client.post(
            "/admin/lessons",
            data={
                "entity_id": str(student.id),
                "lesson_date": LESSON_DATE.isoformat(),
                "start_time": "   ",
                "topic": "Дроби",
            },
            follow_redirects=False,
        )
        lessons = await repo.lessons.list_by_entity(tenant_id, student.id)
        assert lessons[0].start_time is None

    async def test_create_invalid_start_time_is_none(
        self, client: AsyncClient, repo: Repository, tenant_id: int, student
    ) -> None:
        """Некорректное время не ломает создание и сохраняется как ``None``."""
        await client.post(
            "/admin/lessons",
            data={
                "entity_id": str(student.id),
                "lesson_date": LESSON_DATE.isoformat(),
                "start_time": "не-время",
                "topic": "Дроби",
            },
            follow_redirects=False,
        )
        lessons = await repo.lessons.list_by_entity(tenant_id, student.id)
        assert lessons[0].start_time is None

    async def test_create_writes_audit_log(
        self, client: AsyncClient, repo: Repository, tenant_id: int, student
    ) -> None:
        """Создание занятия пишется в журнал действий."""
        await client.post(
            "/admin/lessons",
            data={
                "entity_id": str(student.id),
                "topic": "Дроби",
                "attendance": "present",
            },
            follow_redirects=False,
        )
        entries = await repo.audit.list_by_tenant(tenant_id)
        assert any(entry.action == "lesson_created" for entry in entries)

    async def test_create_requires_session(self, anon_client: AsyncClient, student) -> None:
        """Без cookie-сессии создание недоступно."""
        response = await anon_client.post(
            "/admin/lessons",
            data={"entity_id": str(student.id), "topic": "Дроби"},
            follow_redirects=False,
        )
        assert response.status_code == 401


class TestLessonCard:
    """Проверки карточки занятия."""

    async def test_card_renders_lesson(
        self, client: AsyncClient, repo: Repository, tenant_id: int, student
    ) -> None:
        """Карточка показывает тему и ученика."""
        lesson = await _make_lesson(repo, tenant_id, student.id, topic="Дроби")
        response = await client.get(f"/admin/lessons/{lesson.id}")
        assert response.status_code == 200
        assert "Дроби" in response.text
        assert "Аня" in response.text

    async def test_card_shows_report(
        self, client: AsyncClient, repo: Repository, tenant_id: int, student
    ) -> None:
        """Карточка содержит отчёт родителю."""
        lesson = await _make_lesson(
            repo,
            tenant_id,
            student.id,
            topic="Дроби",
            homework="Стр. 12",
            rating="5",
        )
        response = await client.get(f"/admin/lessons/{lesson.id}")
        assert response.status_code == 200
        assert "Стр. 12" in response.text

    async def test_card_hides_internal_notes_from_report(
        self, repo: Repository, tenant_id: int, student
    ) -> None:
        """Приватные заметки не попадают в отчёт родителю."""
        lesson = await _make_lesson(
            repo,
            tenant_id,
            student.id,
            topic="Дроби",
            internal_notes="Секретная заметка",
        )
        report = _build_report(lesson, student, currency="₪")
        assert "Секретная заметка" not in report

    async def test_card_unknown_redirects(self, client: AsyncClient) -> None:
        """Несуществующее занятие ведёт в журнал с ошибкой."""
        response = await client.get("/admin/lessons/999999", follow_redirects=False)
        assert response.status_code == 303
        assert "error=" in response.headers["location"]

    async def test_card_scoped_to_tenant(self, client: AsyncClient, repo: Repository) -> None:
        """Занятие чужого тенанта недоступно."""
        other = await repo.tenants.create(name="Чужой", telegram_user_id=4002)
        await repo.session.flush()
        foreign = await repo.entities.create(
            tenant_id=other.id, name="Чужой ученик", entity_type="student"
        )
        await repo.session.flush()
        lesson = await _make_lesson(repo, other.id, foreign.id, topic="Чужое")

        response = await client.get(f"/admin/lessons/{lesson.id}", follow_redirects=False)
        assert response.status_code == 303
        assert "error=" in response.headers["location"]

    async def test_card_requires_session(
        self, anon_client: AsyncClient, repo: Repository, tenant_id: int, student
    ) -> None:
        """Без cookie-сессии карточка недоступна."""
        lesson = await _make_lesson(repo, tenant_id, student.id)
        response = await anon_client.get(f"/admin/lessons/{lesson.id}", follow_redirects=False)
        assert response.status_code == 401


class TestLessonShare:
    """Проверки публичной ссылки на занятие из карточки."""

    async def test_card_shows_share_form_without_link(
        self, client: AsyncClient, repo: Repository, tenant_id: int, student
    ) -> None:
        """Пока ссылки нет — карточка предлагает её создать."""
        lesson = await _make_lesson(repo, tenant_id, student.id, topic="Дроби")
        response = await client.get(f"/admin/lessons/{lesson.id}")
        assert response.status_code == 200
        assert f"/admin/lessons/{lesson.id}/share" in response.text
        assert "Создать ссылку" in response.text

    async def test_share_create_redirects_and_persists(
        self, client: AsyncClient, repo: Repository, tenant_id: int, student
    ) -> None:
        """POST создаёт ссылку и возвращает на карточку."""
        lesson = await _make_lesson(repo, tenant_id, student.id, topic="Дроби")
        response = await client.post(
            f"/admin/lessons/{lesson.id}/share",
            data={"ttl_days": "7"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert f"/admin/lessons/{lesson.id}?saved=1" in response.headers["location"]

        link = await repo.share_links.get_active_for_lesson(tenant_id, lesson.id)
        assert link is not None
        assert link.expires_at is not None

    async def test_share_create_reuses_active_link(
        self, client: AsyncClient, repo: Repository, tenant_id: int, student
    ) -> None:
        """Повторный POST не создаёт вторую активную ссылку."""
        lesson = await _make_lesson(repo, tenant_id, student.id, topic="Дроби")
        await client.post(f"/admin/lessons/{lesson.id}/share", data={}, follow_redirects=False)
        await client.post(f"/admin/lessons/{lesson.id}/share", data={}, follow_redirects=False)

        links = await repo.share_links.list_by_lesson(tenant_id, lesson.id)
        assert len(links) == 1

    async def test_share_create_unknown_lesson_redirects(self, client: AsyncClient) -> None:
        """Ссылка для несуществующего занятия не создаётся."""
        response = await client.post("/admin/lessons/999999/share", data={}, follow_redirects=False)
        assert response.status_code == 303
        assert "error=" in response.headers["location"]

    async def test_card_shows_link_when_active(
        self, client: AsyncClient, repo: Repository, tenant_id: int, student
    ) -> None:
        """Активная ссылка отображается в карточке вместе с кнопкой отзыва."""
        lesson = await _make_lesson(repo, tenant_id, student.id, topic="Дроби")
        await client.post(f"/admin/lessons/{lesson.id}/share", data={}, follow_redirects=False)

        response = await client.get(f"/admin/lessons/{lesson.id}")
        assert response.status_code == 200
        assert "/lesson/" in response.text
        assert "Отозвать" in response.text

    async def test_card_shows_copy_button_for_active_link(
        self, client: AsyncClient, repo: Repository, tenant_id: int, student
    ) -> None:
        """Активная ссылка сопровождается кнопкой копирования в буфер обмена."""
        lesson = await _make_lesson(repo, tenant_id, student.id, topic="Дроби")
        await client.post(f"/admin/lessons/{lesson.id}/share", data={}, follow_redirects=False)

        response = await client.get(f"/admin/lessons/{lesson.id}")
        assert response.status_code == 200
        assert "Скопировать" in response.text
        assert 'data-copy-target="share-url"' in response.text

    async def test_card_shows_whatsapp_button_for_active_link(
        self, client: AsyncClient, repo: Repository, tenant_id: int, student
    ) -> None:
        """Активная ссылка отправляется родителю в WhatsApp одной кнопкой."""
        await repo.entities.update(tenant_id, student.id, parent_phone="972541234567")
        lesson = await _make_lesson(repo, tenant_id, student.id, topic="Дроби")
        await client.post(f"/admin/lessons/{lesson.id}/share", data={}, follow_redirects=False)

        response = await client.get(f"/admin/lessons/{lesson.id}")
        assert response.status_code == 200
        assert "Отправить в WhatsApp" in response.text
        assert "wa.me/972541234567" in response.text

    async def test_card_hides_whatsapp_button_without_phone(
        self, client: AsyncClient, repo: Repository, tenant_id: int, student
    ) -> None:
        """Без телефона родителя кнопка WhatsApp не показывается."""
        lesson = await _make_lesson(repo, tenant_id, student.id, topic="Дроби")
        await client.post(f"/admin/lessons/{lesson.id}/share", data={}, follow_redirects=False)

        response = await client.get(f"/admin/lessons/{lesson.id}")
        assert response.status_code == 200
        assert "Отправить в WhatsApp" not in response.text

    async def test_card_shows_copy_report_button(
        self, client: AsyncClient, repo: Repository, tenant_id: int, student
    ) -> None:
        """Отчёт можно скопировать одной кнопкой."""
        lesson = await _make_lesson(repo, tenant_id, student.id, topic="Дроби")

        response = await client.get(f"/admin/lessons/{lesson.id}")
        assert response.status_code == 200
        assert "Скопировать отчёт" in response.text
        assert 'data-copy-target="report-text"' in response.text

    async def test_card_group_links_to_chat(
        self, client: AsyncClient, repo: Repository, tenant_id: int
    ) -> None:
        """Для группы кнопка ведёт в сохранённый чат WhatsApp."""
        group = await repo.entities.create(
            tenant_id=tenant_id,
            name="Группа А",
            entity_type="group",
            whatsapp_chat_url="https://chat.whatsapp.com/AbCdEf123",
        )
        lesson = await _make_lesson(repo, tenant_id, group.id, topic="Дроби")

        response = await client.get(f"/admin/lessons/{lesson.id}")
        assert response.status_code == 200
        assert "https://chat.whatsapp.com/AbCdEf123" in response.text
        assert "Открыть чат группы" in response.text

    async def test_card_shows_days_left_for_limited_link(
        self, client: AsyncClient, repo: Repository, tenant_id: int, student
    ) -> None:
        """Для срочной ссылки карточка показывает остаток дней."""
        lesson = await _make_lesson(repo, tenant_id, student.id, topic="Дроби")
        await client.post(
            f"/admin/lessons/{lesson.id}/share",
            data={"ttl_days": "7"},
            follow_redirects=False,
        )

        response = await client.get(f"/admin/lessons/{lesson.id}")
        assert response.status_code == 200
        assert "осталось" in response.text

    async def test_card_marks_expired_link(
        self, client: AsyncClient, repo: Repository, tenant_id: int, student
    ) -> None:
        """Истёкшая ссылка помечается в карточке как недействительная."""
        lesson = await _make_lesson(repo, tenant_id, student.id, topic="Дроби")
        await repo.share_links.create(
            tenant_id=tenant_id,
            lesson_id=lesson.id,
            expires_at=datetime(2020, 1, 1, tzinfo=UTC),
        )
        await repo.session.flush()

        response = await client.get(f"/admin/lessons/{lesson.id}")
        assert response.status_code == 200
        assert "истёк" in response.text.lower()
        assert "Создать ссылку" in response.text

    async def test_share_revoke_marks_link(
        self, client: AsyncClient, repo: Repository, tenant_id: int, student
    ) -> None:
        """POST /share/revoke помечает ссылку отозванной."""
        lesson = await _make_lesson(repo, tenant_id, student.id, topic="Дроби")
        await client.post(f"/admin/lessons/{lesson.id}/share", data={}, follow_redirects=False)

        response = await client.post(
            f"/admin/lessons/{lesson.id}/share/revoke", follow_redirects=False
        )
        assert response.status_code == 303
        assert f"/admin/lessons/{lesson.id}?saved=1" in response.headers["location"]

        assert await repo.share_links.get_active_for_lesson(tenant_id, lesson.id) is None

    async def test_share_revoke_without_link_is_idempotent(
        self, client: AsyncClient, repo: Repository, tenant_id: int, student
    ) -> None:
        """Отзыв без активной ссылки не ломает страницу."""
        lesson = await _make_lesson(repo, tenant_id, student.id, topic="Дроби")
        response = await client.post(
            f"/admin/lessons/{lesson.id}/share/revoke", follow_redirects=False
        )
        assert response.status_code == 303
        assert f"/admin/lessons/{lesson.id}?saved=1" in response.headers["location"]

    async def test_share_scoped_to_tenant(self, client: AsyncClient, repo: Repository) -> None:
        """Ссылку нельзя создать для занятия чужого тенанта."""
        other = await repo.tenants.create(name="Чужой", telegram_user_id=4003)
        await repo.session.flush()
        foreign = await repo.entities.create(
            tenant_id=other.id, name="Чужой ученик", entity_type="student"
        )
        await repo.session.flush()
        lesson = await _make_lesson(repo, other.id, foreign.id, topic="Чужое")

        response = await client.post(
            f"/admin/lessons/{lesson.id}/share", data={}, follow_redirects=False
        )
        assert response.status_code == 303
        assert "error=" in response.headers["location"]
        assert await repo.share_links.get_active_for_lesson(other.id, lesson.id) is None

    async def test_share_requires_session(
        self, anon_client: AsyncClient, repo: Repository, tenant_id: int, student
    ) -> None:
        """Без cookie-сессии создать ссылку нельзя."""
        lesson = await _make_lesson(repo, tenant_id, student.id, topic="Дроби")
        response = await anon_client.post(
            f"/admin/lessons/{lesson.id}/share", data={}, follow_redirects=False
        )
        assert response.status_code == 401


class TestLessonUpdate:
    """Проверки редактирования занятия."""

    async def test_update_changes_fields(
        self, client: AsyncClient, repo: Repository, tenant_id: int, student
    ) -> None:
        """Редактирование сохраняет новые значения."""
        lesson = await _make_lesson(repo, tenant_id, student.id, topic="Дроби")
        response = await client.post(
            f"/admin/lessons/{lesson.id}",
            data={
                "lesson_date": SECOND_LESSON_DATE.isoformat(),
                "topic": "Уравнения",
                "homework": "Стр. 20",
                "rating": "4",
                "next_step": "Повторить",
                "attendance": "present",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == f"/admin/lessons/{lesson.id}?saved=1"

        await repo.session.refresh(lesson)
        assert lesson.topic == "Уравнения"
        assert lesson.homework == "Стр. 20"
        assert lesson.rating == "4"
        assert lesson.next_step == "Повторить"
        assert lesson.lesson_date == SECOND_LESSON_DATE

    async def test_update_blank_topic_returns_error(
        self, client: AsyncClient, repo: Repository, tenant_id: int, student
    ) -> None:
        """Пустая тема не сохраняется."""
        lesson = await _make_lesson(repo, tenant_id, student.id, topic="Дроби")
        response = await client.post(
            f"/admin/lessons/{lesson.id}",
            data={"topic": "   "},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert "error=" in response.headers["location"]

    async def test_update_unknown_redirects(self, client: AsyncClient) -> None:
        """Несуществующее занятие ведёт в журнал с ошибкой."""
        response = await client.post(
            "/admin/lessons/999999",
            data={"topic": "Дроби"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert "error=" in response.headers["location"]

    async def test_update_changes_start_time(
        self, client: AsyncClient, repo: Repository, tenant_id: int, student
    ) -> None:
        """Редактирование сохраняет новое время начала."""
        lesson = await _make_lesson(repo, tenant_id, student.id, topic="Дроби")
        await client.post(
            f"/admin/lessons/{lesson.id}",
            data={"topic": "Дроби", "start_time": "18:45"},
            follow_redirects=False,
        )
        await repo.session.refresh(lesson)
        assert lesson.start_time == "18:45"

    async def test_update_clears_start_time(
        self, client: AsyncClient, repo: Repository, tenant_id: int, student
    ) -> None:
        """Пустое время при редактировании очищает поле."""
        lesson = await _make_lesson(repo, tenant_id, student.id, topic="Дроби")
        lesson.start_time = "17:30"
        await repo.session.flush()

        await client.post(
            f"/admin/lessons/{lesson.id}",
            data={"topic": "Дроби", "start_time": ""},
            follow_redirects=False,
        )
        await repo.session.refresh(lesson)
        assert lesson.start_time is None

    async def test_update_writes_audit_log(
        self, client: AsyncClient, repo: Repository, tenant_id: int, student
    ) -> None:
        """Редактирование занятия пишется в журнал действий."""
        lesson = await _make_lesson(repo, tenant_id, student.id, topic="Дроби")
        await client.post(
            f"/admin/lessons/{lesson.id}",
            data={"topic": "Уравнения", "attendance": "present"},
            follow_redirects=False,
        )
        entries = await repo.audit.list_by_tenant(tenant_id)
        assert any(entry.action == "lesson_updated" for entry in entries)

    async def test_update_scoped_to_tenant(self, client: AsyncClient, repo: Repository) -> None:
        """Занятие чужого тенанта нельзя отредактировать."""
        other = await repo.tenants.create(name="Чужой", telegram_user_id=4003)
        await repo.session.flush()
        foreign = await repo.entities.create(
            tenant_id=other.id, name="Чужой ученик", entity_type="student"
        )
        await repo.session.flush()
        lesson = await _make_lesson(repo, other.id, foreign.id, topic="Чужое")

        response = await client.post(
            f"/admin/lessons/{lesson.id}",
            data={"topic": "Взлом"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert "error=" in response.headers["location"]

        await repo.session.refresh(lesson)
        assert lesson.topic == "Чужое"

    async def test_update_requires_session(
        self, anon_client: AsyncClient, repo: Repository, tenant_id: int, student
    ) -> None:
        """Без cookie-сессии редактирование недоступно."""
        lesson = await _make_lesson(repo, tenant_id, student.id)
        response = await anon_client.post(
            f"/admin/lessons/{lesson.id}",
            data={"topic": "Дроби"},
            follow_redirects=False,
        )
        assert response.status_code == 401


class TestLessonDelete:
    """Проверки удаления занятия."""

    async def test_delete_removes_lesson(
        self, client: AsyncClient, repo: Repository, tenant_id: int, student
    ) -> None:
        """Удаление убирает занятие из журнала."""
        lesson = await _make_lesson(repo, tenant_id, student.id)
        response = await client.post(f"/admin/lessons/{lesson.id}/delete", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/admin/lessons?saved=1"

        assert await repo.lessons.get_by_id(tenant_id, lesson.id) is None

    async def test_delete_writes_audit_log(
        self, client: AsyncClient, repo: Repository, tenant_id: int, student
    ) -> None:
        """Удаление занятия пишется в журнал действий."""
        lesson = await _make_lesson(repo, tenant_id, student.id)
        await client.post(f"/admin/lessons/{lesson.id}/delete", follow_redirects=False)
        entries = await repo.audit.list_by_tenant(tenant_id)
        assert any(entry.action == "lesson_deleted" for entry in entries)

    async def test_delete_unknown_is_idempotent(self, client: AsyncClient) -> None:
        """Повторное удаление не ломает страницу."""
        response = await client.post("/admin/lessons/999999/delete", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/admin/lessons?saved=1"

    async def test_delete_scoped_to_tenant(self, client: AsyncClient, repo: Repository) -> None:
        """Занятие чужого тенанта нельзя удалить."""
        other = await repo.tenants.create(name="Чужой", telegram_user_id=4004)
        await repo.session.flush()
        foreign = await repo.entities.create(
            tenant_id=other.id, name="Чужой ученик", entity_type="student"
        )
        await repo.session.flush()
        lesson = await _make_lesson(repo, other.id, foreign.id)

        await client.post(f"/admin/lessons/{lesson.id}/delete", follow_redirects=False)
        assert await repo.lessons.get_by_id(other.id, lesson.id) is not None

    async def test_delete_requires_session(
        self, anon_client: AsyncClient, repo: Repository, tenant_id: int, student
    ) -> None:
        """Без cookie-сессии удаление недоступно."""
        lesson = await _make_lesson(repo, tenant_id, student.id)
        response = await anon_client.post(
            f"/admin/lessons/{lesson.id}/delete", follow_redirects=False
        )
        assert response.status_code == 401


class TestLessonPaid:
    """Проверки отсутствия отметки «Оплачено»."""

    async def test_card_has_no_paid_checkbox(
        self, client: AsyncClient, repo: Repository, tenant_id: int, student
    ) -> None:
        """Чекбокс «Оплачено» убран из карточки занятия."""
        lesson = await _make_lesson(repo, tenant_id, student.id)

        response = await client.get(f"/admin/lessons/{lesson.id}")

        assert response.status_code == 200
        assert 'name="paid"' not in response.text
        assert "Оплачено" not in response.text

    async def test_new_form_has_no_paid_checkbox(self, client: AsyncClient) -> None:
        """Форма создания занятия больше не содержит галочку «Оплачено»."""
        response = await client.get("/admin/lessons/new")

        assert response.status_code == 200
        assert 'name="paid"' not in response.text


class _FakeLesson:
    """Минимальная заглушка занятия для проверок чистых функций.

    Позволяет тестировать ``_filter_lessons`` и ``_build_report`` без
    обращения к базе данных.
    """

    def __init__(
        self,
        lesson_id: int,
        lesson_date: date,
        *,
        entity_id: int = 1,
        topic: str = "Тема",
        homework: str | None = None,
        rating: str | None = None,
        next_step: str | None = None,
        internal_notes: str | None = None,
        attendance: str | None = None,
        start_time: str | None = None,
    ) -> None:
        self.id = lesson_id
        self.lesson_date = lesson_date
        self.entity_id = entity_id
        self.topic = topic
        self.homework = homework
        self.rating = rating
        self.next_step = next_step
        self.internal_notes = internal_notes
        self.attendance = attendance
        self.start_time = start_time


class _FakeEntity:
    """Минимальная заглушка ученика для проверок ``_build_report``."""

    def __init__(
        self,
        entity_id: int,
        *,
        name: str = "Ученик",
        balance_lessons: int = 0,
        parent_phone: str | None = None,
    ) -> None:
        self.id = entity_id
        self.name = name
        self.balance_lessons = balance_lessons
        self.parent_phone = parent_phone


class TestLessonHelpers:
    """Проверки вспомогательных функций раздела."""

    @pytest.mark.parametrize("raw", [None, "", "   "])
    def test_clean_blank(self, raw: str | None) -> None:
        """Пустые значения приводятся к пустой строке."""
        assert _clean(raw) == ""

    def test_clean_strips(self) -> None:
        """Пробелы по краям убираются."""
        assert _clean("  Дроби  ") == "Дроби"

    def test_parse_date_valid(self) -> None:
        """Корректная дата разбирается."""
        assert _parse_date("2026-09-14") == LESSON_DATE

    @pytest.mark.parametrize("raw", [None, "", "   ", "не-дата", "14.09.2026"])
    def test_parse_date_invalid(self, raw: str | None) -> None:
        """Некорректная дата даёт ``None``."""
        assert _parse_date(raw) is None

    def test_parse_time_valid(self) -> None:
        """Корректное время разбирается и нормализуется."""
        assert _parse_time("17:30") == "17:30"
        assert _parse_time(" 09:05 ") == "09:05"

    @pytest.mark.parametrize("raw", [None, "", "   ", "не-время", "25:00", "17-30"])
    def test_parse_time_invalid(self, raw: str | None) -> None:
        """Некорректное время даёт ``None``."""
        assert _parse_time(raw) is None

    def test_parse_int_valid(self) -> None:
        """Корректное число разбирается."""
        assert _parse_int("42") == 42

    @pytest.mark.parametrize("raw", [None, "", "   ", "abc", "1.5"])
    def test_parse_int_invalid(self, raw: str | None) -> None:
        """Некорректное число даёт ``None``."""
        assert _parse_int(raw) is None

    def test_resolve_period_default(self) -> None:
        """Без параметров берётся период по умолчанию."""
        start, end, code = _resolve_period(None, None, None, LESSON_DATE)
        assert code == "month"
        assert end == LESSON_DATE
        assert start == LESSON_DATE - timedelta(days=DEFAULT_PERIOD_DAYS)

    @pytest.mark.parametrize("code,days", [(c, d) for c, _, d in PERIOD_OPTIONS])
    def test_resolve_period_known(self, code: str, days: int) -> None:
        """Известные коды периодов дают правильные границы."""
        start, end, resolved = _resolve_period(code, None, None, LESSON_DATE)
        assert resolved == code
        assert end == LESSON_DATE
        assert start == LESSON_DATE - timedelta(days=days)

    def test_resolve_period_unknown_falls_back(self) -> None:
        """Неизвестный код периода откатывается к месяцу."""
        _, _, code = _resolve_period("нечто", None, None, LESSON_DATE)
        assert code == "month"

    def test_resolve_period_custom_dates(self) -> None:
        """Явные даты дают код ``custom``."""
        start, end, code = _resolve_period(None, "2026-01-01", "2026-01-31", LESSON_DATE)
        assert code == "custom"
        assert start == date(2026, 1, 1)
        assert end == date(2026, 1, 31)

    def test_resolve_period_swaps_reversed(self) -> None:
        """Перепутанные даты меняются местами."""
        start, end, _ = _resolve_period(None, "2026-01-31", "2026-01-01", LESSON_DATE)
        assert start == date(2026, 1, 1)
        assert end == date(2026, 1, 31)

    def test_resolve_period_keeps_explicit_range(self) -> None:
        """Явные даты возвращаются без обрезки, даже если период длинный."""
        start, end, code = _resolve_period(None, "2020-01-01", "2026-01-01", LESSON_DATE)
        assert code == "custom"
        assert start == date(2020, 1, 1)
        assert end == date(2026, 1, 1)
        assert (end - start).days > MAX_PERIOD_DAYS

    def test_filter_lessons_by_entity(self) -> None:
        """Фильтр по ученику оставляет только его занятия."""
        rows = [
            _FakeLesson(1, LESSON_DATE, entity_id=1),
            _FakeLesson(2, SECOND_LESSON_DATE, entity_id=2),
        ]
        result = _filter_lessons(
            rows,
            entity_id=1,
            start=OLD_LESSON_DATE,
            end=SECOND_LESSON_DATE,
        )
        assert [lesson.id for lesson in result] == [1]

    def test_filter_lessons_by_period(self) -> None:
        """Фильтр по периоду отсекает старые занятия."""
        rows = [
            _FakeLesson(1, OLD_LESSON_DATE, entity_id=1),
            _FakeLesson(2, LESSON_DATE, entity_id=1),
        ]
        result = _filter_lessons(
            rows,
            entity_id=None,
            start=LESSON_DATE,
            end=SECOND_LESSON_DATE,
        )
        assert [lesson.id for lesson in result] == [2]

    def test_filter_lessons_sorts_newest_first(self) -> None:
        """Занятия сортируются от новых к старым."""
        rows = [
            _FakeLesson(1, LESSON_DATE, entity_id=1),
            _FakeLesson(2, SECOND_LESSON_DATE, entity_id=1),
        ]
        result = _filter_lessons(
            rows,
            entity_id=None,
            start=OLD_LESSON_DATE,
            end=SECOND_LESSON_DATE,
        )
        assert [lesson.id for lesson in result] == [2, 1]

    def test_build_report_includes_core_fields(self) -> None:
        """Отчёт содержит дату, тему, домашку и оценку."""
        lesson = _FakeLesson(
            1,
            LESSON_DATE,
            entity_id=1,
            topic="Дроби",
            homework="№ 12",
            rating="5",
            next_step="Уравнения",
            attendance="present",
        )
        entity = _FakeEntity(1, name="Аня", balance_lessons=7)
        report = _build_report(lesson, entity, currency="₪")
        assert "Дроби" in report
        assert "№ 12" in report
        assert "5" in report
        assert "Уравнения" in report
        assert "Аня" in report

    def test_build_report_hides_internal_notes(self) -> None:
        """Внутренние заметки не попадают в отчёт родителю."""
        lesson = _FakeLesson(
            1,
            LESSON_DATE,
            entity_id=1,
            topic="Дроби",
            internal_notes="СЕКРЕТ",
        )
        entity = _FakeEntity(1, name="Аня", balance_lessons=7)
        report = _build_report(lesson, entity, currency="₪")
        assert "СЕКРЕТ" not in report

    def test_build_report_omits_currency(self) -> None:
        """Валюта больше не попадает в отчёт родителю."""
        lesson = _FakeLesson(1, LESSON_DATE, entity_id=1, topic="Дроби")
        entity = _FakeEntity(1, name="Аня", balance_lessons=7, parent_phone="972501234567")
        report = _build_report(lesson, entity, currency="₪")
        assert "₪" not in report

    def test_build_report_omits_attendance_and_balance(self) -> None:
        """Посещаемость и остаток занятий убраны из отчёта."""
        lesson = _FakeLesson(1, LESSON_DATE, entity_id=1, topic="Дроби", attendance="Аня")
        entity = _FakeEntity(1, name="Аня", balance_lessons=7)
        report = _build_report(lesson, entity, currency="₪")
        assert "Посещаемость" not in report
        assert "Остаток" not in report
        assert "Аня" in report
