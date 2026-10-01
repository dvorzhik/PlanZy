"""HTTP-роуты кабинета: ученики и группы.

Раздел «Ученики» — основной рабочий экран тренера: список с фильтрами,
карточка ученика, создание, редактирование, удаление и привязка телефона.

Все изменения пишутся в журнал действий (``audit_logs``), чтобы тренер
мог понять, откуда взялось то или иное значение.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.core.config import settings
from app.core.logging import get_logger
from app.db.models import Entity, ScheduleSlot, Tenant
from app.db.repository import Repository
from app.services.audit_service import AuditService
from app.services.entity_service import EntityService
from app.services.payment_service import PaymentService
from app.services.schedule_service import ScheduleService
from app.services.settings_service import TenantSettingsService
from app.web.dependencies import (
    get_entity_service,
    get_repository,
    get_schedule_service,
    require_tenant,
)
from app.web.templating import templates

logger = get_logger(__name__)

router = APIRouter(prefix="/admin/students", tags=["admin-students"])

# Дни недели для фильтра и форм: код → подпись.
WEEKDAY_OPTIONS: list[tuple[str, str]] = [
    ("mon", "Понедельник"),
    ("tue", "Вторник"),
    ("wed", "Среда"),
    ("thu", "Четверг"),
    ("fri", "Пятница"),
    ("sat", "Суббота"),
    ("sun", "Воскресенье"),
]

# Порядок дней недели для сортировки слотов в сводке расписания.
_WEEKDAY_ORDER: dict[str, int] = {code: index for index, (code, _) in enumerate(WEEKDAY_OPTIONS)}

# Допустимые типы сущностей.
ENTITY_KINDS: tuple[str, ...] = ("student", "group")

# Максимальная длина имени и предмета — совпадает с колонками БД.
MAX_NAME_LENGTH = 255
MAX_SUBJECT_LENGTH = 255
MAX_PHONE_LENGTH = 32
MAX_MEMBERS_LENGTH = 2000
MAX_CHAT_URL_LENGTH = 512

# Границы индивидуальной цены занятия (совпадают с настройками тенанта).
MAX_LESSON_PRICE = 100_000.0

# Верхняя граница суммы одного платежа — защита от опечаток.
MAX_PAYMENT_AMOUNT = 1_000_000.0

# Допустимые хосты ссылки на групповой чат WhatsApp.
_WHATSAPP_CHAT_HOSTS: tuple[str, ...] = (
    "chat.whatsapp.com",
    "www.chat.whatsapp.com",
)


def _today_in_tenant_zone(tenant: Tenant) -> datetime.date:
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


def _clean_members(raw: str | None) -> str | None:
    """Нормализует список участников группы.

    Имена могут быть введены через запятую или с новой строки. Пустые
    строки и лишние пробелы убираются, результат сохраняется как текст
    с именами через запятую. Если участников нет — возвращает ``None``.

    Args:
        raw: Сырое значение из формы.

    Returns:
        Строка с именами через запятую либо ``None``.
    """
    cleaned = _clean(raw)
    if not cleaned:
        return None

    normalized = cleaned.replace("\r\n", "\n").replace("\r", "\n")
    names: list[str] = []
    for chunk in normalized.split("\n"):
        names.extend(chunk.split(","))

    result = ", ".join(name.strip() for name in names if name.strip())
    if not result:
        return None
    return result[:MAX_MEMBERS_LENGTH]


def _normalize_phone(raw: str | None) -> str | None:
    """Приводит телефон к формату ``972XXXXXXXXX``.

    Разрешает ввод с пробелами, дефисами, скобками и ведущим ``+``.
    Если после очистки остались нецифровые символы — возвращает ``None``,
    чтобы не сохранять мусор.

    Args:
        raw: Сырое значение из формы.

    Returns:
        Нормализованный телефон либо ``None``.
    """
    cleaned = _clean(raw)
    if not cleaned:
        return None

    digits = "".join(char for char in cleaned if char.isdigit())
    if not digits:
        return None

    # Локальный израильский формат: 0XXXXXXXXX → 972XXXXXXXXX.
    if digits.startswith("0"):
        digits = f"972{digits[1:]}"

    if len(digits) > MAX_PHONE_LENGTH:
        return None
    return digits


def _normalize_chat_url(raw: str | None) -> str | None:
    """Нормализует ссылку на групповой чат WhatsApp.

    Принимает ссылку вида ``https://chat.whatsapp.com/<код>`` (можно без
    схемы — она подставится). Ссылки на другие хосты отбрасываются,
    чтобы в базе не оказалось произвольного адреса.

    Args:
        raw: Сырое значение из формы.

    Returns:
        Нормализованная ссылка либо ``None``.
    """
    cleaned = _clean(raw)
    if not cleaned:
        return None

    candidate = cleaned if "://" in cleaned else f"https://{cleaned}"
    parsed = urlparse(candidate)
    if parsed.scheme not in ("http", "https"):
        return None
    if (parsed.hostname or "").lower() not in _WHATSAPP_CHAT_HOSTS:
        return None
    if not parsed.path.strip("/"):
        return None

    normalized = f"https://chat.whatsapp.com/{parsed.path.strip('/')}"
    if len(normalized) > MAX_CHAT_URL_LENGTH:
        return None
    return normalized


def _parse_lesson_price(raw: str | None) -> float | None:
    """Разбирает индивидуальную цену занятия из формы.

    Пустое значение означает «использовать цену тенанта» и сохраняется
    как ``None``. Некорректные и выходящие за разумные границы значения
    тоже приводятся к ``None``, чтобы не сломать расчёты.

    Args:
        raw: Сырое значение из формы.

    Returns:
        Цена занятия либо ``None``.
    """
    cleaned = _clean(raw).replace(",", ".")
    if not cleaned:
        return None
    try:
        price = float(cleaned)
    except ValueError:
        return None
    if price <= 0 or price > MAX_LESSON_PRICE:
        return None
    return round(price, 2)


def _parse_int(raw: str | None) -> int | None:
    """Разбирает целое число из формы.

    Args:
        raw: Сырое значение из формы.

    Returns:
        Целое число либо ``None``, если значение пустое или некорректное.
    """
    cleaned = _clean(raw)
    if not cleaned:
        return None
    try:
        return int(cleaned)
    except ValueError:
        return None


def _parse_date(raw: str | None) -> date | None:
    """Разбирает дату из формы в формате ``ГГГГ-ММ-ДД``.

    Args:
        raw: Сырое значение из формы.

    Returns:
        Дата либо ``None``, если значение пустое или некорректное.
    """
    cleaned = _clean(raw)
    if not cleaned:
        return None
    try:
        return date.fromisoformat(cleaned)
    except ValueError:
        return None


def _parse_payment_amount(raw: str | None) -> float | None:
    """Разбирает сумму оплаты из формы.

    Args:
        raw: Сырое значение из формы.

    Returns:
        Положительная сумма либо ``None``, если значение пустое или
        некорректное.
    """
    cleaned = _clean(raw).replace(",", ".")
    if not cleaned:
        return None
    try:
        amount = float(cleaned)
    except ValueError:
        return None
    if amount <= 0 or amount > MAX_PAYMENT_AMOUNT:
        return None
    return round(amount, 2)


def _build_filters(
    kind: str | None,
    weekday: str | None,
) -> dict[str, Any]:
    """Собирает нормализованный словарь фильтров из query-параметров.

    Args:
        kind: Тип сущности (``student``/``group``) или пусто.
        weekday: Код дня недели или пусто.

    Returns:
        Словарь с ключами ``kind`` и ``weekday``.
    """
    normalized_kind = _clean(kind).lower()
    if normalized_kind not in ENTITY_KINDS:
        normalized_kind = ""

    normalized_weekday = _clean(weekday).lower()
    valid_days = {code for code, _ in WEEKDAY_OPTIONS}
    if normalized_weekday not in valid_days:
        normalized_weekday = ""

    return {
        "kind": normalized_kind,
        "weekday": normalized_weekday,
    }


def _apply_filters(
    entities: list[Entity],
    filters: dict[str, Any],
    weekdays_by_entity: dict[int, set[str]] | None = None,
) -> list[Entity]:
    """Применяет фильтры списка к выборке учеников.

    Фильтрация выполняется в памяти: у одного тренера десятки учеников,
    поэтому отдельные SQL-запросы под каждый фильтр избыточны.

    Args:
        entities: Полный список учеников тенанта.
        filters: Нормализованные фильтры из :func:`_build_filters`.
        weekdays_by_entity: Карта ``entity_id`` → множество дней недели,
            в которые у ученика есть регулярные слоты расписания.

    Returns:
        Отфильтрованный список.
    """
    result = entities

    if filters["kind"]:
        result = [item for item in result if item.entity_type == filters["kind"]]

    if filters["weekday"]:
        days_map = weekdays_by_entity or {}
        result = [item for item in result if filters["weekday"] in days_map.get(item.id, set())]

    return result


def _sort_entities(entities: list[Entity]) -> list[Entity]:
    """Сортирует список: сначала ученики, затем группы; внутри — по имени.

    Тренеру удобнее видеть частных учеников сверху, а группы — отдельным
    блоком ниже. Регистр имени не влияет на порядок.

    Args:
        entities: Список учеников и групп.

    Returns:
        Новый отсортированный список.
    """
    return sorted(
        entities,
        key=lambda item: (
            1 if item.is_group else 0,
            item.name.casefold(),
        ),
    )


def _filters_active(filters: dict[str, Any]) -> bool:
    """Проверяет, задан ли хотя бы один фильтр.

    Args:
        filters: Нормализованные фильтры.

    Returns:
        ``True``, если фильтры активны.
    """
    return bool(filters["kind"] or filters["weekday"])


# Названия месяцев в родительном падеже — для подписи вида «сентябрь 2026».
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


def _parse_month(raw: str | None, today: date) -> date:
    """Разбирает параметр ``month`` из query-строки.

    Ожидается формат ``ГГГГ-ММ``. Некорректные значения и месяцы из
    будущего откатываются к текущему месяцу — чтобы тренер не мог
    случайно «уехать» в будущее и увидеть пустые нули.

    Args:
        raw: Сырое значение параметра ``month``.
        today: Сегодняшняя дата в поясе тренера.

    Returns:
        Первое число выбранного месяца.
    """
    current_start = today.replace(day=1)
    cleaned = _clean(raw)
    if not cleaned:
        return current_start
    try:
        year_str, month_str = cleaned.split("-", 1)
        parsed = date(int(year_str), int(month_str), 1)
    except (ValueError, TypeError):
        return current_start
    if parsed > current_start:
        return current_start
    return parsed


def _shift_month(month_start: date, delta: int) -> date:
    """Сдвигает дату первого числа месяца на заданное число месяцев.

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


