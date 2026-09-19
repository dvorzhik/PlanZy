"""HTTP-роуты кабинета: настройки тенанта и правила напоминаний.

Раздел «Настройки» хранит цену занятия, валюту, порядок оплаты и
часовой пояс прямо в БД (``tenants``).
Раздел «Напоминания» управляет правилами ``reminder_rules``.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.core.config import settings
from app.core.logging import get_logger
from app.db.models import Tenant
from app.db.repository import Repository
from app.services.audit_service import AuditService
from app.services.reminder_service import REMINDER_KINDS, ReminderService
from app.services.settings_service import TenantSettingsService
from app.web.dependencies import get_repository, require_tenant
from app.web.templating import templates

logger = get_logger(__name__)

router = APIRouter(prefix="/admin", tags=["admin-settings"])

# Подписи видов напоминаний для интерфейса.
REMINDER_LABELS: dict[str, str] = {
    "day_before": "Накануне занятия",
    "morning_of": "Утром в день занятия",
}

# Каналы отправки: значение → подпись.
CHANNEL_OPTIONS: list[tuple[str, str]] = [
    ("telegram", "Telegram"),
    ("whatsapp", "WhatsApp (ссылка)"),
]

# Часовые пояса, которые чаще всего нужны тренеру.
TIMEZONE_OPTIONS: list[str] = [
    "Asia/Jerusalem",
    "Europe/Moscow",
    "Europe/Kyiv",
    "Europe/Berlin",
    "Europe/London",
    "America/New_York",
    "UTC",
]

# Пример текста утренней сводки для предпросмотра.
PREVIEW_TEMPLATE = (
    "☀️ Доброе утро!\n\n"
    "Сегодня {count} занятий:\n"
    "• 17:30 — Миша (шахматы)\n"
    "• 18:30 — Группа «Вторник»"
)


def _clean(value: str | None) -> str:
    """Убирает пробелы по краям и приводит ``None`` к пустой строке.

    Args:
        value: Сырое значение из формы.

    Returns:
        Очищенная строка.
    """
    return (value or "").strip()


def _parse_float(raw: str | None) -> float | None:
    """Разбирает число с плавающей точкой из формы.

    Args:
        raw: Сырое значение из формы.

    Returns:
        Число либо ``None``, если разобрать не удалось.
    """
    cleaned = _clean(raw).replace(",", ".")
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _parse_int(raw: str | None) -> int | None:
    """Разбирает целое число из формы.

    Args:
        raw: Сырое значение из формы.

    Returns:
        Число либо ``None``, если разобрать не удалось.
    """
    cleaned = _clean(raw)
    if not cleaned:
        return None
    try:
        return int(cleaned)
    except ValueError:
        return None


def _checkbox(raw: str | None) -> bool:
    """Преобразует значение чекбокса в булево.

    Args:
        raw: Сырое значение из формы.

    Returns:
        ``True``, если флажок установлен.
    """
    return _clean(raw).lower() in {"1", "on", "true", "yes"}


@router.get("/settings", response_class=HTMLResponse, include_in_schema=False)
async def settings_page(
    request: Request,
    tenant: Tenant = Depends(require_tenant),  # noqa: B008
    repo: Repository = Depends(get_repository),  # noqa: B008
) -> HTMLResponse:
    """Страница настроек тенанта.

    Args:
        request: Текущий HTTP-запрос.
        tenant: Тенант из cookie-сессии.
        repo: Фасад репозиториев.

    Returns:
        HTML-страница с формой настроек.
    """
    snapshot = await TenantSettingsService(repo).get(tenant.id)
    admin_token = await repo.tenants.ensure_admin_token(tenant.id)
    admin_url = f"{settings.base_url.rstrip('/')}/admin/{admin_token}" if admin_token else None

    return templates.TemplateResponse(
        request=request,
        name="admin/settings.html",
        context={
            "title": "Настройки",
            "active": "settings",
            "tenant": tenant,
            "settings": snapshot,
            "admin_url": admin_url,
            "timezone_options": TIMEZONE_OPTIONS,
            "saved": request.query_params.get("saved") == "1",
            "error": request.query_params.get("error"),
        },
    )


@router.post("/settings", include_in_schema=False)
async def settings_update(
    lesson_price: str | None = Form(None),
    currency: str | None = Form(None),
    timezone: str | None = Form(None),
    payment_instructions: str | None = Form(None),
    tenant: Tenant = Depends(require_tenant),  # noqa: B008
    repo: Repository = Depends(get_repository),  # noqa: B008
) -> RedirectResponse:
    """Сохраняет настройки тенанта и пишет изменения в журнал.

    Args:
        lesson_price: Цена занятия.
        currency: Валюта.
        timezone: Часовой пояс.
        payment_instructions: Порядок оплаты для родителей.
        tenant: Тенант из cookie-сессии.
        repo: Фасад репозиториев.

    Returns:
        Редирект на страницу настроек.
    """
    fields: dict[str, Any] = {}

    price = _parse_float(lesson_price)
    if price is not None:
        fields["lesson_price"] = price

    clean_currency = _clean(currency).upper()
    if clean_currency:
        fields["currency"] = clean_currency

    clean_timezone = _clean(timezone)
    if clean_timezone:
        fields["timezone"] = clean_timezone

    # Порядок оплаты можно очистить — тогда блок скрывается у родителей.
    fields["payment_instructions"] = _clean(payment_instructions) or None

    try:
        updated = await TenantSettingsService(repo).update(tenant.id, **fields)
    except ValueError as exc:
        logger.warning("Не удалось сохранить настройки: %s", exc)
        return RedirectResponse(url=f"/admin/settings?error={exc}", status_code=303)

    if updated is None:
        return RedirectResponse(url="/admin/settings?error=Тенант+не+найден", status_code=303)

    await AuditService(repo).log_settings_updated(tenant.id, fields, actor="trainer")

    return RedirectResponse(url="/admin/settings?saved=1", status_code=303)


@router.get("/reminders", response_class=HTMLResponse, include_in_schema=False)
async def reminders_page(
    request: Request,
    tenant: Tenant = Depends(require_tenant),  # noqa: B008
    repo: Repository = Depends(get_repository),  # noqa: B008
) -> HTMLResponse:
    """Страница правил напоминаний.

    Args:
        request: Текущий HTTP-запрос.
        tenant: Тенант из cookie-сессии.
        repo: Фасад репозиториев.

    Returns:
        HTML-страница со списком правил.
    """
    service = ReminderService(repo)
    rules = await service.list_rules(tenant.id)

    described: list[dict[str, Any]] = []
    for rule in rules:
        described.append(
            {
                "id": rule.id,
                "kind": rule.kind,
                "enabled": rule.is_enabled,
                "send_at": rule.time_of_day or "",
                "offset_days": _offset_days(rule),
                "channel": rule.channel,
                "description": service.describe(rule),
                "human_schedule": _human_schedule(rule),
                "last_sent": None,
            }
        )

    await _attach_last_sent(repo, tenant.id, described)

    return templates.TemplateResponse(
        request=request,
        name="admin/reminders.html",
        context={
            "title": "Напоминания",
            "active": "reminders",
            "tenant": tenant,
            "rules": described,
            "rule_labels": REMINDER_LABELS,
            "channel_options": CHANNEL_OPTIONS,
            "preview": PREVIEW_TEMPLATE.format(count=3),
            "schedule_hint": _schedule_hint(),
            "saved": request.query_params.get("saved") == "1",
            "error": request.query_params.get("error"),
        },
    )


async def _attach_last_sent(
    repo: Repository,
    tenant_id: int,
    described: list[dict[str, Any]],
) -> None:
    """Дополняет правила датой последней успешной рассылки.

    Тренеру важно видеть, что рассылка действительно уходила: без этого
    «нет напоминаний» невозможно отличить от «планировщик не работал».

    Args:
        repo: Фасад репозиториев.
        tenant_id: Идентификатор тенанта.
        described: Список правил для шаблона (изменяется на месте).
    """
    today = date.today()
    for item in described:
        try:
            found = None
            # Ищем ближайшую дату в пределах двух недель назад.
            for offset in range(15):
                probe = today - timedelta(days=offset)
                if await repo.dispatch_log.was_sent(tenant_id, item["kind"], probe):
                    found = probe
                    break
            item["last_sent"] = found
        except Exception as exc:  # pragma: no cover - диагностический путь
            logger.warning("Не удалось прочитать журнал отправок: %s", exc)


def _schedule_hint() -> dict[str, str]:
    """Возвращает расписание задач планировщика для подсказки в UI.

    Returns:
        Словарь с временем утренней и вечерней рассылки и часовым поясом.
    """
    return {
        "morning": settings.morning_dispatch_time,
        "day_before": settings.day_before_dispatch_time,
        "timezone": settings.timezone,
    }


def _offset_days(rule: Any) -> int:
    """Переводит смещение правила из минут в дни для формы.

    В модели ``ReminderRule`` смещение хранится в минутах
    (``offset_minutes``), а тренеру в интерфейсе удобнее видеть дни.

    Args:
        rule: ORM-объект правила напоминания.

    Returns:
        Количество дней (0, если смещение не задано).
    """
    minutes = getattr(rule, "offset_minutes", None) or 0
    return minutes // (24 * 60)


def _human_schedule(rule: Any) -> str:
    """Описывает правило человеческим языком.

    Args:
        rule: ORM-объект правила напоминания.

    Returns:
        Строка вида «в 20:00, за 1 день».
    """
    parts = [f"в {rule.time_of_day or '—'}"]
    offset = _offset_days(rule)
    if offset == 1:
        parts.append("за 1 день")
    elif offset > 1:
        parts.append(f"за {offset} дней")
    if not rule.is_enabled:
        parts.append("выключено")
    return ", ".join(parts)


@router.post("/reminders/defaults", include_in_schema=False)
async def reminders_create_defaults(
    tenant: Tenant = Depends(require_tenant),  # noqa: B008
    repo: Repository = Depends(get_repository),  # noqa: B008
) -> RedirectResponse:
    """Создаёт стандартный набор правил напоминаний.

    Args:
        tenant: Тенант из cookie-сессии.
        repo: Фасад репозиториев.

    Returns:
        Редирект на страницу напоминаний.
    """
    await ReminderService(repo).ensure_defaults(tenant.id)
    return RedirectResponse(url="/admin/reminders?saved=1", status_code=303)


@router.post("/reminders/{rule_id}", include_in_schema=False)
async def reminder_update(
    rule_id: int,
    send_at: str | None = Form(None),
    offset_days: str | None = Form(None),
    channel: str | None = Form(None),
    enabled: str | None = Form(None),
    tenant: Tenant = Depends(require_tenant),  # noqa: B008
    repo: Repository = Depends(get_repository),  # noqa: B008
) -> RedirectResponse:
    """Обновляет правило напоминания.

    Args:
        rule_id: Идентификатор правила.
        send_at: Время отправки в формате ``ЧЧ:ММ``.
        offset_days: Сдвиг в днях.
        channel: Канал отправки.
        enabled: Признак включённости.
        tenant: Тенант из cookie-сессии.
        repo: Фасад репозиториев.

    Returns:
        Редирект на страницу напоминаний.
    """
    fields: dict[str, Any] = {"is_enabled": _checkbox(enabled)}

    clean_time = _clean(send_at)
    if clean_time:
        fields["time_of_day"] = clean_time

    offset = _parse_int(offset_days)
    if offset is not None:
        # В форме тренер задаёт сдвиг в днях, в модели он хранится в минутах.
        fields["offset_minutes"] = offset * 24 * 60

    clean_channel = _clean(channel).lower()
    if clean_channel:
        fields["channel"] = clean_channel

    try:
        updated = await ReminderService(repo).update_rule(tenant.id, rule_id, **fields)
    except ValueError as exc:
        logger.warning("Не удалось обновить правило %s: %s", rule_id, exc)
        return RedirectResponse(url=f"/admin/reminders?error={exc}", status_code=303)

    if updated is None:
        return RedirectResponse(url="/admin/reminders?error=Правило+не+найдено", status_code=303)

    return RedirectResponse(url="/admin/reminders?saved=1", status_code=303)


@router.post("/reminders/{rule_id}/delete", include_in_schema=False)
async def reminder_delete(
    rule_id: int,
    tenant: Tenant = Depends(require_tenant),  # noqa: B008
    repo: Repository = Depends(get_repository),  # noqa: B008
) -> RedirectResponse:
    """Удаляет правило напоминания.

    Args:
        rule_id: Идентификатор правила.
        tenant: Тенант из cookie-сессии.
        repo: Фасад репозиториев.

    Returns:
        Редирект на страницу напоминаний.
    """
    await ReminderService(repo).delete_rule(tenant.id, rule_id)
    return RedirectResponse(url="/admin/reminders?saved=1", status_code=303)


__all__ = ["REMINDER_KINDS", "router"]
