"""Сервис журнала действий тренера.

Пишет записи в ``audit_logs``: кто (тренер или бот) и что изменил.
Используется веб-кабинетом для истории правок и отладки.
"""

from __future__ import annotations

from typing import Any

from app.core.logging import get_logger
from app.db.models import AuditLog
from app.db.repository import Repository

logger = get_logger(__name__)

# Известные действия — помогают держать журнал единообразным.
ACTION_STUDENT_CREATED = "student_created"
ACTION_STUDENT_UPDATED = "student_updated"
ACTION_STUDENT_DELETED = "student_deleted"
ACTION_PAYMENT_REGISTERED = "payment_registered"
ACTION_PAYMENT_DELETED = "payment_deleted"
ACTION_SLOT_CREATED = "slot_created"
ACTION_SLOT_UPDATED = "slot_updated"
ACTION_SLOT_DELETED = "slot_deleted"
ACTION_EXCEPTION_CREATED = "exception_created"
ACTION_EXCEPTION_DELETED = "exception_deleted"
ACTION_LESSON_CREATED = "lesson_created"
ACTION_LESSON_UPDATED = "lesson_updated"
ACTION_LESSON_DELETED = "lesson_deleted"
ACTION_SETTINGS_UPDATED = "settings_updated"
ACTION_REMINDER_UPDATED = "reminder_updated"
ACTION_SHARE_LINK_CREATED = "share_link_created"
ACTION_SHARE_LINK_REVOKED = "share_link_revoked"

# Допустимые авторы действий.
ACTOR_TRAINER = "trainer"
ACTOR_BOT = "bot"
ACTOR_SYSTEM = "system"

ACTORS = (ACTOR_TRAINER, ACTOR_BOT, ACTOR_SYSTEM)

# Человекочитаемые названия действий для интерфейса.
ACTION_LABELS = {
    ACTION_STUDENT_CREATED: "Добавлен ученик",
    ACTION_STUDENT_UPDATED: "Изменён ученик",
    ACTION_STUDENT_DELETED: "Удалён ученик",
    ACTION_PAYMENT_REGISTERED: "Внесена оплата",
    ACTION_PAYMENT_DELETED: "Удалена оплата",
    ACTION_SLOT_CREATED: "Добавлено занятие в расписание",
    ACTION_SLOT_UPDATED: "Изменено занятие в расписании",
    ACTION_SLOT_DELETED: "Удалено занятие из расписания",
    ACTION_EXCEPTION_CREATED: "Добавлено отклонение от расписания",
    ACTION_EXCEPTION_DELETED: "Удалено отклонение от расписания",
    ACTION_LESSON_CREATED: "Добавлено занятие в журнал",
    ACTION_LESSON_UPDATED: "Изменено занятие в журнале",
    ACTION_LESSON_DELETED: "Удалено занятие из журнала",
    ACTION_SETTINGS_UPDATED: "Изменены настройки",
    ACTION_REMINDER_UPDATED: "Изменены напоминания",
    ACTION_SHARE_LINK_CREATED: "Создана ссылка на занятие",
    ACTION_SHARE_LINK_REVOKED: "Отозвана ссылка на занятие",
}


