"""HTTP-роуты кабинета тренера.

Кабинет защищён cookie-сессией. Вход выполняется по секретной ссылке
``GET /admin/{admin_token}``: сервис аутентификации создаёт серверную
сессию, а ответ ставит HttpOnly-cookie с её токеном и перенаправляет
тренера на дашборд.

Публичные страницы (карточка родителя, досье) живут в ``routes.py``
и авторизации не требуют.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.core.config import settings
from app.core.logging import get_logger
from app.db.models import Entity, Tenant
from app.db.repository import Repository
from app.services.audit_service import AuditService
from app.services.auth_service import (
    DEFAULT_SESSION_TTL_DAYS,
    SESSION_COOKIE_NAME,
    AuthService,
)
from app.services.payment_service import PaymentService
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

# Верхняя граница суммы распределяемого платежа — защита от опечаток.
MAX_PAYMENT_AMOUNT = 1_000_000.0

# Русские названия месяцев в именительном падеже для подписи «сентябрь 2026».
_MONTH_NAMES: tuple[str, ...] = (
    "январь",
    "февраль",
    "март",
    "апрель",
    "май",
    "июнь",
    "июль",
    "август",
    "сентябрь",
    "октябрь",
    "ноябрь",
    "декабрь",
)


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


def _shift_month(month_start: date, delta: int) -> date:
    """Сдвигает первое число месяца на заданное количество месяцев.

    Args:
        month_start: Первое число исходного месяца.
        delta: Сдвиг в месяцах (может быть отрицательным).

    Returns:
        Первое число сдвинутого месяца.
    """
    total = month_start.year * 12 + (month_start.month - 1) + delta
    return date(total // 12, total % 12 + 1, 1)


def _month_label(month_start: date) -> str:
    """Собирает человекочитаемую подпись месяца.

    Args:
        month_start: Первое число месяца.

    Returns:
        Строка вида ``«сентябрь 2026»``.
    """
    return f"{_MONTH_NAMES[month_start.month - 1]} {month_start.year}"


def _payments_split(
    entities: list[Entity],
    overview: dict[int, dict[str, float | int]],
) -> dict[str, float]:
    """Суммирует оплаты месяца отдельно по ученикам и группам.

    Args:
        entities: Все ученики и группы тенанта.
        overview: Месячные показатели от ``PaymentService.monthly_overview``.

    Returns:
        Словарь ``{"students": ..., "groups": ..., "total": ...}`` с суммами
        оплат за месяц.
    """
    students_paid = sum(
        float(overview[item.id]["paid"])
        for item in entities
        if item.entity_type == "student" and item.id in overview
    )
    groups_paid = sum(
        float(overview[item.id]["paid"])
        for item in entities
        if item.entity_type == "group" and item.id in overview
    )
    return {
        "students": round(students_paid, 2),
        "groups": round(groups_paid, 2),
        "total": round(students_paid + groups_paid, 2),
    }


def _today_in_tenant_zone(tenant: Tenant) -> date:
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


def _clean_flag(raw: str | None) -> bool:
    """Проверяет наличие флага успеха в query-параметрах.

    Args:
        raw: Значение флага из строки запроса.

    Returns:
        ``True``, если флаг установлен (например, ``?distributed=1``).
    """
    return bool(raw) and raw not in {"0", "false", "False"}


def _parse_payment_amount(raw: str | None) -> float | None:
    """Разбирает сумму оплаты из формы.

    Args:
        raw: Сырое значение из формы.

    Returns:
        Положительная сумма либо ``None``, если значение пустое или
        некорректное.
    """
    cleaned = (raw or "").strip().replace(",", ".")
    if not cleaned:
        return None
    try:
        amount = float(cleaned)
    except ValueError:
        return None
    if amount <= 0 or amount > MAX_PAYMENT_AMOUNT:
        return None
    return round(amount, 2)


def _parse_iso_date(raw: str | None) -> date | None:
    """Разбирает дату из формы в формате ``ГГГГ-ММ-ДД``.

    Args:
        raw: Сырое значение из формы.

    Returns:
        Дата либо ``None``, если значение пустое или некорректное.
    """
    cleaned = (raw or "").strip()
    if not cleaned:
        return None
    try:
        return date.fromisoformat(cleaned)
    except ValueError:
        return None


def _parse_month(raw: str | None) -> date | None:
    """Разбирает месяц из формы в формате ``ГГГГ-ММ``.

    Args:
        raw: Сырое значение из формы (``ГГГГ-ММ`` или ``ГГГГ-ММ-ДД``).

    Returns:
        Первое число месяца либо ``None``, если значение некорректное.
    """
    cleaned = (raw or "").strip()
    if not cleaned:
        return None
    parts = cleaned.split("-")
    if len(parts) < 2:
        return None
    try:
        year = int(parts[0])
        month_number = int(parts[1])
    except ValueError:
        return None
    if not 1 <= month_number <= 12:
        return None
    return date(year, month_number, 1)


def _month_options(before: int = 6, after: int = 1) -> list[tuple[str, str]]:
    """Собирает список месяцев для селекта формы распределения.

    Args:
        before: Сколько прошедших месяцев включить (считая текущий).
        after: Сколько будущих месяцев включить.

    Returns:
        Список пар ``(значение ГГГГ-ММ, подпись)`` от новых к старым.
    """
    today = date.today().replace(day=1)
    months: list[tuple[str, str]] = []
    for delta in range(after, -before, -1):
        month_start = _shift_month(today, delta)
        months.append((month_start.strftime("%Y-%m"), _month_label(month_start)))
    return months


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

    # Оплаты: отдельно по ученикам и группам за текущий и предыдущий месяц.
    payment_service = PaymentService(repo)
    current_month = today.replace(day=1)
    prev_month = _shift_month(current_month, -1)
    currency = await payment_service.resolve_currency(tenant.id)
    current_overview = await payment_service.monthly_overview(
        tenant.id, entities, today=current_month
    )
    prev_overview = await payment_service.monthly_overview(tenant.id, entities, today=prev_month)
    current_payments = _payments_split(entities, current_overview)
    prev_payments = _payments_split(entities, prev_overview)

    # Флаги и параметры формы распределения общей оплаты по группам.
    distribute_month = _shift_month(current_month, -1)
    distribute_month_options = _month_options()
    distributed = _clean_flag(request.query_params.get("distributed"))
    error = request.query_params.get("error")

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
            "currency": currency,
            "current_month": current_month,
            "prev_month": prev_month,
            "current_month_label": _month_label(current_month),
            "prev_month_label": _month_label(prev_month),
            "current_payments": current_payments,
            "prev_payments": prev_payments,
            "distribute_month": distribute_month,
            "distribute_month_options": distribute_month_options,
            "distributed": distributed,
            "error": error,
        },
    )


@router.post("/groups/monthly-payment", include_in_schema=False)
async def groups_monthly_payment(
    total_amount: str | None = Form(None),
    payment_date: str | None = Form(None),
    month: str | None = Form(None),
    tenant: Tenant = Depends(require_tenant),  # noqa: B008
    repo: Repository = Depends(get_repository),  # noqa: B008
) -> RedirectResponse:
    """Распределяет общую оплату за месяц по группам.

    Тренер вводит одну сумму и дату платежа — система делит сумму между
    группами пропорционально числу проведённых занятий за выбранный месяц
    и создаёт по одному платежу на каждую группу. Каждое созданное действие
    пишется в журнал.

    Args:
        total_amount: Общая сумма оплаты за месяц.
        payment_date: Дата платежа в формате ``ГГГГ-ММ-ДД`` (пусто — сегодня).
        month: Месяц оплаты в формате ``ГГГГ-ММ-01`` (пусто — прошлый месяц).
        tenant: Тенант из cookie-сессии.
        repo: Фасад репозиториев.

    Returns:
        Редирект на дашборд с флагом успеха или ошибки.
    """
    today = _today_in_tenant_zone(tenant)
    fallback_month = _shift_month(today.replace(day=1), -1)

    parsed_amount = _parse_payment_amount(total_amount)
    if parsed_amount is None:
        return RedirectResponse(
            url="/admin?error=Укажите+корректную+сумму",
            status_code=303,
        )

    parsed_month = _parse_month(month) or fallback_month
    parsed_date = _parse_iso_date(payment_date) or today

    service = PaymentService(repo)
    try:
        payments = await service.distribute_monthly_group_payment(
            tenant.id,
            total_amount=parsed_amount,
            payment_date=parsed_date,
            month=parsed_month,
        )
    except ValueError as exc:
        return RedirectResponse(
            url=f"/admin?error={str(exc)}",
            status_code=303,
        )

    audit = AuditService(repo)
    for payment in payments:
        await audit.log_payment_registered(
            tenant.id,
            payment.entity_id,
            amount=payment.amount,
            lessons_added=payment.lessons_added,
            actor="trainer",
        )

    logger.info(
        "Распределена оплата за месяц по группам: tenant=%s сумма=%s групп=%s месяц=%s",
        tenant.id,
        parsed_amount,
        len(payments),
        parsed_month,
    )

    return RedirectResponse(url="/admin?distributed=1", status_code=303)


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
