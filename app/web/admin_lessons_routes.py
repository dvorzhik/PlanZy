"""HTTP-роуты кабинета: журнал занятий.

Раздел «Занятия» — второй по важности экран тренера после расписания:
список проведённых уроков с фильтрами по ученику и периоду, ручное
создание занятия, редактирование темы/ДЗ/оценки/заметок/посещаемости,
удаление, генерация отчёта родителю и публичные
ссылки на конкретное занятие (``/lesson/{token}``).

Все изменения пишутся в журнал действий (``audit_logs``), чтобы тренер
мог понять, откуда взялось то или иное значение.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.core.config import settings
from app.core.logging import get_logger
from app.db.models import Entity, Lesson, Tenant
from app.db.repository import Repository
from app.services.audit_service import AuditService
from app.services.settings_service import TenantSettingsService
from app.services.share_link_service import ShareLinkService
from app.web.dependencies import get_repository, require_tenant
from app.web.templating import templates

logger = get_logger(__name__)

router = APIRouter(prefix="/admin/lessons", tags=["admin-lessons"])

# Значения по умолчанию для фильтра периода.
DEFAULT_PERIOD_DAYS = 30
MAX_PERIOD_DAYS = 365
MAX_TOPIC_LENGTH = 2000
MAX_RATING_LENGTH = 32
MAX_ATTENDANCE_LENGTH = 2000

# Периоды для быстрого выбора в фильтре: код → (подпись, дней).
PERIOD_OPTIONS: list[tuple[str, str, int]] = [
    ("week", "За неделю", 7),
    ("month", "За месяц", 30),
    ("quarter", "За 3 месяца", 90),
    ("year", "За год", 365),
]


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


def _clean(value: str | None) -> str:
    """Убирает пробелы по краям и приводит ``None`` к пустой строке.

    Args:
        value: Сырое значение из формы.

    Returns:
        Очищенная строка.
    """
    return (value or "").strip()


def _parse_date(raw: str | None) -> date | None:
    """Разбирает дату из формы или адреса.

    Args:
        raw: Строка вида ``2026-09-14``.

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


def _parse_time(raw: str | None) -> str | None:
    """Разбирает время начала занятия из формы.

    Args:
        raw: Строка вида ``17:30`` (значение ``<input type="time">``).

    Returns:
        Нормализованное время ``ЧЧ:ММ`` либо ``None``, если разобрать
        не удалось.
    """
    cleaned = _clean(raw)
    if not cleaned:
        return None
    try:
        parsed = time.fromisoformat(cleaned)
    except ValueError:
        return None
    return parsed.strftime("%H:%M")


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


def _resolve_period(
    period: str | None,
    date_from: str | None,
    date_to: str | None,
    today: date,
) -> tuple[date, date, str]:
    """Определяет границы периода для фильтра журнала.

    Приоритет: явные даты ``date_from``/``date_to`` → код периода →
    значение по умолчанию (последние 30 дней).

    Args:
        period: Код периода (``week``, ``month``, ``quarter``, ``year``).
        date_from: Начало периода из формы.
        date_to: Конец периода из формы.
        today: Сегодняшняя дата в поясе тренера.

    Returns:
        Кортеж ``(начало, конец, код_периода)``.
    """
    explicit_from = _parse_date(date_from)
    explicit_to = _parse_date(date_to)

    if explicit_from or explicit_to:
        start = explicit_from or (explicit_to - timedelta(days=DEFAULT_PERIOD_DAYS))
        end = explicit_to or today
        if start > end:
            start, end = end, start
        return start, end, "custom"

    known = {code: days for code, _, days in PERIOD_OPTIONS}
    code = _clean(period)
    days = known.get(code, DEFAULT_PERIOD_DAYS)
    if code not in known:
        code = "month"
    return today - timedelta(days=days), today, code


