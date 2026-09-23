"""HTTP-роуты веб-приложения PlanZy.

Публичные страницы доступны по токену без авторизации:
    * ``GET /l/{token}``       — смарт-карточка ученика для родителя;
    * ``GET /lesson/{token}``  — страница одного занятия (ДЗ, примечание, отчёт);
    * ``GET /dossier/{token}`` — досье ученика для тренера;
    * ``GET /health``          — проверка живости сервиса.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.core.config import settings
from app.core.logging import get_logger
from app.db.base import check_db_connection
from app.services.dossier_service import DossierService, StudentDossier
from app.services.schedule_service import ScheduleService
from app.services.share_link_service import ShareLinkService
from app.services.whatsapp_service import WhatsAppLinkService
from app.web.dependencies import (
    get_dossier_service,
    get_schedule_service,
    get_share_link_service,
    get_whatsapp_service,
)
from app.web.templating import templates

logger = get_logger(__name__)

router = APIRouter()


def _not_found(request: Request, *, title: str, message: str) -> HTMLResponse:
    """Рендерит страницу 404 в общем стиле приложения.

    Args:
        request: Текущий HTTP-запрос.
        title: Заголовок страницы.
        message: Поясняющий текст для пользователя.

    Returns:
        HTML-ответ со статусом 404.
    """
    return templates.TemplateResponse(
        request=request,
        name="404.html",
        context={"title": title, "message": message},
        status_code=404,
    )


async def _build_public_context(
    dossier: StudentDossier,
    whatsapp: WhatsAppLinkService,
    schedule_service: ScheduleService,
) -> dict:
    """Готовит контекст публичной карточки родителя.

    Внутренние заметки тренера (``internal_notes``) сюда не попадают
    принципиально — они не должны утекать на публичную страницу.

    Расписание берётся из таблицы ``schedule_slots`` (актуальный источник),
    а не из устаревших полей ``entities.schedule_days``/``schedule_time``.

    Цена занятия и порядок оплаты на карточке родителя не показываются,
    поэтому настройки тенанта здесь не запрашиваются.

    Args:
        dossier: Собранное досье ученика.
        whatsapp: Сервис ссылок WhatsApp.
        schedule_service: Сервис расписания.

    Returns:
        Словарь с данными для шаблона ``card.html``.
    """
    entity = dossier.entity
    last = dossier.history[0] if dossier.history else None
    # Примечание — то же поле, что тренер заполняет в карточке занятия.
    # Для группы это список участников, для ученика — короткая отметка.
    # Берём последнее занятие, где поле заполнено: пустое значение означает
    # «занятие не проведено» и примечания там быть не может.
    last_note = next((entry.attendance for entry in dossier.history if entry.attendance), None)
    slots = await schedule_service.list_slots_for_entity(
        entity.tenant_id,
        entity.id,
        only_active=True,
    )
    return {
        "title": f"Карточка · {dossier.name}",
        "dossier": dossier,
        "entity": entity,
        "last_lesson": last,
        "last_note": last_note,
        "slots": slots,
        "whatsapp_url": whatsapp.build_plain_link(
            "+972534766343",
            f"Доброго дня! Пишу по занятиям ({dossier.name}).",
        ),
        "has_phone": True,
    }


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def index(request: Request) -> HTMLResponse:
    """Корневая страница-заглушка.

    Args:
        request: Текущий HTTP-запрос.

    Returns:
        Простая HTML-страница с описанием сервиса.
    """
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"title": "PlanZy — ассистент тренера"},
    )


@router.get("/l/{token}", response_class=HTMLResponse)
async def public_card(
    request: Request,
    token: str,
    dossier_service: DossierService = Depends(get_dossier_service),  # noqa: B008
    whatsapp: WhatsAppLinkService = Depends(get_whatsapp_service),  # noqa: B008
    schedule_service: ScheduleService = Depends(get_schedule_service),  # noqa: B008
) -> HTMLResponse:
    """Публичная смарт-карточка ученика для родителя.

    Показывает актуальное расписание из ``schedule_slots``, последнее
    занятие, домашнее задание, примечание и сводные итоги. Историю занятий
    и оплату карточка не показывает — эти разделы остались только в досье
    тренера. Приватные заметки тренера не отображаются.

    Args:
        request: Текущий HTTP-запрос.
        token: Публичный токен ученика.
        dossier_service: Сервис сборки досье.
        whatsapp: Сервис ссылок WhatsApp.
        schedule_service: Сервис расписания.

    Returns:
        HTML-страница карточки или 404, если токен неизвестен.
    """
    dossier = await dossier_service.build_by_token(token)
    if dossier is None:
        logger.info("Публичная карточка: неизвестный токен %r", token)
        return _not_found(
            request,
            title="Карточка не найдена",
            message="Ссылка недействительна или устарела. Попросите тренера прислать новую.",
        )

    context = await _build_public_context(
        dossier,
        whatsapp,
        schedule_service,
    )
    return templates.TemplateResponse(request=request, name="card.html", context=context)


@router.get("/lesson/{token}", response_class=HTMLResponse)
async def shared_lesson(
    request: Request,
    token: str,
    share_service: ShareLinkService = Depends(get_share_link_service),  # noqa: B008
    whatsapp: WhatsAppLinkService = Depends(get_whatsapp_service),  # noqa: B008
) -> HTMLResponse:
    """Публичная страница одного занятия по ссылке тренера.

    Показывает тему, домашнее задание, оценку, примечание (кто был /
    короткая отметка) и план на следующий урок. Приватные заметки тренера
    (``internal_notes``) не отображаются. Если ссылка отозвана или истёк
    её срок — показывается страница «ссылка недействительна».

    Args:
        request: Текущий HTTP-запрос.
        token: Токен публичной ссылки на занятие.
        share_service: Сервис публичных ссылок на занятия.
        whatsapp: Сервис ссылок WhatsApp.

    Returns:
        HTML-страница занятия, страница «ссылка истекла» или 404.
    """
    shared = await share_service.resolve(token)
    if shared is None:
        logger.info("Страница занятия: неизвестный токен %r", token)
        return _not_found(
            request,
            title="Занятие не найдено",
            message="Ссылка недействительна или устарела. Попросите тренера прислать новую.",
        )

    if not shared.is_valid:
        reason = "Ссылка отозвана тренером." if shared.is_revoked else "Срок действия ссылки истёк."
        return templates.TemplateResponse(
            request=request,
            name="lesson_expired.html",
            context={
                "title": "Ссылка недействительна",
                "reason": reason,
                "entity": shared.entity,
                "expires_at": shared.link.expires_at,
            },
            status_code=410,
        )

    lesson = shared.lesson
    entity = shared.entity
    context = {
        "title": f"Занятие · {entity.name}",
        "lesson": lesson,
        "entity": entity,
        "whatsapp_url": whatsapp.build_plain_link(
            "+972534766343",
            f"Доброго дня! Пишу по занятию ({entity.name}).",
        ),
        "has_phone": True,
    }
    return templates.TemplateResponse(request=request, name="lesson_share.html", context=context)


@router.get("/dossier/{token}", response_class=HTMLResponse)
async def student_dossier(
    request: Request,
    token: str,
    dossier_service: DossierService = Depends(get_dossier_service),  # noqa: B008
    whatsapp: WhatsAppLinkService = Depends(get_whatsapp_service),  # noqa: B008
) -> HTMLResponse:
    """Досье ученика для тренера.

    Расширенное представление: история тем, динамика оценок, слабые темы,
    план на следующий урок и ссылка на WhatsApp родителя.

    Args:
        request: Текущий HTTP-запрос.
        token: Публичный токен ученика.
        dossier_service: Сервис сборки досье.
        whatsapp: Сервис ссылок WhatsApp.

    Returns:
        HTML-страница досье или 404, если токен неизвестен.
    """
    dossier = await dossier_service.build_by_token(token)
    if dossier is None:
        logger.info("Досье: неизвестный токен %r", token)
        return _not_found(
            request,
            title="Досье не найдено",
            message="Ученик с таким токеном не найден. Проверьте ссылку.",
        )

    entity = dossier.entity
    context = {
        "title": f"Досье · {dossier.name}",
        "dossier": dossier,
        "entity": entity,
        "whatsapp_url": whatsapp.build_plain_link(
            entity.parent_phone,
            f"Доброго дня! Пишу по занятиям ({dossier.name}).",
        ),
        "has_phone": bool(entity.parent_phone),
    }
    return templates.TemplateResponse(request=request, name="dossier.html", context=context)


@router.get("/health", response_class=JSONResponse)
async def health() -> JSONResponse:
    """Проверка живости сервиса и доступности БД.

    Returns:
        JSON со статусом, окружением и результатом проверки БД.
    """
    db_ok = await check_db_connection()
    payload = {
        "status": "ok" if db_ok else "degraded",
        "app": "PlanZy",
        "env": settings.app_env,
        "database": "ok" if db_ok else "unavailable",
    }
    return JSONResponse(content=payload, status_code=200 if db_ok else 503)