def _group_slots_by_entity(slots: list[ScheduleSlot]) -> dict[int, list[ScheduleSlot]]:
    """Группирует регулярные слоты по идентификатору ученика.

    Args:
        slots: Все слоты тенанта.

    Returns:
        Словарь ``entity_id`` → список слотов этого ученика.
    """
    grouped: dict[int, list[ScheduleSlot]] = {}
    for slot in slots:
        if slot.entity_id is None:
            continue
        grouped.setdefault(slot.entity_id, []).append(slot)
    return grouped


def _build_schedule_summary(slots: list[ScheduleSlot]) -> dict[int, str]:
    """Строит текстовую сводку расписания для каждого ученика.

    Сводка собирается из регулярных слотов (таблица ``schedule_slots``),
    а не из устаревших полей ``Entity.schedule_days``/``schedule_time``.

    Args:
        slots: Все слоты тенанта.

    Returns:
        Словарь ``entity_id`` → строка вида ``«Пн, Ср · 17:30»``.
    """
    weekday_labels = dict(WEEKDAY_OPTIONS)
    summary: dict[int, str] = {}

    for entity_id, entity_slots in _group_slots_by_entity(slots).items():
        ordered = sorted(
            entity_slots,
            key=lambda slot: (
                _WEEKDAY_ORDER.get(slot.weekday, 99),
                slot.start_time or "",
            ),
        )
        days: list[str] = []
        times: list[str] = []
        for slot in ordered:
            label = weekday_labels.get(slot.weekday, slot.weekday)
            if label not in days:
                days.append(label)
            if slot.start_time and slot.start_time not in times:
                times.append(slot.start_time)

        parts = [", ".join(days)]
        if times:
            parts.append(" · ".join(times))
        summary[entity_id] = " · ".join(part for part in parts if part)

    return summary