def _filter_lessons(
    lessons: list[Lesson],
    *,
    entity_id: int | None,
    start: date,
    end: date,
) -> list[Lesson]:
    """Фильтрует уроки по ученику и периоду.

    Фильтрация выполняется в памяти: журнал тренера невелик, а такой
    подход позволяет обойтись без дополнительных запросов к БД.

    Args:
        lessons: Полный список уроков тенанта.
        entity_id: Идентификатор ученика или ``None`` для всех.
        start: Начало периода включительно.
        end: Конец периода включительно.

    Returns:
        Отфильтрованный список уроков, новые сверху.
    """
    result = [
        lesson
        for lesson in lessons
        if start <= lesson.lesson_date <= end
        and (entity_id is None or lesson.entity_id == entity_id)
    ]
    result.sort(key=lambda item: (item.lesson_date, item.start_time or "", item.id), reverse=True)
    return result


def _build_report(lesson: Lesson, entity: Entity, *, currency: str) -> str:
    """Собирает текст отчёта родителю по занятию.

    Отчёт содержит только публичные поля: тему, домашнее задание,
    оценку и план на следующий урок. Приватные заметки тренера
    (``internal_notes``) в отчёт не попадают никогда. Посещаемость,
    остаток занятий и валюта в отчёт не включаются — это внутренние
    данные тренера.

    Args:
        lesson: Занятие, по которому строится отчёт.
        entity: Ученик или группа.
        currency: Валюта тенанта (не используется в отчёте, оставлена
            для совместимости вызовов).

    Returns:
        Готовый текст отчёта.
    """
    lines = [f"Занятие {lesson.lesson_date.strftime('%d.%m.%Y')} — {entity.name}"]

    if lesson.topic:
        lines.append(f"Тема: {lesson.topic}")
    if lesson.homework:
        lines.append(f"Домашнее задание: {lesson.homework}")
    if lesson.rating:
        lines.append(f"Оценка: {lesson.rating}")
    if lesson.next_step:
        lines.append(f"План на следующий урок: {lesson.next_step}")

    return "\n".join(lines)


@router.get("", response_class=HTMLResponse, include_in_schema=False)
async def lessons_list(
    request: Request,
    entity_id: str | None = None,
    period: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    saved: str | None = None,
    error: str | None = None,
    tenant: Tenant = Depends(require_tenant),  # noqa: B008
    repo: Repository = Depends(get_repository),  # noqa: B008
) -> HTMLResponse:
    """Показывает журнал занятий с фильтрами по ученику и периоду.

    Args:
        request: HTTP-запрос.
        entity_id: Фильтр по ученику или группе.
        period: Код периода (``week``, ``month``, ``quarter``, ``year``).
        date_from: Начало произвольного периода.
        date_to: Конец произвольного периода.
        saved: Флаг «изменения сохранены».
        error: Текст ошибки для баннера.
        tenant: Тенант из cookie-сессии.
        repo: Фасад репозиториев.

    Returns:
        HTML-страница журнала занятий.
    """
    today = _today_in_tenant_zone(tenant)
    start, end, period_code = _resolve_period(period, date_from, date_to, today)

    selected_entity_id = _parse_int(entity_id)
    entities = await repo.entities.list_by_tenant(tenant.id)
    entity_by_id = {item.id: item for item in entities}

    # Если выбран несуществующий ученик — сбрасываем фильтр, а не падаем.
    if selected_entity_id is not None and selected_entity_id not in entity_by_id:
        selected_entity_id = None

    all_lessons = await repo.lessons.list_by_tenant(tenant.id)
    lessons = _filter_lessons(
        all_lessons,
        entity_id=selected_entity_id,
        start=start,
        end=end,
    )

    rows = [
        {
            "lesson": lesson,
            "entity": entity_by_id.get(lesson.entity_id),
            # Посещаемость — свободный текст: для группы это список участников.
            "attendance_label": lesson.attendance or "—",
        }
        for lesson in lessons
    ]

    context: dict[str, Any] = {
        "title": "Занятия",
        "active": "lessons",
        "tenant": tenant,
        "rows": rows,
        "total": len(rows),
        "entities": entities,
        "selected_entity_id": selected_entity_id,
        "period": period_code,
        "period_options": PERIOD_OPTIONS,
        "date_from": start.isoformat(),
        "date_to": end.isoformat(),
        "today": today,
        "saved": bool(saved),
        "error": error,
    }
    return templates.TemplateResponse(request, "admin/lessons.html", context)


