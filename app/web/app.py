"""Фабрика FastAPI-приложения PlanZy.

Здесь собирается веб-часть: монтируется статика, подключаются роуты
и настраиваются обработчики ошибок. Запуск бота и планировщика
происходит в ``app/main.py`` (Этап 7).
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from app.core.config import settings
from app.core.logging import get_logger
from app.web.admin_lessons_routes import router as admin_lessons_router
from app.web.admin_routes import router as admin_router
from app.web.admin_schedule_routes import router as admin_schedule_router
from app.web.admin_settings_routes import router as admin_settings_router
from app.web.admin_students_routes import router as admin_students_router
from app.web.routes import router
from app.web.templating import STATIC_DIR, templates

logger = get_logger(__name__)


def create_app() -> FastAPI:
    """Создаёт и настраивает экземпляр FastAPI.

    Returns:
        Готовое к запуску приложение.
    """
    app = FastAPI(
        title="PlanZy",
        description="Ассистент тренера: карточки учеников, досье и напоминания.",
        version="0.1.0",
        docs_url="/api/docs" if not settings.is_production else None,
        redoc_url=None,
        openapi_url="/api/openapi.json" if not settings.is_production else None,
    )

    # Статика (Tailwind-сборка, иконки). Директория может отсутствовать
    # на раннем этапе — тогда монтирование пропускается.
    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    else:
        logger.warning("Директория статики не найдена: %s", STATIC_DIR)

    app.include_router(router)

    # ВАЖНО: порядок регистрации определяет приоритет маршрутов.
    # ``admin_router`` содержит catch-all ``GET /admin/{admin_token}``
    # (вход по секретной ссылке), поэтому конкретные разделы кабинета
    # (``/admin/students``, ``/admin/schedule``, ``/admin/lessons``,
    # ``/admin/settings``, ``/admin/reminders``) обязаны регистрироваться
    # РАНЬШЕ него — иначе их пути будут перехвачены как «неизвестный токен».
    app.include_router(admin_students_router)
    app.include_router(admin_schedule_router)
    app.include_router(admin_lessons_router)
    app.include_router(admin_settings_router)
    app.include_router(admin_router)

    _register_error_handlers(app)

    logger.info("Веб-приложение PlanZy собрано (env=%s)", settings.app_env)
    return app


def _register_error_handlers(app: FastAPI) -> None:
    """Регистрирует обработчики ошибок в общем стиле.

    Args:
        app: Экземпляр FastAPI.
    """

    @app.exception_handler(404)
    async def _handle_404(request: Request, exc: Exception) -> HTMLResponse:  # noqa: ARG001
        """Отдаёт стилизованную страницу 404 для HTML-запросов."""
        return templates.TemplateResponse(
            request=request,
            name="404.html",
            context={
                "title": "Страница не найдена",
                "message": "Такой страницы не существует. Проверьте ссылку.",
            },
            status_code=404,
        )

    @app.exception_handler(500)
    async def _handle_500(request: Request, exc: Exception) -> HTMLResponse:  # noqa: ARG001
        """Отдаёт стилизованную страницу 500."""
        logger.exception("Внутренняя ошибка веб-приложения")
        return templates.TemplateResponse(
            request=request,
            name="500.html",
            context={
                "title": "Внутренняя ошибка",
                "message": "Что-то пошло не так. Мы уже разбираемся.",
            },
            status_code=500,
        )