@router.get("", response_class=HTMLResponse, include_in_schema=False)
@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def students_list(
    request: Request,
    kind: str | None = None,
    weekday: str | None = None,
    month: str | None = None,
    tenant: Tenant = Depends(require_tenant),  # noqa: B008
    repo: Repository = Depends(get_repository),  # noqa: B008
) -> HTMLResponse:
    """Список учеников и групп с фильтрами.

    Args:
        request: Текущий HTTP-запрос.
        kind: Фильтр по типу сущности.
        weekday: Фильтр по дню недели.
        month: Месяц показателей в формате ``ГГГГ-ММ`` (по умолчанию —
            текущий). Позволяет заглянуть в прошлые месяцы.
        tenant: Тенант из cookie-сессии.
        repo: Фасад репозиториев.

    Returns:
        HTML-страница со списком учеников.
    """
    today = _today_in_tenant_zone(tenant)
    selected_month = _parse_month(month, today)
    current_month = today.replace(day=1)
    is_current_month = selected_month == current_month

    all_entities = await repo.entities.list_by_tenant(tenant.id)
    filters = _build_filters(kind, weekday)

    slots = await repo.schedule.list_by_tenant(tenant.id)
    schedule_by_entity = _build_schedule_summary(slots)
    weekdays_by_entity = {
        entity_id: {slot.weekday for slot in entity_slots}
        for entity_id, entity_slots in _group_slots_by_entity(slots).items()
    }

    payment_service = PaymentService(repo)
    monthly_by_entity = await payment_service.monthly_overview(
        tenant.id, all_entities, today=selected_month
    )
    currency = await payment_service.resolve_currency(tenant.id)

    entities = _apply_filters(
        all_entities,
        filters,
        weekdays_by_entity,
    )
    entities = _sort_entities(entities)

    # Расчет общей суммы оплат учеников и долгов групп за месяц
    total_paid_students = sum(
        monthly_by_entity[item.id]["paid"]
        for item in all_entities
        if item.entity_type == "student" and item.id in monthly_by_entity
    )
    total_debt_groups = sum(
        monthly_by_entity[item.id]["debt"]
        for item in all_entities
        if item.entity_type == "group"
        and item.id in monthly_by_entity
        and monthly_by_entity[item.id]["debt"] > 0
    )

    return templates.TemplateResponse(
        request=request,
        name="admin/students.html",
        context={
            "title": "Ученики",
            "active": "students",
            "tenant": tenant,
            "entities": entities,
            "entities_count": len(all_entities),
            "filters": filters,
            "filters_active": _filters_active(filters),
            "weekday_options": WEEKDAY_OPTIONS,
            "schedule_by_entity": schedule_by_entity,
            "monthly_by_entity": monthly_by_entity,
            "currency": currency,
            "total_paid_students": total_paid_students,
            "total_debt_groups": total_debt_groups,
            "selected_month": selected_month,
            "month_value": selected_month.strftime("%Y-%m"),
            "month_label": _month_label(selected_month),
            "prev_month_value": _shift_month(selected_month, -1).strftime("%Y-%m"),
            "next_month_value": (
                None if is_current_month else _shift_month(selected_month, 1).strftime("%Y-%m")
            ),
            "is_current_month": is_current_month,
        },
    )


