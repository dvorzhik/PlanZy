"""Сервис настроек тенанта: цена, валюта, порядок оплаты, часовой пояс.

Настройки хранятся в таблице ``tenants`` и редактируются из веб-кабинета.
Сервис валидирует значения и подставляет значения из ``.env`` как фолбэк,
чтобы поведение старых установок не менялось.
"""

from __future__ import annotations

from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.core.config import settings
from app.core.logging import get_logger
from app.db.models import Tenant
from app.db.repository import Repository

logger = get_logger(__name__)

# Поля настроек, доступные для редактирования из кабинета.
EDITABLE_FIELDS = (
    "name",
    "timezone",
    "lesson_price",
    "currency",
    "payment_instructions",
)

# Разумные границы, чтобы тренер не сломал расчёты случайным вводом.
MAX_LESSON_PRICE = 100_000.0
MAX_CURRENCY_LENGTH = 8


@dataclass(slots=True)
class TenantSettings:
    """Снимок настроек тенанта для отображения и расчётов.

    Attributes:
        tenant_id: Идентификатор тенанта.
        name: Имя тренера или название школы.
        timezone: Часовой пояс (например, ``Asia/Jerusalem``).
        lesson_price: Цена одного занятия.
        currency: Код валюты (например, ``ILS``).
        payment_instructions: Порядок оплаты для родителей.
    """

    tenant_id: int
    name: str
    timezone: str
    lesson_price: float
    currency: str
    payment_instructions: str | None

    @property
    def zone(self) -> ZoneInfo:
        """Возвращает :class:`ZoneInfo` с фолбэком на пояс из ``.env``."""
        try:
            return ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            logger.warning(
                "Неизвестный часовой пояс %r у тенанта %s, использую %s",
                self.timezone,
                self.tenant_id,
                settings.timezone,
            )
            return ZoneInfo(settings.timezone)


class TenantSettingsService:
    """Бизнес-логика чтения и записи настроек тенанта."""

    def __init__(self, repo: Repository) -> None:
        """Инициализирует сервис.

        Args:
            repo: Фасад репозиториев.
        """
        self._repo = repo

    async def get(self, tenant_id: int) -> TenantSettings | None:
        """Возвращает настройки тенанта.

        Args:
            tenant_id: Идентификатор тенанта.

        Returns:
            Снимок настроек или ``None``, если тенант не найден.
        """
        tenant = await self._repo.tenants.get_by_id(tenant_id)
        if tenant is None:
            return None
        return self._to_snapshot(tenant)

    async def get_lesson_price(self, tenant_id: int) -> float:
        """Возвращает цену занятия с фолбэком на значение из ``.env``.

        Args:
            tenant_id: Идентификатор тенанта.

        Returns:
            Цена занятия.
        """
        tenant = await self._repo.tenants.get_by_id(tenant_id)
        if tenant is None or tenant.lesson_price is None or tenant.lesson_price <= 0:
            return settings.lesson_price
        return float(tenant.lesson_price)

    async def get_currency(self, tenant_id: int) -> str:
        """Возвращает валюту тенанта с фолбэком на значение из ``.env``.

        Args:
            tenant_id: Идентификатор тенанта.

        Returns:
            Код валюты.
        """
        tenant = await self._repo.tenants.get_by_id(tenant_id)
        if tenant is None or not tenant.currency:
            return settings.currency
        return tenant.currency

    async def update(self, tenant_id: int, **fields: object) -> TenantSettings | None:
        """Обновляет настройки тенанта с валидацией значений.

        Args:
            tenant_id: Идентификатор тенанта.
            **fields: Изменяемые поля из :data:`EDITABLE_FIELDS`.

        Returns:
            Обновлённые настройки или ``None``, если тенант не найден.

        Raises:
            ValueError: Если значение поля некорректно.
        """
        payload = self._validate(fields)
        if not payload:
            return await self.get(tenant_id)

        tenant = await self._repo.tenants.update_settings(tenant_id, **payload)
        if tenant is None:
            return None
        logger.info("Настройки тенанта %s обновлены: %s", tenant_id, sorted(payload))
        return self._to_snapshot(tenant)

    async def ensure_admin_token(self, tenant_id: int) -> str | None:
        """Гарантирует наличие секретного токена кабинета.

        Args:
            tenant_id: Идентификатор тенанта.

        Returns:
            Токен или ``None``, если тенант не найден.
        """
        return await self._repo.tenants.ensure_admin_token(tenant_id)

    @staticmethod
    def _validate(fields: dict[str, object]) -> dict[str, object]:
        """Проверяет и нормализует значения настроек.

        Args:
            fields: Сырые значения из формы кабинета.

        Returns:
            Словарь только с допустимыми полями и приведёнными типами.

        Raises:
            ValueError: Если значение поля некорректно.
        """
        payload: dict[str, object] = {}

        if "name" in fields and fields["name"] is not None:
            name = str(fields["name"]).strip()
            if not name:
                raise ValueError("Имя тренера не может быть пустым.")
            payload["name"] = name

        if "timezone" in fields and fields["timezone"] is not None:
            timezone = str(fields["timezone"]).strip()
            try:
                ZoneInfo(timezone)
            except (ZoneInfoNotFoundError, ValueError) as exc:
                raise ValueError(f"Неизвестный часовой пояс: {timezone!r}.") from exc
            payload["timezone"] = timezone

        if "lesson_price" in fields and fields["lesson_price"] is not None:
            price = float(fields["lesson_price"])  # type: ignore[arg-type]
            if price <= 0:
                raise ValueError("Цена занятия должна быть больше нуля.")
            if price > MAX_LESSON_PRICE:
                raise ValueError(f"Цена занятия слишком велика (максимум {MAX_LESSON_PRICE:g}).")
            payload["lesson_price"] = price

        if "currency" in fields and fields["currency"] is not None:
            currency = str(fields["currency"]).strip().upper()
            if not currency:
                raise ValueError("Валюта не может быть пустой.")
            if len(currency) > MAX_CURRENCY_LENGTH:
                raise ValueError("Код валюты слишком длинный.")
            payload["currency"] = currency

        if "payment_instructions" in fields:
            raw = fields["payment_instructions"]
            if raw is None:
                payload["payment_instructions"] = None
            else:
                text = str(raw).strip()
                payload["payment_instructions"] = text or None

        return payload

    @staticmethod
    def _to_snapshot(tenant: Tenant) -> TenantSettings:
        """Преобразует ORM-модель тенанта в снимок настроек.

        Args:
            tenant: ORM-модель тенанта.

        Returns:
            Снимок настроек с подставленными фолбэками.
        """
        return TenantSettings(
            tenant_id=tenant.id,
            name=tenant.name,
            timezone=tenant.timezone or settings.timezone,
            lesson_price=(
                float(tenant.lesson_price)
                if tenant.lesson_price is not None and tenant.lesson_price > 0
                else settings.lesson_price
            ),
            currency=tenant.currency or settings.currency,
            payment_instructions=tenant.payment_instructions,
        )
