"""HTTP-роуты кабинета: конструктор расписания.

Раздел «Расписание» показывает недельную сетку и позволяет тренеру
управлять регулярными слотами (``schedule_slots``) и разовыми
отклонениями (``schedule_exceptions``): перенос, отмена, доп. занятие.

Конфликты (два занятия в одно время) подсвечиваются прямо в сетке.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.core.config import settings
from app.core.logging import get_logger
from app.db.models import Tenant
from app.db.repository import Repository
from app.services.schedule_service import ScheduleService
from app.web.dependencies import (
    get_repository,
    get_schedule_service,
    require_tenant,
)
from app.web.templating import templates

logger = get_logger(__name__)

router = APIRouter(prefix="/admin/schedule", tags=["admin-schedule"])

# Дни недели: код → подпись. Порядок совпадает с ISO (понедельник первый).
WEEKDAY_OPTIONS: list[tuple[str, str]] = [
    ("mon", "Понедельник"),
    ("tue", "Вторник"),
    ("wed", "Среда"),
    ("thu", "Четверг"),
    ("fri", "Пятница"),
    ("sat", "Суббота"),
    ("sun", "Воскресенье"),
]

# Виды разовых отклонений.
EXCEPTION_KINDS: tuple[str, ...] = ("moved", "cancelled", "extra")

# Длительность занятия по умолчанию, минуты.
DEFAULT_DURATION_MINUTES = 60

# Границы разумной длительности.
MIN_DURATION_MINUTES = 15
MAX_DURATION_MINUTES = 480


def _today_in_tenant_zone(tenant: Tenant) -> date:
    """Возвращает сегодняшнюю дату в часовом поясе тренера.

    Args:
        tenant: Тенант, для которого считается дата.

    Returns:
        Сегодняшняя дата в поясе тренера.
    """
    try:
        zone = ZoneInfo(tenant.timezone or settings.timezone)
    except Exception:  # noqa: BLE001 — некорректный пояс не должен ломать страницу
        zone = ZoneInfo(settings.timezone)
    return datetime.now(zone).date()


def _monday_of(target: date) -> date:
    """Возвращает понедельник недели, в которую попадает дата.

    Args:
        target: Произвольная дата.

    Returns:
        Дата понедельника той же недели.
    """
    return target - timedelta(days=target.weekday())


def _parse_week(raw: str | None, fallback: date) -> date:
    """Разбирает параметр ``week`` из адреса.

    Принимает дату в формате ISO (``YYYY-MM-DD``) и приводит её
    к понедельнику соответствующей недели.

    Args:
        raw: Сырое значение query-параметра.
        fallback: Дата, используемая при ошибке разбора.

    Returns:
        Понедельник запрошенной недели.
    """
    if not raw:
        return _monday_of(fallback)
    try:
        parsed = date.fromisoformat(raw.strip())
    except ValueError:
        return _monday_of(fallback)
    return _monday_of(parsed)


def _clean(value: str | None) -> str:
    """Убирает пробелы по краям и приводит ``None`` к пустой строке.

    Args:
        value: Сырое значение из формы.

    Returns:
        Очищенная строка.
    """
    return (value or "").strip()


def _parse_duration(raw: str | None) -> int:
    """Разбирает длительность занятия из формы.

    Args:
        raw: Сырое значение из формы.

    Returns:
        Длительность в минутах в допустимых границах.
    """
    try:
        value = int(_clean(raw))
    except (TypeError, ValueError):
        return DEFAULT_DURATION_MINUTES
    if value < MIN_DURATION_MINUTES:
        return MIN_DURATION_MINUTES
    if value > MAX_DURATION_MINUTES:
        return MAX_DURATION_MINUTES
    return value


def _parse_date(raw: str | None) -> date | None:
    """Разбирает дату из формы.

    Args:
        raw: Сырое значение из формы.

    Returns:
        Дата либо ``None``, если разобрать не удалось.
    """
    cleaned = _clean(raw)
    if not cleaned:
        return None
    try:
        return date.fromisoformat(cleaned)
    except ValueError:
        return None


def _build_days(week_start: date, schedule: Any, today: date) -> list[dict[str, Any]]:
    """Собирает семь дней недели для сетки расписания.

    Args:
        week_start: Понедельник отображаемой недели.
        schedule: Результат ``ScheduleService.get_week_schedule``.
        today: Сегодняшняя дата в поясе тренера.

    Returns:
        Список словарей с датой, подписью дня и занятиями.
    """
    labels = dict(WEEKDAY_OPTIONS)
    by_date = getattr(schedule, "days", {}) or {}
    days: list[dict[str, Any]] = []

    for offset in range(7):
        current = week_start + timedelta(days=offset)
        code = WEEKDAY_OPTIONS[current.weekday()][0]
        days.append(
            {
                "date": current,
                "weekday_code": code,
                "weekday_label": labels.get(code, code),
                "is_today": current == today,
                "lessons": by_date.get(current, []),
            }
        )

    return days


@router.get("", response_class=HTMLResponse, include_in_schema=False)
@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def schedule_week(
    request: Request,
    week: str | None = None,
    tenant: Tenant = Depends(require_tenant),  # noqa: B008
    repo: Repository = Depends(get_repository),  # noqa: B008
    schedule_service: ScheduleService = Depends(get_schedule_service),  # noqa: B008
) -> HTMLResponse:
    """Недельная сетка расписания с конфликтами.

    Args:
        request: Текущий HTTP-запрос.
        week: Понедельник отображаемой недели в формате ISO.
        tenant: Тенант из cookie-сессии.
        repo: Фасад репозиториев.
        schedule_service: Сервис расписания.

    Returns:
        HTML-страница с недельной сеткой.
    """
    today = _today_in_tenant_zone(tenant)
    week_start = _parse_week(week, today)
    week_end = week_start + timedelta(days=6)

    # Отменённые занятия тоже показываем — тренер должен видеть, что занятие
    # отменено, и иметь возможность вернуть его одним нажатием.
    schedule = await schedule_service.get_week_schedule(
        tenant.id, week_start, include_cancelled=True
    )
    days = _build_days(week_start, schedule, today)

    conflicts: list[Any] = []
    for day in days:
        day_conflicts = await schedule_service.find_conflicts_for_date(tenant.id, day["date"])
        conflicts.extend(day_conflicts)

    entities = await repo.entities.list_by_tenant(tenant.id)
    slots = await repo.schedule.list_by_tenant(tenant.id)

    return templates.TemplateResponse(
        request=request,
        name="admin/schedule.html",
        context={
            "title": "Расписание",
            "active": "schedule",
            "tenant": tenant,
            "week_start": week_start,
            "week_end": week_end,
            "prev_week": (week_start - timedelta(days=7)).isoformat(),
            "next_week": (week_start + timedelta(days=7)).isoformat(),
            "days": days,
            "total_lessons": sum(len(day["lessons"]) for day in days),
            "conflicts": conflicts,
            "entities": entities,
            "slots": slots,
            "weekday_options": WEEKDAY_OPTIONS,
            "saved": request.query_params.get("saved") == "1",
            "error": request.query_params.get("error"),
        },
    )


@router.post("/slots", include_in_schema=False)
async def slot_create(
    entity_id: int = Form(...),
    weekday: str = Form(...),
    start_time: str = Form(...),
    duration_minutes: str | None = Form(None),
    place: str | None = Form(None),
    tenant: Tenant = Depends(require_tenant),  # noqa: B008
    schedule_service: ScheduleService = Depends(get_schedule_service),  # noqa: B008
) -> RedirectResponse:
    """Добавляет регулярный слот расписания.

    Args:
        entity_id: Идентификатор ученика или группы.
        weekday: Код дня недели.
        start_time: Время начала в формате ``ЧЧ:ММ``.
        duration_minutes: Длительность занятия в минутах.
        place: Место проведения.
        tenant: Тенант из cookie-сессии.
        schedule_service: Сервис расписания.

    Returns:
        Редирект на недельную сетку.
    """
    try:
        await schedule_service.add_slot(
            tenant.id,
            entity_id=entity_id,
            weekday=_clean(weekday),
            start_time=_clean(start_time),
            duration_min=_parse_duration(duration_minutes),
            location=_clean(place) or None,
        )
    except ValueError as exc:
        logger.warning("Не удалось добавить слот: %s", exc)
        return RedirectResponse(url=f"/admin/schedule?error={exc}", status_code=303)

    return RedirectResponse(url="/admin/schedule?saved=1", status_code=303)


@router.post("/slots/{slot_id}", include_in_schema=False)
async def slot_update(
    slot_id: int,
    weekday: str = Form(...),
    start_time: str = Form(...),
    duration_minutes: str | None = Form(None),
    place: str | None = Form(None),
    is_active: str | None = Form(None),
    tenant: Tenant = Depends(require_tenant),  # noqa: B008
    schedule_service: ScheduleService = Depends(get_schedule_service),  # noqa: B008
) -> RedirectResponse:
    """Обновляет регулярный слот расписания.

    Args:
        slot_id: Идентификатор слота.
        weekday: Новый день недели.
        start_time: Новое время начала.
        duration_minutes: Новая длительность.
        place: Новое место.
        is_active: Признак активности слота.
        tenant: Тенант из cookie-сессии.
        schedule_service: Сервис расписания.

    Returns:
        Редирект на недельную сетку.
    """
    fields: dict[str, Any] = {
        "weekday": _clean(weekday),
        "start_time": _clean(start_time),
        "duration_min": _parse_duration(duration_minutes),
        "location": _clean(place) or None,
        "is_active": _clean(is_active).lower() in {"1", "on", "true", "yes"},
    }

    try:
        updated = await schedule_service.update_slot(tenant.id, slot_id, **fields)
    except ValueError as exc:
        logger.warning("Не удалось обновить слот %s: %s", slot_id, exc)
        return RedirectResponse(url=f"/admin/schedule?error={exc}", status_code=303)

    if updated is None:
        return RedirectResponse(url="/admin/schedule?error=Слот+не+найден", status_code=303)

    return RedirectResponse(url="/admin/schedule?saved=1", status_code=303)


@router.post("/slots/{slot_id}/delete", include_in_schema=False)
async def slot_delete(
    slot_id: int,
    tenant: Tenant = Depends(require_tenant),  # noqa: B008
    schedule_service: ScheduleService = Depends(get_schedule_service),  # noqa: B008
) -> RedirectResponse:
    """Удаляет регулярный слот расписания.

    История разовых отклонений сохраняется: у исключений обнуляется
    ссылка на слот, но сами записи остаются.

    Args:
        slot_id: Идентификатор слота.
        tenant: Тенант из cookie-сессии.
        schedule_service: Сервис расписания.

    Returns:
        Редирект на недельную сетку.
    """
    await schedule_service.delete_slot(tenant.id, slot_id)
    return RedirectResponse(url="/admin/schedule?saved=1", status_code=303)


@router.post("/exceptions", include_in_schema=False)
async def exception_create(
    entity_id: int = Form(...),
    target_date: str = Form(...),
    kind: str = Form(...),
    slot_id: int | None = Form(None),
    start_time: str | None = Form(None),
    note: str | None = Form(None),
    tenant: Tenant = Depends(require_tenant),  # noqa: B008
    repo: Repository = Depends(get_repository),  # noqa: B008
    schedule_service: ScheduleService = Depends(get_schedule_service),  # noqa: B008
) -> RedirectResponse:
    """Создаёт разовое отклонение: перенос, отмену или доп. занятие.

    Args:
        entity_id: Идентификатор ученика или группы.
        target_date: Дата, к которой относится отклонение.
        kind: Вид отклонения (``moved``/``cancelled``/``extra``).
        slot_id: Идентификатор регулярного слота (для переноса и отмены).
        start_time: Новое время начала (для переноса и доп. занятия).
        note: Комментарий тренера.
        tenant: Тенант из cookie-сессии.
        repo: Фасад репозиториев.
        schedule_service: Сервис расписания.

    Returns:
        Редирект на недельную сетку.
    """
    parsed_date = _parse_date(target_date)
    if parsed_date is None:
        return RedirectResponse(url="/admin/schedule?error=Некорректная+дата", status_code=303)

    clean_kind = _clean(kind).lower()
    if clean_kind not in EXCEPTION_KINDS:
        return RedirectResponse(
            url="/admin/schedule?error=Неизвестный+вид+отклонения", status_code=303
        )

    week = _monday_of(parsed_date).isoformat()

    # Для переноса и отмены нужно знать, какой именно слот затрагивается.
    # Если тренер не выбрал слот явно, ищем единственный слот ученика
    # на этот день недели.
    resolved_slot_id = slot_id
    if resolved_slot_id is None and clean_kind in {"moved", "cancelled"}:
        weekday = WEEKDAY_OPTIONS[parsed_date.weekday()][0]
        slots = await repo.schedule.list_by_entity(tenant.id, entity_id)
        matching = [slot for slot in slots if slot.weekday == weekday and slot.is_active]
        if len(matching) == 1:
            resolved_slot_id = matching[0].id

    try:
        await schedule_service.add_exception(
            tenant.id,
            entity_id=entity_id,
            exception_date=parsed_date,
            kind=clean_kind,
            slot_id=resolved_slot_id,
            new_time=_clean(start_time) or None,
            note=_clean(note) or None,
        )
    except ValueError as exc:
        logger.warning("Не удалось создать отклонение: %s", exc)
        return RedirectResponse(url=f"/admin/schedule?week={week}&error={exc}", status_code=303)

    return RedirectResponse(url=f"/admin/schedule?week={week}&saved=1", status_code=303)


@router.post("/exceptions/{exception_id}/delete", include_in_schema=False)
async def exception_delete(
    exception_id: int,
    week: str | None = Form(None),
    tenant: Tenant = Depends(require_tenant),  # noqa: B008
    schedule_service: ScheduleService = Depends(get_schedule_service),  # noqa: B008
) -> RedirectResponse:
    """Удаляет разовое отклонение.

    Args:
        exception_id: Идентификатор отклонения.
        week: Неделя, на которую вернуть тренера.
        tenant: Тенант из cookie-сессии.
        schedule_service: Сервис расписания.

    Returns:
        Редирект на недельную сетку.
    """
    await schedule_service.delete_exception(tenant.id, exception_id)

    target = _clean(week)
    if target:
        return RedirectResponse(url=f"/admin/schedule?week={target}&saved=1", status_code=303)
    return RedirectResponse(url="/admin/schedule?saved=1", status_code=303)


__all__ = ["router"]