class AuditService:
    """Бизнес-логика журнала действий."""

    def __init__(self, repo: Repository) -> None:
        """Инициализирует сервис.

        Args:
            repo: Фасад репозиториев.
        """
        self._repo = repo

    async def log(
        self,
        tenant_id: int,
        action: str,
        payload: dict[str, Any] | None = None,
        *,
        actor: str = ACTOR_TRAINER,
    ) -> AuditLog:
        """Записывает действие в журнал.

        Args:
            tenant_id: Идентификатор тенанта.
            action: Код действия (см. константы ``ACTION_*``).
            payload: Дополнительные данные действия.
            actor: Автор действия: ``trainer``, ``bot`` или ``system``.

        Returns:
            Созданная запись журнала.
        """
        effective_actor = actor if actor in ACTORS else ACTOR_TRAINER
        entry = await self._repo.audit.create(
            tenant_id=tenant_id,
            action=action,
            payload=payload,
            actor=effective_actor,
        )
        logger.debug("Аудит: %s (%s) тенант=%s", action, effective_actor, tenant_id)
        return entry

    async def log_student_created(
        self, tenant_id: int, entity_id: int, name: str, *, actor: str = ACTOR_TRAINER
    ) -> AuditLog:
        """Записывает создание ученика или группы.

        Args:
            tenant_id: Идентификатор тенанта.
            entity_id: Идентификатор ученика.
            name: Имя ученика или название группы.
            actor: Автор действия.

        Returns:
            Созданная запись журнала.
        """
        return await self.log(
            tenant_id,
            ACTION_STUDENT_CREATED,
            {"entity_id": entity_id, "name": name},
            actor=actor,
        )

    async def log_student_updated(
        self,
        tenant_id: int,
        entity_id: int,
        changes: dict[str, Any],
        *,
        actor: str = ACTOR_TRAINER,
    ) -> AuditLog:
        """Записывает изменение ученика.

        Args:
            tenant_id: Идентификатор тенанта.
            entity_id: Идентификатор ученика.
            changes: Изменённые поля.
            actor: Автор действия.

        Returns:
            Созданная запись журнала.
        """
        return await self.log(
            tenant_id,
            ACTION_STUDENT_UPDATED,
            {"entity_id": entity_id, "changes": changes},
            actor=actor,
        )

    async def log_student_deleted(
        self, tenant_id: int, entity_id: int, name: str, *, actor: str = ACTOR_TRAINER
    ) -> AuditLog:
        """Записывает удаление ученика.

        Args:
            tenant_id: Идентификатор тенанта.
            entity_id: Идентификатор ученика.
            name: Имя ученика на момент удаления.
            actor: Автор действия.

        Returns:
            Созданная запись журнала.
        """
        return await self.log(
            tenant_id,
            ACTION_STUDENT_DELETED,
            {"entity_id": entity_id, "name": name},
            actor=actor,
        )

    async def log_payment_registered(
        self,
        tenant_id: int,
        entity_id: int,
        *,
        amount: float | None = None,
        lessons_added: int | None = None,
        actor: str = ACTOR_TRAINER,
    ) -> AuditLog:
        """Записывает внесение оплаты ученика или группы.

        Args:
            tenant_id: Идентификатор тенанта.
            entity_id: Идентификатор ученика или группы.
            amount: Сумма оплаты.
            lessons_added: Сколько занятий добавлено к балансу.
            actor: Автор действия.

        Returns:
            Созданная запись журнала.
        """
        return await self.log(
            tenant_id,
            ACTION_PAYMENT_REGISTERED,
            {"entity_id": entity_id, "amount": amount, "lessons_added": lessons_added},
            actor=actor,
        )

    async def log_payment_deleted(
        self,
        tenant_id: int,
        entity_id: int,
        *,
        amount: float | None = None,
        lessons_added: int | None = None,
        actor: str = ACTOR_TRAINER,
    ) -> AuditLog:
        """Записывает удаление оплаты ученика или группы.

        Args:
            tenant_id: Идентификатор тенанта.
            entity_id: Идентификатор ученика или группы.
            amount: Сумма удалённого платежа.
            lessons_added: Сколько занятий было зачтено платежом.
            actor: Автор действия.

        Returns:
            Созданная запись журнала.
        """
        return await self.log(
            tenant_id,
            ACTION_PAYMENT_DELETED,
            {"entity_id": entity_id, "amount": amount, "lessons_added": lessons_added},
            actor=actor,
        )

    async def log_settings_updated(
        self, tenant_id: int, changes: dict[str, Any], *, actor: str = ACTOR_TRAINER
    ) -> AuditLog:
        """Записывает изменение настроек тенанта.

        Args:
            tenant_id: Идентификатор тенанта.
            changes: Изменённые поля.
            actor: Автор действия.

        Returns:
            Созданная запись журнала.
        """
        return await self.log(
            tenant_id,
            ACTION_SETTINGS_UPDATED,
            {"changes": changes},
            actor=actor,
        )

    async def log_lesson_created(
        self,
        tenant_id: int,
        lesson_id: int,
        entity_id: int,
        *,
        actor: str = ACTOR_TRAINER,
    ) -> AuditLog:
        """Записывает создание занятия.

        Args:
            tenant_id: Идентификатор тенанта.
            lesson_id: Идентификатор занятия.
            entity_id: Идентификатор ученика.
            actor: Автор действия.

        Returns:
            Созданная запись журнала.
        """
        return await self.log(
            tenant_id,
            ACTION_LESSON_CREATED,
            {"lesson_id": lesson_id, "entity_id": entity_id},
            actor=actor,
        )

    async def log_lesson_updated(
        self,
        tenant_id: int,
        lesson_id: int,
        changes: dict[str, Any] | list[str] | None = None,
        *,
        actor: str = ACTOR_TRAINER,
    ) -> AuditLog:
        """Записывает изменение занятия.

        Args:
            tenant_id: Идентификатор тенанта.
            lesson_id: Идентификатор занятия.
            changes: Изменённые поля (словарь или список имён полей).
            actor: Автор действия.

        Returns:
            Созданная запись журнала.
        """
        return await self.log(
            tenant_id,
            ACTION_LESSON_UPDATED,
            {"lesson_id": lesson_id, "changes": changes},
            actor=actor,
        )

    async def log_lesson_deleted(
        self,
        tenant_id: int,
        lesson_id: int,
        entity_id: int,
        *,
        actor: str = ACTOR_TRAINER,
    ) -> AuditLog:
        """Записывает удаление занятия.

        Args:
            tenant_id: Идентификатор тенанта.
            lesson_id: Идентификатор занятия.
            entity_id: Идентификатор ученика.
            actor: Автор действия.

        Returns:
            Созданная запись журнала.
        """
        return await self.log(
            tenant_id,
            ACTION_LESSON_DELETED,
            {"lesson_id": lesson_id, "entity_id": entity_id},
            actor=actor,
        )

    async def list_entries(self, tenant_id: int, limit: int | None = None) -> list[AuditLog]:
        """Возвращает последние записи журнала.

        Args:
            tenant_id: Идентификатор тенанта.
            limit: Максимальное число записей.

        Returns:
            Список записей, начиная с самых новых.
        """
        return await self._repo.audit.list_by_tenant(tenant_id, limit=limit)

    @staticmethod
    def describe(entry: AuditLog) -> str:
        """Формирует человекочитаемое описание записи журнала.

        Args:
            entry: Запись журнала.

        Returns:
            Описание для интерфейса кабинета.
        """
        label = ACTION_LABELS.get(entry.action, entry.action)
        if entry.created_at is None:
            return label
        return f"{label} — {entry.created_at:%d.%m.%Y %H:%M}"
