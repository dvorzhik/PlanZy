"""Middleware бота: whitelist по telegram_user_id и резолв тенанта.

Бот обслуживает только тренеров из белого списка. Каждое входящее
сообщение проходит через :class:`WhitelistMiddleware`, который:

1. Проверяет, что ``telegram_user_id`` отправителя есть в таблице ``tenants``
   и тенант активен.
2. Кладёт найденного тенанта и открытую сессию БД в ``data`` — хендлеры
   получают их без повторных запросов.
3. Молча игнорирует всех остальных (никаких ответов «доступ запрещён»,
   чтобы не раскрывать существование бота).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, User

from app.core.logging import get_logger
from app.db.base import get_session
from app.db.models import Tenant
from app.db.repository import Repository

logger = get_logger(__name__)

# Ключ, под которым тенант кладётся в data хендлера.
TENANT_KEY = "tenant"
# Ключ, под которым репозиторий кладётся в data хендлера.
REPO_KEY = "repo"


class WhitelistMiddleware(BaseMiddleware):
    """Пропускает только тренеров из белого списка и резолвит их тенанта."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        """Проверяет отправителя и подготавливает контекст хендлера.

        Args:
            handler: Следующий обработчик в цепочке.
            event: Входящее событие Telegram.
            data: Контекст обработки (мутируется).

        Returns:
            Результат работы хендлера или None, если доступ запрещён.
        """
        user: User | None = data.get("event_from_user")
        if user is None:
            logger.debug("Событие без отправителя — пропускаем")
            return None

        async with get_session() as session:
            repo = Repository(session)
            tenant = await repo.tenants.get_by_telegram_id(user.id)

            if tenant is None or not tenant.is_active:
                logger.warning(
                    "Отклонён доступ: telegram_user_id=%s (@%s) не в белом списке",
                    user.id,
                    user.username,
                )
                return None

            data[TENANT_KEY] = tenant
            data[REPO_KEY] = repo
            return await handler(event, data)


def get_tenant(data: dict[str, Any]) -> Tenant:
    """Достаёт тенанта из контекста хендлера.

    Args:
        data: Контекст обработки, подготовленный middleware.

    Returns:
        Тенант текущего тренера.

    Raises:
        RuntimeError: Если middleware не был подключён.
    """
    tenant = data.get(TENANT_KEY)
    if tenant is None:
        raise RuntimeError("Тенант не найден в контексте: проверьте WhitelistMiddleware")
    return tenant


def get_repo(data: dict[str, Any]) -> Repository:
    """Достаёт репозиторий из контекста хендлера.

    Args:
        data: Контекст обработки, подготовленный middleware.

    Returns:
        Фасад репозиториев, привязанный к открытой сессии.

    Raises:
        RuntimeError: Если middleware не был подключён.
    """
    repo = data.get(REPO_KEY)
    if repo is None:
        raise RuntimeError("Репозиторий не найден в контексте: проверьте WhitelistMiddleware")
    return repo
