"""Тесты разделов кабинета «Настройки» и «Напоминания».

Покрывают:

* ``GET /admin/settings`` — форма настроек тенанта;
* ``POST /admin/settings`` — сохранение цены, валюты, пояса и оплаты;
* ``GET /admin/reminders`` — список правил и предпросмотр текста;
* ``POST /admin/reminders/defaults`` — создание стандартного набора правил;
* ``POST /admin/reminders/{id}`` — обновление правила;
* ``POST /admin/reminders/{id}/delete`` — удаление правила;
* вспомогательные функции раздела (``_parse_float``, ``_parse_int``,
  ``_checkbox``, ``_human_schedule``).
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repository import Repository
from app.services.auth_service import SESSION_COOKIE_NAME, AuthService
from app.services.reminder_service import ReminderService
from app.web.admin_settings_routes import (
    CHANNEL_OPTIONS,
    REMINDER_LABELS,
    TIMEZONE_OPTIONS,
    _checkbox,
    _human_schedule,
    _parse_float,
    _parse_int,
)
from app.web.app import create_app
from app.web.dependencies import get_db_session


@pytest_asyncio.fixture
async def cabinet_token(repo: Repository, tenant_id: int) -> str:
    """Создаёт секретный токен кабинета для тенанта.

    Args:
        repo: Фасад репозиториев.
        tenant_id: Идентификатор тенанта.

    Returns:
        Секретный токен для входа в кабинет.
    """
    token = await AuthService(repo).ensure_admin_token(tenant_id)
    assert token is not None
    return token


@pytest_asyncio.fixture
async def client(session: AsyncSession, cabinet_token: str) -> AsyncIterator[AsyncClient]:
    """HTTP-клиент с уже установленной cookie-сессией тренера.

    Args:
        session: Асинхронная сессия БД.
        cabinet_token: Секретный токен кабинета.

    Yields:
        Клиент httpx, авторизованный в кабинете.
    """
    app = create_app()

    async def _override_session() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_db_session] = _override_session

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http_client:
        # Вход по секретной ссылке выставляет cookie-сессию.
        login = await http_client.get(f"/admin/{cabinet_token}", follow_redirects=False)
        assert login.status_code in {302, 303, 307}
        assert SESSION_COOKIE_NAME in http_client.cookies
        yield http_client

    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def anon_client(session: AsyncSession) -> AsyncIterator[AsyncClient]:
    """HTTP-клиент без cookie-сессии.

    Args:
        session: Асинхронная сессия БД.

    Yields:
        Неавторизованный клиент httpx.
    """
    app = create_app()

    async def _override_session() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_db_session] = _override_session

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http_client:
        yield http_client

    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# GET /admin/settings
# ---------------------------------------------------------------------------


class TestSettingsPage:
    """Проверки страницы настроек тенанта."""

    async def test_settings_page_renders(self, client: AsyncClient) -> None:
        """Страница настроек отдаётся с формой."""
        response = await client.get("/admin/settings")

        assert response.status_code == 200
        assert "Настройки" in response.text
        assert "Цена занятия" in response.text
        assert "Порядок оплаты" in response.text

    async def test_settings_page_shows_current_values(
        self, client: AsyncClient, repo: Repository, tenant_id: int
    ) -> None:
        """Форма показывает сохранённые значения из БД."""
        await repo.tenants.update_settings(tenant_id, lesson_price=175.5, currency="USD")

        response = await client.get("/admin/settings")

        assert response.status_code == 200
        assert "175.5" in response.text
        assert "USD" in response.text

    async def test_settings_page_shows_timezone_options(self, client: AsyncClient) -> None:
        """В форме перечислены доступные часовые пояса."""
        response = await client.get("/admin/settings")

        assert response.status_code == 200
        for zone in TIMEZONE_OPTIONS:
            assert zone in response.text

    async def test_settings_page_shows_admin_link(
        self, client: AsyncClient, cabinet_token: str
    ) -> None:
        """Страница показывает секретную ссылку на кабинет."""
        response = await client.get("/admin/settings")

        assert response.status_code == 200
        assert cabinet_token in response.text

    async def test_settings_page_shows_saved_banner(self, client: AsyncClient) -> None:
        """После сохранения показывается подтверждение."""
        response = await client.get("/admin/settings?saved=1")

        assert response.status_code == 200
        assert "Настройки сохранены" in response.text

    async def test_settings_page_shows_error_banner(self, client: AsyncClient) -> None:
        """При ошибке показывается текст проблемы."""
        response = await client.get("/admin/settings?error=Что-то+сломалось")

        assert response.status_code == 200
        assert "Что-то сломалось" in response.text

    async def test_settings_page_requires_session(self, anon_client: AsyncClient) -> None:
        """Без сессии страница недоступна."""
        response = await anon_client.get("/admin/settings")

        assert response.status_code == 401


# ---------------------------------------------------------------------------
# POST /admin/settings
# ---------------------------------------------------------------------------


class TestSettingsUpdate:
    """Проверки сохранения настроек тенанта."""

    async def test_update_saves_all_fields(
        self, client: AsyncClient, repo: Repository, tenant_id: int
    ) -> None:
        """Все поля формы сохраняются в БД."""
        response = await client.post(
            "/admin/settings",
            data={
                "lesson_price": "200",
                "currency": "usd",
                "timezone": "Europe/Berlin",
                "payment_instructions": "Перевод на карту до 5 числа.",
            },
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert response.headers["location"] == "/admin/settings?saved=1"

        tenant = await repo.tenants.get_by_id(tenant_id)
        assert tenant is not None
        assert tenant.lesson_price == 200
        assert tenant.currency == "USD"
        assert tenant.timezone == "Europe/Berlin"
        assert tenant.payment_instructions == "Перевод на карту до 5 числа."

    async def test_update_accepts_comma_decimal(
        self, client: AsyncClient, repo: Repository, tenant_id: int
    ) -> None:
        """Цена с запятой вместо точки разбирается корректно."""
        response = await client.post(
            "/admin/settings",
            data={"lesson_price": "150,50"},
            follow_redirects=False,
        )

        assert response.status_code == 303
        tenant = await repo.tenants.get_by_id(tenant_id)
        assert tenant is not None
        assert tenant.lesson_price == pytest.approx(150.5)

    async def test_update_clears_payment_instructions(
        self, client: AsyncClient, repo: Repository, tenant_id: int
    ) -> None:
        """Пустой текст оплаты очищает поле."""
        await repo.tenants.update_settings(tenant_id, payment_instructions="Старый текст")

        response = await client.post(
            "/admin/settings",
            data={"payment_instructions": "   "},
            follow_redirects=False,
        )

        assert response.status_code == 303
        tenant = await repo.tenants.get_by_id(tenant_id)
        assert tenant is not None
        assert tenant.payment_instructions is None

    async def test_update_ignores_blank_price(
        self, client: AsyncClient, repo: Repository, tenant_id: int
    ) -> None:
        """Пустая цена не затирает сохранённое значение."""
        await repo.tenants.update_settings(tenant_id, lesson_price=123)

        response = await client.post(
            "/admin/settings",
            data={"lesson_price": ""},
            follow_redirects=False,
        )

        assert response.status_code == 303
        tenant = await repo.tenants.get_by_id(tenant_id)
        assert tenant is not None
        assert tenant.lesson_price == 123

    async def test_update_rejects_bad_timezone(
        self, client: AsyncClient, repo: Repository, tenant_id: int
    ) -> None:
        """Неизвестный часовой пояс возвращает ошибку."""
        response = await client.post(
            "/admin/settings",
            data={"timezone": "Mars/Olympus"},
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert "error=" in response.headers["location"]

        tenant = await repo.tenants.get_by_id(tenant_id)
        assert tenant is not None
        assert tenant.timezone != "Mars/Olympus"

    async def test_update_rejects_non_positive_price(
        self, client: AsyncClient, repo: Repository, tenant_id: int
    ) -> None:
        """Нулевая цена отклоняется сервисом."""
        response = await client.post(
            "/admin/settings",
            data={"lesson_price": "0"},
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert "error=" in response.headers["location"]

    async def test_update_writes_audit_log(
        self, client: AsyncClient, repo: Repository, tenant_id: int
    ) -> None:
        """Изменение настроек попадает в журнал действий."""
        await client.post(
            "/admin/settings",
            data={"lesson_price": "210", "currency": "ILS"},
            follow_redirects=False,
        )

        entries = await repo.audit.list_by_tenant(tenant_id)
        assert any(entry.action == "settings_updated" for entry in entries)

    async def test_update_scoped_to_tenant(
        self, client: AsyncClient, repo: Repository, tenant_id: int
    ) -> None:
        """Настройки другого тенанта не затрагиваются."""
        other = await repo.tenants.create(telegram_user_id=3001, name="Другой тренер")
        await repo.tenants.update_settings(other.id, lesson_price=999)

        await client.post(
            "/admin/settings",
            data={"lesson_price": "111"},
            follow_redirects=False,
        )

        refreshed = await repo.tenants.get_by_id(other.id)
        assert refreshed is not None
        assert refreshed.lesson_price == 999

    async def test_update_requires_session(self, anon_client: AsyncClient) -> None:
        """Без сессии сохранение недоступно."""
        response = await anon_client.post(
            "/admin/settings",
            data={"lesson_price": "100"},
            follow_redirects=False,
        )

        assert response.status_code == 401


# ---------------------------------------------------------------------------
# GET /admin/reminders
# ---------------------------------------------------------------------------


class TestRemindersPage:
    """Проверки страницы правил напоминаний."""

    async def test_reminders_page_renders_empty_state(self, client: AsyncClient) -> None:
        """Без правил показывается предложение создать стандартные."""
        response = await client.get("/admin/reminders")

        assert response.status_code == 200
        assert "Напоминания" in response.text
        assert "Правила напоминаний ещё не созданы" in response.text
        assert "Создать стандартные правила" in response.text

    async def test_reminders_page_lists_rules(
        self, client: AsyncClient, repo: Repository, tenant_id: int
    ) -> None:
        """Созданные правила выводятся с подписями."""
        await ReminderService(repo).ensure_defaults(tenant_id)

        response = await client.get("/admin/reminders")

        assert response.status_code == 200
        for label in REMINDER_LABELS.values():
            assert label in response.text

    async def test_reminders_page_shows_preview(self, client: AsyncClient) -> None:
        """На странице есть предпросмотр текста сводки."""
        response = await client.get("/admin/reminders")

        assert response.status_code == 200
        assert "Предпросмотр" in response.text
        assert "Доброе утро" in response.text

    async def test_reminders_page_shows_channel_options(
        self, client: AsyncClient, repo: Repository, tenant_id: int
    ) -> None:
        """В форме правила перечислены каналы отправки."""
        await ReminderService(repo).ensure_defaults(tenant_id)

        response = await client.get("/admin/reminders")

        assert response.status_code == 200
        for value, _label in CHANNEL_OPTIONS:
            assert f'value="{value}"' in response.text

    async def test_reminders_page_shows_saved_banner(self, client: AsyncClient) -> None:
        """После сохранения показывается подтверждение."""
        response = await client.get("/admin/reminders?saved=1")

        assert response.status_code == 200
        assert "Правило сохранено" in response.text

    async def test_reminders_page_shows_error_banner(self, client: AsyncClient) -> None:
        """При ошибке показывается текст проблемы."""
        response = await client.get("/admin/reminders?error=Некорректное+время")

        assert response.status_code == 200
        assert "Некорректное время" in response.text

    async def test_reminders_page_scoped_to_tenant(
        self, client: AsyncClient, repo: Repository, tenant_id: int
    ) -> None:
        """Правила другого тенанта не показываются."""
        other = await repo.tenants.create(telegram_user_id=3002, name="Другой тренер")
        await ReminderService(repo).ensure_defaults(other.id)

        response = await client.get("/admin/reminders")

        assert response.status_code == 200
        assert "Правила напоминаний ещё не созданы" in response.text

    async def test_reminders_page_requires_session(self, anon_client: AsyncClient) -> None:
        """Без сессии страница недоступна."""
        response = await anon_client.get("/admin/reminders")

        assert response.status_code == 401


# ---------------------------------------------------------------------------
# POST /admin/reminders/defaults
# ---------------------------------------------------------------------------


class TestRemindersDefaults:
    """Проверки создания стандартного набора правил."""

    async def test_create_defaults_redirects(
        self, client: AsyncClient, repo: Repository, tenant_id: int
    ) -> None:
        """Кнопка создаёт правила и возвращает на страницу."""
        response = await client.post("/admin/reminders/defaults", follow_redirects=False)

        assert response.status_code == 303
        assert response.headers["location"] == "/admin/reminders?saved=1"

        rules = await repo.reminders.list_by_tenant(tenant_id)
        assert {rule.kind for rule in rules} == {
            "day_before",
            "morning_of",
        }

    async def test_create_defaults_is_idempotent(
        self, client: AsyncClient, repo: Repository, tenant_id: int
    ) -> None:
        """Повторное нажатие не создаёт дубликатов."""
        await client.post("/admin/reminders/defaults", follow_redirects=False)
        await client.post("/admin/reminders/defaults", follow_redirects=False)

        rules = await repo.reminders.list_by_tenant(tenant_id)
        assert len(rules) == 2

    async def test_create_defaults_requires_session(self, anon_client: AsyncClient) -> None:
        """Без сессии создание правил недоступно."""
        response = await anon_client.post("/admin/reminders/defaults", follow_redirects=False)

        assert response.status_code == 401


# ---------------------------------------------------------------------------
# POST /admin/reminders/{rule_id}
# ---------------------------------------------------------------------------


class TestReminderUpdate:
    """Проверки обновления правила напоминания."""

    async def test_update_changes_fields(
        self, client: AsyncClient, repo: Repository, tenant_id: int
    ) -> None:
        """Время, сдвиг и канал сохраняются."""
        rule = await ReminderService(repo).upsert_rule(tenant_id, "day_before", time_of_day="19:00")

        response = await client.post(
            f"/admin/reminders/{rule.id}",
            data={
                "send_at": "20:30",
                "offset_days": "1",
                "channel": "whatsapp",
                "enabled": "1",
            },
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert response.headers["location"] == "/admin/reminders?saved=1"

        updated = await repo.reminders.get_by_id(tenant_id, rule.id)
        assert updated is not None
        assert updated.time_of_day == "20:30"
        assert updated.offset_minutes == 24 * 60
        assert updated.channel == "whatsapp"
        assert updated.is_enabled is True

    async def test_update_disables_rule(
        self, client: AsyncClient, repo: Repository, tenant_id: int
    ) -> None:
        """Снятый флажок выключает правило."""
        rule = await ReminderService(repo).upsert_rule(tenant_id, "morning_of", time_of_day="08:00")

        response = await client.post(
            f"/admin/reminders/{rule.id}",
            data={"send_at": "08:00"},
            follow_redirects=False,
        )

        assert response.status_code == 303
        updated = await repo.reminders.get_by_id(tenant_id, rule.id)
        assert updated is not None
        assert updated.is_enabled is False

    async def test_update_rejects_bad_time(
        self, client: AsyncClient, repo: Repository, tenant_id: int
    ) -> None:
        """Некорректное время возвращает ошибку."""
        rule = await ReminderService(repo).upsert_rule(tenant_id, "day_before", time_of_day="19:00")

        response = await client.post(
            f"/admin/reminders/{rule.id}",
            data={"send_at": "25:99"},
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert "error=" in response.headers["location"]

        unchanged = await repo.reminders.get_by_id(tenant_id, rule.id)
        assert unchanged is not None
        assert unchanged.time_of_day == "19:00"

    async def test_update_rejects_bad_channel(
        self, client: AsyncClient, repo: Repository, tenant_id: int
    ) -> None:
        """Неизвестный канал возвращает ошибку."""
        rule = await ReminderService(repo).upsert_rule(tenant_id, "day_before", time_of_day="19:00")

        response = await client.post(
            f"/admin/reminders/{rule.id}",
            data={"channel": "carrier-pigeon"},
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert "error=" in response.headers["location"]

    async def test_update_unknown_rule_returns_error(self, client: AsyncClient) -> None:
        """Несуществующее правило даёт понятную ошибку."""
        response = await client.post(
            "/admin/reminders/99999",
            data={"send_at": "10:00"},
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert "error=" in response.headers["location"]

    async def test_update_scoped_to_tenant(
        self, client: AsyncClient, repo: Repository, tenant_id: int
    ) -> None:
        """Правило другого тенанта изменить нельзя."""
        other = await repo.tenants.create(telegram_user_id=3003, name="Другой тренер")
        foreign = await ReminderService(repo).upsert_rule(
            other.id, "day_before", time_of_day="19:00"
        )

        response = await client.post(
            f"/admin/reminders/{foreign.id}",
            data={"send_at": "21:00"},
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert "error=" in response.headers["location"]

        unchanged = await repo.reminders.get_by_id(other.id, foreign.id)
        assert unchanged is not None
        assert unchanged.time_of_day == "19:00"

    async def test_update_requires_session(
        self, anon_client: AsyncClient, repo: Repository, tenant_id: int
    ) -> None:
        """Без сессии обновление недоступно."""
        rule = await ReminderService(repo).upsert_rule(tenant_id, "day_before", time_of_day="19:00")

        response = await anon_client.post(
            f"/admin/reminders/{rule.id}",
            data={"send_at": "20:00"},
            follow_redirects=False,
        )

        assert response.status_code == 401


# ---------------------------------------------------------------------------
# POST /admin/reminders/{rule_id}/delete
# ---------------------------------------------------------------------------


class TestReminderDelete:
    """Проверки удаления правила напоминания."""

    async def test_delete_removes_rule(
        self, client: AsyncClient, repo: Repository, tenant_id: int
    ) -> None:
        """Правило удаляется из БД."""
        rule = await ReminderService(repo).upsert_rule(tenant_id, "day_before", time_of_day="09:00")

        response = await client.post(f"/admin/reminders/{rule.id}/delete", follow_redirects=False)

        assert response.status_code == 303
        assert response.headers["location"] == "/admin/reminders?saved=1"
        assert await repo.reminders.get_by_id(tenant_id, rule.id) is None

    async def test_delete_unknown_rule_is_idempotent(self, client: AsyncClient) -> None:
        """Удаление несуществующего правила не ломает страницу."""
        response = await client.post("/admin/reminders/99999/delete", follow_redirects=False)

        assert response.status_code == 303
        assert response.headers["location"] == "/admin/reminders?saved=1"

    async def test_delete_scoped_to_tenant(
        self, client: AsyncClient, repo: Repository, tenant_id: int
    ) -> None:
        """Правило другого тенанта не удаляется."""
        other = await repo.tenants.create(telegram_user_id=3004, name="Другой тренер")
        foreign = await ReminderService(repo).upsert_rule(
            other.id, "day_before", time_of_day="19:00"
        )

        response = await client.post(
            f"/admin/reminders/{foreign.id}/delete", follow_redirects=False
        )

        assert response.status_code == 303
        assert await repo.reminders.get_by_id(other.id, foreign.id) is not None

    async def test_delete_requires_session(
        self, anon_client: AsyncClient, repo: Repository, tenant_id: int
    ) -> None:
        """Без сессии удаление недоступно."""
        rule = await ReminderService(repo).upsert_rule(tenant_id, "day_before", time_of_day="19:00")

        response = await anon_client.post(
            f"/admin/reminders/{rule.id}/delete", follow_redirects=False
        )

        assert response.status_code == 401


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------


class TestSettingsHelpers:
    """Проверки вспомогательных функций раздела настроек."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("100", 100.0),
            ("150,50", 150.5),
            ("  42.25  ", 42.25),
            ("-5", -5.0),
        ],
    )
    def test_parse_float_valid(self, raw: str, expected: float) -> None:
        """Корректные числа разбираются, запятая заменяется точкой."""
        assert _parse_float(raw) == pytest.approx(expected)

    @pytest.mark.parametrize("raw", [None, "", "   ", "abc", "12abc"])
    def test_parse_float_invalid(self, raw: str | None) -> None:
        """Некорректные значения дают ``None``."""
        assert _parse_float(raw) is None

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("3", 3), ("  7  ", 7), ("-2", -2), ("0", 0)],
    )
    def test_parse_int_valid(self, raw: str, expected: int) -> None:
        """Целые числа разбираются."""
        assert _parse_int(raw) == expected

    @pytest.mark.parametrize("raw", [None, "", "   ", "три", "1.5"])
    def test_parse_int_invalid(self, raw: str | None) -> None:
        """Некорректные значения дают ``None``."""
        assert _parse_int(raw) is None

    @pytest.mark.parametrize("raw", ["1", "on", "true", "yes", "ON", " True "])
    def test_checkbox_truthy(self, raw: str) -> None:
        """Разные варианты «включено» дают ``True``."""
        assert _checkbox(raw) is True

    @pytest.mark.parametrize("raw", [None, "", "0", "off", "no", "нет"])
    def test_checkbox_falsy(self, raw: str | None) -> None:
        """Остальные значения дают ``False``."""
        assert _checkbox(raw) is False