@router.get("/new", response_class=HTMLResponse, include_in_schema=False)
async def student_new_form(
    request: Request,
    tenant: Tenant = Depends(require_tenant),  # noqa: B008
) -> HTMLResponse:
    """Форма создания ученика или группы.

    Args:
        request: Текущий HTTP-запрос.
        tenant: Тенант из cookie-сессии.

    Returns:
        HTML-страница с формой создания.
    """
    return templates.TemplateResponse(
        request=request,
        name="admin/student_form.html",
        context={
            "title": "Новый ученик",
            "active": "students",
            "tenant": tenant,
            "entity": None,
            "weekday_options": WEEKDAY_OPTIONS,
            "error": None,
        },
    )


@router.post("", include_in_schema=False)
@router.post("/", include_in_schema=False)
async def student_create(
    name: str = Form(...),
    entity_type: str = Form("student"),
    subject: str = Form("Шахматы"),
    parent_phone: str | None = Form(None),
    members: str | None = Form(None),
    whatsapp_chat_url: str | None = Form(None),
    lesson_price: str | None = Form(None),
    tenant: Tenant = Depends(require_tenant),  # noqa: B008
    repo: Repository = Depends(get_repository),  # noqa: B008
) -> Response:
    """Создаёт ученика или группу.

    Args:
        name: Имя ученика или название группы.
        entity_type: Тип сущности (``student``/``group``).
        subject: Предмет (по умолчанию «Шахматы»).
        parent_phone: Телефон родителя в свободном формате.
        members: Участники группы (только для типа ``group``).
        whatsapp_chat_url: Ссылка на групповой чат WhatsApp (только для группы).
        lesson_price: Индивидуальная цена занятия (пусто — цена тенанта).
        tenant: Тенант из cookie-сессии.
        repo: Фасад репозиториев.

    Returns:
        Редирект на карточку созданной сущности либо обратно на форму.
    """
    clean_name = _clean(name)[:MAX_NAME_LENGTH]
    clean_kind = _clean(entity_type).lower()
    if clean_kind not in ENTITY_KINDS:
        clean_kind = "student"

    clean_subject = _clean(subject)[:MAX_SUBJECT_LENGTH] or "Шахматы"

    if not clean_name:
        return RedirectResponse(url="/admin/students/new", status_code=303)

    entity = await repo.entities.create(
        tenant_id=tenant.id,
        name=clean_name,
        entity_type=clean_kind,
        subject=clean_subject,
        parent_phone=_normalize_phone(parent_phone),
        members=_clean_members(members) if clean_kind == "group" else None,
        whatsapp_chat_url=(
            _normalize_chat_url(whatsapp_chat_url) if clean_kind == "group" else None
        ),
        lesson_price=_parse_lesson_price(lesson_price),
    )

    await AuditService(repo).log_student_created(
        tenant.id, entity.id, name=entity.name, actor="trainer"
    )

    return RedirectResponse(url=f"/admin/students/{entity.id}", status_code=303)


