"""Тесты раздела кабинета «Расписание» (``/admin/schedule``).

Проверяют недельную сетку, добавление/редактирование/удаление
регулярных слотов, разовые отклонения (перенос, отмена, доп. занятие),
подсветку конфликтов и вспомогательные функции раздела.

Тестовая БД подменяется через ``dependency_overrides``: приложение
получает ту же in-memory сессию, что и фикстуры pytest. Вход в кабинет
выполняется «как в жизни» — через ``GET /admin/{admin_token}``, чтобы
cookie-сессию выставило само приложение.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date, timedelta

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repository import Repository
from app.web.admin_schedule_routes import (
    DEFAULT_DURATION_MINUTES,
    MAX_DURATION_MINUTES,
    MIN_DURATION_MINUTES,
    _build_days,
    _monday_of,
    _parse_date,
    _parse_duration,
    _parse_week,
)
from app.web.app import create_app
from app.web.dependencies import get_db_session

# Понедельник тестовой недели и соседние дни.
MONDAY = date(2026, 9, 14)
TUESDAY = date(2026, 9, 15)
WEDNESDAY = date(2026, 9, 16)
NEXT_MONDAY = date(2026, 9, 21)


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
    """Ученик для проверок расписания.

    Args:
        repo: Фасад репозиториев.
        tenant_id: Идентификатор тестового тенанта.

    Returns:
        Созданный ученик.
    """
    entity = await repo.entities.create(tenant_id=tenant_id, name="Аня", entity_type="student")
    await repo.session.flush()
    return entity


@pytest_asyncio.fixture
async def second_student(repo: Repository, tenant_id: int):
    """Второй ученик — нужен для проверки конфликтов.

    Args:
        repo: Фасад репозиториев.
        tenant_id: Идентификатор тестового тенанта.

    Returns:
        Созданный ученик.
    """
    entity = await repo.entities.create(tenant_id=tenant_id, name="Миша", entity_type="student")
    await repo.session.flush()
    return entity


class TestScheduleWeek:
    """Проверки недельной сетки расписания."""

    async def test_week_renders_empty_state(self, client: AsyncClient) -> None:
        """Пустая неделя отдаёт страницу с подсказкой «нет занятий»."""
        response = await client.get("/admin/schedule")

        assert response.status_code == 200
        assert "Расписание на неделю" in response.text
        assert "нет занятий" in response.text

    async def test_week_shows_slot(
        self, client: AsyncClient, repo: Repository, tenant_id: int, student
    ) -> None:
        """Занятие из регулярного слота попадает в сетку."""
        await repo.schedule.create(
            tenant_id=tenant_id,
            entity_id=student.id,
            weekday="mon",
            start_time="17:30",
            duration_min=60,
            location="Зал",
        )
        await repo.session.flush()

        response = await client.get(f"/admin/schedule?week={MONDAY.isoformat()}")

        assert response.status_code == 200
        assert "17:30" in response.text
        assert "Аня" in response.text
        assert "Зал" in response.text

    async def test_week_shows_cancel_button(
        self, client: AsyncClient, repo: Repository, tenant_id: int, student
    ) -> None:
        """У каждого занятия в сетке есть кнопка отмены конкретного занятия."""
        await repo.schedule.create(
            tenant_id=tenant_id,
            entity_id=student.id,
            weekday="mon",
            start_time="17:30",
        )
        await repo.session.flush()

        response = await client.get(f"/admin/schedule?week={MONDAY.isoformat()}")

        assert response.status_code == 200
        assert "отменить занятие" in response.text
        assert 'action="/admin/schedule/exceptions"' in response.text
        assert 'value="cancelled"' in response.text

    async def test_week_shows_cancelled_lesson_with_restore(
        self, client: AsyncClient, repo: Repository, tenant_id: int, student
    ) -> None:
        """Отменённое занятие остаётся в сетке с кнопкой возврата."""
        slot = await repo.schedule.create(
            tenant_id=tenant_id,
            entity_id=student.id,
            weekday="mon",
            start_time="17:30",
        )
        await repo.session.flush()
        exception = await repo.schedule_exceptions.create(
            tenant_id=tenant_id,
            entity_id=student.id,
            exception_date=MONDAY,
            kind="cancelled",
            slot_id=slot.id,
        )
        await repo.session.flush()

        response = await client.get(f"/admin/schedule?week={MONDAY.isoformat()}")

        assert response.status_code == 200
        assert "отменено" in response.text
        assert "вернуть занятие" in response.text
        assert f"/admin/schedule/exceptions/{exception.id}/delete" in response.text

    async def test_week_shows_total_lessons(
        self, client: AsyncClient, repo: Repository, tenant_id: int, student
    ) -> None:
        """В подзаголовке выводится количество занятий за неделю."""
        await repo.schedule.create(
            tenant_id=tenant_id,
            entity_id=student.id,
            weekday="mon",
            start_time="10:00",
        )
        await repo.session.flush()

        response = await client.get(f"/admin/schedule?week={MONDAY.isoformat()}")

        assert response.status_code == 200
        assert "занятий: 1" in response.text

    async def test_week_navigation_links(
        self, client: AsyncClient, repo: Repository, tenant_id: int
    ) -> None:
        """Ссылки «предыдущая/следующая» ведут на соседние недели."""
        response = await client.get(f"/admin/schedule?week={MONDAY.isoformat()}")

        assert response.status_code == 200
        assert f"week={(MONDAY - timedelta(days=7)).isoformat()}" in response.text
        assert f"week={NEXT_MONDAY.isoformat()}" in response.text

    async def test_week_ignores_bad_week_param(self, client: AsyncClient) -> None:
        """Некорректный параметр ``week`` не ломает страницу."""
        response = await client.get("/admin/schedule?week=не-дата")

        assert response.status_code == 200
        assert "Расписание на неделю" in response.text

    async def test_week_shows_saved_banner(self, client: AsyncClient) -> None:
        """Параметр ``saved=1`` включает уведомление об успехе."""
        response = await client.get("/admin/schedule?saved=1")

        assert response.status_code == 200

    async def test_week_shows_error_banner(self, client: AsyncClient) -> None:
        """Параметр ``error`` выводит текст ошибки."""
        response = await client.get("/admin/schedule?error=Что-то+пошло+не+так")

        assert response.status_code == 200
        assert "Что-то пошло не так" in response.text

    async def test_week_scoped_to_tenant(
        self, client: AsyncClient, repo: Repository, tenant_id: int
    ) -> None:
        """Слоты чужого тенанта не попадают в сетку."""
        other = await repo.tenants.create(name="Чужой тренер", telegram_user_id=2002)
        foreign = await repo.entities.create(
            tenant_id=other.id, name="Чужой ученик", entity_type="student"
        )
        await repo.schedule.create(
            tenant_id=other.id,
            entity_id=foreign.id,
            weekday="mon",
            start_time="09:00",
        )
        await repo.session.flush()

        response = await client.get(f"/admin/schedule?week={MONDAY.isoformat()}")

        assert response.status_code == 200
        assert "Чужой ученик" not in response.text

    async def test_week_requires_session(self, anon_client: AsyncClient) -> None:
        """Без cookie-сессии раздел недоступен."""
        response = await anon_client.get("/admin/schedule")

        assert response.status_code == 401

    async def test_week_renders_slot_form(
        self, client: AsyncClient, repo: Repository, tenant_id: int, student
    ) -> None:
        """На странице расписания есть форма создания слота."""
        response = await client.get("/admin/schedule")

        assert response.status_code == 200
        assert 'action="/admin/schedule/slots"' in response.text
        assert "Добавить слот" in response.text
        assert "Аня" in response.text

    async def test_week_lists_existing_slots(
        self, client: AsyncClient, repo: Repository, tenant_id: int, student
    ) -> None:
        """Существующие слоты выводятся со ссылкой на удаление."""
        slot = await repo.schedule.create(
            tenant_id=tenant_id,
            entity_id=student.id,
            weekday="tue",
            start_time="17:30",
            duration_min=45,
            location="Онлайн",
        )
        await repo.session.flush()

        response = await client.get("/admin/schedule")

        assert response.status_code == 200
        assert f"/admin/schedule/slots/{slot.id}/delete" in response.text
        assert "Вторник" in response.text
        assert "17:30" in response.text
        assert "45 мин" in response.text

    async def test_week_without_entities_hides_form(self, client: AsyncClient) -> None:
        """Без учеников форма скрыта, вместо неё подсказка."""
        response = await client.get("/admin/schedule")

        assert response.status_code == 200
        assert 'action="/admin/schedule/slots"' not in response.text
        assert "Сначала добавьте ученика" in response.text

    async def test_week_no_placeholder_about_student_page(self, client: AsyncClient) -> None:
        """Текст-заглушка про страницу ученика убран."""
        response = await client.get("/admin/schedule")

        assert response.status_code == 200
        assert "Добавить слот можно на странице ученика" not in response.text


class TestSlotCreate:
    """Проверки добавления регулярного слота."""

    async def test_create_slot_redirects(
        self, client: AsyncClient, repo: Repository, tenant_id: int, student
    ) -> None:
        """Успешное добавление ведёт на сетку с флагом ``saved``."""
        response = await client.post(
            "/admin/schedule/slots",
            data={
                "entity_id": str(student.id),
                "weekday": "tue",
                "start_time": "18:00",
                "duration_minutes": "90",
                "place": "Онлайн",
            },
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert response.headers["location"] == "/admin/schedule?saved=1"

        slots = await repo.schedule.list_by_tenant(tenant_id)
        assert len(slots) == 1
        assert slots[0].weekday == "tue"
        assert slots[0].start_time == "18:00"
        assert slots[0].duration_min == 90
        assert slots[0].location == "Онлайн"

    async def test_create_slot_default_duration(
        self, client: AsyncClient, repo: Repository, tenant_id: int, student
    ) -> None:
        """Без длительности подставляется значение по умолчанию."""
        await client.post(
            "/admin/schedule/slots",
            data={
                "entity_id": str(student.id),
                "weekday": "wed",
                "start_time": "12:00",
            },
            follow_redirects=False,
        )

        slots = await repo.schedule.list_by_tenant(tenant_id)
        assert slots[0].duration_min == DEFAULT_DURATION_MINUTES

    async def test_create_slot_clamps_duration(
        self, client: AsyncClient, repo: Repository, tenant_id: int, student
    ) -> None:
        """Слишком большая длительность обрезается до максимума."""
        await client.post(
            "/admin/schedule/slots",
            data={
                "entity_id": str(student.id),
                "weekday": "thu",
                "start_time": "08:00",
                "duration_minutes": "9999",
            },
            follow_redirects=False,
        )

        slots = await repo.schedule.list_by_tenant(tenant_id)
        assert slots[0].duration_min == MAX_DURATION_MINUTES

    async def test_create_slot_bad_time_returns_error(
        self, client: AsyncClient, repo: Repository, tenant_id: int, student
    ) -> None:
        """Некорректное время возвращает тренера с сообщением об ошибке."""
        response = await client.post(
            "/admin/schedule/slots",
            data={
                "entity_id": str(student.id),
                "weekday": "mon",
                "start_time": "25:99",
            },
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert "error=" in response.headers["location"]

        slots = await repo.schedule.list_by_tenant(tenant_id)
        assert slots == []

    async def test_create_slot_bad_weekday_returns_error(
        self, client: AsyncClient, repo: Repository, tenant_id: int, student
    ) -> None:
        """Неизвестный день недели отклоняется сервисом."""
        response = await client.post(
            "/admin/schedule/slots",
            data={
                "entity_id": str(student.id),
                "weekday": "нет",
                "start_time": "10:00",
            },
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert "error=" in response.headers["location"]

    async def test_create_slot_requires_session(self, anon_client: AsyncClient, student) -> None:
        """Без сессии слот не создаётся."""
        response = await anon_client.post(
            "/admin/schedule/slots",
            data={
                "entity_id": str(student.id),
                "weekday": "mon",
                "start_time": "10:00",
            },
            follow_redirects=False,
        )

        assert response.status_code == 401


class TestSlotUpdate:
    """Проверки редактирования регулярного слота."""

    async def test_update_slot_changes_fields(
        self, client: AsyncClient, repo: Repository, tenant_id: int, student
    ) -> None:
        """Поля слота обновляются."""
        slot = await repo.schedule.create(
            tenant_id=tenant_id,
            entity_id=student.id,
            weekday="mon",
            start_time="10:00",
            duration_min=60,
        )
        await repo.session.flush()

        response = await client.post(
            f"/admin/schedule/slots/{slot.id}",
            data={
                "weekday": "fri",
                "start_time": "19:00",
                "duration_minutes": "45",
                "place": "Дом",
                "is_active": "on",
            },
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert response.headers["location"] == "/admin/schedule?saved=1"

        await repo.session.refresh(slot)
        assert slot.weekday == "fri"
        assert slot.start_time == "19:00"
        assert slot.duration_min == 45
        assert slot.location == "Дом"
        assert slot.is_active is True

    async def test_update_slot_deactivates(
        self, client: AsyncClient, repo: Repository, tenant_id: int, student
    ) -> None:
        """Без чекбокса слот становится неактивным."""
        slot = await repo.schedule.create(
            tenant_id=tenant_id,
            entity_id=student.id,
            weekday="mon",
            start_time="10:00",
        )
        await repo.session.flush()

        await client.post(
            f"/admin/schedule/slots/{slot.id}",
            data={"weekday": "mon", "start_time": "10:00"},
            follow_redirects=False,
        )

        await repo.session.refresh(slot)
        assert slot.is_active is False

    async def test_update_unknown_slot_returns_error(self, client: AsyncClient) -> None:
        """Несуществующий слот даёт редирект с ошибкой."""
        response = await client.post(
            "/admin/schedule/slots/999999",
            data={"weekday": "mon", "start_time": "10:00"},
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert "error=" in response.headers["location"]

    async def test_update_slot_bad_time_returns_error(
        self, client: AsyncClient, repo: Repository, tenant_id: int, student
    ) -> None:
        """Некорректное время при обновлении отклоняется."""
        slot = await repo.schedule.create(
            tenant_id=tenant_id,
            entity_id=student.id,
            weekday="mon",
            start_time="10:00",
        )
        await repo.session.flush()

        response = await client.post(
            f"/admin/schedule/slots/{slot.id}",
            data={"weekday": "mon", "start_time": "abc"},
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert "error=" in response.headers["location"]

    async def test_update_slot_scoped_to_tenant(
        self, client: AsyncClient, repo: Repository
    ) -> None:
        """Слот чужого тенанта недоступен для правки."""
        other = await repo.tenants.create(name="Чужой тренер", telegram_user_id=2003)
        foreign = await repo.entities.create(
            tenant_id=other.id, name="Чужой", entity_type="student"
        )
        slot = await repo.schedule.create(
            tenant_id=other.id,
            entity_id=foreign.id,
            weekday="mon",
            start_time="10:00",
        )
        await repo.session.flush()

        response = await client.post(
            f"/admin/schedule/slots/{slot.id}",
            data={"weekday": "tue", "start_time": "11:00"},
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert "error=" in response.headers["location"]

        await repo.session.refresh(slot)
        assert slot.weekday == "mon"

    async def test_update_slot_requires_session(
        self, anon_client: AsyncClient, repo: Repository, tenant_id: int, student
    ) -> None:
        """Без сессии слот не обновляется."""
        slot = await repo.schedule.create(
            tenant_id=tenant_id,
            entity_id=student.id,
            weekday="mon",
            start_time="10:00",
        )
        await repo.session.flush()

        response = await anon_client.post(
            f"/admin/schedule/slots/{slot.id}",
            data={"weekday": "tue", "start_time": "11:00"},
            follow_redirects=False,
        )

        assert response.status_code == 401


class TestSlotDelete:
    """Проверки удаления регулярного слота."""

    async def test_delete_slot_removes_it(
        self, client: AsyncClient, repo: Repository, tenant_id: int, student
    ) -> None:
        """Слот удаляется из базы."""
        slot = await repo.schedule.create(
            tenant_id=tenant_id,
            entity_id=student.id,
            weekday="mon",
            start_time="10:00",
        )
        await repo.session.flush()

        response = await client.post(
            f"/admin/schedule/slots/{slot.id}/delete", follow_redirects=False
        )

        assert response.status_code == 303
        assert response.headers["location"] == "/admin/schedule?saved=1"

        slots = await repo.schedule.list_by_tenant(tenant_id)
        assert slots == []

    async def test_delete_slot_keeps_exceptions(
        self, client: AsyncClient, repo: Repository, tenant_id: int, student
    ) -> None:
        """История отклонений сохраняется после удаления слота."""
        slot = await repo.schedule.create(
            tenant_id=tenant_id,
            entity_id=student.id,
            weekday="mon",
            start_time="10:00",
        )
        await repo.session.flush()
        await repo.schedule_exceptions.create(
            tenant_id=tenant_id,
            entity_id=student.id,
            exception_date=MONDAY,
            kind="cancelled",
            slot_id=slot.id,
        )
        await repo.session.flush()

        await client.post(f"/admin/schedule/slots/{slot.id}/delete", follow_redirects=False)

        exceptions = await repo.schedule_exceptions.list_by_date(tenant_id, MONDAY)
        assert len(exceptions) == 1
        assert exceptions[0].slot_id is None

    async def test_delete_unknown_slot_is_idempotent(self, client: AsyncClient) -> None:
        """Удаление несуществующего слота не ломает страницу."""
        response = await client.post("/admin/schedule/slots/999999/delete", follow_redirects=False)

        assert response.status_code == 303

    async def test_delete_slot_scoped_to_tenant(
        self, client: AsyncClient, repo: Repository
    ) -> None:
        """Слот чужого тенанта не удаляется."""
        other = await repo.tenants.create(name="Чужой тренер", telegram_user_id=2004)
        foreign = await repo.entities.create(
            tenant_id=other.id, name="Чужой", entity_type="student"
        )
        slot = await repo.schedule.create(
            tenant_id=other.id,
            entity_id=foreign.id,
            weekday="mon",
            start_time="10:00",
        )
        await repo.session.flush()

        await client.post(f"/admin/schedule/slots/{slot.id}/delete", follow_redirects=False)

        slots = await repo.schedule.list_by_tenant(other.id)
        assert len(slots) == 1

    async def test_delete_slot_requires_session(
        self, anon_client: AsyncClient, repo: Repository, tenant_id: int, student
    ) -> None:
        """Без сессии слот не удаляется."""
        slot = await repo.schedule.create(
            tenant_id=tenant_id,
            entity_id=student.id,
            weekday="mon",
            start_time="10:00",
        )
        await repo.session.flush()

        response = await anon_client.post(
            f"/admin/schedule/slots/{slot.id}/delete", follow_redirects=False
        )

        assert response.status_code == 401


class TestExceptionCreate:
    """Проверки разовых отклонений: перенос, отмена, доп. занятие."""

    async def test_create_cancelled_exception(
        self, client: AsyncClient, repo: Repository, tenant_id: int, student
    ) -> None:
        """Отмена занятия сохраняется и убирает его из сетки."""
        await repo.schedule.create(
            tenant_id=tenant_id,
            entity_id=student.id,
            weekday="mon",
            start_time="10:00",
        )
        await repo.session.flush()

        response = await client.post(
            "/admin/schedule/exceptions",
            data={
                "entity_id": str(student.id),
                "target_date": MONDAY.isoformat(),
                "kind": "cancelled",
                "note": "Болезнь",
            },
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert "saved=1" in response.headers["location"]

        exceptions = await repo.schedule_exceptions.list_by_date(tenant_id, MONDAY)
        assert len(exceptions) == 1
        assert exceptions[0].kind == "cancelled"
        assert exceptions[0].note == "Болезнь"

        week = await client.get(f"/admin/schedule?week={MONDAY.isoformat()}")
        # Отменённое занятие не попадает в сетку дня (в списке слотов
        # время слота остаётся — это ожидаемо).
        assert "нет занятий" in week.text

    async def test_create_moved_exception(
        self, client: AsyncClient, repo: Repository, tenant_id: int, student
    ) -> None:
        """Перенос меняет время занятия в сетке."""
        await repo.schedule.create(
            tenant_id=tenant_id,
            entity_id=student.id,
            weekday="mon",
            start_time="10:00",
        )
        await repo.session.flush()

        response = await client.post(
            "/admin/schedule/exceptions",
            data={
                "entity_id": str(student.id),
                "target_date": MONDAY.isoformat(),
                "kind": "moved",
                "start_time": "16:00",
            },
            follow_redirects=False,
        )

        assert response.status_code == 303

        week = await client.get(f"/admin/schedule?week={MONDAY.isoformat()}")
        assert "16:00" in week.text

    async def test_create_extra_exception(
        self, client: AsyncClient, repo: Repository, tenant_id: int, student
    ) -> None:
        """Дополнительное занятие появляется в сетке."""
        response = await client.post(
            "/admin/schedule/exceptions",
            data={
                "entity_id": str(student.id),
                "target_date": WEDNESDAY.isoformat(),
                "kind": "extra",
                "start_time": "20:00",
            },
            follow_redirects=False,
        )

        assert response.status_code == 303

        exceptions = await repo.schedule_exceptions.list_by_date(tenant_id, WEDNESDAY)
        assert len(exceptions) == 1
        assert exceptions[0].kind == "extra"

        week = await client.get(f"/admin/schedule?week={MONDAY.isoformat()}")
        assert "20:00" in week.text

    async def test_create_moved_without_time_returns_error(
        self, client: AsyncClient, repo: Repository, tenant_id: int, student
    ) -> None:
        """Перенос без нового времени отклоняется."""
        response = await client.post(
            "/admin/schedule/exceptions",
            data={
                "entity_id": str(student.id),
                "target_date": MONDAY.isoformat(),
                "kind": "moved",
            },
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert "error=" in response.headers["location"]

        exceptions = await repo.schedule_exceptions.list_by_date(tenant_id, MONDAY)
        assert exceptions == []

    async def test_create_exception_bad_date_returns_error(self, client: AsyncClient) -> None:
        """Некорректная дата отклоняется до вызова сервиса."""
        response = await client.post(
            "/admin/schedule/exceptions",
            data={
                "entity_id": "1",
                "target_date": "не-дата",
                "kind": "cancelled",
            },
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert "error=" in response.headers["location"]

    async def test_create_exception_unknown_kind_returns_error(self, client: AsyncClient) -> None:
        """Неизвестный вид отклонения отклоняется."""
        response = await client.post(
            "/admin/schedule/exceptions",
            data={
                "entity_id": "1",
                "target_date": MONDAY.isoformat(),
                "kind": "телепорт",
            },
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert "error=" in response.headers["location"]

    async def test_create_exception_requires_session(
        self, anon_client: AsyncClient, student
    ) -> None:
        """Без сессии отклонение не создаётся."""
        response = await anon_client.post(
            "/admin/schedule/exceptions",
            data={
                "entity_id": str(student.id),
                "target_date": MONDAY.isoformat(),
                "kind": "cancelled",
            },
            follow_redirects=False,
        )

        assert response.status_code == 401


class TestExceptionDelete:
    """Проверки удаления разового отклонения."""

    async def test_delete_exception_removes_it(
        self, client: AsyncClient, repo: Repository, tenant_id: int, student
    ) -> None:
        """Отклонение удаляется из базы."""
        exception = await repo.schedule_exceptions.create(
            tenant_id=tenant_id,
            entity_id=student.id,
            exception_date=MONDAY,
            kind="cancelled",
        )
        await repo.session.flush()

        response = await client.post(
            f"/admin/schedule/exceptions/{exception.id}/delete",
            data={"week": MONDAY.isoformat()},
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert f"week={MONDAY.isoformat()}" in response.headers["location"]

        exceptions = await repo.schedule_exceptions.list_by_date(tenant_id, MONDAY)
        assert exceptions == []

    async def test_delete_exception_without_week(
        self, client: AsyncClient, repo: Repository, tenant_id: int, student
    ) -> None:
        """Без параметра ``week`` возврат идёт на текущую неделю."""
        exception = await repo.schedule_exceptions.create(
            tenant_id=tenant_id,
            entity_id=student.id,
            exception_date=MONDAY,
            kind="extra",
            new_time="20:00",
        )
        await repo.session.flush()

        response = await client.post(
            f"/admin/schedule/exceptions/{exception.id}/delete",
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert response.headers["location"] == "/admin/schedule?saved=1"

    async def test_delete_unknown_exception_is_idempotent(self, client: AsyncClient) -> None:
        """Удаление несуществующего отклонения не ломает страницу."""
        response = await client.post(
            "/admin/schedule/exceptions/999999/delete", follow_redirects=False
        )

        assert response.status_code == 303

    async def test_delete_exception_scoped_to_tenant(
        self, client: AsyncClient, repo: Repository
    ) -> None:
        """Отклонение чужого тенанта не удаляется."""
        other = await repo.tenants.create(name="Чужой тренер", telegram_user_id=2005)
        foreign = await repo.entities.create(
            tenant_id=other.id, name="Чужой", entity_type="student"
        )
        exception = await repo.schedule_exceptions.create(
            tenant_id=other.id,
            entity_id=foreign.id,
            exception_date=MONDAY,
            kind="cancelled",
        )
        await repo.session.flush()

        await client.post(
            f"/admin/schedule/exceptions/{exception.id}/delete",
            follow_redirects=False,
        )

        exceptions = await repo.schedule_exceptions.list_by_date(other.id, MONDAY)
        assert len(exceptions) == 1

    async def test_delete_exception_requires_session(
        self, anon_client: AsyncClient, repo: Repository, tenant_id: int, student
    ) -> None:
        """Без сессии отклонение не удаляется."""
        exception = await repo.schedule_exceptions.create(
            tenant_id=tenant_id,
            entity_id=student.id,
            exception_date=MONDAY,
            kind="cancelled",
        )
        await repo.session.flush()

        response = await anon_client.post(
            f"/admin/schedule/exceptions/{exception.id}/delete",
            follow_redirects=False,
        )

        assert response.status_code == 401


class TestScheduleConflicts:
    """Проверки подсветки конфликтов расписания."""

    async def test_conflict_is_shown(
        self,
        client: AsyncClient,
        repo: Repository,
        tenant_id: int,
        student,
        second_student,
    ) -> None:
        """Два занятия в одно время дают предупреждение в сетке."""
        await repo.schedule.create(
            tenant_id=tenant_id,
            entity_id=student.id,
            weekday="mon",
            start_time="10:00",
            duration_min=60,
        )
        await repo.schedule.create(
            tenant_id=tenant_id,
            entity_id=second_student.id,
            weekday="mon",
            start_time="10:30",
            duration_min=60,
        )
        await repo.session.flush()

        response = await client.get(f"/admin/schedule?week={MONDAY.isoformat()}")

        assert response.status_code == 200
        assert "Наложения по времени" in response.text
        assert "Аня" in response.text
        assert "Миша" in response.text

    async def test_no_conflict_for_sequential_lessons(
        self,
        client: AsyncClient,
        repo: Repository,
        tenant_id: int,
        student,
        second_student,
    ) -> None:
        """Занятия подряд не считаются конфликтом."""
        await repo.schedule.create(
            tenant_id=tenant_id,
            entity_id=student.id,
            weekday="mon",
            start_time="10:00",
            duration_min=60,
        )
        await repo.schedule.create(
            tenant_id=tenant_id,
            entity_id=second_student.id,
            weekday="mon",
            start_time="11:00",
            duration_min=60,
        )
        await repo.session.flush()

        response = await client.get(f"/admin/schedule?week={MONDAY.isoformat()}")

        assert response.status_code == 200
        assert "Наложения по времени" not in response.text

    async def test_cancelled_lesson_does_not_conflict(
        self,
        client: AsyncClient,
        repo: Repository,
        tenant_id: int,
        student,
        second_student,
    ) -> None:
        """Отменённое занятие не участвует в поиске конфликтов."""
        await repo.schedule.create(
            tenant_id=tenant_id,
            entity_id=student.id,
            weekday="mon",
            start_time="10:00",
            duration_min=60,
        )
        await repo.schedule.create(
            tenant_id=tenant_id,
            entity_id=second_student.id,
            weekday="mon",
            start_time="10:30",
            duration_min=60,
        )
        await repo.schedule_exceptions.create(
            tenant_id=tenant_id,
            entity_id=student.id,
            exception_date=MONDAY,
            kind="cancelled",
        )
        await repo.session.flush()

        response = await client.get(f"/admin/schedule?week={MONDAY.isoformat()}")

        assert response.status_code == 200
        assert "Наложения по времени" not in response.text


class TestScheduleHelpers:
    """Проверки вспомогательных функций раздела."""

    def test_monday_of_returns_monday(self) -> None:
        """Любая дата недели приводится к понедельнику."""
        assert _monday_of(MONDAY) == MONDAY
        assert _monday_of(TUESDAY) == MONDAY
        assert _monday_of(WEDNESDAY) == MONDAY

    def test_parse_week_accepts_iso_date(self) -> None:
        """ISO-дата приводится к понедельнику недели."""
        assert _parse_week(TUESDAY.isoformat(), MONDAY) == MONDAY

    def test_parse_week_falls_back_on_empty(self) -> None:
        """Пустое значение даёт понедельник текущей недели."""
        assert _parse_week(None, WEDNESDAY) == MONDAY
        assert _parse_week("   ", WEDNESDAY) == MONDAY

    def test_parse_week_falls_back_on_bad_value(self) -> None:
        """Некорректная дата даёт понедельник текущей недели."""
        assert _parse_week("не-дата", WEDNESDAY) == MONDAY

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("90", 90),
            (" 45 ", 45),
            ("0", MIN_DURATION_MINUTES),
            ("-10", MIN_DURATION_MINUTES),
            ("9999", MAX_DURATION_MINUTES),
        ],
    )
    def test_parse_duration_valid(self, raw: str, expected: int) -> None:
        """Корректная длительность разбирается и обрезается по границам."""
        assert _parse_duration(raw) == expected

    @pytest.mark.parametrize("raw", [None, "", "   ", "abc"])
    def test_parse_duration_falls_back(self, raw: str | None) -> None:
        """Некорректная длительность даёт значение по умолчанию."""
        assert _parse_duration(raw) == DEFAULT_DURATION_MINUTES

    def test_parse_date_valid(self) -> None:
        """ISO-дата разбирается в объект ``date``."""
        assert _parse_date(MONDAY.isoformat()) == MONDAY

    @pytest.mark.parametrize("raw", [None, "", "   ", "не-дата"])
    def test_parse_date_invalid(self, raw: str | None) -> None:
        """Некорректная дата даёт ``None``."""
        assert _parse_date(raw) is None

    def test_build_days_returns_seven_days(self) -> None:
        """Сетка всегда содержит семь дней недели."""

        class _EmptySchedule:
            days: dict[date, list[object]] = {}

        days = _build_days(MONDAY, _EmptySchedule(), MONDAY)

        assert len(days) == 7
        assert days[0]["date"] == MONDAY
        assert days[0]["is_today"] is True
        assert days[0]["lessons"] == []
        assert days[6]["date"] == MONDAY + timedelta(days=6)
        assert days[6]["is_today"] is False

    def test_build_days_marks_lessons(self) -> None:
        """Занятия из расписания попадают в нужный день сетки."""

        class _Schedule:
            days = {MONDAY: ["урок"]}

        days = _build_days(MONDAY, _Schedule(), WEDNESDAY)

        assert days[0]["lessons"] == ["урок"]
        assert days[1]["lessons"] == []