class TestHumanSchedule:
    """Проверки человекочитаемого описания правила."""

    def test_human_schedule_without_offset(self) -> None:
        """Правило без сдвига описывается только временем."""

        class _Rule:
            time_of_day = "08:00"
            offset_minutes = 0
            is_enabled = True

        assert _human_schedule(_Rule()) == "в 08:00"

    def test_human_schedule_with_single_day_offset(self) -> None:
        """Сдвиг в один день описывается как «за 1 день»."""

        class _Rule:
            time_of_day = "19:00"
            offset_minutes = 24 * 60
            is_enabled = True

        assert _human_schedule(_Rule()) == "в 19:00, за 1 день"

    def test_human_schedule_with_multi_day_offset(self) -> None:
        """Сдвиг больше дня описывается числом дней."""

        class _Rule:
            time_of_day = "19:00"
            offset_minutes = 3 * 24 * 60
            is_enabled = True

        assert _human_schedule(_Rule()) == "в 19:00, за 3 дней"

    def test_human_schedule_marks_disabled(self) -> None:
        """Выключенное правило помечается."""

        class _Rule:
            time_of_day = "19:00"
            offset_minutes = 24 * 60
            is_enabled = False

        assert _human_schedule(_Rule()) == "в 19:00, за 1 день, выключено"