@router.get("/{entity_id}", response_class=HTMLResponse, include_in_schema=False)
async def student_card(
    request: Request,
    entity_id: int,
    tenant: Tenant = Depends(require_tenant),  # noqa: B008
    repo: Repository = Depends(get_repository),  # noqa: B008
    schedule_service: ScheduleService = Depends(get_schedule_service),  # noqa: B008
) -> Response:
    """Карточка ученика: профиль, расписание, уроки, платежи.

    Args:
        request: Текущий HTTP-запрос.
        entity_id: Идентификатор ученика или группы.
        tenant: Тенант из cookie-сессии.
        repo: Фасад репозиториев.
        schedule_service: Сервис расписания.

    Returns:
        HTML-страница карточки либо редирект на список, если ученик не найден.
    """
    entity = await repo.entities.get_by_id(tenant.id, entity_id)
    if entity is None:
        return RedirectResponse(url="/admin/students", status_code=303)

    lessons = await repo.lessons.list_by_entity(tenant.id, entity_id, limit=20)
    payments = await repo.payments.list_by_entity(tenant.id, entity_id, limit=20)
    slots = await repo.schedule.list_by_entity(tenant.id, entity_id)
    week_schedule = await schedule_service.get_week_schedule_for_entity(
        tenant.id, entity_id, _today_in_tenant_zone(tenant)
    )

    # Для групп оплата месячная: считаем долг за текущий календарный месяц.
    monthly = await PaymentService(repo).monthly_summary(
        tenant.id, entity_id, today=_today_in_tenant_zone(tenant)
    )
    snapshot = await TenantSettingsService(repo).get(tenant.id)
    currency = snapshot.currency if snapshot is not None else settings.currency

    return templates.TemplateResponse(
        request=request,
        name="admin/student_card.html",
        context={
            "title": entity.name,
            "active": "students",
            "tenant": tenant,
            "entity": entity,
            "lessons": lessons,
            "payments": payments,
            "slots": slots,
            "week_schedule": week_schedule,
            "weekday_options": WEEKDAY_OPTIONS,
            "monthly": monthly,
            "currency": currency,
            "saved": request.query_params.get("saved") == "1",
            "error": request.query_params.get("error"),
        },
    )


