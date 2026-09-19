"""FastAPI-зависимости веб-приложения.

Предоставляют доступ к сессии БД и сервисам внутри HTTP-хендлеров,
а также защищают кабинет тренера: ``require_tenant`` достаёт тенанта
из cookie-сессии и прерывает запрос редиректом, если сессии нет.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import get_session
from app.db.models import Tenant
from app.db.repository import Repository
from app.services.auth_service import SESSION_COOKIE_NAME, AuthService
from app.services.dossier_service import DossierService
from app.services.entity_service import EntityService
from app.services.schedule_service import ScheduleService
from app.services.settings_service import TenantSettingsService
from app.services.share_link_service import ShareLinkService
from app.services.whatsapp_service import WhatsAppLinkService


async def get_db_session() -> AsyncIterator[AsyncSession]:
    """Отдаёт сессию БД на время обработки запроса.

    Yields:
        Асинхронная сессия SQLAlchemy.
    """
    async with get_session() as session:
        yield session


async def get_repository(
    session: AsyncSession = Depends(get_db_session),  # noqa: B008
) -> Repository:
    """Создаёт фасад репозиториев для текущего запроса.

    Args:
        session: Сессия БД из зависимости.

    Returns:
        Фасад ``Repository``.
    """
    return Repository(session)


async def get_dossier_service(
    repo: Repository = Depends(get_repository),  # noqa: B008
) -> DossierService:
    """Создаёт сервис досье для текущего запроса.

    Args:
        repo: Фасад репозиториев.

    Returns:
        Экземпляр ``DossierService``.
    """
    return DossierService(repo)


async def get_entity_service(
    repo: Repository = Depends(get_repository),  # noqa: B008
) -> EntityService:
    """Создаёт сервис сущностей для текущего запроса.

    Args:
        repo: Фасад репозиториев.

    Returns:
        Экземпляр ``EntityService``.
    """
    return EntityService(repo)


async def get_schedule_service(
    repo: Repository = Depends(get_repository),  # noqa: B008
) -> ScheduleService:
    """Создаёт сервис расписания для текущего запроса.

    Args:
        repo: Фасад репозиториев.

    Returns:
        Экземпляр ``ScheduleService``.
    """
    return ScheduleService(repo)


async def get_settings_service(
    repo: Repository = Depends(get_repository),  # noqa: B008
) -> TenantSettingsService:
    """Создаёт сервис настроек тенанта для текущего запроса.

    Args:
        repo: Фасад репозиториев.

    Returns:
        Экземпляр ``TenantSettingsService``.
    """
    return TenantSettingsService(repo)


async def get_share_link_service(
    repo: Repository = Depends(get_repository),  # noqa: B008
) -> ShareLinkService:
    """Создаёт сервис публичных ссылок на занятия.

    Args:
        repo: Фасад репозиториев.

    Returns:
        Экземпляр ``ShareLinkService``.
    """
    return ShareLinkService(repo)


async def get_whatsapp_service() -> WhatsAppLinkService:
    """Создаёт сервис ссылок WhatsApp.

    Returns:
        Экземпляр ``WhatsAppLinkService``.
    """
    return WhatsAppLinkService()


async def get_auth_service(
    repo: Repository = Depends(get_repository),  # noqa: B008
) -> AuthService:
    """Создаёт сервис аутентификации для текущего запроса.

    Args:
        repo: Фасад репозиториев.

    Returns:
        Экземпляр ``AuthService``.
    """
    return AuthService(repo)


async def get_current_tenant(
    request: Request,
    auth: AuthService = Depends(get_auth_service),  # noqa: B008
) -> Tenant | None:
    """Возвращает тенанта из cookie-сессии или ``None``.

    Не прерывает запрос: используется на страницах, которые должны
    показать «мягкую» ошибку вместо редиректа.

    Args:
        request: Текущий HTTP-запрос.
        auth: Сервис аутентификации.

    Returns:
        Тенант либо ``None``, если сессии нет или она истекла.
    """
    token = request.cookies.get(SESSION_COOKIE_NAME)
    return await auth.resolve_session(token)


async def require_tenant(
    tenant: Tenant | None = Depends(get_current_tenant),  # noqa: B008
) -> Tenant:
    """Требует активную сессию тренера.

    Args:
        tenant: Тенант из cookie-сессии.

    Returns:
        Тенант, вошедший в кабинет.

    Raises:
        HTTPException: 401, если сессии нет. Веб-слой превращает её
            в редирект на страницу входа.
    """
    if tenant is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Требуется вход в кабинет",
        )
    return tenant