@router.get("/new", response_class=HTMLResponse, include_in_schema=False)
async def lesson_new_form(
    request: Request,
    entity_id: str | None = None,
    tenant: Tenant = Depends(require_tenant),  # noqa: B008
    repo: Repository = Depends(get_repository),  # noqa: B008
) -> HTMLResponse:
    """Показывает форму ручного создания занятия.

    Args:
        request: HTTP-запрос.
        entity_id: Предвыбранный ученик.
        tenant: Тенант из cookie-сессии.
        repo: Фасад репозиториев.

    Returns:
        HTML-страница формы.
    """
    entities = await repo.entities.list_by_tenant(tenant.id)
    # Участники групп — для автоподстановки в поле «Посещаемость».
    members_by_entity = {
        str(entity.id): ", ".join(entity.members_list)
        for entity in entities
        if entity.is_group and entity.members_list
    }
    context: dict[str, Any] = {
        "title": "Новое занятие",
        "active": "lessons",
        "tenant": tenant,
        "entities": entities,
        "members_by_entity": members_by_entity,
        "lesson": None,
        "selected_entity_id": _parse_int(entity_id),
        "today": _today_in_tenant_zone(tenant),
        "error": None,
    }
    return templates.TemplateResponse(request, "admin/lesson_form.html", context)


@router.post("", include_in_schema=False)
async def lesson_create(
    entity_id: str | None = Form(None),
    lesson_date: str | None = Form(None),
    start_time: str | None = Form(None),
    topic: str | None = Form(None),
    homework: str | None = Form(None),
    internal_notes: str | None = Form(None),
    rating: str | None = Form(None),
    next_step: str | None = Form(None),
    attendance: str | None = Form(None),
    tenant: Tenant = Depends(require_tenant),  # noqa: B008
    repo: Repository = Depends(get_repository),  # noqa: B008
) -> RedirectResponse:
    """Создаёт занятие вручную.

    Args:
        entity_id: Идентификатор ученика или группы.
        lesson_date: Дата занятия.
        start_time: Время начала занятия.
        topic: Тема занятия.
        homework: Домашнее задание.
        internal_notes: Приватные заметки тренера.
        rating: Оценка.
        next_step: План на следующий урок.
        attendance: Посещаемость.
        tenant: Тенант из cookie-сессии.
        repo: Фасад репозиториев.

    Returns:
        Редирект на журнал занятий.
    """
    parsed_entity_id = _parse_int(entity_id)
    if parsed_entity_id is None:
        return RedirectResponse(url="/admin/lessons/new?error=Выберите+ученика", status_code=303)

    entity = await repo.entities.get_by_id(tenant.id, parsed_entity_id)
    if entity is None:
        return RedirectResponse(url="/admin/lessons/new?error=Ученик+не+найден", status_code=303)

    parsed_date = _parse_date(lesson_date) or _today_in_tenant_zone(tenant)
    clean_topic = _clean(topic)[:MAX_TOPIC_LENGTH] or "—"
    # Посещаемость — свободный текст: для группы это список участников,
    # для ученика — короткая отметка. Пустое значение = занятие не проведено.
    clean_attendance = _clean(attendance)[:MAX_ATTENDANCE_LENGTH] or None

    lesson = await repo.lessons.create(
        tenant_id=tenant.id,
        entity_id=entity.id,
        lesson_date=parsed_date,
        start_time=_parse_time(start_time),
        topic=clean_topic,
        homework=_clean(homework) or None,
        internal_notes=_clean(internal_notes) or None,
        rating=_clean(rating)[:MAX_RATING_LENGTH] or None,
        next_step=_clean(next_step) or None,
        attendance=clean_attendance,
    )

    await AuditService(repo).log_lesson_created(
        tenant.id, lesson.id, entity_id=entity.id, actor="trainer"
    )

    logger.info("Создано занятие id=%s entity_id=%s", lesson.id, entity.id)
    return RedirectResponse(url="/admin/lessons?saved=1", status_code=303)