@router.post("/{entity_id}", include_in_schema=False)
async def student_update(
    entity_id: int,
    name: str = Form(...),
    entity_type: str = Form("student"),
    subject: str = Form("Шахматы"),
    parent_phone: str | None = Form(None),
    members: str | None = Form(None),
    whatsapp_chat_url: str | None = Form(None),
    lesson_price: str | None = Form(None),
    tenant: Tenant = Depends(require_tenant),  # noqa: B008
    repo: Repository = Depends(get_repository),  # noqa: B008
) -> Response:
    """Обновляет профиль ученика и пишет изменения в журнал.

    Args:
        entity_id: Идентификатор ученика.
        name: Новое имя.
        entity_type: Новый тип сущности.
        subject: Новый предмет.
        parent_phone: Новый телефон родителя.
        members: Участники группы (только для типа ``group``).
        whatsapp_chat_url: Ссылка на групповой чат WhatsApp (только для группы).
        lesson_price: Индивидуальная цена занятия (пусто — цена тенанта).
        tenant: Тенант из cookie-сессии.
        repo: Фасад репозиториев.

    Returns:
        Редирект на карточку ученика.
    """
    entity = await repo.entities.get_by_id(tenant.id, entity_id)
    if entity is None:
        return RedirectResponse(url="/admin/students", status_code=303)

    clean_name = _clean(name)[:MAX_NAME_LENGTH]
    if not clean_name:
        return RedirectResponse(
            url=f"/admin/students/{entity_id}?error=Имя+не+может+быть+пустым",
            status_code=303,
        )

    clean_kind = _clean(entity_type).lower()
    if clean_kind not in ENTITY_KINDS:
        clean_kind = entity.entity_type

    clean_subject = _clean(subject)[:MAX_SUBJECT_LENGTH] or entity.subject
    phone = _normalize_phone(parent_phone)
    clean_members = _clean_members(members) if clean_kind == "group" else None
    chat_url = _normalize_chat_url(whatsapp_chat_url) if clean_kind == "group" else None
    price = _parse_lesson_price(lesson_price)

    changes: dict[str, Any] = {}
    if clean_name != entity.name:
        changes["name"] = clean_name
    if clean_kind != entity.entity_type:
        changes["entity_type"] = clean_kind
    if clean_subject != entity.subject:
        changes["subject"] = clean_subject
    if phone != entity.parent_phone:
        changes["parent_phone"] = phone
    if clean_members != entity.members:
        changes["members"] = clean_members
    if chat_url != entity.whatsapp_chat_url:
        changes["whatsapp_chat_url"] = chat_url
    if price != entity.lesson_price:
        changes["lesson_price"] = price

    if changes:
        await repo.entities.update(tenant.id, entity_id, **changes)
        await AuditService(repo).log_student_updated(tenant.id, entity_id, changes, actor="trainer")

    return RedirectResponse(url=f"/admin/students/{entity_id}?saved=1", status_code=303)


