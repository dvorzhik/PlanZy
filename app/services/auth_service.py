"""Сервис аутентификации тренера в веб-кабинете.

Вход устроен по «секретной ссылке»: у каждого тенанта есть постоянный
``admin_token``. Переход по адресу ``/admin/{admin_token}`` создаёт
серверную сессию (запись в ``admin_sessions``) и ставит cookie с её
токеном. Cookie живёт ограниченное число дней, после чего тренеру
нужно снова открыть секретную ссылку.

Модуль намеренно не знает про FastAPI: он оперирует только
репозиториями и возвращает доменные объекты. Работа с cookie и
редиректами живёт в веб-слое.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.core.logging import get_logger
from app.db.models import AdminSession, Tenant
from app.db.repository import Repository

logger = get_logger(__name__)

# Имя cookie с токеном сессии кабинета.
SESSION_COOKIE_NAME = "planzy_admin"

# Срок жизни сессии по умолчанию — 30 дней.
DEFAULT_SESSION_TTL_DAYS = 30

# Минимальная и максимальная допустимая длительность сессии в днях.
MIN_SESSION_TTL_DAYS = 1
MAX_SESSION_TTL_DAYS = 365


@dataclass(slots=True)
class AuthResult:
    """Результат успешного входа тренера.

    Attributes:
        tenant: Тенант (тренер), вошедший в кабинет.
        session: Созданная серверная сессия.
        token: Токен сессии для установки в cookie.
        expires_at: Момент истечения сессии (UTC).
    """

    tenant: Tenant
    session: AdminSession
    token: str
    expires_at: datetime


class AuthService:
    """Бизнес-логика входа, проверки и выхода из кабинета."""

    def __init__(self, repo: Repository) -> None:
        """Сохраняет фасад репозиториев.

        Args:
            repo: Фасад репозиториев.
        """
        self._repo = repo

    # --- Публичный API -------------------------------------------------

    async def ensure_admin_token(self, tenant_id: int) -> str | None:
        """Возвращает секретный токен тенанта, создавая его при отсутствии.

        Args:
            tenant_id: Идентификатор тенанта.

        Returns:
            Токен кабинета или ``None``, если тенант не найден.
        """
        return await self._repo.tenants.ensure_admin_token(tenant_id)

    async def login_by_admin_token(
        self,
        admin_token: str,
        *,
        ttl_days: int = DEFAULT_SESSION_TTL_DAYS,
    ) -> AuthResult | None:
        """Создаёт сессию по секретной ссылке.

        Args:
            admin_token: Секретный токен из адреса ``/admin/{token}``.
            ttl_days: Срок жизни сессии в днях.

        Returns:
            :class:`AuthResult` при успехе или ``None``, если токен неизвестен.
        """
        token = (admin_token or "").strip()
        if not token:
            return None

        tenant = await self._repo.tenants.get_by_admin_token(token)
        if tenant is None:
            logger.warning("Попытка входа по неизвестному admin_token")
            return None

        return await self._create_session(tenant, ttl_days=ttl_days)

    async def resolve_session(self, session_token: str | None) -> Tenant | None:
        """Возвращает тенанта по токену cookie, если сессия ещё жива.

        Args:
            session_token: Значение cookie ``planzy_admin``.

        Returns:
            Тенант или ``None``, если сессии нет либо она истекла.
        """
        token = (session_token or "").strip()
        if not token:
            return None

        session = await self._repo.admin_sessions.get_valid(token)
        if session is None:
            return None

        tenant = await self._repo.tenants.get_by_id(session.tenant_id)
        if tenant is None or not tenant.is_active:
            logger.warning(
                "Сессия ссылается на недоступного тенанта: tenant_id=%s",
                session.tenant_id,
            )
            return None

        return tenant

    async def logout(self, session_token: str | None) -> bool:
        """Удаляет сессию по токену cookie.

        Args:
            session_token: Значение cookie ``planzy_admin``.

        Returns:
            ``True``, если запись сессии была удалена.
        """
        token = (session_token or "").strip()
        if not token:
            return False

        deleted = await self._repo.admin_sessions.delete(token)
        if deleted:
            logger.info("Тренер вышел из кабинета")
        return deleted

    async def logout_all(self, tenant_id: int) -> int:
        """Удаляет все сессии тенанта (выход со всех устройств).

        Args:
            tenant_id: Идентификатор тенанта.

        Returns:
            Количество удалённых сессий.
        """
        removed = await self._repo.admin_sessions.delete_by_tenant(tenant_id)
        if removed:
            logger.info("Удалено сессий тенанта %s: %s", tenant_id, removed)
        return removed

    async def purge_expired(self, *, now: datetime | None = None) -> int:
        """Удаляет истёкшие сессии.

        Args:
            now: Момент сравнения (по умолчанию — текущее время UTC).

        Returns:
            Количество удалённых сессий.
        """
        return await self._repo.admin_sessions.delete_expired(now=now)

    # --- Вспомогательное ----------------------------------------------

    async def _create_session(
        self,
        tenant: Tenant,
        *,
        ttl_days: int,
    ) -> AuthResult:
        """Создаёт запись сессии и возвращает результат входа.

        Args:
            tenant: Тенант, который входит в кабинет.
            ttl_days: Срок жизни сессии в днях.

        Returns:
            :class:`AuthResult` с токеном и сроком истечения.
        """
        ttl = self._normalize_ttl(ttl_days)
        expires_at = datetime.now(UTC) + timedelta(days=ttl)

        session = await self._repo.admin_sessions.create(
            tenant.id,
            expires_at=expires_at,
        )

        logger.info(
            "Тренер вошёл в кабинет: tenant_id=%s, срок до %s",
            tenant.id,
            expires_at.isoformat(),
        )
        return AuthResult(
            tenant=tenant,
            session=session,
            token=session.token,
            expires_at=expires_at,
        )

    @staticmethod
    def _normalize_ttl(ttl_days: int) -> int:
        """Приводит срок жизни сессии к допустимому диапазону.

        Args:
            ttl_days: Запрошенный срок в днях.

        Returns:
            Срок в днях в пределах ``MIN_SESSION_TTL_DAYS``–``MAX_SESSION_TTL_DAYS``.
        """
        try:
            value = int(ttl_days)
        except (TypeError, ValueError):
            return DEFAULT_SESSION_TTL_DAYS
        return max(MIN_SESSION_TTL_DAYS, min(MAX_SESSION_TTL_DAYS, value))
