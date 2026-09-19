"""Тесты раздела кабинета «Ученики» (``/admin/students``).

Проверяют список с фильтрами, создание, карточку, редактирование,
удаление и привязку телефона.

Тестовая БД подменяется через ``dependency_overrides``: приложение
получает ту же in-memory сессию, что и фикстуры pytest. Вход в кабинет
выполняется «как в жизни» — через ``GET /admin/{admin_token}``, чтобы
cookie-сессию выставило само приложение.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repository import Repository
from app.web.app import create_app
from app.web.dependencies import get_db_session

# Дата занятия для проверок карточки (понедельник).
LESSON_DATE = date(2026, 9, 14)


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


class TestStudentsList:
    """Проверки списка учеников и фильтров."""

    async def test_list_renders_empty_state(self, client: AsyncClient) -> None:
        """Пустой список отдаёт страницу с подсказкой."""
        response = await client.get("/admin/students")

        assert response.status_code == 200
        assert "Ученики" in response.text

    async def test_list_has_new_student_button(self, client: AsyncClient) -> None:
        """В списке есть кнопка «Новый ученик» со ссылкой на форму."""
        response = await client.get("/admin/students")

        assert response.status_code == 200
        assert "Новый ученик" in response.text
        assert "/admin/students/new" in response.text

    async def test_list_shows_created_students(
        self, client: AsyncClient, repo: Repository, tenant_id: int
    ) -> None:
        """Созданные ученики попадают в список."""
        await repo.entities.create(tenant_id=tenant_id, name="Миша", entity_type="student")
        await repo.entities.create(tenant_id=tenant_id, name="Группа А", entity_type="group")

        response = await client.get("/admin/students")

        assert response.status_code == 200
        assert "Миша" in response.text
        assert "Группа А" in response.text

    async def test_list_filters_by_kind(
        self, client: AsyncClient, repo: Repository, tenant_id: int
    ) -> None:
        """Фильтр ``kind=group`` оставляет только группы."""
        await repo.entities.create(tenant_id=tenant_id, name="Миша", entity_type="student")
        await repo.entities.create(tenant_id=tenant_id, name="Группа А", entity_type="group")

        response = await client.get("/admin/students", params={"kind": "group"})

        assert response.status_code == 200
        assert "Группа А" in response.text
        assert "Миша" not in response.text

    async def test_list_filters_by_weekday(
        self, client: AsyncClient, repo: Repository, tenant_id: int
    ) -> None:
        """Фильтр по дню недели учитывает регулярные слоты расписания."""
        tuesday = await repo.entities.create(
            tenant_id=tenant_id, name="Вторничный", entity_type="student"
        )
        wednesday = await repo.entities.create(
            tenant_id=tenant_id, name="Средовый", entity_type="student"
        )
        await repo.schedule.create(
            tenant_id=tenant_id, entity_id=tuesday.id, weekday="tue", start_time="17:00"
        )
        await repo.schedule.create(
            tenant_id=tenant_id, entity_id=wednesday.id, weekday="wed", start_time="18:00"
        )

        response = await client.get("/admin/students", params={"weekday": "tue"})

        assert response.status_code == 200
        assert "Вторничный" in response.text
        assert "Средовый" not in response.text

    async def test_list_shows_schedule_from_slots(
        self, client: AsyncClient, repo: Repository, tenant_id: int
    ) -> None:
        """В колонке «Расписание» показывается сводка из регулярных слотов."""
        entity = await repo.entities.create(
            tenant_id=tenant_id, name="Слотованный", entity_type="student"
        )
        await repo.schedule.create(
            tenant_id=tenant_id, entity_id=entity.id, weekday="mon", start_time="17:30"
        )
        await repo.schedule.create(
            tenant_id=tenant_id, entity_id=entity.id, weekday="wed", start_time="17:30"
        )

        response = await client.get("/admin/students")

        assert response.status_code == 200
        assert "Понедельник" in response.text
        assert "Среда" in response.text
        assert "17:30" in response.text
        assert "не задано" not in response.text

    async def test_list_shows_not_set_without_slots(
        self, client: AsyncClient, repo: Repository, tenant_id: int
    ) -> None:
        """Без регулярных слотов в колонке «Расписание» выводится «не задано»."""
        await repo.entities.create(tenant_id=tenant_id, name="Безслотовый", entity_type="student")

        response = await client.get("/admin/students")

        assert response.status_code == 200
        assert "не задано" in response.text

    async def test_list_ignores_unknown_filters(
        self, client: AsyncClient, repo: Repository, tenant_id: int
    ) -> None:
        """Неизвестные значения фильтров не ломают страницу."""
        await repo.entities.create(tenant_id=tenant_id, name="Миша", entity_type="student")

        response = await client.get(
            "/admin/students",
            params={"kind": "нет-такого", "weekday": "нет-такого"},
        )

        assert response.status_code == 200
        assert "Миша" in response.text

    async def test_list_scoped_to_tenant(
        self, client: AsyncClient, repo: Repository, tenant_id: int
    ) -> None:
        """Чужие ученики не попадают в список."""
        other = await repo.tenants.create(telegram_user_id=999_000_111, name="Другой тренер")
        await repo.entities.create(tenant_id=other.id, name="Чужой ученик", entity_type="student")
        await repo.entities.create(tenant_id=tenant_id, name="Свой ученик", entity_type="student")

        response = await client.get("/admin/students")

        assert response.status_code == 200
        assert "Свой ученик" in response.text
        assert "Чужой ученик" not in response.text

    async def test_list_requires_session(self, anon_client: AsyncClient) -> None:
        """Без сессии список недоступен — 401 от ``require_tenant``."""
        response = await anon_client.get("/admin/students", follow_redirects=False)

        assert response.status_code == 401

    async def test_list_sorts_students_before_groups(
        self, client: AsyncClient, repo: Repository, tenant_id: int
    ) -> None:
        """Сначала идут ученики (по имени), затем группы (по имени)."""
        await repo.entities.create(tenant_id=tenant_id, name="Яна", entity_type="student")
        await repo.entities.create(tenant_id=tenant_id, name="Аня", entity_type="student")
        await repo.entities.create(tenant_id=tenant_id, name="Группа Б", entity_type="group")
        await repo.entities.create(tenant_id=tenant_id, name="Группа А", entity_type="group")

        response = await client.get("/admin/students")

        assert response.status_code == 200
        text = response.text
        # Ученики идут раньше групп, внутри каждой группы — по алфавиту.
        assert text.index("Аня") < text.index("Яна") < text.index("Группа А")
        assert text.index("Группа А") < text.index("Группа Б")

    async def test_list_shows_monthly_paid_for_student(
        self, client: AsyncClient, repo: Repository, tenant_id: int
    ) -> None:
        """Для ученика в колонке «За месяц» показывается оплаченная сумма."""
        entity = await repo.entities.create(
            tenant_id=tenant_id, name="Плательщик", entity_type="student"
        )
        await repo.payments.create(
            tenant_id=tenant_id,
            entity_id=entity.id,
            payment_date=date.today(),
            amount=250.0,
            currency="ILS",
            lessons_added=2,
        )

        response = await client.get("/admin/students")

        assert response.status_code == 200
        assert "За месяц" in response.text
        assert "250" in response.text

    async def test_list_shows_monthly_debt_for_group(
        self, client: AsyncClient, repo: Repository, tenant_id: int
    ) -> None:
        """Для группы в колонке «За месяц» показывается долг за месяц."""
        group = await repo.entities.create(
            tenant_id=tenant_id,
            name="Должная группа",
            entity_type="group",
            lesson_price=100.0,
        )
        await repo.lessons.create(
            tenant_id=tenant_id,
            entity_id=group.id,
            lesson_date=date.today(),
            topic="Занятие",
            attendance="все были",
        )

        response = await client.get("/admin/students")

        assert response.status_code == 200
        # 1 проведённое занятие × 100 − 0 платежей = 100.
        assert "100" in response.text


class TestStudentCreate:
    """Проверки создания ученика и группы."""

    async def test_new_form_renders(self, client: AsyncClient) -> None:
        """Форма создания отдаётся с кодом 200."""
        response = await client.get("/admin/students/new")

        assert response.status_code == 200
        assert "Новый ученик" in response.text

    async def test_create_student_redirects_to_card(
        self, client: AsyncClient, repo: Repository, tenant_id: int
    ) -> None:
        """Создание ученика редиректит на его карточку."""
        response = await client.post(
            "/admin/students",
            data={"name": "Миша", "entity_type": "student", "subject": "Шахматы"},
        )

        assert response.status_code == 303
        assert response.headers["location"].startswith("/admin/students/")

        entity = await repo.entities.get_by_name(tenant_id, "Миша")
        assert entity is not None
        assert entity.entity_type == "student"
        assert entity.subject == "Шахматы"

    async def test_create_group(
        self, client: AsyncClient, repo: Repository, tenant_id: int
    ) -> None:
        """Создание группы сохраняет тип ``group``."""
        await client.post(
            "/admin/students",
            data={"name": "Группа А", "entity_type": "group", "subject": "Шахматы"},
        )

        entity = await repo.entities.get_by_name(tenant_id, "Группа А")
        assert entity is not None
        assert entity.entity_type == "group"

    async def test_create_group_saves_members(
        self, client: AsyncClient, repo: Repository, tenant_id: int
    ) -> None:
        """Участники группы сохраняются и нормализуются."""
        await client.post(
            "/admin/students",
            data={
                "name": "Группа А",
                "entity_type": "group",
                "subject": "Шахматы",
                "members": "Аня\nМарк, Лена",
            },
        )

        entity = await repo.entities.get_by_name(tenant_id, "Группа А")
        assert entity is not None
        assert entity.members == "Аня, Марк, Лена"
        assert entity.members_list == ["Аня", "Марк", "Лена"]

    async def test_create_group_saves_chat_url(
        self, client: AsyncClient, repo: Repository, tenant_id: int
    ) -> None:
        """Ссылка на чат WhatsApp группы сохраняется и нормализуется."""
        await client.post(
            "/admin/students",
            data={
                "name": "Группа А",
                "entity_type": "group",
                "subject": "Шахматы",
                "whatsapp_chat_url": "chat.whatsapp.com/AbCdEf123",
            },
        )

        entity = await repo.entities.get_by_name(tenant_id, "Группа А")
        assert entity is not None
        assert entity.whatsapp_chat_url == "https://chat.whatsapp.com/AbCdEf123"

    async def test_create_group_rejects_foreign_chat_url(
        self, client: AsyncClient, repo: Repository, tenant_id: int
    ) -> None:
        """Ссылка на посторонний хост не сохраняется."""
        await client.post(
            "/admin/students",
            data={
                "name": "Группа А",
                "entity_type": "group",
                "subject": "Шахматы",
                "whatsapp_chat_url": "https://example.com/chat",
            },
        )

        entity = await repo.entities.get_by_name(tenant_id, "Группа А")
        assert entity is not None
        assert entity.whatsapp_chat_url is None

    async def test_create_group_saves_lesson_price(
        self, client: AsyncClient, repo: Repository, tenant_id: int
    ) -> None:
        """Индивидуальная цена группы сохраняется."""
        await client.post(
            "/admin/students",
            data={
                "name": "Группа А",
                "entity_type": "group",
                "subject": "Шахматы",
                "lesson_price": "80",
            },
        )

        entity = await repo.entities.get_by_name(tenant_id, "Группа А")
        assert entity is not None
        assert entity.lesson_price == 80.0

    async def test_create_student_saves_lesson_price(
        self, client: AsyncClient, repo: Repository, tenant_id: int
    ) -> None:
        """Индивидуальная цена частного ученика сохраняется."""
        await client.post(
            "/admin/students",
            data={
                "name": "Миша",
                "entity_type": "student",
                "subject": "Шахматы",
                "lesson_price": "120.50",
            },
        )

        entity = await repo.entities.get_by_name(tenant_id, "Миша")
        assert entity is not None
        assert entity.lesson_price == 120.5

    async def test_create_blank_lesson_price_is_none(
        self, client: AsyncClient, repo: Repository, tenant_id: int
    ) -> None:
        """Пустая цена означает «как в настройках» и сохраняется как None."""
        await client.post(
            "/admin/students",
            data={
                "name": "Миша",
                "entity_type": "student",
                "subject": "Шахматы",
                "lesson_price": "   ",
            },
        )

        entity = await repo.entities.get_by_name(tenant_id, "Миша")
        assert entity is not None
        assert entity.lesson_price is None

    async def test_create_invalid_lesson_price_is_none(
        self, client: AsyncClient, repo: Repository, tenant_id: int
    ) -> None:
        """Некорректная цена не сохраняется."""
        await client.post(
            "/admin/students",
            data={
                "name": "Миша",
                "entity_type": "student",
                "subject": "Шахматы",
                "lesson_price": "abc",
            },
        )

        entity = await repo.entities.get_by_name(tenant_id, "Миша")
        assert entity is not None
        assert entity.lesson_price is None

    async def test_create_student_ignores_members(
        self, client: AsyncClient, repo: Repository, tenant_id: int
    ) -> None:
        """Для обычного ученика участники не сохраняются."""
        await client.post(
            "/admin/students",
            data={
                "name": "Миша",
                "entity_type": "student",
                "subject": "Шахматы",
                "members": "Аня, Марк",
            },
        )

        entity = await repo.entities.get_by_name(tenant_id, "Миша")
        assert entity is not None
        assert entity.members is None

    async def test_create_student_ignores_chat_url(
        self, client: AsyncClient, repo: Repository, tenant_id: int
    ) -> None:
        """Для обычного ученика ссылка на чат не сохраняется."""
        await client.post(
            "/admin/students",
            data={
                "name": "Миша",
                "entity_type": "student",
                "subject": "Шахматы",
                "whatsapp_chat_url": "https://chat.whatsapp.com/AbCdEf123",
            },
        )

        entity = await repo.entities.get_by_name(tenant_id, "Миша")
        assert entity is not None
        assert entity.whatsapp_chat_url is None

    async def test_create_normalizes_phone(
        self, client: AsyncClient, repo: Repository, tenant_id: int
    ) -> None:
        """Локальный телефон приводится к формату ``972XXXXXXXXX``."""
        await client.post(
            "/admin/students",
            data={
                "name": "Миша",
                "entity_type": "student",
                "subject": "Шахматы",
                "parent_phone": "+972 54-123-45-67",
            },
        )

        entity = await repo.entities.get_by_name(tenant_id, "Миша")
        assert entity is not None
        assert entity.parent_phone == "972541234567"

    async def test_create_blank_name_returns_to_form(
        self, client: AsyncClient, repo: Repository, tenant_id: int
    ) -> None:
        """Пустое имя не создаёт ученика и возвращает на форму."""
        response = await client.post(
            "/admin/students",
            data={"name": "   ", "entity_type": "student", "subject": "Шахматы"},
        )

        assert response.status_code == 303
        assert response.headers["location"] == "/admin/students/new"

        entities = await repo.entities.list_by_tenant(tenant_id)
        assert entities == []

    async def test_create_unknown_kind_falls_back_to_student(
        self, client: AsyncClient, repo: Repository, tenant_id: int
    ) -> None:
        """Неизвестный тип сущности заменяется на ``student``."""
        await client.post(
            "/admin/students",
            data={"name": "Миша", "entity_type": "робот", "subject": "Шахматы"},
        )

        entity = await repo.entities.get_by_name(tenant_id, "Миша")
        assert entity is not None
        assert entity.entity_type == "student"

    async def test_create_writes_audit_log(
        self, client: AsyncClient, repo: Repository, tenant_id: int
    ) -> None:
        """Создание ученика фиксируется в журнале действий."""
        await client.post(
            "/admin/students",
            data={"name": "Миша", "entity_type": "student", "subject": "Шахматы"},
        )

        entries = await repo.audit.list_by_tenant(tenant_id)
        assert any(entry.action == "student_created" for entry in entries)

    async def test_create_requires_session(self, anon_client: AsyncClient) -> None:
        """Без сессии создание недоступно — 401."""
        response = await anon_client.post(
            "/admin/students",
            data={"name": "Миша", "entity_type": "student", "subject": "Шахматы"},
            follow_redirects=False,
        )

        assert response.status_code == 401


class TestStudentCard:
    """Проверки карточки ученика."""

    async def test_card_renders_profile(
        self, client: AsyncClient, repo: Repository, tenant_id: int
    ) -> None:
        """Карточка показывает имя и предмет ученика."""
        entity = await repo.entities.create(
            tenant_id=tenant_id,
            name="Миша",
            entity_type="student",
            subject="Шахматы",
        )

        response = await client.get(f"/admin/students/{entity.id}")

        assert response.status_code == 200
        assert "Миша" in response.text
        assert "Шахматы" in response.text

    async def test_card_group_shows_chat_button(
        self, client: AsyncClient, repo: Repository, tenant_id: int
    ) -> None:
        """Карточка группы показывает кнопку перехода в чат WhatsApp."""
        entity = await repo.entities.create(
            tenant_id=tenant_id,
            name="Группа А",
            entity_type="group",
            whatsapp_chat_url="https://chat.whatsapp.com/AbCdEf123",
        )

        response = await client.get(f"/admin/students/{entity.id}")

        assert response.status_code == 200
        assert "https://chat.whatsapp.com/AbCdEf123" in response.text
        assert "Открыть чат группы в WhatsApp" in response.text

    async def test_card_group_without_chat_hides_button(
        self, client: AsyncClient, repo: Repository, tenant_id: int
    ) -> None:
        """Без ссылки на чат кнопка группы не показывается."""
        entity = await repo.entities.create(
            tenant_id=tenant_id, name="Группа А", entity_type="group"
        )

        response = await client.get(f"/admin/students/{entity.id}")

        assert response.status_code == 200
        assert "Открыть чат группы в WhatsApp" not in response.text

    async def test_card_shows_lessons(
        self, client: AsyncClient, repo: Repository, tenant_id: int
    ) -> None:
        """Карточка показывает последние занятия ученика."""
        entity = await repo.entities.create(tenant_id=tenant_id, name="Миша", entity_type="student")
        await repo.lessons.create(
            tenant_id=tenant_id,
            entity_id=entity.id,
            lesson_date=LESSON_DATE,
            topic="Дебютный принцип",
        )

        response = await client.get(f"/admin/students/{entity.id}")

        assert response.status_code == 200
        assert "Дебютный принцип" in response.text

    async def test_card_renders_regular_slots(
        self, client: AsyncClient, repo: Repository, tenant_id: int
    ) -> None:
        """Карточка рендерит регулярные слоты без ошибки 500.

        Регрессия: шаблон обращался к несуществующим атрибутам
        ``ScheduleSlot`` (``weekday_label``, ``place``, ``duration_minutes``),
        из-за чего карточка падала с «Внутренней ошибкой».
        """
        entity = await repo.entities.create(tenant_id=tenant_id, name="Миша", entity_type="student")
        await repo.schedule.create(
            tenant_id=tenant_id,
            entity_id=entity.id,
            weekday="tue",
            start_time="17:30",
            duration_min=45,
            location="Онлайн",
        )

        response = await client.get(f"/admin/students/{entity.id}")

        assert response.status_code == 200
        assert "Вторник" in response.text
        assert "17:30" in response.text
        assert "Онлайн" in response.text
        assert "45 мин" in response.text

    async def test_card_renders_payments(
        self, client: AsyncClient, repo: Repository, tenant_id: int
    ) -> None:
        """Карточка рендерит платежи без ошибки 500.

        Регрессия: шаблон обращался к несуществующим атрибутам ``Payment``
        (``paid_at``, ``lessons_count``) вместо ``payment_date`` и
        ``lessons_added``.
        """
        entity = await repo.entities.create(tenant_id=tenant_id, name="Миша", entity_type="student")
        await repo.payments.create(
            tenant_id=tenant_id,
            entity_id=entity.id,
            payment_date=LESSON_DATE,
            amount=400.0,
            lessons_added=8,
        )

        response = await client.get(f"/admin/students/{entity.id}")

        assert response.status_code == 200
        assert "400" in response.text
        assert "8" in response.text

    async def test_card_has_slot_form(
        self, client: AsyncClient, repo: Repository, tenant_id: int
    ) -> None:
        """На карточке есть форма добавления слота с предзаполненным учеником."""
        entity = await repo.entities.create(tenant_id=tenant_id, name="Миша", entity_type="student")

        response = await client.get(f"/admin/students/{entity.id}")

        assert response.status_code == 200
        assert 'action="/admin/schedule/slots"' in response.text
        assert f'name="entity_id" value="{entity.id}"' in response.text
        assert "Добавить слот" in response.text

    async def test_card_slot_form_creates_slot(
        self, client: AsyncClient, repo: Repository, tenant_id: int
    ) -> None:
        """Отправка формы с карточки создаёт регулярный слот."""
        entity = await repo.entities.create(tenant_id=tenant_id, name="Миша", entity_type="student")

        response = await client.post(
            "/admin/schedule/slots",
            data={
                "entity_id": str(entity.id),
                "weekday": "wed",
                "start_time": "16:00",
                "duration_minutes": "45",
                "place": "Онлайн",
            },
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert response.headers["location"] == "/admin/schedule?saved=1"

        slots = await repo.schedule.list_by_entity(tenant_id, entity.id)
        assert len(slots) == 1
        assert slots[0].weekday == "wed"
        assert slots[0].start_time == "16:00"
        assert slots[0].duration_min == 45
        assert slots[0].location == "Онлайн"

    async def test_card_no_placeholder_about_schedule_page(
        self, client: AsyncClient, repo: Repository, tenant_id: int
    ) -> None:
        """Текст-заглушка «добавьте в разделе Расписание» убран."""
        entity = await repo.entities.create(tenant_id=tenant_id, name="Миша", entity_type="student")

        response = await client.get(f"/admin/students/{entity.id}")

        assert response.status_code == 200
        assert "Регулярных занятий нет. Добавьте их в разделе" not in response.text

    async def test_card_unknown_redirects_to_list(self, client: AsyncClient) -> None:
        """Несуществующий ученик редиректит на список."""
        response = await client.get("/admin/students/999999")

        assert response.status_code == 303
        assert response.headers["location"] == "/admin/students"

    async def test_card_scoped_to_tenant(self, client: AsyncClient, repo: Repository) -> None:
        """Чужой ученик недоступен — редирект на список."""
        other = await repo.tenants.create(telegram_user_id=999_000_222, name="Другой тренер")
        foreign = await repo.entities.create(
            tenant_id=other.id, name="Чужой", entity_type="student"
        )

        response = await client.get(f"/admin/students/{foreign.id}")

        assert response.status_code == 303
        assert response.headers["location"] == "/admin/students"

    async def test_card_requires_session(self, anon_client: AsyncClient) -> None:
        """Без сессии карточка недоступна — 401."""
        response = await anon_client.get("/admin/students/1", follow_redirects=False)

        assert response.status_code == 401


class TestStudentUpdate:
    """Проверки редактирования профиля ученика."""

    async def test_update_changes_name(
        self, client: AsyncClient, repo: Repository, tenant_id: int
    ) -> None:
        """Новое имя сохраняется в БД."""
        entity = await repo.entities.create(tenant_id=tenant_id, name="Миша", entity_type="student")

        response = await client.post(
            f"/admin/students/{entity.id}",
            data={"name": "Михаил", "entity_type": "student", "subject": "Шахматы"},
        )

        assert response.status_code == 303
        assert response.headers["location"] == f"/admin/students/{entity.id}?saved=1"

        updated = await repo.entities.get_by_id(tenant_id, entity.id)
        assert updated is not None
        assert updated.name == "Михаил"

    async def test_update_changes_kind_and_subject(
        self, client: AsyncClient, repo: Repository, tenant_id: int
    ) -> None:
        """Тип и предмет тоже обновляются."""
        entity = await repo.entities.create(tenant_id=tenant_id, name="Миша", entity_type="student")

        await client.post(
            f"/admin/students/{entity.id}",
            data={"name": "Миша", "entity_type": "group", "subject": "Математика"},
        )

        updated = await repo.entities.get_by_id(tenant_id, entity.id)
        assert updated is not None
        assert updated.entity_type == "group"
        assert updated.subject == "Математика"

    async def test_update_group_members(
        self, client: AsyncClient, repo: Repository, tenant_id: int
    ) -> None:
        """Участники группы обновляются через карточку."""
        entity = await repo.entities.create(
            tenant_id=tenant_id, name="Группа А", entity_type="group"
        )

        await client.post(
            f"/admin/students/{entity.id}",
            data={
                "name": "Группа А",
                "entity_type": "group",
                "subject": "Шахматы",
                "members": "Аня, Марк",
            },
        )

        updated = await repo.entities.get_by_id(tenant_id, entity.id)
        assert updated is not None
        assert updated.members == "Аня, Марк"

    async def test_update_group_chat_url(
        self, client: AsyncClient, repo: Repository, tenant_id: int
    ) -> None:
        """Ссылка на чат WhatsApp обновляется через карточку."""
        entity = await repo.entities.create(
            tenant_id=tenant_id, name="Группа А", entity_type="group"
        )

        await client.post(
            f"/admin/students/{entity.id}",
            data={
                "name": "Группа А",
                "entity_type": "group",
                "subject": "Шахматы",
                "whatsapp_chat_url": "https://chat.whatsapp.com/AbCdEf123",
            },
        )

        updated = await repo.entities.get_by_id(tenant_id, entity.id)
        assert updated is not None
        assert updated.whatsapp_chat_url == "https://chat.whatsapp.com/AbCdEf123"

    async def test_update_group_clears_chat_url(
        self, client: AsyncClient, repo: Repository, tenant_id: int
    ) -> None:
        """Пустое поле ссылки очищает чат группы."""
        entity = await repo.entities.create(
            tenant_id=tenant_id,
            name="Группа А",
            entity_type="group",
            whatsapp_chat_url="https://chat.whatsapp.com/AbCdEf123",
        )

        await client.post(
            f"/admin/students/{entity.id}",
            data={
                "name": "Группа А",
                "entity_type": "group",
                "subject": "Шахматы",
                "whatsapp_chat_url": "   ",
            },
        )

        updated = await repo.entities.get_by_id(tenant_id, entity.id)
        assert updated is not None
        assert updated.whatsapp_chat_url is None

    async def test_update_sets_lesson_price(
        self, client: AsyncClient, repo: Repository, tenant_id: int
    ) -> None:
        """Индивидуальная цена устанавливается при редактировании."""
        entity = await repo.entities.create(tenant_id=tenant_id, name="Миша", entity_type="student")

        await client.post(
            f"/admin/students/{entity.id}",
            data={
                "name": "Миша",
                "entity_type": "student",
                "subject": "Шахматы",
                "lesson_price": "150",
            },
        )

        updated = await repo.entities.get_by_id(tenant_id, entity.id)
        assert updated is not None
        assert updated.lesson_price == 150.0

    async def test_update_clears_lesson_price(
        self, client: AsyncClient, repo: Repository, tenant_id: int
    ) -> None:
        """Пустое поле цены возвращает «как в настройках»."""
        entity = await repo.entities.create(
            tenant_id=tenant_id,
            name="Миша",
            entity_type="student",
            lesson_price=150.0,
        )

        await client.post(
            f"/admin/students/{entity.id}",
            data={
                "name": "Миша",
                "entity_type": "student",
                "subject": "Шахматы",
                "lesson_price": "   ",
            },
        )

        updated = await repo.entities.get_by_id(tenant_id, entity.id)
        assert updated is not None
        assert updated.lesson_price is None

    async def test_update_group_clears_members(
        self, client: AsyncClient, repo: Repository, tenant_id: int
    ) -> None:
        """Пустое поле участников очищает список."""
        entity = await repo.entities.create(
            tenant_id=tenant_id,
            name="Группа А",
            entity_type="group",
            members="Аня, Марк",
        )

        await client.post(
            f"/admin/students/{entity.id}",
            data={
                "name": "Группа А",
                "entity_type": "group",
                "subject": "Шахматы",
                "members": "   ",
            },
        )

        updated = await repo.entities.get_by_id(tenant_id, entity.id)
        assert updated is not None
        assert updated.members is None

    async def test_update_blank_name_returns_error(
        self, client: AsyncClient, repo: Repository, tenant_id: int
    ) -> None:
        """Пустое имя не сохраняется, возвращается ошибка."""
        entity = await repo.entities.create(tenant_id=tenant_id, name="Миша", entity_type="student")

        response = await client.post(
            f"/admin/students/{entity.id}",
            data={"name": "  ", "entity_type": "student", "subject": "Шахматы"},
        )

        assert response.status_code == 303
        assert "error=" in response.headers["location"]

        updated = await repo.entities.get_by_id(tenant_id, entity.id)
        assert updated is not None
        assert updated.name == "Миша"

    async def test_update_without_changes_skips_audit(
        self, client: AsyncClient, repo: Repository, tenant_id: int
    ) -> None:
        """Если ничего не изменилось, журнал не пополняется."""
        entity = await repo.entities.create(
            tenant_id=tenant_id,
            name="Миша",
            entity_type="student",
            subject="Шахматы",
        )

        await client.post(
            f"/admin/students/{entity.id}",
            data={"name": "Миша", "entity_type": "student", "subject": "Шахматы"},
        )

        entries = await repo.audit.list_by_tenant(tenant_id)
        assert not any(entry.action == "student_updated" for entry in entries)

    async def test_update_writes_audit_log(
        self, client: AsyncClient, repo: Repository, tenant_id: int
    ) -> None:
        """Изменение профиля фиксируется в журнале."""
        entity = await repo.entities.create(tenant_id=tenant_id, name="Миша", entity_type="student")

        await client.post(
            f"/admin/students/{entity.id}",
            data={"name": "Михаил", "entity_type": "student", "subject": "Шахматы"},
        )

        entries = await repo.audit.list_by_tenant(tenant_id)
        assert any(entry.action == "student_updated" for entry in entries)

    async def test_update_unknown_redirects_to_list(self, client: AsyncClient) -> None:
        """Редактирование несуществующего ученика редиректит на список."""
        response = await client.post(
            "/admin/students/999999",
            data={"name": "Миша", "entity_type": "student", "subject": "Шахматы"},
        )

        assert response.status_code == 303
        assert response.headers["location"] == "/admin/students"


class TestStudentDelete:
    """Проверки удаления ученика."""

    async def test_delete_removes_entity(
        self, client: AsyncClient, repo: Repository, tenant_id: int
    ) -> None:
        """Удаление убирает ученика из БД."""
        entity = await repo.entities.create(tenant_id=tenant_id, name="Миша", entity_type="student")

        response = await client.post(f"/admin/students/{entity.id}/delete")

        assert response.status_code == 303
        assert response.headers["location"] == "/admin/students"
        assert await repo.entities.get_by_id(tenant_id, entity.id) is None

    async def test_delete_writes_audit_log(
        self, client: AsyncClient, repo: Repository, tenant_id: int
    ) -> None:
        """Удаление фиксируется в журнале."""
        entity = await repo.entities.create(tenant_id=tenant_id, name="Миша", entity_type="student")

        await client.post(f"/admin/students/{entity.id}/delete")

        entries = await repo.audit.list_by_tenant(tenant_id)
        assert any(entry.action == "student_deleted" for entry in entries)

    async def test_delete_unknown_is_idempotent(self, client: AsyncClient) -> None:
        """Повторное удаление не падает."""
        response = await client.post("/admin/students/999999/delete")

        assert response.status_code == 303
        assert response.headers["location"] == "/admin/students"

    async def test_delete_scoped_to_tenant(self, client: AsyncClient, repo: Repository) -> None:
        """Чужого ученика удалить нельзя."""
        other = await repo.tenants.create(telegram_user_id=999_000_333, name="Другой тренер")
        foreign = await repo.entities.create(
            tenant_id=other.id, name="Чужой", entity_type="student"
        )

        await client.post(f"/admin/students/{foreign.id}/delete")

        assert await repo.entities.get_by_id(other.id, foreign.id) is not None


class TestStudentPayment:
    """Проверки внесения оплаты из карточки ученика или группы."""

    async def test_register_payment_by_amount(
        self, client: AsyncClient, repo: Repository, tenant_id: int
    ) -> None:
        """Оплата суммой пополняет баланс и создаёт платёж."""
        await repo.tenants.update_settings(tenant_id, lesson_price=100.0)
        entity = await repo.entities.create(tenant_id=tenant_id, name="Миша")

        response = await client.post(
            f"/admin/students/{entity.id}/payment",
            data={"amount": "300", "payment_date": "2026-09-10"},
        )

        assert response.status_code == 303
        assert response.headers["location"] == f"/admin/students/{entity.id}?saved=1"

        payments = await repo.payments.list_by_entity(tenant_id, entity.id)
        assert len(payments) == 1
        assert payments[0].amount == 300.0
        assert payments[0].lessons_added == 3

    async def test_register_payment_by_lessons_only(
        self, client: AsyncClient, repo: Repository, tenant_id: int
    ) -> None:
        """Можно указать только количество занятий без суммы."""
        entity = await repo.entities.create(tenant_id=tenant_id, name="Миша")

        response = await client.post(
            f"/admin/students/{entity.id}/payment",
            data={"lessons_added": "4"},
        )

        assert response.status_code == 303
        payments = await repo.payments.list_by_entity(tenant_id, entity.id)
        assert len(payments) == 1
        assert payments[0].amount is None
        assert payments[0].lessons_added == 4

    async def test_register_payment_without_amount_or_lessons_returns_error(
        self, client: AsyncClient, repo: Repository, tenant_id: int
    ) -> None:
        """Без суммы и занятий возвращает ошибку и не создаёт платёж."""
        entity = await repo.entities.create(tenant_id=tenant_id, name="Миша")

        response = await client.post(f"/admin/students/{entity.id}/payment", data={})

        assert response.status_code == 303
        assert "error=" in response.headers["location"]
        payments = await repo.payments.list_by_entity(tenant_id, entity.id)
        assert not payments

    async def test_register_payment_invalid_amount_returns_error(
        self, client: AsyncClient, repo: Repository, tenant_id: int
    ) -> None:
        """Некорректная сумма (ноль) отклоняется."""
        entity = await repo.entities.create(tenant_id=tenant_id, name="Миша")

        response = await client.post(f"/admin/students/{entity.id}/payment", data={"amount": "0"})

        assert response.status_code == 303
        assert "error=" in response.headers["location"]
        payments = await repo.payments.list_by_entity(tenant_id, entity.id)
        assert not payments

    async def test_register_payment_defaults_to_today(
        self, client: AsyncClient, repo: Repository, tenant_id: int
    ) -> None:
        """Без даты используется сегодняшняя дата."""
        entity = await repo.entities.create(tenant_id=tenant_id, name="Миша")

        await client.post(f"/admin/students/{entity.id}/payment", data={"lessons_added": "2"})

        payments = await repo.payments.list_by_entity(tenant_id, entity.id)
        assert len(payments) == 1
        assert payments[0].payment_date == date.today()

    async def test_register_payment_writes_audit_log(
        self, client: AsyncClient, repo: Repository, tenant_id: int
    ) -> None:
        """Внесение оплаты фиксируется в журнале."""
        entity = await repo.entities.create(tenant_id=tenant_id, name="Миша")

        await client.post(f"/admin/students/{entity.id}/payment", data={"lessons_added": "2"})

        entries = await repo.audit.list_by_tenant(tenant_id)
        assert any(entry.action == "payment_registered" for entry in entries)

    async def test_register_payment_unknown_redirects_to_list(self, client: AsyncClient) -> None:
        """Оплата несуществующего ученика редиректит на список."""
        response = await client.post("/admin/students/999999/payment", data={"lessons_added": "2"})

        assert response.status_code == 303
        assert response.headers["location"] == "/admin/students"

    async def test_register_payment_requires_session(
        self, anon_client: AsyncClient, repo: Repository, tenant_id: int
    ) -> None:
        """Без сессии оплата недоступна."""
        entity = await repo.entities.create(tenant_id=tenant_id, name="Миша")

        response = await anon_client.post(
            f"/admin/students/{entity.id}/payment", data={"lessons_added": "2"}
        )

        assert response.status_code == 401

    async def test_card_group_shows_monthly_debt(
        self, client: AsyncClient, repo: Repository, tenant_id: int
    ) -> None:
        """Карточка группы показывает блок «Долг за месяц»."""
        await repo.tenants.update_settings(tenant_id, lesson_price=100.0)
        group = await repo.entities.create(tenant_id=tenant_id, name="Группа", entity_type="group")
        await repo.lessons.create(
            tenant_id=tenant_id,
            entity_id=group.id,
            lesson_date=date.today(),
            topic="Занятие",
            attendance="Аня",
        )

        response = await client.get(f"/admin/students/{group.id}")

        assert response.status_code == 200
        assert "Долг за месяц" in response.text
        assert "Внести оплату" in response.text

    async def test_card_student_hides_monthly_debt(
        self, client: AsyncClient, repo: Repository, tenant_id: int
    ) -> None:
        """Карточка частного ученика не показывает блок долга за месяц."""
        entity = await repo.entities.create(tenant_id=tenant_id, name="Миша")

        response = await client.get(f"/admin/students/{entity.id}")

        assert response.status_code == 200
        assert "Долг за месяц" not in response.text
        assert "Внести оплату" in response.text


class TestStudentPhone:
    """Проверки привязки телефона WhatsApp."""

    async def test_set_phone_normalizes(
        self, client: AsyncClient, repo: Repository, tenant_id: int
    ) -> None:
        """Телефон сохраняется в нормализованном виде."""
        entity = await repo.entities.create(tenant_id=tenant_id, name="Миша", entity_type="student")

        response = await client.post(
            f"/admin/students/{entity.id}/phone",
            data={"parent_phone": "054-123-45-67"},
        )

        assert response.status_code == 303
        assert response.headers["location"] == f"/admin/students/{entity.id}?saved=1"

        updated = await repo.entities.get_by_id(tenant_id, entity.id)
        assert updated is not None
        assert updated.parent_phone == "972541234567"

    async def test_set_phone_invalid_returns_error(
        self, client: AsyncClient, repo: Repository, tenant_id: int
    ) -> None:
        """Мусор вместо телефона отклоняется с ошибкой."""
        entity = await repo.entities.create(tenant_id=tenant_id, name="Миша", entity_type="student")

        response = await client.post(
            f"/admin/students/{entity.id}/phone",
            data={"parent_phone": "позвоните маме"},
        )

        assert response.status_code == 303
        assert "error=" in response.headers["location"]

        updated = await repo.entities.get_by_id(tenant_id, entity.id)
        assert updated is not None
        assert updated.parent_phone is None

    async def test_set_phone_writes_audit_log(
        self, client: AsyncClient, repo: Repository, tenant_id: int
    ) -> None:
        """Привязка телефона фиксируется в журнале."""
        entity = await repo.entities.create(tenant_id=tenant_id, name="Миша", entity_type="student")

        await client.post(
            f"/admin/students/{entity.id}/phone",
            data={"parent_phone": "0541234567"},
        )

        entries = await repo.audit.list_by_tenant(tenant_id)
        assert any(entry.action == "student_updated" for entry in entries)

    async def test_set_phone_unknown_redirects_to_list(self, client: AsyncClient) -> None:
        """Привязка телефона несуществующему ученику редиректит на список."""
        response = await client.post(
            "/admin/students/999999/phone", data={"parent_phone": "0541234567"}
        )

        assert response.status_code == 303
        assert response.headers["location"] == "/admin/students"


class TestStudentHelpers:
    """Проверки вспомогательных функций раздела."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("0541234567", "972541234567"),
            ("+972 54 123 45 67", "972541234567"),
            ("972-54-123-45-67", "972541234567"),
            ("(054) 123-45-67", "972541234567"),
        ],
    )
    def test_normalize_phone_valid(self, raw: str, expected: str) -> None:
        """Разные форматы приводятся к одному виду."""
        from app.web.admin_students_routes import _normalize_phone

        assert _normalize_phone(raw) == expected

    @pytest.mark.parametrize("raw", [None, "", "   ", "нет цифр"])
    def test_normalize_phone_invalid(self, raw: str | None) -> None:
        """Пустое значение и мусор дают ``None``."""
        from app.web.admin_students_routes import _normalize_phone

        assert _normalize_phone(raw) is None

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("https://chat.whatsapp.com/AbCdEf123", "https://chat.whatsapp.com/AbCdEf123"),
            ("chat.whatsapp.com/AbCdEf123", "https://chat.whatsapp.com/AbCdEf123"),
            ("https://chat.whatsapp.com/AbCdEf123/", "https://chat.whatsapp.com/AbCdEf123"),
            ("  https://chat.whatsapp.com/AbCdEf123  ", "https://chat.whatsapp.com/AbCdEf123"),
        ],
    )
    def test_normalize_chat_url_valid(self, raw: str, expected: str) -> None:
        """Ссылка на чат приводится к каноническому виду."""
        from app.web.admin_students_routes import _normalize_chat_url

        assert _normalize_chat_url(raw) == expected

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("80", 80.0),
            ("120.50", 120.5),
            ("120,50", 120.5),
            ("  150  ", 150.0),
        ],
    )
    def test_parse_lesson_price_valid(self, raw: str, expected: float) -> None:
        """Корректная цена разбирается в число."""
        from app.web.admin_students_routes import _parse_lesson_price

        assert _parse_lesson_price(raw) == expected

    @pytest.mark.parametrize("raw", [None, "", "   ", "abc", "0", "-10", "999999"])
    def test_parse_lesson_price_invalid(self, raw: str | None) -> None:
        """Пустое, мусорное или выходящее за границы значение даёт ``None``."""
        from app.web.admin_students_routes import _parse_lesson_price

        assert _parse_lesson_price(raw) is None

    @pytest.mark.parametrize(
        "raw",
        [
            None,
            "",
            "   ",
            "https://example.com/chat",
            "https://wa.me/972541234567",
            "https://chat.whatsapp.com/",
            "javascript:alert(1)",
        ],
    )
    def test_normalize_chat_url_invalid(self, raw: str | None) -> None:
        """Посторонние и пустые ссылки дают ``None``."""
        from app.web.admin_students_routes import _normalize_chat_url

        assert _normalize_chat_url(raw) is None

    def test_build_filters_normalizes(self) -> None:
        """Фильтры приводятся к нижнему регистру и валидируются."""
        from app.web.admin_students_routes import _build_filters

        filters = _build_filters("GROUP", "TUE")

        assert filters == {"kind": "group", "weekday": "tue"}

    def test_build_filters_rejects_unknown(self) -> None:
        """Неизвестные значения обнуляются."""
        from app.web.admin_students_routes import _build_filters

        filters = _build_filters("робот", "нет")

        assert filters == {"kind": "", "weekday": ""}

    def test_filters_active(self) -> None:
        """Признак активности фильтров."""
        from app.web.admin_students_routes import _filters_active

        assert _filters_active({"kind": "", "weekday": ""}) is False
        assert _filters_active({"kind": "group", "weekday": ""}) is True
        assert _filters_active({"kind": "", "weekday": "tue"}) is True

    def test_sort_entities_students_before_groups(self) -> None:
        """Сначала ученики, затем группы; внутри — по имени без учёта регистра."""
        from app.db.models import Entity
        from app.web.admin_students_routes import _sort_entities

        entities = [
            Entity(id=1, tenant_id=1, name="Яна", entity_type="student"),
            Entity(id=2, tenant_id=1, name="Группа А", entity_type="group"),
            Entity(id=3, tenant_id=1, name="аня", entity_type="student"),
            Entity(id=4, tenant_id=1, name="Группа Б", entity_type="group"),
        ]

        ordered = _sort_entities(entities)

        assert [item.name for item in ordered] == ["аня", "Яна", "Группа А", "Группа Б"]

    def test_sort_entities_empty(self) -> None:
        """Пустой список сортируется без ошибок."""
        from app.web.admin_students_routes import _sort_entities

        assert _sort_entities([]) == []