@router.post("/{entity_id}/delete", include_in_schema=False)
async def student_delete(
    entity_id: int,
    tenant: Tenant = Depends(require_tenant),  # noqa: B008
    repo: Repository = Depends(get_repository),  # noqa: B008
) -> RedirectResponse:
    """Удаляет ученика вместе со связанными данными.

    Args:
        entity_id: Идентификатор ученика.
        tenant: Тенант из cookie-сессии.
        repo: Фасад репозиториев.

    Returns:
        Редирект на список учеников.
    """
    entity = await repo.entities.get_by_id(tenant.id, entity_id)
    if entity is not None:
        name = entity.name
        await repo.entities.delete(tenant.id, entity_id)
        await AuditService(repo).log_student_deleted(
            tenant.id, entity_id, name=name, actor="trainer"
        )

    return RedirectResponse(url="/admin/students", status_code=303)


@router.post("/{entity_id}/payment", include_in_schema=False)
async def student_register_payment(
    entity_id: int,
    amount: str | None = Form(None),
    payment_date: str | None = Form(None),
    lessons_added: str | None = Form(None),
    tenant: Tenant = Depends(require_tenant),  # noqa: B008
    repo: Repository = Depends(get_repository),  # noqa: B008
) -> RedirectResponse:
    """Регистрирует оплату ученика или группы.

    Создаёт запись о платеже. Если указана только сумма, количество
    занятий вычисляется из цены ученика (индивидуальной или цены тенанта)
    и сохраняется в записи платежа.

    Args:
        entity_id: Идентификатор ученика или группы.
        amount: Сумма оплаты.
        payment_date: Дата оплаты в формате ``ГГГГ-ММ-ДД``.
        lessons_added: Явное количество занятий (необязательно).
        tenant: Тенант из cookie-сессии.
        repo: Фасад репозиториев.

    Returns:
        Редирект на карточку ученика.
    """
    entity = await repo.entities.get_by_id(tenant.id, entity_id)
    if entity is None:
        return RedirectResponse(url="/admin/students", status_code=303)

    parsed_amount = _parse_payment_amount(amount)
    parsed_lessons = _parse_int(lessons_added)
    if parsed_amount is None and parsed_lessons is None:
        return RedirectResponse(
            url=f"/admin/students/{entity_id}?error=Укажите+сумму+оплаты",
            status_code=303,
        )

    parsed_date = _parse_date(payment_date) or _today_in_tenant_zone(tenant)
    payment = await PaymentService(repo).register_payment(
        tenant.id,
        entity_id,
        amount=parsed_amount,
        lessons_added=parsed_lessons,
        payment_date=parsed_date,
    )
    await AuditService(repo).log_payment_registered(
        tenant.id,
        entity_id,
        amount=parsed_amount,
        lessons_added=payment.lessons_added,
        actor="trainer",
    )

    logger.info(
        "Оплата ученика: entity_id=%s amount=%s lessons=%s",
        entity_id,
        parsed_amount,
        payment.lessons_added,
    )
    return RedirectResponse(url=f"/admin/students/{entity_id}?saved=1", status_code=303)


@router.post("/{entity_id}/phone", include_in_schema=False)
async def student_set_phone(
    entity_id: int,
    parent_phone: str | None = Form(None),
    tenant: Tenant = Depends(require_tenant),  # noqa: B008
    entity_service: EntityService = Depends(get_entity_service),  # noqa: B008
    repo: Repository = Depends(get_repository),  # noqa: B008
) -> RedirectResponse:
    """Привязывает телефон WhatsApp к ученику или группе.

    Args:
        entity_id: Идентификатор ученика.
        parent_phone: Телефон в свободном формате.
        tenant: Тенант из cookie-сессии.
        entity_service: Сервис сущностей.
        repo: Фасад репозиториев.

    Returns:
        Редирект на карточку ученика.
    """
    phone = _normalize_phone(parent_phone)
    if phone is None:
        return RedirectResponse(
            url=f"/admin/students/{entity_id}?error=Некорректный+телефон",
            status_code=303,
        )

    updated = await entity_service.set_parent_phone(tenant.id, entity_id, phone)
    if updated is None:
        return RedirectResponse(url="/admin/students", status_code=303)

    await AuditService(repo).log_student_updated(
        tenant.id, entity_id, {"parent_phone": phone}, actor="trainer"
    )

    return RedirectResponse(url=f"/admin/students/{entity_id}?saved=1", status_code=303)


__all__ = ["router"]
