"""Проверяет сценарий записи занятия: разбор → предпросмотр → запись.

Показывает, что бот не создаёт сущности, не пишет в БД до подтверждения и
записывает занятие после «Да».

Запуск:
    python -m scripts.check_lesson_flow
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

from sqlalchemy import select

from app.bot.handlers import (
    callback_lesson_cancel,
    callback_lesson_confirm,
    handle_dictation,
)
from app.core.config import settings
from app.core.logging import setup_logging
from app.db.base import dispose_engine, get_session
from app.db.models import Entity, Lesson
from app.db.repository import Repository
from app.services.lesson_service import LessonService, UnknownEntityError

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


class StubMessage:
    """Заглушка сообщения: собирает ответы бота."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.answers: list[dict[str, Any]] = []
        self.chat = type("Chat", (), {"id": 1})()
        self.bot = type("Bot", (), {"send_chat_action": self._noop})()

    async def _noop(self, *args: Any, **kwargs: Any) -> None:
        """Имитация «печатает…»."""

    async def answer(self, text: str, **kwargs: Any) -> None:
        self.answers.append({"text": text, **kwargs})

    @property
    def last(self) -> str:
        return self.answers[-1]["text"]


class StubState:
    """Заглушка FSM-хранилища."""

    def __init__(self) -> None:
        self.data: dict[str, Any] = {}

    async def set_state(self, state: Any) -> None:
        self.current_state = state

    async def update_data(self, **kwargs: Any) -> None:
        self.data.update(kwargs)

    async def get_data(self) -> dict[str, Any]:
        return dict(self.data)

    async def clear(self) -> None:
        self.data.clear()


class StubCallback:
    """Заглушка нажатия кнопки."""

    def __init__(self, data: str) -> None:
        self.data = data
        self.message = StubMessage("")
        self.alert: str | None = None

    async def answer(self, text: str | None = None, **_: Any) -> None:
        self.alert = text


async def _count_lessons(session: Any, tenant_id: int, name: str) -> int:
    """Считает уроки ученика по имени."""
    entity = (
        await session.execute(
            select(Entity).where(Entity.tenant_id == tenant_id, Entity.name == name)
        )
    ).scalar_one_or_none()
    if entity is None:
        return -1
    lessons = (
        (
            await session.execute(
                select(Lesson).where(Lesson.tenant_id == tenant_id, Lesson.entity_id == entity.id)
            )
        )
        .scalars()
        .all()
    )
    return len(lessons)


async def main() -> None:
    """Прогоняет сценарий и печатает результат каждого шага."""
    setup_logging()

    async with get_session() as session:
        repo = Repository(session)
        tenant = await repo.tenants.get_by_telegram_id(settings.owner_telegram_id)
        if tenant is None:
            print("Тенант не найден.")
            await dispose_engine()
            return

        entity = (
            await session.execute(
                select(Entity)
                .where(Entity.tenant_id == tenant.id, Entity.entity_type == "student")
                .order_by(Entity.id)
                .limit(1)
            )
        ).scalar_one_or_none()
        if entity is None:
            print("В базе нет учеников.")
            await dispose_engine()
            return

        context: dict[str, Any] = {"repo": repo, "tenant": tenant}
        name = entity.name
        before = await _count_lessons(session, tenant.id, name)
        print(f"Ученик: {name}; уроков до проверки: {before}")

        text = f"{name}, тема — проверка бота, оценка 4, ДЗ — тест, были все"
        print(f"\n=== 1. Диктовка: {text!r} ===")
        state = StubState()
        message = StubMessage(text)
        await handle_dictation(message, state=state, **context)
        print(message.last)
        after_preview = await _count_lessons(session, tenant.id, name)
        print(f"\nУроков после разбора (до подтверждения): {after_preview}")

        print("\n=== 2. Нажатие «Нет» ===")
        cancel = StubCallback("lesson_cancel")
        await callback_lesson_cancel(cancel, state=state, **context)
        print(cancel.message.last)
        after_cancel = await _count_lessons(session, tenant.id, name)
        print(f"Уроков после отмены: {after_cancel}")

        print("\n=== 3. Диктовка заново и нажатие «Да» ===")
        state = StubState()
        message = StubMessage(text)
        await handle_dictation(message, state=state, **context)
        print("Предпросмотр показан, кнопки:", "да/нет")

        confirm = StubCallback("lesson_save")
        await callback_lesson_confirm(confirm, state=state, **context)
        print(confirm.message.last[:400])
        after_confirm = await _count_lessons(session, tenant.id, name)
        print(f"\nУроков после подтверждения: {after_confirm}")

        print("\n=== 4. Незнакомое имя ===")
        state = StubState()
        message = StubMessage("Незнакомый Ученик, тема — проверка")
        await handle_dictation(message, state=state, **context)
        print(message.last)

        print("\n=== 5. Проверка запрета создания сущностей ===")
        try:
            await LessonService(repo).prepare(tenant.id, "Совсем Новый, тема — тест")
            print("!!! ОШИБКА: сущность не должна создаваться")
        except UnknownEntityError as exc:
            print(f"OK: UnknownEntityError, ненайденные имена: {exc.names}")

        print("\n=== 6. Группа по короткому названию ===")
        group = (
            await session.execute(
                select(Entity)
                .where(Entity.tenant_id == tenant.id, Entity.entity_type == "group")
                .order_by(Entity.id)
                .limit(1)
            )
        ).scalar_one_or_none()
        if group is None:
            print("В базе нет групп.")
        else:
            # Короткая форма: первое слово названия без уточнения.
            short = group.name.split()[0]
            text = f"группа {short}, тема — проверка группы, оценка 5, ДЗ — тест, были все"
            print(f"Группа в базе: {group.name!r}; в диктовке: {text!r}")
            state = StubState()
            message = StubMessage(text)
            await handle_dictation(message, state=state, **context)
            print(message.last)

            if state.data:
                confirm = StubCallback("lesson_save")
                await callback_lesson_confirm(confirm, state=state, **context)
                print(f"\nЗаписано: {'да' if confirm.message.last else 'нет'}")
                print(confirm.message.last[:300])

    await dispose_engine()


if __name__ == "__main__":
    asyncio.run(main())
