"""Ручная проверка страниц кабинета: код ответа и заголовки таблиц.

Запускается вручную::

    .venv\\Scripts\\python.exe scripts\\check_admin_pages.py
"""

from __future__ import annotations

import asyncio
import re

from httpx import ASGITransport, AsyncClient

from app.db.base import get_session
from app.db.repository import Repository
from app.services.auth_service import SESSION_COOKIE_NAME, AuthService
from app.web.app import create_app


async def main() -> None:
    """Запрашивает страницы кабинета и печатает статус и заголовки."""
    async with get_session() as session:
        repo = Repository(session)
        tenant = await repo.tenants.get_by_id(1)
        if tenant is None:
            print("Тенант id=1 не найден")
            return
        # Вход в кабинет: сервис создаёт cookie-сессию по admin_token.
        auth_result = await AuthService(repo).login_by_admin_token(tenant.admin_token)
        if auth_result is None:
            print("Не удалось создать сессию кабинета")
            return
        session_token = auth_result.token

    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        client.cookies.set(SESSION_COOKIE_NAME, session_token)
        for url in ("/admin/lessons", "/admin/students", "/admin/reminders"):
            response = await client.get(url)
            print(f"{url}: {response.status_code} ({len(response.text)} байт)")
            if url == "/admin/lessons":
                for cell in re.findall(r"<th[^>]*class=\"([^\"]*)\"[^>]*>([^<]*)</th>", response.text):
                    print(f"   th class={cell[0]!r} text={cell[1]!r}")
            if url == "/admin/reminders":
                if "Рассылка идёт автоматически" in response.text:
                    print("   подсказка о расписании: есть")
                print("   упоминание отправок: " + ("есть" if "Последняя отправка" in response.text or "Отправок пока не было" in response.text else "нет"))


if __name__ == "__main__":
    asyncio.run(main())
