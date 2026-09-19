"""Тесты аутентификации тренера в веб-кабинете.

Проверяют сервис ``AuthService`` (вход по секретной ссылке, проверка
сессии, выход) и HTTP-роуты кабинета ``/admin/*``: редирект после входа,
установку и удаление cookie, защиту дашборда и обработку чужого токена.

Тестовая БД подменяется через ``dependency_overrides``: приложение
получает ту же in-memory сессию, что и фикстуры pytest.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repository import Repository
from app.services.auth_service import (
    DEFAULT_SESSION_TTL_DAYS,
    MAX_SESSION_TTL_DAYS,
    MIN_SESSION_TTL_DAYS,
    SESSION_COOKIE_NAME,
    AuthService,
)
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
async def auth(repo: Repository) -> AuthService:
    """Сервис аутентификации поверх тестового репозитория.

    Args:
        repo: Фасад репозиториев.

    Returns:
        Экземпляр ``AuthService``.
    """
    return AuthService(repo)


@pytest_asyncio.fixture
async def admin_token(repo: Repository, tenant_id: int) -> str:
    """Секретный токен кабинета для тестового тенанта.

    Args:
        repo: Фасад репозиториев.
        tenant_id: Идентификатор тестового тенанта.

    Returns:
        Строка-токен кабинета.
    """
    token = await repo.tenants.ensure_admin_token(tenant_id)
    assert token is not None
    return token


class TestAuthServiceLogin:
    """Проверки входа по секретной ссылке."""

    async def test_login_creates_session(
        self, auth: AuthService, repo: Repository, tenant_id: int, admin_token: str
    ) -> None:
        """Успешный вход создаёт серверную сессию и возвращает токен."""
        result = await auth.login_by_admin_token(admin_token)

        assert result is not None
        assert result.tenant.id == tenant_id
        assert result.token
        assert result.session.token == result.token
        assert result.expires_at > datetime.now(UTC)

        stored = await repo.admin_sessions.get_by_token(result.token)
        assert stored is not None
        assert stored.tenant_id == tenant_id

    async def test_login_unknown_token_returns_none(self, auth: AuthService) -> None:
        """Неизвестный токен не создаёт сессию."""
        assert await auth.login_by_admin_token("нет-такого-токена") is None

    @pytest.mark.parametrize("raw", ["", "   ", "\t"])
    async def test_login_blank_token_returns_none(self, auth: AuthService, raw: str) -> None:
        """Пустой или пробельный токен отклоняется без обращения к БД."""
        assert await auth.login_by_admin_token(raw) is None

    async def test_login_strips_whitespace(self, auth: AuthService, admin_token: str) -> None:
        """Пробелы вокруг токена не мешают входу."""
        result = await auth.login_by_admin_token(f"  {admin_token}  ")
        assert result is not None

    async def test_login_respects_ttl(self, auth: AuthService, admin_token: str) -> None:
        """Явно заданный срок жизни влияет на момент истечения сессии."""
        result = await auth.login_by_admin_token(admin_token, ttl_days=7)

        assert result is not None
        delta = result.expires_at - datetime.now(UTC)
        assert timedelta(days=6) < delta < timedelta(days=8)

    async def test_login_clamps_ttl_to_minimum(self, auth: AuthService, admin_token: str) -> None:
        """Слишком маленький срок поднимается до минимума."""
        result = await auth.login_by_admin_token(admin_token, ttl_days=0)

        assert result is not None
        delta = result.expires_at - datetime.now(UTC)
        assert delta > timedelta(days=MIN_SESSION_TTL_DAYS - 1)

    async def test_login_clamps_ttl_to_maximum(self, auth: AuthService, admin_token: str) -> None:
        """Слишком большой срок опускается до максимума."""
        result = await auth.login_by_admin_token(admin_token, ttl_days=9999)

        assert result is not None
        delta = result.expires_at - datetime.now(UTC)
        assert delta < timedelta(days=MAX_SESSION_TTL_DAYS + 1)

    async def test_login_creates_distinct_sessions(
        self, auth: AuthService, admin_token: str
    ) -> None:
        """Повторный вход создаёт новую сессию, не переиспользуя старую."""
        first = await auth.login_by_admin_token(admin_token)
        second = await auth.login_by_admin_token(admin_token)

        assert first is not None
        assert second is not None
        assert first.token != second.token


class TestAuthServiceResolve:
    """Проверки проверки сессии по токену cookie."""

    async def test_resolve_returns_tenant(
        self, auth: AuthService, tenant_id: int, admin_token: str
    ) -> None:
        """Живая сессия возвращает своего тенанта."""
        result = await auth.login_by_admin_token(admin_token)
        assert result is not None

        tenant = await auth.resolve_session(result.token)

        assert tenant is not None
        assert tenant.id == tenant_id

    @pytest.mark.parametrize("raw", [None, "", "   "])
    async def test_resolve_without_token_returns_none(
        self, auth: AuthService, raw: str | None
    ) -> None:
        """Отсутствие cookie не даёт доступа."""
        assert await auth.resolve_session(raw) is None

    async def test_resolve_unknown_token_returns_none(self, auth: AuthService) -> None:
        """Неизвестный токен сессии не даёт доступа."""
        assert await auth.resolve_session("чужой-токен") is None

    async def test_resolve_expired_session_returns_none(
        self, auth: AuthService, repo: Repository, tenant_id: int
    ) -> None:
        """Истёкшая сессия не даёт доступа."""
        expired = await repo.admin_sessions.create(
            tenant_id,
            expires_at=datetime.now(UTC) - timedelta(days=1),
        )

        assert await auth.resolve_session(expired.token) is None

    async def test_resolve_scoped_to_own_tenant(
        self, auth: AuthService, repo: Repository, tenant_id: int, admin_token: str
    ) -> None:
        """Сессия одного тенанта не открывает доступ другому."""
        other = await repo.tenants.create(
            telegram_user_id=999_000_111,
            name="Другой тренер",
        )
        other_token = await repo.tenants.ensure_admin_token(other.id)
        assert other_token is not None

        result = await auth.login_by_admin_token(other_token)
        assert result is not None

        tenant = await auth.resolve_session(result.token)
        assert tenant is not None
        assert tenant.id == other.id
        assert tenant.id != tenant_id


class TestAuthServiceLogout:
    """Проверки выхода из кабинета."""

    async def test_logout_removes_session(self, auth: AuthService, admin_token: str) -> None:
        """Выход удаляет сессию, и токен перестаёт работать."""
        result = await auth.login_by_admin_token(admin_token)
        assert result is not None

        assert await auth.logout(result.token) is True
        assert await auth.resolve_session(result.token) is None

    async def test_logout_unknown_token_returns_false(self, auth: AuthService) -> None:
        """Выход по неизвестному токену не падает."""
        assert await auth.logout("нет-такого-токена") is False

    @pytest.mark.parametrize("raw", [None, "", "   "])
    async def test_logout_without_token_returns_false(
        self, auth: AuthService, raw: str | None
    ) -> None:
        """Выход без cookie безопасен."""
        assert await auth.logout(raw) is False

    async def test_logout_all_removes_every_session(
        self, auth: AuthService, tenant_id: int, admin_token: str
    ) -> None:
        """Выход со всех устройств удаляет все сессии тенанта."""
        first = await auth.login_by_admin_token(admin_token)
        second = await auth.login_by_admin_token(admin_token)
        assert first is not None
        assert second is not None

        removed = await auth.logout_all(tenant_id)

        assert removed == 2
        assert await auth.resolve_session(first.token) is None
        assert await auth.resolve_session(second.token) is None

    async def test_logout_all_keeps_other_tenants(
        self, auth: AuthService, repo: Repository, tenant_id: int, admin_token: str
    ) -> None:
        """Выход со всех устройств не затрагивает чужие сессии."""
        other = await repo.tenants.create(
            telegram_user_id=999_000_222,
            name="Другой тренер",
        )
        other_token = await repo.tenants.ensure_admin_token(other.id)
        assert other_token is not None

        mine = await auth.login_by_admin_token(admin_token)
        theirs = await auth.login_by_admin_token(other_token)
        assert mine is not None
        assert theirs is not None

        await auth.logout_all(tenant_id)

        assert await auth.resolve_session(mine.token) is None
        assert await auth.resolve_session(theirs.token) is not None


class TestAuthServiceTokens:
    """Проверки работы с секретным токеном тенанта."""

    async def test_ensure_admin_token_is_stable(self, auth: AuthService, tenant_id: int) -> None:
        """Повторный вызов возвращает тот же токен."""
        first = await auth.ensure_admin_token(tenant_id)
        second = await auth.ensure_admin_token(tenant_id)

        assert first is not None
        assert first == second

    async def test_ensure_admin_token_unknown_tenant(self, auth: AuthService) -> None:
        """Для несуществующего тенанта токен не создаётся."""
        assert await auth.ensure_admin_token(999_999) is None

    async def test_purge_expired_removes_only_expired(
        self, auth: AuthService, repo: Repository, tenant_id: int, admin_token: str
    ) -> None:
        """Очистка удаляет истёкшие сессии, сохраняя живые."""
        live = await auth.login_by_admin_token(admin_token)
        assert live is not None

        await repo.admin_sessions.create(
            tenant_id,
            expires_at=datetime.now(UTC) - timedelta(days=2),
        )

        removed = await auth.purge_expired()

        assert removed == 1
        assert await auth.resolve_session(live.token) is not None


class TestAdminLoginRoutes:
    """Проверки HTTP-входа в кабинет по секретной ссылке."""

    async def test_login_by_token_redirects_to_dashboard(
        self, client: AsyncClient, admin_token: str
    ) -> None:
        """Переход по секретной ссылке перенаправляет в кабинет."""
        response = await client.get(f"/admin/{admin_token}", follow_redirects=False)

        assert response.status_code == 303
        assert response.headers["location"] == "/admin"

    async def test_login_by_token_sets_cookie(self, client: AsyncClient, admin_token: str) -> None:
        """Успешный вход ставит HttpOnly-cookie с токеном сессии."""
        response = await client.get(f"/admin/{admin_token}", follow_redirects=False)

        cookie = response.cookies.get(SESSION_COOKIE_NAME)
        assert cookie

        set_cookie = response.headers["set-cookie"]
        assert "HttpOnly" in set_cookie
        assert "samesite=lax" in set_cookie.lower()

    async def test_login_by_token_cookie_lifetime(
        self, client: AsyncClient, admin_token: str
    ) -> None:
        """Срок жизни cookie соответствует сроку жизни сессии."""
        response = await client.get(f"/admin/{admin_token}", follow_redirects=False)

        expected = DEFAULT_SESSION_TTL_DAYS * 24 * 60 * 60
        assert f"Max-Age={expected}" in response.headers["set-cookie"]

    async def test_login_by_unknown_token_shows_login_page(self, client: AsyncClient) -> None:
        """Неизвестный токен показывает страницу с инструкцией и статус 401."""
        response = await client.get("/admin/нет-такого-токена")

        assert response.status_code == 401
        assert "Вход в кабинет" in response.text
        assert SESSION_COOKIE_NAME not in response.cookies

    async def test_login_page_available(self, client: AsyncClient) -> None:
        """Страница входа доступна без сессии."""
        response = await client.get("/admin/login")

        assert response.status_code == 401
        assert "/admin" in response.text

    async def test_login_page_mentions_bot_command(self, client: AsyncClient) -> None:
        """Страница входа подсказывает команду бота."""
        response = await client.get("/admin/login")
        assert "/admin" in response.text


class TestAdminSessionRoutes:
    """Проверки защищённых роутов кабинета."""

    async def test_dashboard_requires_session(self, client: AsyncClient) -> None:
        """Без cookie дашборд недоступен."""
        response = await client.get("/admin", follow_redirects=False)
        assert response.status_code == 401

    async def test_dashboard_renders_with_session(
        self, client: AsyncClient, admin_token: str
    ) -> None:
        """С валидной cookie дашборд отдаёт HTML."""
        await client.get(f"/admin/{admin_token}", follow_redirects=False)

        response = await client.get("/admin")

        assert response.status_code == 200
        assert "Кабинет тренера" in response.text

    async def test_dashboard_shows_tenant_name(
        self, client: AsyncClient, admin_token: str, repo: Repository, tenant_id: int
    ) -> None:
        """Дашборд показывает имя тренера из сессии."""
        tenant = await repo.tenants.get_by_id(tenant_id)
        assert tenant is not None

        await client.get(f"/admin/{admin_token}", follow_redirects=False)
        response = await client.get("/admin")

        assert tenant.name in response.text

    async def test_dashboard_shows_counts(
        self, client: AsyncClient, admin_token: str, repo: Repository, tenant_id: int
    ) -> None:
        """Дашборд отдельно показывает количество учеников и групп."""
        await repo.entities.create(tenant_id=tenant_id, name="Аня", entity_type="student")
        await repo.entities.create(tenant_id=tenant_id, name="Группа 1", entity_type="group")

        await client.get(f"/admin/{admin_token}", follow_redirects=False)
        response = await client.get("/admin")

        assert response.status_code == 200
        assert "Ученики" in response.text
        assert "Группы" in response.text
        assert "Занятий сегодня" in response.text
        assert "Занятий завтра" in response.text

    async def test_dashboard_omits_debtors(
        self, client: AsyncClient, admin_token: str, repo: Repository, tenant_id: int
    ) -> None:
        """Блок напоминаний об оплате убран с дашборда."""
        await repo.entities.create(tenant_id=tenant_id, name="Должник")

        await client.get(f"/admin/{admin_token}", follow_redirects=False)
        response = await client.get("/admin")

        assert "Пора напомнить об оплате" not in response.text

    async def test_dashboard_shows_today_and_tomorrow_lessons(
        self, client: AsyncClient, admin_token: str, repo: Repository, tenant_id: int
    ) -> None:
        """Дашборд показывает занятия на сегодня и на завтра из расписания."""
        today_student = await repo.entities.create(tenant_id=tenant_id, name="Сегодняшний")
        tomorrow_student = await repo.entities.create(tenant_id=tenant_id, name="Завтрашний")

        today = datetime.now(UTC).date()
        tomorrow = today + timedelta(days=1)
        weekday_names = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

        await repo.schedule.create(
            tenant_id=tenant_id,
            entity_id=today_student.id,
            weekday=weekday_names[today.weekday()],
            start_time="17:30",
        )
        await repo.schedule.create(
            tenant_id=tenant_id,
            entity_id=tomorrow_student.id,
            weekday=weekday_names[tomorrow.weekday()],
            start_time="19:00",
        )

        await client.get(f"/admin/{admin_token}", follow_redirects=False)
        response = await client.get("/admin")

        assert "Сегодняшний" in response.text
        assert "17:30" in response.text
        assert "Завтрашний" in response.text
        assert "19:00" in response.text

    async def test_session_status_without_session(self, client: AsyncClient) -> None:
        """Служебный роут сообщает об отсутствии сессии."""
        response = await client.get("/admin/session/status")

        assert response.status_code == 200
        assert response.json() == {"authenticated": False, "tenant_id": None}

    async def test_session_status_with_session(
        self, client: AsyncClient, admin_token: str, tenant_id: int
    ) -> None:
        """Служебный роут сообщает о вошедшем тенанте."""
        await client.get(f"/admin/{admin_token}", follow_redirects=False)

        response = await client.get("/admin/session/status")

        assert response.status_code == 200
        assert response.json() == {"authenticated": True, "tenant_id": tenant_id}


class TestAdminLogoutRoutes:
    """Проверки выхода из кабинета через HTTP."""

    async def test_logout_clears_cookie(self, client: AsyncClient, admin_token: str) -> None:
        """Выход удаляет cookie сессии."""
        await client.get(f"/admin/{admin_token}", follow_redirects=False)

        response = await client.post("/admin/logout", follow_redirects=False)

        assert response.status_code == 303
        assert response.headers["location"] == "/admin/login"
        assert not response.cookies.get(SESSION_COOKIE_NAME)

    async def test_logout_invalidates_session(self, client: AsyncClient, admin_token: str) -> None:
        """После выхода дашборд снова недоступен."""
        await client.get(f"/admin/{admin_token}", follow_redirects=False)
        await client.post("/admin/logout", follow_redirects=False)

        response = await client.get("/admin", follow_redirects=False)

        assert response.status_code == 401

    async def test_logout_without_session_is_safe(self, client: AsyncClient) -> None:
        """Выход без активной сессии не падает."""
        response = await client.post("/admin/logout", follow_redirects=False)
        assert response.status_code == 303

    async def test_logout_all_requires_session(self, client: AsyncClient) -> None:
        """Выход со всех устройств требует активной сессии."""
        response = await client.post("/admin/logout-all", follow_redirects=False)
        assert response.status_code == 401

    async def test_logout_all_invalidates_session(
        self, client: AsyncClient, admin_token: str
    ) -> None:
        """Выход со всех устройств закрывает текущую сессию."""
        await client.get(f"/admin/{admin_token}", follow_redirects=False)

        response = await client.post("/admin/logout-all", follow_redirects=False)

        assert response.status_code == 303
        assert (await client.get("/admin", follow_redirects=False)).status_code == 401


class TestAdminRouteIsolation:
    """Проверки изоляции кабинета между тенантами."""

    async def test_foreign_session_does_not_leak_data(
        self, client: AsyncClient, repo: Repository, tenant_id: int, admin_token: str
    ) -> None:
        """Сессия другого тренера не показывает чужих учеников."""
        await repo.entities.create(tenant_id=tenant_id, name="МойУченик")

        other = await repo.tenants.create(
            telegram_user_id=999_000_333,
            name="Чужой тренер",
        )
        other_token = await repo.tenants.ensure_admin_token(other.id)
        assert other_token is not None

        await client.get(f"/admin/{other_token}", follow_redirects=False)
        response = await client.get("/admin")

        assert response.status_code == 200
        assert "МойУченик" not in response.text

    async def test_public_routes_stay_open(
        self, client: AsyncClient, repo: Repository, tenant_id: int
    ) -> None:
        """Публичные страницы не требуют входа в кабинет."""
        entity = await repo.entities.create(tenant_id=tenant_id, name="Публичный")

        response = await client.get(f"/l/{entity.public_token}")

        assert response.status_code == 200
        assert "Публичный" in response.text
