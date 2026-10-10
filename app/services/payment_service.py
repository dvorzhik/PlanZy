"""Сервис платежей: учёт оплат учеников и групп.

Цена занятия и валюта берутся из настроек тенанта в БД (редактируются в
веб-кабинете). Если в БД значение не задано, используется фолбэк на
значение из ``.env`` — так поведение старых установок не меняется.
"""

from __future__ import annotations

from datetime import date, timedelta

from app.core.config import settings
from app.core.logging import get_logger
from app.db.models import Entity, Payment
from app.db.repository import Repository

logger = get_logger(__name__)


class PaymentService:
    """Бизнес-логика учёта платежей."""

    def __init__(self, repo: Repository) -> None:
        """Инициализирует сервис.

        Args:
            repo: Фасад репозиториев.
        """
        self._repo = repo

    @staticmethod
    def lessons_from_amount(
        amount: float | None,
        lesson_price: float | None = None,
    ) -> int | None:
        """Считает, сколько занятий даёт названная сумма.

        Тренер часто диктует только сумму («оплата 120 шекелей», «занятия
        оплачено») и не называет число занятий. Если задана цена занятия,
        количество можно вычислить.

        Args:
            amount: Сумма платежа.
            lesson_price: Цена одного занятия. По умолчанию берётся из ``.env``.

        Returns:
            Количество занятий (целое, не меньше 1) или None, если посчитать
            нельзя: сумма не названа, цена не задана или сумма меньше цены.
        """
        price = settings.lesson_price if lesson_price is None else lesson_price
        if amount is None or price <= 0:
            return None
        if amount < price:
            return None
        return int(amount // price)

    async def resolve_lesson_price(
        self,
        tenant_id: int,
        entity_id: int | None = None,
    ) -> float:
        """Возвращает цену занятия с учётом индивидуальной цены ученика.

        Приоритет источников цены:

        1. Индивидуальная цена сущности (``entities.lesson_price``) —
           у группы и частного ученика цена может отличаться.
        2. Цена тенанта (``tenants.lesson_price``).
        3. Значение из ``.env`` (``settings.lesson_price``).

        Args:
            tenant_id: Идентификатор тенанта.
            entity_id: Идентификатор ученика/группы. Если не задан,
                индивидуальная цена не учитывается.

        Returns:
            Цена одного занятия.
        """
        if entity_id is not None:
            entity = await self._repo.entities.get_by_id(tenant_id, entity_id)
            if entity is not None and entity.lesson_price is not None and entity.lesson_price > 0:
                return float(entity.lesson_price)

        tenant = await self._repo.tenants.get_by_id(tenant_id)
        if tenant is None or tenant.lesson_price is None or tenant.lesson_price <= 0:
            return settings.lesson_price
        return float(tenant.lesson_price)

    async def resolve_currency(self, tenant_id: int) -> str:
        """Возвращает валюту тенанта с фолбэком на ``.env``.

        Args:
            tenant_id: Идентификатор тенанта.

        Returns:
            Код валюты (например, ``ILS``).
        """
        tenant = await self._repo.tenants.get_by_id(tenant_id)
        if tenant is None or not tenant.currency:
            return settings.currency
        return tenant.currency

    async def register_payment(
        self,
        tenant_id: int,
        entity_id: int,
        amount: float | None = None,
        lessons_added: int | None = None,
        payment_date: date | None = None,
        currency: str | None = None,
        *,
        infer_lessons: bool = True,
    ) -> Payment:
        """Регистрирует платёж.

        Когда названа только сумма, а ``infer_lessons`` включён, количество
        занятий вычисляется из цены занятия тенанта (таблица ``tenants``) с
        фолбэком на значение из ``.env`` — и сохраняется в записи платежа
        как исторический факт.

        Args:
            tenant_id: Идентификатор тенанта.
            entity_id: Идентификатор ученика/группы.
            amount: Сумма платежа в валюте тенанта.
            lessons_added: Сколько занятий добавлено на баланс.
            payment_date: Дата платежа. По умолчанию — сегодня.
            currency: Код валюты. По умолчанию берётся из настроек тенанта.
            infer_lessons: Вычислять ли количество занятий из суммы.

        Returns:
            Созданная запись о платеже.

        Raises:
            ValueError: Если не указаны ни сумма, ни количество занятий.
        """
        if amount is None and lessons_added is None:
            raise ValueError("Платёж должен содержать хотя бы сумму или количество занятий.")

        effective_lessons = lessons_added
        if effective_lessons is None and infer_lessons:
            lesson_price = await self.resolve_lesson_price(tenant_id, entity_id)
            effective_lessons = self.lessons_from_amount(amount, lesson_price)
            if effective_lessons is not None:
                logger.info(
                    "Количество занятий вычислено из суммы: %s / %s = %s",
                    amount,
                    lesson_price,
                    effective_lessons,
                )

        effective_currency = currency or await self.resolve_currency(tenant_id)

        payment = await self._repo.payments.create(
            tenant_id=tenant_id,
            entity_id=entity_id,
            payment_date=payment_date or date.today(),
            amount=amount,
            currency=effective_currency,
            lessons_added=effective_lessons,
        )

        if effective_lessons:
            logger.info(
                "Платёж id=%s для entity_id=%s: зачтено %s занятий",
                payment.id,
                entity_id,
                effective_lessons,
            )

        return payment

    async def list_history(
        self,
        tenant_id: int,
        entity_id: int,
        limit: int | None = None,
    ) -> list[Payment]:
        """Возвращает историю платежей сущности (новые сверху)."""
        return await self._repo.payments.list_by_entity(tenant_id, entity_id, limit=limit)

    async def get_last_payment(self, tenant_id: int, entity_id: int) -> Payment | None:
        """Возвращает последний платёж сущности (для квитанции)."""
        return await self._repo.payments.get_last_payment(tenant_id, entity_id)

    async def total_paid(self, tenant_id: int, entity_id: int) -> float:
        """Возвращает суммарно оплаченную сумму по сущности."""
        return await self._repo.payments.total_amount(tenant_id, entity_id)

    async def list_group_payments(
        self,
        tenant_id: int,
        *,
        date_from: date | None = None,
        date_to: date | None = None,
        limit: int | None = None,
    ) -> list[Payment]:
        """Возвращает платежи групп тенанта, новые сверху.

        Args:
            tenant_id: Идентификатор тенанта.
            date_from: Начало периода включительно (необязательно).
            date_to: Конец периода включительно (необязательно).
            limit: Максимальное число записей (необязательно).

        Returns:
            Список платежей по группам.
        """
        return await self._repo.payments.list_group_payments(
            tenant_id, date_from=date_from, date_to=date_to, limit=limit
        )

    async def delete_payment(self, tenant_id: int, payment_id: int) -> Payment | None:
        """Удаляет платёж в рамках тенанта.

        Используется, чтобы отменить ошибочно внесённую оплату. Возвращает
        удалённую запись (для журнала действий) либо ``None``, если платёж
        не найден или принадлежит другому тенанту.

        Args:
            tenant_id: Идентификатор тенанта.
            payment_id: Идентификатор платежа.

        Returns:
            Удалённый платёж или ``None``.
        """
        payment = await self._repo.payments.get_by_id(tenant_id, payment_id)
        if payment is None:
            return None
        removed = await self._repo.payments.delete(tenant_id, payment_id)
        if not removed:
            return None
        logger.info(
            "Удалён платёж id=%s entity_id=%s amount=%s",
            payment_id,
            payment.entity_id,
            payment.amount,
        )
        return payment


    @staticmethod
    def month_bounds(reference: date) -> tuple[date, date]:
        """Возвращает границы календарного месяца, содержащего дату.

        Args:
            reference: Любая дата внутри интересующего месяца.

        Returns:
            Кортеж ``(первое, последнее)`` число месяца включительно.
        """
        month_start = reference.replace(day=1)
        if month_start.month == 12:
            next_month = month_start.replace(year=month_start.year + 1, month=1, day=1)
        else:
            next_month = month_start.replace(month=month_start.month + 1, day=1)
        return month_start, next_month - timedelta(days=1)

    async def monthly_debt(
        self,
        tenant_id: int,
        entity_id: int,
        *,
        today: date | None = None,
    ) -> float:
        """Считает долг сущности за календарный месяц.

        Формула: ``проведённые занятия × цена занятия − платежи месяца``.
        Проведённым считается любое занесённое в журнал занятие месяца;
        поле «Примечание» (``lessons.attendance``) на расчёт не влияет.
        Цена берётся с учётом индивидуальной цены ученика/группы.

        Долг может быть отрицательным — это означает переплату (аванс).

        Args:
            tenant_id: Идентификатор тенанта.
            entity_id: Идентификатор ученика или группы.
            today: Дата, относительно которой определяется месяц. По
                умолчанию — сегодняшняя дата. Любая дата внутри месяца
                даёт один и тот же результат.

        Returns:
            Долг за месяц в валюте тенанта (может быть отрицательным).
        """
        month_start, month_end = self.month_bounds(today or date.today())

        conducted = await self._repo.lessons.count_conducted_in_period(
            tenant_id, entity_id, month_start, month_end
        )
        price = await self.resolve_lesson_price(tenant_id, entity_id)
        paid = await self._repo.payments.total_amount_in_period(
            tenant_id, entity_id, month_start, month_end
        )
        return round(conducted * price - paid, 2)

    async def monthly_summary(
        self,
        tenant_id: int,
        entity_id: int,
        *,
        today: date | None = None,
    ) -> dict[str, float | int]:
        """Возвращает разбор долга за месяц для карточки группы.

        Args:
            tenant_id: Идентификатор тенанта.
            entity_id: Идентификатор ученика или группы.
            today: Дата, относительно которой определяется месяц.

        Returns:
            Словарь с ключами ``conducted`` (проведённых занятий),
            ``price`` (цена занятия), ``charged`` (начислено),
            ``paid`` (оплачено) и ``debt`` (долг).
        """
        month_start, month_end = self.month_bounds(today or date.today())

        conducted = await self._repo.lessons.count_conducted_in_period(
            tenant_id, entity_id, month_start, month_end
        )
        price = await self.resolve_lesson_price(tenant_id, entity_id)
        paid = await self._repo.payments.total_amount_in_period(
            tenant_id, entity_id, month_start, month_end
        )
        charged = round(conducted * price, 2)
        return {
            "conducted": conducted,
            "price": price,
            "charged": charged,
            "paid": round(paid, 2),
            "debt": round(charged - paid, 2),
        }

    async def monthly_overview(
        self,
        tenant_id: int,
        entities: list[Entity],
        *,
        today: date | None = None,
    ) -> dict[int, dict[str, float | int]]:
        """Считает месячные показатели сразу для списка сущностей.

        Используется списком учеников: вместо запроса на каждого ученика
        (N+1) выполняется два агрегатных запроса по всему тенанту.

        Для каждой сущности возвращаются:

        * ``conducted`` — проведённых занятий за месяц;
        * ``price`` — цена занятия (с учётом индивидуальной);
        * ``charged`` — начислено (``conducted × price``);
        * ``paid`` — оплачено за месяц;
        * ``debt`` — долг (``charged − paid``, может быть отрицательным).

        Args:
            tenant_id: Идентификатор тенанта.
            entities: Список учеников и групп тенанта.
            today: Дата, относительно которой определяется месяц. Любая
                дата внутри месяца даёт один и тот же результат.

        Returns:
            Словарь ``entity_id`` → словарь месячных показателей.
        """
        month_start, month_end = self.month_bounds(today or date.today())

        conducted_map = await self._repo.lessons.conducted_counts_in_period(
            tenant_id, month_start, month_end
        )
        paid_map = await self._repo.payments.totals_in_period(tenant_id, month_start, month_end)

        tenant = await self._repo.tenants.get_by_id(tenant_id)
        tenant_price = (
            float(tenant.lesson_price)
            if tenant is not None and tenant.lesson_price and tenant.lesson_price > 0
            else settings.lesson_price
        )

        overview: dict[int, dict[str, float | int]] = {}
        for entity in entities:
            if entity.lesson_price is not None and entity.lesson_price > 0:
                price = float(entity.lesson_price)
            else:
                price = tenant_price
            conducted = conducted_map.get(entity.id, 0)
            paid = paid_map.get(entity.id, 0.0)
            charged = round(conducted * price, 2)
            overview[entity.id] = {
                "conducted": conducted,
                "price": price,
                "charged": charged,
                "paid": round(paid, 2),
                "debt": round(charged - paid, 2),
            }
        return overview

    async def distribute_monthly_group_payment(
        self,
        tenant_id: int,
        *,
        total_amount: float,
        payment_date: date,
        month: date,
        currency: str | None = None,
    ) -> list[Payment]:
        """Распределяет общую сумму оплаты по группам за месяц.

        Тренер вносит одну общую сумму за месяц, а система делит её между
        группами пропорционально числу проведённых занятий каждой группы
        и создаёт отдельный платёж на каждую группу. Так оплата попадает
        в учёт именно той группы, к которой относится.

        Распределение строго аддитивно: сумма долей равна ``total_amount``.
        Остаток от округления (копейки) добавляется к последней группе,
        чтобы не терять деньги при дроблении.

        Группы без проведённых занятий в месяце пропускаются — доля 0 не
        создаёт пустой платёж. Каждой группе к балансу добавляется число
        её проведённых занятий (``lessons_added``): оплата ровно
        соответствует фактически проведённым занятиям.

        Args:
            tenant_id: Идентификатор тенанта.
            total_amount: Общая сумма за месяц (положительная).
            payment_date: Дата платежа. Одна на все созданные платежи.
            month: Любая дата внутри месяца, за который вносится оплата.
            currency: Код валюты. По умолчанию берётся из настроек тенанта.

        Returns:
            Список созданных платежей (по одному на группу).

        Raises:
            ValueError: Если сумма не положительна или в месяце нет групп
                с проведёнными занятиями.
        """
        if total_amount is None or total_amount <= 0:
            raise ValueError("Сумма распределения должна быть положительной.")

        month_start, month_end = self.month_bounds(month)

        conducted_map = await self._repo.lessons.conducted_counts_in_period(
            tenant_id, month_start, month_end
        )
        entities = await self._repo.entities.list_by_tenant(tenant_id)
        group_ids = [entity.id for entity in entities if entity.entity_type == "group"]

        shares = {
            entity_id: conducted_map.get(entity_id, 0)
            for entity_id in group_ids
            if conducted_map.get(entity_id, 0) > 0
        }
        total_lessons = sum(shares.values())
        if total_lessons == 0:
            raise ValueError("В выбранном месяце нет групп с проведёнными занятиями.")

        effective_currency = currency or await self.resolve_currency(tenant_id)
        rounded_total = round(total_amount, 2)

        payments: list[Payment] = []
        distributed = 0.0
        last_index = len(shares) - 1
        for index, (entity_id, lessons) in enumerate(shares.items()):
            if index == last_index:
                # Последней группе отдаём остаток, чтобы сумма долей точно
                # совпала с введённой суммой.
                share = round(rounded_total - distributed, 2)
            else:
                share = round(rounded_total * lessons / total_lessons, 2)
                distributed += share
            payment = await self.register_payment(
                tenant_id,
                entity_id,
                amount=share,
                lessons_added=lessons,
                payment_date=payment_date,
                currency=effective_currency,
                infer_lessons=False,
            )
            payments.append(payment)

        logger.info(
            "Распределено оплат по группам: тенант=%s сумма=%s платежей=%s месяц=%s",
            tenant_id,
            rounded_total,
            len(payments),
            month_start,
        )
        return payments

    @staticmethod
    def _pluralize_lessons(count: int) -> str:
        """Склоняет слово «занятие» по числу.

        Args:
            count: Количество занятий.

        Returns:
            Правильная форма слова: «занятие», «занятия» или «занятий».
        """
        remainder_100 = count % 100
        remainder_10 = count % 10
        if 11 <= remainder_100 <= 19:
            return "занятий"
        if remainder_10 == 1:
            return "занятие"
        if 2 <= remainder_10 <= 4:
            return "занятия"
        return "занятий"
