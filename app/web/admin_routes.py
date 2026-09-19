"""HTTP-роуты кабинета тренера.

Кабинет защищён cookie-сессией. Вход выполняется по секретной ссылке
``GET /admin/{admin_token}``: сервис аутентификации создаёт серверную
сессию, а ответ ставит HttpOnly-cookie с её токеном и перенаправляет
тренера на дашборд.

Публичные страницы (карточка родителя, досье) живут в ``routes.py``
и авторизации не требуют.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.core.config import settings
from app.core.logging import get_logger
from app.db.models import Tenant
from app.db.repository import Repository
from app.services.auth_service import (
    DEFAULT_SESSION_TTL_DAYS,
    SESSION_COOKIE_NAME,
    AuthService,
)
from app.services.schedule_service import ScheduleService
from app.web.dependencies import (
    get_auth_service,
    get_current_tenant,
    get_repository,
    get_schedule_service,
    require_tenant,
)
from app.web.templating import templates

logger = get_logger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])

# Путь дашборда — цель редиректа после успешного входа.
DASHBOARD_PATH = "/admin"


def _set_session_cookie(response: Response, token: str, *, ttl_days: int) -> None:
    """Ставит cookie с токеном сессии кабинета.

    Cookie всегда ``HttpOnly`` и ``SameSite=Lax``; флаг ``Secure``
    включается только в боевом режиме, иначе локальная разработка
    по http перестала бы работать.

    Args:
        response: Ответ, в который добавляется cookie.
        token: Токен сессии.
        ttl_days: Срок жизни cookie в днях.
    """
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=ttl_days * 24 * 60 * 60,
        httponly=True,
        secure=settings.is_production,
        samesite="lax",
        path="/",
    )


def _clear_session_cookie(response: Response) -> None:
    """Удаляет cookie сессии кабинета.

    Args:
        response: Ответ, из которого удаляется cookie.
    """
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        path="/",
        httponly=True,
        secure=settings.is_production,
        samesite="lax",
    )


@router.get("/login", response_class=HTMLResponse, include_in_schema=False)
async def login_page(request: Request) -> HTMLResponse:
    """Страница «ссылка недействительна» с инструкцией.

    Args:
        request: Текущий HTTP-запрос.

    Returns:
        HTML-страница с пояснением, как получить доступ в кабинет.
    """
    return templates.TemplateResponse(
        request=request,
        name="admin/login.html",
        context={
            "title": "Вход в кабинет",
            "message": (
                "Ссылка недействительна или устарела. "
                "Откройте бота и отправьте команду /admin — он пришлёт свежую ссылку."
            ),
        },
        status_code=401,
    )


@router.get("/{admin_token}", response_class=HTMLResponse, include_in_schema=False)
async def login_by_token(
    request: Request,
    admin_token: str,
    auth: AuthService = Depends(get_auth_service),  # noqa: B008
) -> Response:
    """Вход в кабинет по секретной ссылке.

    Создаёт серверную сессию, ставит cookie и перенаправляет на дашборд.
    Если токен неизвестен — показывает страницу с инструкцией.

    Args:
        request: Текущий HTTP-запрос.
        admin_token: Секретный токен тенанта из адреса.
        auth: Сервис аутентификации.

    Returns:
        Редирект на дашборд при успехе либо страница входа со статусом 401.
    """
    result = await auth.login_by_admin_token(admin_token, ttl_days=DEFAULT_SESSION_TTL_DAYS)
    if result is None:
        return templates.TemplateResponse(
            request=request,
            name="admin/login.html",
            context={
                "title": "Вход в кабинет",
                "message": (
                    "Такая ссылка не найдена. Проверьте, что скопировали её целиком, "
                    "или запросите новую командой /admin в боте."
                ),
            },
            status_code=401,
        )

    response = RedirectResponse(url=DASHBOARD_PATH, status_code=303)
    _set_session_cookie(response, result.token, ttl_days=DEFAULT_SESSION_TTL_DAYS)
    return response


def _today_in_tenant_zone(tenant: Tenant) -> datetime.date:
    """Возвращает сегодняшнюю дату в часовом поясе тренера.

    Часовой пояс берётся из настроек тенанта, при некорректном значении
    используется пояс из ``.env``.

    Args:
        tenant: Тенант, для которого считается дата.

    Returns:
        Сегодняшняя дата в поясе тренера.
    """
    try:
        zone = ZoneInfo(tenant.timezone or settings.timezone)
    except Exception:  # noqa: BLE001 — некорректный пояс не должен ломать дашборд
        zone = ZoneInfo(settings.timezone)
    return datetime.now(zone).date()


@router.get("", response_class=HTMLResponse, include_in_schema=False)
@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def dashboard(
    request: Request,
    tenant: Tenant = Depends(require_tenant),  # noqa: B008
    repo: Repository = Depends(get_repository),  # noqa: B008
    schedule_service: ScheduleService = Depends(get_schedule_service),  # noqa: B008
) -> HTMLResponse:
    """Дашборд кабинета: сводка по ученикам и занятиям на сегодня.

    Args:
        request: Текущий HTTP-запрос.
        tenant: Тенант из cookie-сессии.
        repo: Фасад репозиториев.
        schedule_service: Сервис расписания.

    Returns:
        HTML-страница дашборда.
    """
    entities = await repo.entities.list_by_tenant(tenant.id)
    students_count = sum(1 for entity in entities if entity.entity_type == "student")
    groups_count = sum(1 for entity in entities if entity.entity_type == "group")

    today = _today_in_tenant_zone(tenant)
    tomorrow = today + timedelta(days=1)

    today_schedule = await schedule_service.get_day_schedule(tenant.id, today)
    tomorrow_schedule = await schedule_service.get_day_schedule(tenant.id, tomorrow)

    return templates.TemplateResponse(
        request=request,
        name="admin/dashboard.html",
        context={
            "title": "Кабинет тренера",
            "tenant": tenant,
            "entities": entities,
            "entities_count": len(entities),
            "students_count": students_count,
            "groups_count": groups_count,
            "today": today,
            "today_lessons": today_schedule,
            "today_count": len(today_schedule),
            "tomorrow": tomorrow,
            "tomorrow_lessons": tomorrow_schedule,
            "tomorrow_count": len(tomorrow_schedule),
        },
    )


@router.post("/logout", include_in_schema=False)
async def logout(
    request: Request,
    auth: AuthService = Depends(get_auth_service),  # noqa: B008
) -> RedirectResponse:
    """Выход из кабинета: удаляет сессию и cookie.

    Args:
        request: Текущий HTTP-запрос.
        auth: Сервис аутентификации.

    Returns:
        Редирект на страницу входа.
    """
    token = request.cookies.get(SESSION_COOKIE_NAME)
    await auth.logout(token)

    response = RedirectResponse(url="/admin/login", status_code=303)
    _clear_session_cookie(response)
    return response


@router.post("/logout-all", include_in_schema=False)
async def logout_all(
    tenant: Tenant = Depends(require_tenant),  # noqa: B008
    auth: AuthService = Depends(get_auth_service),  # noqa: B008
) -> RedirectResponse:
    """Выход со всех устройств: удаляет все сессии тенанта.

    Args:
        tenant: Тенант из cookie-сессии.
        auth: Сервис аутентификации.

    Returns:
        Редирект на страницу входа.
    """
    await auth.logout_all(tenant.id)

    response = RedirectResponse(url="/admin/login", status_code=303)
    _clear_session_cookie(response)
    return response


@router.get("/session/status", include_in_schema=False)
async def session_status(
    tenant: Tenant | None = Depends(get_current_tenant),  # noqa: B008
) -> dict[str, object]:
    """Служебная проверка состояния сессии (для отладки и тестов).

    Args:
        tenant: Тенант из cookie-сессии или ``None``.

    Returns:
        Словарь с признаком входа и идентификатором тенанта.
    """
    return {
        "authenticated": tenant is not None,
        "tenant_id": tenant.id if tenant else None,
    }


__all__ = ["router"]