@router.get("/{lesson_id}", response_class=HTMLResponse, include_in_schema=False)
async def lesson_card(
    request: Request,
    lesson_id: int,
    saved: str | None = None,
    error: str | None = None,
    tenant: Tenant = Depends(require_tenant),  # noqa: B008
    repo: Repository = Depends(get_repository),  # noqa: B008
) -> HTMLResponse:
    """Показывает карточку занятия с формой редактирования и отчётом.

    Args:
        request: HTTP-запрос.
        lesson_id: Идентификатор занятия.
        saved: Флаг «изменения сохранены».
        error: Текст ошибки для баннера.
        tenant: Тенант из cookie-сессии.
        repo: Фасад репозиториев.

    Returns:
        HTML-страница карточки занятия.
    """
    lesson = await repo.lessons.get_by_id(tenant.id, lesson_id)
    if lesson is None:
        return RedirectResponse(url="/admin/lessons?error=Занятие+не+найдено", status_code=303)

    entity = await repo.entities.get_by_id(tenant.id, lesson.entity_id)
    snapshot = await TenantSettingsService(repo).get(tenant.id)
    report = _build_report(lesson, entity, currency=snapshot.currency) if entity is not None else ""

    share_service = ShareLinkService(repo)
    active_link = await share_service.get_active_link(tenant.id, lesson_id)
    latest_link = active_link or await share_service.get_latest_link(tenant.id, lesson_id)
    share_url = (
        f"{settings.base_url.rstrip('/')}/lesson/{active_link.token}"
        if active_link is not None
        else None
    )
    share_days_left = ShareLinkService.days_left(latest_link) if latest_link is not None else None
    share_expired = latest_link is not None and share_days_left == 0

    context: dict[str, Any] = {
        "title": "Занятие",
        "active": "lessons",
        "tenant": tenant,
        "lesson": lesson,
        "entity": entity,
        "report": report,
        "share_link": latest_link,
        "share_url": share_url,
        "share_days_left": share_days_left,
        "share_expired": share_expired,
        "saved": bool(saved),
        "error": error,
    }
    return templates.TemplateResponse(request, "admin/lesson_card.html", context)


@router.post("/{lesson_id}", include_in_schema=False)
async def lesson_update(
    lesson_id: int,
    lesson_date: str | None = Form(None),
    start_time: str | None = Form(None),
    topic: str | None = Form(None),
    homework: str | None = Form(None),
    internal_notes: str | None = Form(None),
    rating: str | None = Form(None),
    next_step: str | None = Form(None),
    attendance: str | None = Form(None),
    tenant: Tenant = Depends(require_tenant),  # noqa: B008
    repo: Repository = Depends(get_repository),  # noqa: B008
) -> RedirectResponse:
    """Обновляет поля занятия.

    Args:
        lesson_id: Идентификатор занятия.
        lesson_date: Новая дата занятия.
        start_time: Новое время начала занятия.
        topic: Новая тема.
        homework: Новое домашнее задание.
        internal_notes: Новые приватные заметки.
        rating: Новая оценка.
        next_step: Новый план на следующий урок.
        attendance: Новая посещаемость.
        tenant: Тенант из cookie-сессии.
        repo: Фасад репозиториев.

    Returns:
        Редирект на карточку занятия.
    """
    lesson = await repo.lessons.get_by_id(tenant.id, lesson_id)
    if lesson is None:
        return RedirectResponse(url="/admin/lessons?error=Занятие+не+найдено", status_code=303)

    clean_topic = _clean(topic)[:MAX_TOPIC_LENGTH]
    if not clean_topic:
        return RedirectResponse(
            url=f"/admin/lessons/{lesson_id}?error=Тема+не+может+быть+пустой",
            status_code=303,
        )

    # Посещаемость — свободный текст (для группы список участников).
    clean_attendance = _clean(attendance)[:MAX_ATTENDANCE_LENGTH] or None

    fields: dict[str, Any] = {
        "lesson_date": _parse_date(lesson_date) or lesson.lesson_date,
        "start_time": _parse_time(start_time),
        "topic": clean_topic,
        "homework": _clean(homework) or None,
        "internal_notes": _clean(internal_notes) or None,
        "rating": _clean(rating)[:MAX_RATING_LENGTH] or None,
        "next_step": _clean(next_step) or None,
        "attendance": clean_attendance,
    }

    updated = await repo.lessons.update(tenant.id, lesson_id, **fields)
    if updated is None:
        return RedirectResponse(url="/admin/lessons?error=Занятие+не+найдено", status_code=303)

    await AuditService(repo).log_lesson_updated(
        tenant.id, lesson_id, changes=sorted(fields), actor="trainer"
    )

    logger.info("Обновлено занятие id=%s", lesson_id)
    return RedirectResponse(url=f"/admin/lessons/{lesson_id}?saved=1", status_code=303)


@router.post("/{lesson_id}/delete", include_in_schema=False)
async def lesson_delete(
    lesson_id: int,
    tenant: Tenant = Depends(require_tenant),  # noqa: B008
    repo: Repository = Depends(get_repository),  # noqa: B008
) -> RedirectResponse:
    """Удаляет занятие.

    Args:
        lesson_id: Идентификатор занятия.
        tenant: Тенант из cookie-сессии.
        repo: Фасад репозиториев.

    Returns:
        Редирект на журнал занятий.
    """
    lesson = await repo.lessons.get_by_id(tenant.id, lesson_id)
    if lesson is None:
        # Идемпотентность: повторное удаление не должно ломать страницу.
        return RedirectResponse(url="/admin/lessons?saved=1", status_code=303)

    await repo.lessons.delete(tenant.id, lesson_id)
    await AuditService(repo).log_lesson_deleted(
        tenant.id, lesson_id, entity_id=lesson.entity_id, actor="trainer"
    )

    logger.info("Удалено занятие id=%s", lesson_id)
    return RedirectResponse(url="/admin/lessons?saved=1", status_code=303)


@router.post("/{lesson_id}/share", include_in_schema=False)
async def lesson_share_create(
    lesson_id: int,
    ttl_days: str | None = Form(None),
    tenant: Tenant = Depends(require_tenant),  # noqa: B008
    repo: Repository = Depends(get_repository),  # noqa: B008
) -> RedirectResponse:
    """Создаёт (или переиспользует) публичную ссылку на занятие.

    Args:
        lesson_id: Идентификатор занятия.
        ttl_days: Срок жизни ссылки в днях (пусто — значение по умолчанию).
        tenant: Тенант из cookie-сессии.
        repo: Фасад репозиториев.

    Returns:
        Редирект на карточку занятия.
    """
    lesson = await repo.lessons.get_by_id(tenant.id, lesson_id)
    if lesson is None:
        return RedirectResponse(url="/admin/lessons?error=Занятие+не+найдено", status_code=303)

    parsed_ttl = _parse_int(ttl_days)
    try:
        await ShareLinkService(repo).ensure_link(tenant.id, lesson_id, ttl_days=parsed_ttl)
    except ValueError:
        return RedirectResponse(
            url=f"/admin/lessons/{lesson_id}?error=Некорректный+срок+ссылки",
            status_code=303,
        )

    logger.info("Создана публичная ссылка на занятие id=%s", lesson_id)
    return RedirectResponse(url=f"/admin/lessons/{lesson_id}?saved=1", status_code=303)


@router.post("/{lesson_id}/share/revoke", include_in_schema=False)
async def lesson_share_revoke(
    lesson_id: int,
    tenant: Tenant = Depends(require_tenant),  # noqa: B008
    repo: Repository = Depends(get_repository),  # noqa: B008
) -> RedirectResponse:
    """Отзывает активную публичную ссылку на занятие.

    Args:
        lesson_id: Идентификатор занятия.
        tenant: Тенант из cookie-сессии.
        repo: Фасад репозиториев.

    Returns:
        Редирект на карточку занятия.
    """
    share_service = ShareLinkService(repo)
    active_link = await share_service.get_active_link(tenant.id, lesson_id)
    if active_link is not None:
        await share_service.revoke_link(tenant.id, active_link.id)
        logger.info("Отозвана публичная ссылка на занятие id=%s", lesson_id)

    return RedirectResponse(url=f"/admin/lessons/{lesson_id}?saved=1", status_code=303)


__all__ = ["router"]
