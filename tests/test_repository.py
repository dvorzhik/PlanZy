"""Тесты Repository-слоя на изолированной in-memory SQLite.

Проверяем:
    * CRUD по всем четырём репозиториям (tenants, entities, lessons, payments);
    * изоляцию данных между тенантами (мультитенантность);
    * регистронезависимый поиск по имени с кириллицей;
    * каскадное удаление;
    * сортировку и лимиты выборок.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from app.db.repository import Repository

MONDAY = date(2026, 9, 14)
TUESDAY = date(2026, 9, 15)
WEDNESDAY = date(2026, 9, 16)
NEXT_MONDAY = date(2026, 9, 21)


async def _seed_entity(
    repo: Repository,
    tenant_id: int,
    *,
    name: str = "Миша",
    entity_type: str = "student",
    schedule_days: str | None = None,
    schedule_time: str | None = None,
    parent_phone: str | None = None,
) -> int:
    """Создаёт сущность и возвращает её id."""
    entity = await repo.entities.create(
        tenant_id,
        name=name,
        entity_type=entity_type,
        schedule_days=schedule_days,
        schedule_time=schedule_time,
        parent_phone=parent_phone,
    )
    await repo.session.flush()
    return entity.id


class TestTenantRepository:
    """Проверки операций с тенантами."""

    async def test_create_assigns_id_and_fields(self, repo: Repository) -> None:
        """Создание тенанта заполняет id и переданные поля."""
        tenant = await repo.tenants.create(name="Иван", telegram_user_id=555)

        assert tenant.id is not None
        assert tenant.name == "Иван"
        assert tenant.telegram_user_id == 555
        assert tenant.is_active is True

    async def test_get_by_id_returns_tenant(self, repo: Repository, tenant_id: int) -> None:
        """Поиск тенанта по id возвращает созданную запись."""
        tenant = await repo.tenants.get_by_id(tenant_id)

        assert tenant is not None
        assert tenant.id == tenant_id

    async def test_get_by_id_returns_none_for_unknown(self, repo: Repository) -> None:
        """Несуществующий id возвращает None."""
        assert await repo.tenants.get_by_id(999_999) is None

    async def test_get_by_telegram_id(self, repo: Repository, tenant_id: int) -> None:
        """Поиск по telegram_user_id находит тенанта (whitelist бота)."""
        tenant = await repo.tenants.get_by_telegram_id(1001)

        assert tenant is not None
        assert tenant.id == tenant_id

    async def test_get_by_telegram_id_unknown(self, repo: Repository) -> None:
        """Неизвестный telegram_user_id возвращает None."""
        assert await repo.tenants.get_by_telegram_id(424242) is None

    async def test_list_active_excludes_inactive(self, repo: Repository) -> None:
        """Список активных тенантов не содержит отключённых."""
        await repo.tenants.create(name="Активный", telegram_user_id=1)
        await repo.tenants.create(name="Отключённый", telegram_user_id=2, is_active=False)
        await repo.session.flush()

        active = await repo.tenants.list_active()

        assert [t.name for t in active] == ["Активный"]

    async def test_update_changes_fields(self, repo: Repository, tenant_id: int) -> None:
        """Обновление меняет переданные поля."""
        updated = await repo.tenants.update(tenant_id, name="Новое имя", is_active=False)

        assert updated is not None
        assert updated.name == "Новое имя"
        assert updated.is_active is False

    async def test_update_without_fields_returns_tenant(
        self, repo: Repository, tenant_id: int
    ) -> None:
        """Пустой набор полей возвращает тенанта без изменений."""
        tenant = await repo.tenants.update(tenant_id)

        assert tenant is not None
        assert tenant.id == tenant_id

    async def test_delete_removes_tenant(self, repo: Repository, tenant_id: int) -> None:
        """Удаление тенанта возвращает True и убирает запись."""
        assert await repo.tenants.delete(tenant_id) is True
        assert await repo.tenants.get_by_id(tenant_id) is None

    async def test_delete_unknown_returns_false(self, repo: Repository) -> None:
        """Удаление несуществующего тенанта возвращает False."""
        assert await repo.tenants.delete(999_999) is False


class TestEntityRepository:
    """Проверки операций с учениками и группами."""

    async def test_create_generates_public_token(self, repo: Repository, tenant_id: int) -> None:
        """При создании автоматически генерируется публичный токен."""
        entity = await repo.entities.create(tenant_id, name="Миша")

        assert entity.id is not None
        assert entity.public_token
        assert len(entity.public_token) >= 8

    async def test_create_respects_explicit_token(self, repo: Repository, tenant_id: int) -> None:
        """Явно переданный токен сохраняется как есть."""
        entity = await repo.entities.create(tenant_id, name="Миша", public_token="fixed-token-123")

        assert entity.public_token == "fixed-token-123"

    async def test_get_by_id_scoped_to_tenant(self, repo: Repository, tenant_id: int) -> None:
        """Сущность не видна чужому тенанту."""
        entity_id = await _seed_entity(repo, tenant_id)
        other = await repo.tenants.create(name="Другой", telegram_user_id=2002)
        await repo.session.flush()

        assert await repo.entities.get_by_id(tenant_id, entity_id) is not None
        assert await repo.entities.get_by_id(other.id, entity_id) is None

    async def test_get_by_token_is_global(self, repo: Repository, tenant_id: int) -> None:
        """Поиск по публичному токену не требует tenant_id."""
        entity = await repo.entities.create(tenant_id, name="Миша")
        await repo.session.flush()

        found = await repo.entities.get_by_token(entity.public_token)

        assert found is not None
        assert found.id == entity.id

    async def test_get_by_token_unknown(self, repo: Repository) -> None:
        """Неизвестный токен возвращает None."""
        assert await repo.entities.get_by_token("no-such-token") is None

    @pytest.mark.parametrize("query", ["миша", "МИША", "  Миша  ", "МиШа"])
    async def test_get_by_name_is_case_insensitive_cyrillic(
        self, repo: Repository, tenant_id: int, query: str
    ) -> None:
        """Поиск по имени игнорирует регистр кириллицы и пробелы."""
        await _seed_entity(repo, tenant_id, name="Миша")

        found = await repo.entities.get_by_name(tenant_id, query)

        assert found is not None
        assert found.name == "Миша"

    async def test_get_by_name_scoped_to_tenant(self, repo: Repository, tenant_id: int) -> None:
        """Одноимённая сущность чужого тенанта не находится."""
        other = await repo.tenants.create(name="Другой", telegram_user_id=2002)
        await repo.session.flush()
        await _seed_entity(repo, other.id, name="Миша")

        assert await repo.entities.get_by_name(tenant_id, "Миша") is None

    async def test_get_by_name_unknown(self, repo: Repository, tenant_id: int) -> None:
        """Неизвестное имя возвращает None."""
        assert await repo.entities.get_by_name(tenant_id, "Никто") is None

    async def test_list_by_tenant_sorted_by_name(self, repo: Repository, tenant_id: int) -> None:
        """Список сущностей отсортирован по имени."""
        await _seed_entity(repo, tenant_id, name="Яна")
        await _seed_entity(repo, tenant_id, name="Аня")
        await _seed_entity(repo, tenant_id, name="Боря")

        entities = await repo.entities.list_by_tenant(tenant_id)

        assert [e.name for e in entities] == ["Аня", "Боря", "Яна"]

    async def test_list_by_tenant_filters_type(self, repo: Repository, tenant_id: int) -> None:
        """Фильтр по типу возвращает только нужные сущности."""
        await _seed_entity(repo, tenant_id, name="Миша", entity_type="student")
        await _seed_entity(repo, tenant_id, name="Группа А", entity_type="group")

        groups = await repo.entities.list_by_tenant(tenant_id, entity_type="group")

        assert [e.name for e in groups] == ["Группа А"]

    async def test_list_for_weekday_matches_schedule(
        self, repo: Repository, tenant_id: int
    ) -> None:
        """Выборка по дню недели находит сущности с этим днём в расписании."""
        await _seed_entity(repo, tenant_id, name="Понедельник", schedule_days="mon,wed")
        await _seed_entity(repo, tenant_id, name="Вторник", schedule_days="tue")

        monday = await repo.entities.list_for_weekday(tenant_id, "mon")

        assert [e.name for e in monday] == ["Понедельник"]

    async def test_list_for_weekday_normalizes_code(self, repo: Repository, tenant_id: int) -> None:
        """Код дня недели приводится к нижнему регистру и обрезается."""
        await _seed_entity(repo, tenant_id, name="Миша", schedule_days="mon")

        found = await repo.entities.list_for_weekday(tenant_id, "  MON ")

        assert len(found) == 1

    async def test_list_for_weekday_empty_when_no_match(
        self, repo: Repository, tenant_id: int
    ) -> None:
        """Если занятий в этот день нет — возвращается пустой список."""
        await _seed_entity(repo, tenant_id, name="Миша", schedule_days="tue")

        assert await repo.entities.list_for_weekday(tenant_id, "sun") == []

    async def test_update_changes_fields(self, repo: Repository, tenant_id: int) -> None:
        """Обновление меняет переданные поля сущности."""
        entity_id = await _seed_entity(repo, tenant_id)

        updated = await repo.entities.update(tenant_id, entity_id, parent_phone="972501234567")

        assert updated is not None
        assert updated.parent_phone == "972501234567"

    async def test_update_scoped_to_tenant(self, repo: Repository, tenant_id: int) -> None:
        """Чужой тенант не может обновить сущность."""
        entity_id = await _seed_entity(repo, tenant_id)
        other = await repo.tenants.create(name="Другой", telegram_user_id=2002)
        await repo.session.flush()

        assert await repo.entities.update(other.id, entity_id, name="Взлом") is None

    async def test_delete_removes_entity(self, repo: Repository, tenant_id: int) -> None:
        """Удаление сущности возвращает True и убирает запись."""
        entity_id = await _seed_entity(repo, tenant_id)

        assert await repo.entities.delete(tenant_id, entity_id) is True
        assert await repo.entities.get_by_id(tenant_id, entity_id) is None

    async def test_delete_scoped_to_tenant(self, repo: Repository, tenant_id: int) -> None:
        """Чужой тенант не может удалить сущность."""
        entity_id = await _seed_entity(repo, tenant_id)
        other = await repo.tenants.create(name="Другой", telegram_user_id=2002)
        await repo.session.flush()

        assert await repo.entities.delete(other.id, entity_id) is False
        assert await repo.entities.get_by_id(tenant_id, entity_id) is not None


class TestLessonRepository:
    """Проверки операций с уроками."""

    async def test_create_fills_fields(self, repo: Repository, tenant_id: int) -> None:
        """Создание урока сохраняет все переданные поля."""
        entity_id = await _seed_entity(repo, tenant_id)

        lesson = await repo.lessons.create(
            tenant_id,
            entity_id,
            MONDAY,
            topic="Мат в один ход",
            homework="Задачи 1-10",
            rating="4",
            attendance="present",
        )

        assert lesson.id is not None
        assert lesson.topic == "Мат в один ход"
        assert lesson.homework == "Задачи 1-10"
        assert lesson.rating == "4"
        assert lesson.attendance == "present"

    async def test_get_by_id_scoped_to_tenant(self, repo: Repository, tenant_id: int) -> None:
        """Урок не виден чужому тенанту."""
        entity_id = await _seed_entity(repo, tenant_id)
        lesson = await repo.lessons.create(tenant_id, entity_id, MONDAY, topic="Тема")
        other = await repo.tenants.create(name="Другой", telegram_user_id=2002)
        await repo.session.flush()

        assert await repo.lessons.get_by_id(tenant_id, lesson.id) is not None
        assert await repo.lessons.get_by_id(other.id, lesson.id) is None

    async def test_list_by_entity_newest_first(self, repo: Repository, tenant_id: int) -> None:
        """История уроков отсортирована от новых к старым."""
        entity_id = await _seed_entity(repo, tenant_id)
        await repo.lessons.create(tenant_id, entity_id, MONDAY, topic="Старый")
        await repo.lessons.create(tenant_id, entity_id, TUESDAY, topic="Новый")

        lessons = await repo.lessons.list_by_entity(tenant_id, entity_id)

        assert [lesson.topic for lesson in lessons] == ["Новый", "Старый"]

    async def test_list_by_entity_respects_limit(self, repo: Repository, tenant_id: int) -> None:
        """Лимит ограничивает количество возвращаемых уроков."""
        entity_id = await _seed_entity(repo, tenant_id)
        await repo.lessons.create(tenant_id, entity_id, MONDAY, topic="Первый")
        await repo.lessons.create(tenant_id, entity_id, TUESDAY, topic="Второй")

        lessons = await repo.lessons.list_by_entity(tenant_id, entity_id, limit=1)

        assert len(lessons) == 1
        assert lessons[0].topic == "Второй"

    async def test_get_last_lesson(self, repo: Repository, tenant_id: int) -> None:
        """Последний урок — самый свежий по дате."""
        entity_id = await _seed_entity(repo, tenant_id)
        await repo.lessons.create(tenant_id, entity_id, MONDAY, topic="Старый")
        await repo.lessons.create(tenant_id, entity_id, TUESDAY, topic="Новый")

        last = await repo.lessons.get_last_lesson(tenant_id, entity_id)

        assert last is not None
        assert last.topic == "Новый"

    async def test_get_last_lesson_empty(self, repo: Repository, tenant_id: int) -> None:
        """Без уроков последний урок — None."""
        entity_id = await _seed_entity(repo, tenant_id)

        assert await repo.lessons.get_last_lesson(tenant_id, entity_id) is None

    async def test_list_by_date(self, repo: Repository, tenant_id: int) -> None:
        """Выборка по дате возвращает только уроки этой даты."""
        first = await _seed_entity(repo, tenant_id, name="Миша")
        second = await _seed_entity(repo, tenant_id, name="Петя")
        await repo.lessons.create(tenant_id, first, MONDAY, topic="Понедельник")
        await repo.lessons.create(tenant_id, second, TUESDAY, topic="Вторник")

        lessons = await repo.lessons.list_by_date(tenant_id, MONDAY)

        assert [lesson.topic for lesson in lessons] == ["Понедельник"]

    async def test_count_by_entity(self, repo: Repository, tenant_id: int) -> None:
        """Счётчик возвращает число уроков сущности."""
        entity_id = await _seed_entity(repo, tenant_id)
        await repo.lessons.create(tenant_id, entity_id, MONDAY, topic="Первый")
        await repo.lessons.create(tenant_id, entity_id, TUESDAY, topic="Второй")

        assert await repo.lessons.count_by_entity(tenant_id, entity_id) == 2

    async def test_count_by_entity_zero(self, repo: Repository, tenant_id: int) -> None:
        """Без уроков счётчик равен нулю."""
        entity_id = await _seed_entity(repo, tenant_id)

        assert await repo.lessons.count_by_entity(tenant_id, entity_id) == 0

    async def test_update_changes_fields(self, repo: Repository, tenant_id: int) -> None:
        """Обновление меняет переданные поля урока."""
        entity_id = await _seed_entity(repo, tenant_id)
        lesson = await repo.lessons.create(tenant_id, entity_id, MONDAY, topic="Тема")

        updated = await repo.lessons.update(tenant_id, lesson.id, topic="Новая тема")

        assert updated is not None
        assert updated.topic == "Новая тема"

    async def test_update_scoped_to_tenant(self, repo: Repository, tenant_id: int) -> None:
        """Чужой тенант не может обновить урок."""
        entity_id = await _seed_entity(repo, tenant_id)
        lesson = await repo.lessons.create(tenant_id, entity_id, MONDAY, topic="Тема")
        other = await repo.tenants.create(name="Другой", telegram_user_id=2002)
        await repo.session.flush()

        assert await repo.lessons.update(other.id, lesson.id, topic="Взлом") is None

    async def test_delete_removes_lesson(self, repo: Repository, tenant_id: int) -> None:
        """Удаление урока возвращает True и убирает запись."""
        entity_id = await _seed_entity(repo, tenant_id)
        lesson = await repo.lessons.create(tenant_id, entity_id, MONDAY, topic="Тема")

        assert await repo.lessons.delete(tenant_id, lesson.id) is True
        assert await repo.lessons.get_by_id(tenant_id, lesson.id) is None

    async def test_delete_scoped_to_tenant(self, repo: Repository, tenant_id: int) -> None:
        """Чужой тенант не может удалить урок."""
        entity_id = await _seed_entity(repo, tenant_id)
        lesson = await repo.lessons.create(tenant_id, entity_id, MONDAY, topic="Тема")
        other = await repo.tenants.create(name="Другой", telegram_user_id=2002)
        await repo.session.flush()

        assert await repo.lessons.delete(other.id, lesson.id) is False

    async def test_conducted_counts_in_period_groups_by_entity(
        self, repo: Repository, tenant_id: int
    ) -> None:
        """Пакетный подсчёт возвращает карту entity_id → число занятий."""
        first = await _seed_entity(repo, tenant_id, name="Миша")
        second = await _seed_entity(repo, tenant_id, name="Петя")
        await repo.lessons.create(tenant_id, first, MONDAY, topic="Раз", attendance="был")
        await repo.lessons.create(tenant_id, first, TUESDAY, topic="Два", attendance="был")
        await repo.lessons.create(tenant_id, second, MONDAY, topic="Три", attendance="был")

        counts = await repo.lessons.conducted_counts_in_period(tenant_id, MONDAY, TUESDAY)

        assert counts == {first: 2, second: 1}

    async def test_conducted_counts_in_period_ignores_unconducted(
        self, repo: Repository, tenant_id: int
    ) -> None:
        """Занятия без «Примечания» не считаются проведёнными."""
        entity_id = await _seed_entity(repo, tenant_id)
        await repo.lessons.create(tenant_id, entity_id, MONDAY, topic="Пустое")
        await repo.lessons.create(
            tenant_id, entity_id, TUESDAY, topic="Проведено", attendance="был"
        )

        counts = await repo.lessons.conducted_counts_in_period(tenant_id, MONDAY, TUESDAY)

        assert counts == {entity_id: 1}

    async def test_conducted_counts_in_period_empty(self, repo: Repository, tenant_id: int) -> None:
        """Без проведённых занятий карта пуста."""
        await _seed_entity(repo, tenant_id)

        counts = await repo.lessons.conducted_counts_in_period(tenant_id, MONDAY, TUESDAY)

        assert counts == {}


class TestPaymentRepository:
    """Проверки операций с платежами."""

    async def test_create_fills_fields(self, repo: Repository, tenant_id: int) -> None:
        """Создание платежа сохраняет сумму и количество занятий."""
        entity_id = await _seed_entity(repo, tenant_id)

        payment = await repo.payments.create(
            tenant_id, entity_id, MONDAY, amount=400.0, lessons_added=8
        )

        assert payment.id is not None
        assert payment.amount == 400.0
        assert payment.lessons_added == 8
        assert payment.currency == "ILS"

    async def test_create_allows_null_amount(self, repo: Repository, tenant_id: int) -> None:
        """Платёж может быть без суммы — только с числом занятий."""
        entity_id = await _seed_entity(repo, tenant_id)

        payment = await repo.payments.create(tenant_id, entity_id, MONDAY, lessons_added=4)

        assert payment.amount is None
        assert payment.lessons_added == 4

    async def test_get_by_id_scoped_to_tenant(self, repo: Repository, tenant_id: int) -> None:
        """Платёж не виден чужому тенанту."""
        entity_id = await _seed_entity(repo, tenant_id)
        payment = await repo.payments.create(tenant_id, entity_id, MONDAY, amount=100.0)
        other = await repo.tenants.create(name="Другой", telegram_user_id=2002)
        await repo.session.flush()

        assert await repo.payments.get_by_id(tenant_id, payment.id) is not None
        assert await repo.payments.get_by_id(other.id, payment.id) is None

    async def test_list_by_entity_newest_first(self, repo: Repository, tenant_id: int) -> None:
        """История платежей отсортирована от новых к старым."""
        entity_id = await _seed_entity(repo, tenant_id)
        await repo.payments.create(tenant_id, entity_id, MONDAY, amount=100.0)
        await repo.payments.create(tenant_id, entity_id, TUESDAY, amount=200.0)

        payments = await repo.payments.list_by_entity(tenant_id, entity_id)

        assert [payment.amount for payment in payments] == [200.0, 100.0]

    async def test_list_by_entity_respects_limit(self, repo: Repository, tenant_id: int) -> None:
        """Лимит ограничивает количество возвращаемых платежей."""
        entity_id = await _seed_entity(repo, tenant_id)
        await repo.payments.create(tenant_id, entity_id, MONDAY, amount=100.0)
        await repo.payments.create(tenant_id, entity_id, TUESDAY, amount=200.0)

        payments = await repo.payments.list_by_entity(tenant_id, entity_id, limit=1)

        assert len(payments) == 1
        assert payments[0].amount == 200.0

    async def test_get_last_payment(self, repo: Repository, tenant_id: int) -> None:
        """Последний платёж — самый свежий по дате."""
        entity_id = await _seed_entity(repo, tenant_id)
        await repo.payments.create(tenant_id, entity_id, MONDAY, amount=100.0)
        await repo.payments.create(tenant_id, entity_id, TUESDAY, amount=200.0)

        last = await repo.payments.get_last_payment(tenant_id, entity_id)

        assert last is not None
        assert last.amount == 200.0

    async def test_get_last_payment_empty(self, repo: Repository, tenant_id: int) -> None:
        """Без платежей последний платёж — None."""
        entity_id = await _seed_entity(repo, tenant_id)

        assert await repo.payments.get_last_payment(tenant_id, entity_id) is None

    async def test_total_amount_sums_payments(self, repo: Repository, tenant_id: int) -> None:
        """Суммарная оплата складывает все платежи сущности."""
        entity_id = await _seed_entity(repo, tenant_id)
        await repo.payments.create(tenant_id, entity_id, MONDAY, amount=100.0)
        await repo.payments.create(tenant_id, entity_id, TUESDAY, amount=250.5)

        assert await repo.payments.total_amount(tenant_id, entity_id) == 350.5

    async def test_total_amount_zero_without_payments(
        self, repo: Repository, tenant_id: int
    ) -> None:
        """Без платежей суммарная оплата равна нулю."""
        entity_id = await _seed_entity(repo, tenant_id)

        assert await repo.payments.total_amount(tenant_id, entity_id) == 0.0

    async def test_total_amount_ignores_null_amounts(
        self, repo: Repository, tenant_id: int
    ) -> None:
        """Платежи без суммы не влияют на итог."""
        entity_id = await _seed_entity(repo, tenant_id)
        await repo.payments.create(tenant_id, entity_id, MONDAY, amount=100.0)
        await repo.payments.create(tenant_id, entity_id, TUESDAY, lessons_added=4)

        assert await repo.payments.total_amount(tenant_id, entity_id) == 100.0

    async def test_totals_in_period_groups_by_entity(
        self, repo: Repository, tenant_id: int
    ) -> None:
        """Пакетная сумма платежей возвращает карту entity_id → сумма."""
        first = await _seed_entity(repo, tenant_id, name="Миша")
        second = await _seed_entity(repo, tenant_id, name="Петя")
        await repo.payments.create(tenant_id, first, MONDAY, amount=100.0)
        await repo.payments.create(tenant_id, first, TUESDAY, amount=50.0)
        await repo.payments.create(tenant_id, second, MONDAY, amount=200.0)

        totals = await repo.payments.totals_in_period(tenant_id, MONDAY, TUESDAY)

        assert totals == {first: 150.0, second: 200.0}

    async def test_totals_in_period_ignores_other_periods(
        self, repo: Repository, tenant_id: int
    ) -> None:
        """Платежи вне периода не попадают в итог."""
        entity_id = await _seed_entity(repo, tenant_id)
        await repo.payments.create(tenant_id, entity_id, MONDAY, amount=100.0)
        await repo.payments.create(tenant_id, entity_id, WEDNESDAY, amount=500.0)

        totals = await repo.payments.totals_in_period(tenant_id, MONDAY, TUESDAY)

        assert totals == {entity_id: 100.0}

    async def test_totals_in_period_empty(self, repo: Repository, tenant_id: int) -> None:
        """Без платежей карта пуста."""
        await _seed_entity(repo, tenant_id)

        totals = await repo.payments.totals_in_period(tenant_id, MONDAY, TUESDAY)

        assert totals == {}

    async def test_delete_removes_payment(self, repo: Repository, tenant_id: int) -> None:
        """Удаление платежа возвращает True и убирает запись."""
        entity_id = await _seed_entity(repo, tenant_id)
        payment = await repo.payments.create(tenant_id, entity_id, MONDAY, amount=100.0)

        assert await repo.payments.delete(tenant_id, payment.id) is True
        assert await repo.payments.get_by_id(tenant_id, payment.id) is None

    async def test_delete_scoped_to_tenant(self, repo: Repository, tenant_id: int) -> None:
        """Чужой тенант не может удалить платёж."""
        entity_id = await _seed_entity(repo, tenant_id)
        payment = await repo.payments.create(tenant_id, entity_id, MONDAY, amount=100.0)
        other = await repo.tenants.create(name="Другой", telegram_user_id=2002)
        await repo.session.flush()

        assert await repo.payments.delete(other.id, payment.id) is False


class TestCascadeDelete:
    """Проверки каскадного удаления связанных данных."""

    async def test_delete_entity_removes_lessons_and_payments(
        self, repo: Repository, tenant_id: int
    ) -> None:
        """Удаление сущности убирает её уроки и платежи."""
        entity_id = await _seed_entity(repo, tenant_id)
        await repo.lessons.create(tenant_id, entity_id, MONDAY, topic="Тема")
        await repo.payments.create(tenant_id, entity_id, MONDAY, amount=100.0)

        assert await repo.entities.delete(tenant_id, entity_id) is True

        assert await repo.lessons.list_by_entity(tenant_id, entity_id) == []
        assert await repo.payments.list_by_entity(tenant_id, entity_id) == []

    async def test_delete_tenant_removes_all_data(self, repo: Repository) -> None:
        """Удаление тенанта каскадно убирает сущности, уроки и платежи."""
        tenant = await repo.tenants.create(name="Тренер", telegram_user_id=3003)
        await repo.session.flush()
        entity_id = await _seed_entity(repo, tenant.id)
        await repo.lessons.create(tenant.id, entity_id, MONDAY, topic="Тема")
        await repo.payments.create(tenant.id, entity_id, MONDAY, amount=100.0)

        assert await repo.tenants.delete(tenant.id) is True

        assert await repo.entities.list_by_tenant(tenant.id) == []
        assert await repo.lessons.list_by_entity(tenant.id, entity_id) == []
        assert await repo.payments.list_by_entity(tenant.id, entity_id) == []


class TestTenantSettingsRepository:
    """Проверки новых методов TenantRepository (web-first)."""

    async def test_get_by_admin_token(self, repo: Repository, tenant_id: int) -> None:
        """Поиск тенанта по секретному токену кабинета."""
        tenant = await repo.tenants.get_by_id(tenant_id)
        assert tenant is not None
        assert tenant.admin_token

        found = await repo.tenants.get_by_admin_token(tenant.admin_token)

        assert found is not None
        assert found.id == tenant_id

    async def test_get_by_admin_token_unknown_returns_none(self, repo: Repository) -> None:
        """Неизвестный токен не находит тенанта."""
        assert await repo.tenants.get_by_admin_token("нет-такого-токена") is None

    async def test_ensure_admin_token_returns_existing(
        self, repo: Repository, tenant_id: int
    ) -> None:
        """Если токен уже есть, он не перегенерируется."""
        tenant = await repo.tenants.get_by_id(tenant_id)
        assert tenant is not None
        original = tenant.admin_token

        token = await repo.tenants.ensure_admin_token(tenant_id)

        assert token == original

    async def test_ensure_admin_token_generates_when_missing(self, repo: Repository) -> None:
        """Пустой admin_token заполняется новым значением."""
        tenant = await repo.tenants.create(name="Без токена", telegram_user_id=7001)
        await repo.session.flush()
        tenant.admin_token = ""
        await repo.session.flush()

        token = await repo.tenants.ensure_admin_token(tenant.id)

        assert token
        stored = await repo.tenants.get_by_id(tenant.id)
        assert stored is not None
        assert stored.admin_token == token

    async def test_ensure_admin_token_unknown_tenant(self, repo: Repository) -> None:
        """Для несуществующего тенанта возвращается None."""
        assert await repo.tenants.ensure_admin_token(999_999) is None

    async def test_update_settings_changes_allowed_fields(
        self, repo: Repository, tenant_id: int
    ) -> None:
        """Настройки тренера сохраняются в БД."""
        tenant = await repo.tenants.update_settings(
            tenant_id,
            name="Иван Петрович",
            timezone="Europe/Berlin",
            lesson_price=120.0,
            currency="EUR",
            payment_instructions="Перевод на карту",
        )

        assert tenant is not None
        assert tenant.name == "Иван Петрович"
        assert tenant.timezone == "Europe/Berlin"
        assert tenant.lesson_price == 120.0
        assert tenant.currency == "EUR"
        assert tenant.payment_instructions == "Перевод на карту"

    async def test_update_settings_ignores_service_fields(
        self, repo: Repository, tenant_id: int
    ) -> None:
        """Служебные поля (telegram_user_id, admin_token) не перезаписываются."""
        tenant = await repo.tenants.get_by_id(tenant_id)
        assert tenant is not None
        original_token = tenant.admin_token
        original_tg_id = tenant.telegram_user_id

        updated = await repo.tenants.update_settings(
            tenant_id,
            telegram_user_id=999_999,
            admin_token="подмена",
            is_active=False,
            name="Новое имя",
        )

        assert updated is not None
        assert updated.telegram_user_id == original_tg_id
        assert updated.admin_token == original_token
        assert updated.is_active is True
        assert updated.name == "Новое имя"

    async def test_update_settings_without_fields_returns_tenant(
        self, repo: Repository, tenant_id: int
    ) -> None:
        """Пустой вызов возвращает тенанта без изменений."""
        tenant = await repo.tenants.update_settings(tenant_id)

        assert tenant is not None
        assert tenant.id == tenant_id

    async def test_update_settings_unknown_tenant(self, repo: Repository) -> None:
        """Для несуществующего тенанта возвращается None."""
        assert await repo.tenants.update_settings(999_999, name="Кто-то") is None


class TestScheduleRepository:
    """Проверки операций с регулярными слотами расписания."""

    async def test_create_fills_fields(self, repo: Repository, tenant_id: int) -> None:
        """Создание слота заполняет все переданные поля."""
        entity_id = await _seed_entity(repo, tenant_id)

        slot = await repo.schedule.create(
            tenant_id,
            entity_id,
            "mon",
            "17:30",
            duration_min=90,
            location="Онлайн",
        )

        assert slot.id is not None
        assert slot.tenant_id == tenant_id
        assert slot.entity_id == entity_id
        assert slot.weekday == "mon"
        assert slot.start_time == "17:30"
        assert slot.duration_min == 90
        assert slot.location == "Онлайн"
        assert slot.is_active is True

    async def test_create_uses_defaults(self, repo: Repository, tenant_id: int) -> None:
        """Длительность по умолчанию — 60 минут, слот активен."""
        entity_id = await _seed_entity(repo, tenant_id)

        slot = await repo.schedule.create(tenant_id, entity_id, "tue", "10:00")

        assert slot.duration_min == 60
        assert slot.location is None
        assert slot.is_active is True

    async def test_get_by_id_scoped_to_tenant(self, repo: Repository, tenant_id: int) -> None:
        """Слот не виден чужому тенанту."""
        entity_id = await _seed_entity(repo, tenant_id)
        slot = await repo.schedule.create(tenant_id, entity_id, "mon", "17:30")
        other = await repo.tenants.create(name="Другой", telegram_user_id=4004)
        await repo.session.flush()

        assert await repo.schedule.get_by_id(tenant_id, slot.id) is not None
        assert await repo.schedule.get_by_id(other.id, slot.id) is None

    async def test_list_by_tenant_sorted_by_weekday_and_time(
        self, repo: Repository, tenant_id: int
    ) -> None:
        """Слоты сортируются по дню недели и времени начала."""
        entity_id = await _seed_entity(repo, tenant_id)
        await repo.schedule.create(tenant_id, entity_id, "wed", "09:00")
        await repo.schedule.create(tenant_id, entity_id, "mon", "18:00")
        await repo.schedule.create(tenant_id, entity_id, "mon", "08:00")

        slots = await repo.schedule.list_by_tenant(tenant_id)

        assert [(s.weekday, s.start_time) for s in slots] == [
            ("mon", "08:00"),
            ("mon", "18:00"),
            ("wed", "09:00"),
        ]

    async def test_list_by_tenant_only_active(self, repo: Repository, tenant_id: int) -> None:
        """Флаг only_active отсекает выключенные слоты."""
        entity_id = await _seed_entity(repo, tenant_id)
        await repo.schedule.create(tenant_id, entity_id, "mon", "08:00")
        await repo.schedule.create(tenant_id, entity_id, "mon", "09:00", is_active=False)

        assert len(await repo.schedule.list_by_tenant(tenant_id)) == 2
        active = await repo.schedule.list_by_tenant(tenant_id, only_active=True)
        assert [s.start_time for s in active] == ["08:00"]

    async def test_list_by_tenant_scoped(self, repo: Repository, tenant_id: int) -> None:
        """Слоты чужого тенанта не попадают в выборку."""
        entity_id = await _seed_entity(repo, tenant_id)
        await repo.schedule.create(tenant_id, entity_id, "mon", "08:00")
        other = await repo.tenants.create(name="Другой", telegram_user_id=4005)
        await repo.session.flush()
        other_entity = await _seed_entity(repo, other.id, name="Петя")
        await repo.schedule.create(other.id, other_entity, "mon", "10:00")

        slots = await repo.schedule.list_by_tenant(tenant_id)

        assert len(slots) == 1
        assert slots[0].tenant_id == tenant_id

    async def test_list_for_weekday(self, repo: Repository, tenant_id: int) -> None:
        """Выборка на день недели возвращает только этот день."""
        entity_id = await _seed_entity(repo, tenant_id)
        await repo.schedule.create(tenant_id, entity_id, "mon", "08:00")
        await repo.schedule.create(tenant_id, entity_id, "tue", "09:00")

        slots = await repo.schedule.list_for_weekday(tenant_id, "mon")

        assert len(slots) == 1
        assert slots[0].weekday == "mon"

    async def test_list_for_weekday_normalizes_code(self, repo: Repository, tenant_id: int) -> None:
        """Код дня недели приводится к нижнему регистру."""
        entity_id = await _seed_entity(repo, tenant_id)
        await repo.schedule.create(tenant_id, entity_id, "mon", "08:00")

        assert len(await repo.schedule.list_for_weekday(tenant_id, "MON")) == 1

    async def test_list_for_weekday_skips_inactive_by_default(
        self, repo: Repository, tenant_id: int
    ) -> None:
        """По умолчанию неактивные слоты не показываются."""
        entity_id = await _seed_entity(repo, tenant_id)
        await repo.schedule.create(tenant_id, entity_id, "mon", "08:00", is_active=False)

        assert await repo.schedule.list_for_weekday(tenant_id, "mon") == []
        assert len(await repo.schedule.list_for_weekday(tenant_id, "mon", only_active=False)) == 1

    async def test_list_by_entity(self, repo: Repository, tenant_id: int) -> None:
        """Выборка по ученику возвращает только его слоты."""
        misha = await _seed_entity(repo, tenant_id, name="Миша")
        anya = await _seed_entity(repo, tenant_id, name="Аня")
        await repo.schedule.create(tenant_id, misha, "mon", "08:00")
        await repo.schedule.create(tenant_id, anya, "tue", "09:00")

        slots = await repo.schedule.list_by_entity(tenant_id, misha)

        assert len(slots) == 1
        assert slots[0].entity_id == misha

    async def test_update_changes_fields(self, repo: Repository, tenant_id: int) -> None:
        """Обновление слота меняет разрешённые поля."""
        entity_id = await _seed_entity(repo, tenant_id)
        slot = await repo.schedule.create(tenant_id, entity_id, "mon", "08:00")

        updated = await repo.schedule.update(
            tenant_id, slot.id, start_time="09:30", duration_min=45, location="Зал"
        )

        assert updated is not None
        assert updated.start_time == "09:30"
        assert updated.duration_min == 45
        assert updated.location == "Зал"

    async def test_update_ignores_unknown_fields(self, repo: Repository, tenant_id: int) -> None:
        """Неизвестные поля игнорируются, служебные не ломаются."""
        entity_id = await _seed_entity(repo, tenant_id)
        slot = await repo.schedule.create(tenant_id, entity_id, "mon", "08:00")

        updated = await repo.schedule.update(
            tenant_id, slot.id, **{"id": 999_999, "start_time": "10:00"}
        )

        assert updated is not None
        assert updated.id == slot.id
        assert updated.tenant_id == tenant_id
        assert updated.start_time == "10:00"

    async def test_update_without_fields_returns_slot(
        self, repo: Repository, tenant_id: int
    ) -> None:
        """Пустое обновление возвращает слот без изменений."""
        entity_id = await _seed_entity(repo, tenant_id)
        slot = await repo.schedule.create(tenant_id, entity_id, "mon", "08:00")

        updated = await repo.schedule.update(tenant_id, slot.id)

        assert updated is not None
        assert updated.start_time == "08:00"

    async def test_update_scoped_to_tenant(self, repo: Repository, tenant_id: int) -> None:
        """Чужой тенант не может изменить слот."""
        entity_id = await _seed_entity(repo, tenant_id)
        slot = await repo.schedule.create(tenant_id, entity_id, "mon", "08:00")
        other = await repo.tenants.create(name="Другой", telegram_user_id=4006)
        await repo.session.flush()

        assert await repo.schedule.update(other.id, slot.id, start_time="23:00") is None

    async def test_delete_removes_slot(self, repo: Repository, tenant_id: int) -> None:
        """Удаление слота убирает его из выборок."""
        entity_id = await _seed_entity(repo, tenant_id)
        slot = await repo.schedule.create(tenant_id, entity_id, "mon", "08:00")

        assert await repo.schedule.delete(tenant_id, slot.id) is True

        assert await repo.schedule.get_by_id(tenant_id, slot.id) is None
        assert await repo.schedule.list_by_tenant(tenant_id) == []

    async def test_delete_scoped_to_tenant(self, repo: Repository, tenant_id: int) -> None:
        """Чужой тенант не может удалить слот."""
        entity_id = await _seed_entity(repo, tenant_id)
        slot = await repo.schedule.create(tenant_id, entity_id, "mon", "08:00")
        other = await repo.tenants.create(name="Другой", telegram_user_id=4007)
        await repo.session.flush()

        assert await repo.schedule.delete(other.id, slot.id) is False
        assert await repo.schedule.get_by_id(tenant_id, slot.id) is not None

    async def test_delete_entity_cascades_slots(self, repo: Repository, tenant_id: int) -> None:
        """Удаление ученика убирает его слоты расписания."""
        entity_id = await _seed_entity(repo, tenant_id)
        await repo.schedule.create(tenant_id, entity_id, "mon", "08:00")

        assert await repo.entities.delete(tenant_id, entity_id) is True

        assert await repo.schedule.list_by_entity(tenant_id, entity_id) == []


class TestScheduleExceptionRepository:
    """Проверки операций с разовыми отклонениями от расписания."""

    async def test_create_moved_exception(self, repo: Repository, tenant_id: int) -> None:
        """Перенос привязывается к слоту и хранит новое время."""
        entity_id = await _seed_entity(repo, tenant_id)
        slot = await repo.schedule.create(tenant_id, entity_id, "mon", "17:30")

        exception = await repo.schedule_exceptions.create(
            tenant_id,
            entity_id,
            MONDAY,
            "moved",
            slot_id=slot.id,
            new_time="19:00",
            note="Тренер задержался",
        )

        assert exception.id is not None
        assert exception.slot_id == slot.id
        assert exception.exception_date == MONDAY
        assert exception.kind == "moved"
        assert exception.new_time == "19:00"
        assert exception.note == "Тренер задержался"

    async def test_create_extra_without_slot(self, repo: Repository, tenant_id: int) -> None:
        """Дополнительное занятие может не иметь слота."""
        entity_id = await _seed_entity(repo, tenant_id)

        exception = await repo.schedule_exceptions.create(
            tenant_id, entity_id, WEDNESDAY, "extra", new_time="12:00"
        )

        assert exception.slot_id is None
        assert exception.kind == "extra"

    async def test_get_by_id_scoped_to_tenant(self, repo: Repository, tenant_id: int) -> None:
        """Отклонение не видно чужому тенанту."""
        entity_id = await _seed_entity(repo, tenant_id)
        exception = await repo.schedule_exceptions.create(tenant_id, entity_id, MONDAY, "cancelled")
        other = await repo.tenants.create(name="Другой", telegram_user_id=5001)
        await repo.session.flush()

        assert await repo.schedule_exceptions.get_by_id(tenant_id, exception.id) is not None
        assert await repo.schedule_exceptions.get_by_id(other.id, exception.id) is None

    async def test_list_by_date(self, repo: Repository, tenant_id: int) -> None:
        """Выборка на дату возвращает только эту дату."""
        entity_id = await _seed_entity(repo, tenant_id)
        await repo.schedule_exceptions.create(tenant_id, entity_id, MONDAY, "cancelled")
        await repo.schedule_exceptions.create(tenant_id, entity_id, TUESDAY, "cancelled")

        found = await repo.schedule_exceptions.list_by_date(tenant_id, MONDAY)

        assert len(found) == 1
        assert found[0].exception_date == MONDAY

    async def test_list_by_date_range_inclusive(self, repo: Repository, tenant_id: int) -> None:
        """Диапазон дат включает обе границы."""
        entity_id = await _seed_entity(repo, tenant_id)
        await repo.schedule_exceptions.create(tenant_id, entity_id, MONDAY, "cancelled")
        await repo.schedule_exceptions.create(tenant_id, entity_id, TUESDAY, "moved")
        await repo.schedule_exceptions.create(tenant_id, entity_id, WEDNESDAY, "extra")
        await repo.schedule_exceptions.create(tenant_id, entity_id, NEXT_MONDAY, "extra")

        found = await repo.schedule_exceptions.list_by_date_range(tenant_id, MONDAY, WEDNESDAY)

        assert [e.exception_date for e in found] == [MONDAY, TUESDAY, WEDNESDAY]

    async def test_list_by_date_range_scoped(self, repo: Repository, tenant_id: int) -> None:
        """Диапазон не захватывает данные чужого тенанта."""
        other = await repo.tenants.create(name="Другой", telegram_user_id=5002)
        await repo.session.flush()
        other_entity = await _seed_entity(repo, other.id, name="Петя")
        await repo.schedule_exceptions.create(other.id, other_entity, MONDAY, "cancelled")

        assert await repo.schedule_exceptions.list_by_date_range(tenant_id, MONDAY, WEDNESDAY) == []

    async def test_list_by_entity_newest_first(self, repo: Repository, tenant_id: int) -> None:
        """История отклонений ученика идёт от новых к старым."""
        entity_id = await _seed_entity(repo, tenant_id)
        await repo.schedule_exceptions.create(tenant_id, entity_id, MONDAY, "cancelled")
        await repo.schedule_exceptions.create(tenant_id, entity_id, WEDNESDAY, "moved")

        found = await repo.schedule_exceptions.list_by_entity(tenant_id, entity_id)

        assert [e.exception_date for e in found] == [WEDNESDAY, MONDAY]

    async def test_delete_removes_exception(self, repo: Repository, tenant_id: int) -> None:
        """Удаление отклонения убирает его из выборок."""
        entity_id = await _seed_entity(repo, tenant_id)
        exception = await repo.schedule_exceptions.create(tenant_id, entity_id, MONDAY, "cancelled")

        assert await repo.schedule_exceptions.delete(tenant_id, exception.id) is True

        assert await repo.schedule_exceptions.list_by_date(tenant_id, MONDAY) == []

    async def test_delete_scoped_to_tenant(self, repo: Repository, tenant_id: int) -> None:
        """Чужой тенант не может удалить отклонение."""
        entity_id = await _seed_entity(repo, tenant_id)
        exception = await repo.schedule_exceptions.create(tenant_id, entity_id, MONDAY, "cancelled")
        other = await repo.tenants.create(name="Другой", telegram_user_id=5003)
        await repo.session.flush()

        assert await repo.schedule_exceptions.delete(other.id, exception.id) is False

    async def test_slot_delete_keeps_exception_history(
        self, repo: Repository, tenant_id: int
    ) -> None:
        """Удаление слота не стирает историю переносов (slot_id → NULL)."""
        entity_id = await _seed_entity(repo, tenant_id)
        slot = await repo.schedule.create(tenant_id, entity_id, "mon", "17:30")
        exception = await repo.schedule_exceptions.create(
            tenant_id, entity_id, MONDAY, "moved", slot_id=slot.id, new_time="19:00"
        )

        assert await repo.schedule.delete(tenant_id, slot.id) is True

        stored = await repo.schedule_exceptions.get_by_id(tenant_id, exception.id)
        assert stored is not None
        assert stored.slot_id is None
        assert stored.new_time == "19:00"


class TestReminderRuleRepository:
    """Проверки операций с правилами напоминаний."""

    async def test_create_fills_fields(self, repo: Repository, tenant_id: int) -> None:
        """Создание правила заполняет поля и значения по умолчанию."""
        rule = await repo.reminders.create(tenant_id, "morning_of", time_of_day="08:00")

        assert rule.id is not None
        assert rule.tenant_id == tenant_id
        assert rule.kind == "morning_of"
        assert rule.time_of_day == "08:00"
        assert rule.offset_minutes is None
        assert rule.is_enabled is True
        assert rule.channel == "telegram"

    async def test_create_with_offset(self, repo: Repository, tenant_id: int) -> None:
        """Правило «за день» хранит смещение в минутах."""
        rule = await repo.reminders.create(
            tenant_id, "day_before", time_of_day="19:00", offset_minutes=60
        )

        assert rule.offset_minutes == 60

    async def test_get_by_kind(self, repo: Repository, tenant_id: int) -> None:
        """Поиск правила по типу в рамках тенанта."""
        await repo.reminders.create(tenant_id, "day_before", time_of_day="09:00")

        rule = await repo.reminders.get_by_kind(tenant_id, "day_before")

        assert rule is not None
        assert rule.kind == "day_before"

    async def test_get_by_kind_unknown_returns_none(self, repo: Repository, tenant_id: int) -> None:
        """Отсутствующее правило возвращает None."""
        assert await repo.reminders.get_by_kind(tenant_id, "day_before") is None

    async def test_get_by_id_scoped_to_tenant(self, repo: Repository, tenant_id: int) -> None:
        """Правило не видно чужому тенанту."""
        rule = await repo.reminders.create(tenant_id, "morning_of", time_of_day="08:00")
        other = await repo.tenants.create(name="Другой", telegram_user_id=6001)
        await repo.session.flush()

        assert await repo.reminders.get_by_id(tenant_id, rule.id) is not None
        assert await repo.reminders.get_by_id(other.id, rule.id) is None

    async def test_list_by_tenant(self, repo: Repository, tenant_id: int) -> None:
        """Список правил тенанта отсортирован по id."""
        await repo.reminders.create(tenant_id, "morning_of", time_of_day="08:00")
        await repo.reminders.create(tenant_id, "day_before", time_of_day="19:00")

        rules = await repo.reminders.list_by_tenant(tenant_id)

        assert [r.kind for r in rules] == ["morning_of", "day_before"]

    async def test_list_by_tenant_only_enabled(self, repo: Repository, tenant_id: int) -> None:
        """Флаг only_enabled отсекает выключенные правила."""
        await repo.reminders.create(tenant_id, "morning_of", time_of_day="08:00")
        await repo.reminders.create(tenant_id, "day_before", time_of_day="19:00", is_enabled=False)

        assert len(await repo.reminders.list_by_tenant(tenant_id)) == 2
        enabled = await repo.reminders.list_by_tenant(tenant_id, only_enabled=True)
        assert [r.kind for r in enabled] == ["morning_of"]

    async def test_update_changes_fields(self, repo: Repository, tenant_id: int) -> None:
        """Обновление правила меняет разрешённые поля."""
        rule = await repo.reminders.create(tenant_id, "morning_of", time_of_day="08:00")

        updated = await repo.reminders.update(
            tenant_id, rule.id, time_of_day="07:30", is_enabled=False, channel="whatsapp"
        )

        assert updated is not None
        assert updated.time_of_day == "07:30"
        assert updated.is_enabled is False
        assert updated.channel == "whatsapp"

    async def test_update_ignores_kind_and_tenant(self, repo: Repository, tenant_id: int) -> None:
        """Тип правила и тенант нельзя переписать через update."""
        rule = await repo.reminders.create(tenant_id, "morning_of", time_of_day="08:00")

        updated = await repo.reminders.update(
            tenant_id, rule.id, **{"kind": "day_before", "time_of_day": "06:00"}
        )

        assert updated is not None
        assert updated.kind == "morning_of"
        assert updated.tenant_id == tenant_id
        assert updated.time_of_day == "06:00"

    async def test_update_without_fields_returns_rule(
        self, repo: Repository, tenant_id: int
    ) -> None:
        """Пустое обновление возвращает правило без изменений."""
        rule = await repo.reminders.create(tenant_id, "morning_of", time_of_day="08:00")

        updated = await repo.reminders.update(tenant_id, rule.id)

        assert updated is not None
        assert updated.time_of_day == "08:00"

    async def test_update_scoped_to_tenant(self, repo: Repository, tenant_id: int) -> None:
        """Чужой тенант не может изменить правило."""
        rule = await repo.reminders.create(tenant_id, "morning_of", time_of_day="08:00")
        other = await repo.tenants.create(name="Другой", telegram_user_id=6002)
        await repo.session.flush()

        assert await repo.reminders.update(other.id, rule.id, time_of_day="23:00") is None

    async def test_delete_removes_rule(self, repo: Repository, tenant_id: int) -> None:
        """Удаление правила убирает его из выборок."""
        rule = await repo.reminders.create(tenant_id, "morning_of", time_of_day="08:00")

        assert await repo.reminders.delete(tenant_id, rule.id) is True

        assert await repo.reminders.list_by_tenant(tenant_id) == []

    async def test_delete_scoped_to_tenant(self, repo: Repository, tenant_id: int) -> None:
        """Чужой тенант не может удалить правило."""
        rule = await repo.reminders.create(tenant_id, "morning_of", time_of_day="08:00")
        other = await repo.tenants.create(name="Другой", telegram_user_id=6003)
        await repo.session.flush()

        assert await repo.reminders.delete(other.id, rule.id) is False


class TestShareLinkRepository:
    """Проверки публичных ссылок на конкретное занятие."""

    async def _seed_lesson(self, repo: Repository, tenant_id: int) -> int:
        """Создаёт ученика и занятие, возвращает id занятия."""
        entity_id = await _seed_entity(repo, tenant_id)
        lesson = await repo.lessons.create(
            tenant_id,
            entity_id,
            MONDAY,
            "Шахматы",
            attendance="present",
        )
        return lesson.id

    async def test_create_generates_token(self, repo: Repository, tenant_id: int) -> None:
        """Ссылка получает уникальный токен автоматически."""
        lesson_id = await self._seed_lesson(repo, tenant_id)

        link = await repo.share_links.create(tenant_id, lesson_id)

        assert link.id is not None
        assert link.token
        assert link.lesson_id == lesson_id
        assert link.is_revoked is False
        assert link.expires_at is None

    async def test_create_respects_explicit_token(self, repo: Repository, tenant_id: int) -> None:
        """Явно переданный токен сохраняется как есть."""
        lesson_id = await self._seed_lesson(repo, tenant_id)

        link = await repo.share_links.create(tenant_id, lesson_id, token="fixed-token")

        assert link.token == "fixed-token"

    async def test_get_by_token_is_global(self, repo: Repository, tenant_id: int) -> None:
        """Поиск по токену не требует tenant_id."""
        lesson_id = await self._seed_lesson(repo, tenant_id)
        link = await repo.share_links.create(tenant_id, lesson_id, token="global-token")

        found = await repo.share_links.get_by_token("global-token")

        assert found is not None
        assert found.id == link.id

    async def test_get_by_token_unknown_returns_none(self, repo: Repository) -> None:
        """Неизвестный токен возвращает None."""
        assert await repo.share_links.get_by_token("нет-такого") is None

    async def test_get_by_id_scoped_to_tenant(self, repo: Repository, tenant_id: int) -> None:
        """Чужой тенант не видит ссылку по id."""
        lesson_id = await self._seed_lesson(repo, tenant_id)
        link = await repo.share_links.create(tenant_id, lesson_id)
        other = await repo.tenants.create(name="Другой", telegram_user_id=6101)
        await repo.session.flush()

        assert await repo.share_links.get_by_id(other.id, link.id) is None

    async def test_list_by_lesson_newest_first(self, repo: Repository, tenant_id: int) -> None:
        """Ссылки занятия возвращаются новыми сверху."""
        lesson_id = await self._seed_lesson(repo, tenant_id)
        first = await repo.share_links.create(tenant_id, lesson_id)
        second = await repo.share_links.create(tenant_id, lesson_id)

        links = await repo.share_links.list_by_lesson(tenant_id, lesson_id)

        assert [link.id for link in links] == [second.id, first.id]

    async def test_list_by_lesson_scoped(self, repo: Repository, tenant_id: int) -> None:
        """Чужой тенант не видит ссылки занятия."""
        lesson_id = await self._seed_lesson(repo, tenant_id)
        await repo.share_links.create(tenant_id, lesson_id)
        other = await repo.tenants.create(name="Другой", telegram_user_id=6102)
        await repo.session.flush()

        assert await repo.share_links.list_by_lesson(other.id, lesson_id) == []

    async def test_get_active_returns_latest(self, repo: Repository, tenant_id: int) -> None:
        """Действующей считается самая свежая неотозванная ссылка."""
        lesson_id = await self._seed_lesson(repo, tenant_id)
        await repo.share_links.create(tenant_id, lesson_id)
        latest = await repo.share_links.create(tenant_id, lesson_id)

        active = await repo.share_links.get_active_for_lesson(tenant_id, lesson_id)

        assert active is not None
        assert active.id == latest.id

    async def test_get_active_skips_revoked(self, repo: Repository, tenant_id: int) -> None:
        """Отозванная ссылка не считается действующей."""
        lesson_id = await self._seed_lesson(repo, tenant_id)
        link = await repo.share_links.create(tenant_id, lesson_id)
        await repo.share_links.revoke(tenant_id, link.id)

        assert await repo.share_links.get_active_for_lesson(tenant_id, lesson_id) is None

    async def test_get_active_skips_expired(self, repo: Repository, tenant_id: int) -> None:
        """Истёкшая ссылка не считается действующей."""
        lesson_id = await self._seed_lesson(repo, tenant_id)
        past = datetime.now(UTC) - timedelta(days=1)
        await repo.share_links.create(tenant_id, lesson_id, expires_at=past)

        assert await repo.share_links.get_active_for_lesson(tenant_id, lesson_id) is None

    async def test_get_active_returns_future_expiry(self, repo: Repository, tenant_id: int) -> None:
        """Ссылка с будущим сроком действия считается действующей."""
        lesson_id = await self._seed_lesson(repo, tenant_id)
        future = datetime.now(UTC) + timedelta(days=7)
        link = await repo.share_links.create(tenant_id, lesson_id, expires_at=future)

        active = await repo.share_links.get_active_for_lesson(tenant_id, lesson_id)

        assert active is not None
        assert active.id == link.id

    async def test_revoke_marks_link(self, repo: Repository, tenant_id: int) -> None:
        """Отзыв помечает ссылку, не удаляя её."""
        lesson_id = await self._seed_lesson(repo, tenant_id)
        link = await repo.share_links.create(tenant_id, lesson_id)

        revoked = await repo.share_links.revoke(tenant_id, link.id)

        assert revoked is not None
        assert revoked.is_revoked is True
        assert len(await repo.share_links.list_by_lesson(tenant_id, lesson_id)) == 1

    async def test_revoke_scoped_to_tenant(self, repo: Repository, tenant_id: int) -> None:
        """Чужой тенант не может отозвать ссылку."""
        lesson_id = await self._seed_lesson(repo, tenant_id)
        link = await repo.share_links.create(tenant_id, lesson_id)
        other = await repo.tenants.create(name="Другой", telegram_user_id=6103)
        await repo.session.flush()

        assert await repo.share_links.revoke(other.id, link.id) is None

    async def test_delete_removes_link(self, repo: Repository, tenant_id: int) -> None:
        """Удаление убирает ссылку из выборок."""
        lesson_id = await self._seed_lesson(repo, tenant_id)
        link = await repo.share_links.create(tenant_id, lesson_id)

        assert await repo.share_links.delete(tenant_id, link.id) is True

        assert await repo.share_links.list_by_lesson(tenant_id, lesson_id) == []

    async def test_delete_scoped_to_tenant(self, repo: Repository, tenant_id: int) -> None:
        """Чужой тенант не может удалить ссылку."""
        lesson_id = await self._seed_lesson(repo, tenant_id)
        link = await repo.share_links.create(tenant_id, lesson_id)
        other = await repo.tenants.create(name="Другой", telegram_user_id=6104)
        await repo.session.flush()

        assert await repo.share_links.delete(other.id, link.id) is False


class TestAdminSessionRepository:
    """Проверки сессий тренера в веб-кабинете."""

    async def test_create_generates_token(self, repo: Repository, tenant_id: int) -> None:
        """Сессия получает уникальный токен автоматически."""
        expires = datetime.now(UTC) + timedelta(days=30)

        admin_session = await repo.admin_sessions.create(tenant_id, expires)

        assert admin_session.id is not None
        assert admin_session.token
        assert admin_session.tenant_id == tenant_id

    async def test_create_respects_explicit_token(self, repo: Repository, tenant_id: int) -> None:
        """Явно переданный токен сохраняется как есть."""
        expires = datetime.now(UTC) + timedelta(days=30)

        admin_session = await repo.admin_sessions.create(tenant_id, expires, token="fixed")

        assert admin_session.token == "fixed"

    async def test_get_by_token(self, repo: Repository, tenant_id: int) -> None:
        """Сессия находится по токену из cookie."""
        expires = datetime.now(UTC) + timedelta(days=30)
        created = await repo.admin_sessions.create(tenant_id, expires, token="cookie-token")

        found = await repo.admin_sessions.get_by_token("cookie-token")

        assert found is not None
        assert found.id == created.id

    async def test_get_by_token_unknown_returns_none(self, repo: Repository) -> None:
        """Неизвестный токен возвращает None."""
        assert await repo.admin_sessions.get_by_token("нет-такого") is None

    async def test_get_valid_returns_active_session(self, repo: Repository, tenant_id: int) -> None:
        """Действующая сессия возвращается."""
        expires = datetime.now(UTC) + timedelta(days=30)
        created = await repo.admin_sessions.create(tenant_id, expires, token="valid")

        found = await repo.admin_sessions.get_valid("valid")

        assert found is not None
        assert found.id == created.id

    async def test_get_valid_skips_expired(self, repo: Repository, tenant_id: int) -> None:
        """Истёкшая сессия не возвращается."""
        expired = datetime.now(UTC) - timedelta(minutes=1)
        await repo.admin_sessions.create(tenant_id, expired, token="expired")

        assert await repo.admin_sessions.get_valid("expired") is None

    async def test_delete_removes_session(self, repo: Repository, tenant_id: int) -> None:
        """Выход из кабинета удаляет сессию."""
        expires = datetime.now(UTC) + timedelta(days=30)
        await repo.admin_sessions.create(tenant_id, expires, token="to-delete")

        assert await repo.admin_sessions.delete("to-delete") is True

        assert await repo.admin_sessions.get_by_token("to-delete") is None

    async def test_delete_unknown_returns_false(self, repo: Repository) -> None:
        """Удаление неизвестной сессии возвращает False."""
        assert await repo.admin_sessions.delete("нет-такого") is False

    async def test_delete_expired_removes_only_expired(
        self, repo: Repository, tenant_id: int
    ) -> None:
        """Массовая очистка удаляет только истёкшие сессии."""
        now = datetime.now(UTC)
        await repo.admin_sessions.create(tenant_id, now - timedelta(days=1), token="old")
        await repo.admin_sessions.create(tenant_id, now + timedelta(days=1), token="fresh")

        removed = await repo.admin_sessions.delete_expired()

        assert removed == 1
        assert await repo.admin_sessions.get_by_token("fresh") is not None

    async def test_delete_by_tenant_removes_all(self, repo: Repository, tenant_id: int) -> None:
        """Смена admin_token удаляет все сессии тенанта."""
        expires = datetime.now(UTC) + timedelta(days=30)
        await repo.admin_sessions.create(tenant_id, expires, token="one")
        await repo.admin_sessions.create(tenant_id, expires, token="two")
        other = await repo.tenants.create(name="Другой", telegram_user_id=6201)
        await repo.session.flush()
        await repo.admin_sessions.create(other.id, expires, token="other")

        removed = await repo.admin_sessions.delete_by_tenant(tenant_id)

        assert removed == 2
        assert await repo.admin_sessions.get_by_token("other") is not None


class TestAuditLogRepository:
    """Проверки журнала действий тренера."""

    async def test_create_fills_fields(self, repo: Repository, tenant_id: int) -> None:
        """Запись журнала сохраняет действие и автора."""
        entry = await repo.audit.create(tenant_id, "student.created", payload='{"id": 1}')

        assert entry.id is not None
        assert entry.tenant_id == tenant_id
        assert entry.action == "student.created"
        assert entry.payload == '{"id": 1}'
        assert entry.actor == "trainer"

    async def test_create_respects_actor(self, repo: Repository, tenant_id: int) -> None:
        """Автор действия можно переопределить."""
        entry = await repo.audit.create(tenant_id, "lesson.created", actor="bot")

        assert entry.actor == "bot"

    async def test_create_allows_null_payload(self, repo: Repository, tenant_id: int) -> None:
        """Payload может отсутствовать."""
        entry = await repo.audit.create(tenant_id, "settings.updated")

        assert entry.payload is None

    async def test_list_by_tenant_newest_first(self, repo: Repository, tenant_id: int) -> None:
        """Журнал возвращается новыми записями сверху."""
        first = await repo.audit.create(tenant_id, "first")
        second = await repo.audit.create(tenant_id, "second")

        entries = await repo.audit.list_by_tenant(tenant_id)

        assert [entry.id for entry in entries] == [second.id, first.id]

    async def test_list_by_tenant_respects_limit(self, repo: Repository, tenant_id: int) -> None:
        """Лимит ограничивает число записей."""
        for index in range(3):
            await repo.audit.create(tenant_id, f"action-{index}")

        entries = await repo.audit.list_by_tenant(tenant_id, limit=2)

        assert len(entries) == 2

    async def test_list_by_tenant_scoped(self, repo: Repository, tenant_id: int) -> None:
        """Чужой тенант не видит записи журнала."""
        await repo.audit.create(tenant_id, "mine")
        other = await repo.tenants.create(name="Другой", telegram_user_id=6301)
        await repo.session.flush()

        assert await repo.audit.list_by_tenant(other.id) == []


class TestDispatchLogRepository:
    """Проверки журнала отправленных напоминаний."""

    async def test_create_for_entity(self, repo: Repository, tenant_id: int) -> None:
        """Запись для ученика сохраняет все поля."""
        entity_id = await _seed_entity(repo, tenant_id)

        entry = await repo.dispatch_log.create(tenant_id, "day_before", MONDAY, entity_id)

        assert entry.id is not None
        assert entry.tenant_id == tenant_id
        assert entry.entity_id == entity_id
        assert entry.rule_kind == "day_before"
        assert entry.dispatch_date == MONDAY

    async def test_create_for_tenant_level(self, repo: Repository, tenant_id: int) -> None:
        """Запись уровня тенанта не привязана к ученику."""
        entry = await repo.dispatch_log.create(tenant_id, "morning_of", MONDAY)

        assert entry.entity_id is None

    async def test_was_sent_false_before_dispatch(self, repo: Repository, tenant_id: int) -> None:
        """До отправки факт не подтверждён."""
        assert await repo.dispatch_log.was_sent(tenant_id, "day_before", MONDAY) is False

    async def test_was_sent_true_after_dispatch(self, repo: Repository, tenant_id: int) -> None:
        """После записи факт подтверждён."""
        entity_id = await _seed_entity(repo, tenant_id)
        await repo.dispatch_log.create(tenant_id, "day_before", MONDAY, entity_id)

        assert await repo.dispatch_log.was_sent(tenant_id, "day_before", MONDAY, entity_id) is True

    async def test_was_sent_distinguishes_entities(self, repo: Repository, tenant_id: int) -> None:
        """Отправка одному ученику не влияет на другого."""
        first = await _seed_entity(repo, tenant_id, name="Миша")
        second = await _seed_entity(repo, tenant_id, name="Аня")
        await repo.dispatch_log.create(tenant_id, "day_before", MONDAY, first)

        assert await repo.dispatch_log.was_sent(tenant_id, "day_before", MONDAY, second) is False

    async def test_was_sent_distinguishes_dates(self, repo: Repository, tenant_id: int) -> None:
        """Отправка в один день не влияет на другой."""
        await repo.dispatch_log.create(tenant_id, "day_before", MONDAY)

        assert await repo.dispatch_log.was_sent(tenant_id, "day_before", TUESDAY) is False

    async def test_was_sent_distinguishes_kinds(self, repo: Repository, tenant_id: int) -> None:
        """Разные типы правил учитываются отдельно."""
        await repo.dispatch_log.create(tenant_id, "day_before", MONDAY)

        assert await repo.dispatch_log.was_sent(tenant_id, "morning_of", MONDAY) is False

    async def test_was_sent_scoped_to_tenant(self, repo: Repository, tenant_id: int) -> None:
        """Чужой тенант не видит отправки."""
        await repo.dispatch_log.create(tenant_id, "day_before", MONDAY)
        other = await repo.tenants.create(name="Другой", telegram_user_id=6401)
        await repo.session.flush()

        assert await repo.dispatch_log.was_sent(other.id, "day_before", MONDAY) is False

    async def test_last_sent_date_none_without_entries(
        self, repo: Repository, tenant_id: int
    ) -> None:
        """Без отправок последней даты нет."""
        assert await repo.dispatch_log.last_sent_date(tenant_id, "day_before") is None

    async def test_last_sent_date_ignores_entity_filter(
        self, repo: Repository, tenant_id: int
    ) -> None:
        """Дата находится, даже если запись привязана к ученику."""
        entity_id = await _seed_entity(repo, tenant_id)
        await repo.dispatch_log.create(tenant_id, "day_before", MONDAY, entity_id)

        assert await repo.dispatch_log.last_sent_date(tenant_id, "day_before") == MONDAY

    async def test_last_sent_date_returns_latest(self, repo: Repository, tenant_id: int) -> None:
        """Из нескольких отправок возвращается самая поздняя."""
        await repo.dispatch_log.create(tenant_id, "day_before", MONDAY)
        await repo.dispatch_log.create(tenant_id, "day_before", TUESDAY)

        assert await repo.dispatch_log.last_sent_date(tenant_id, "day_before") == TUESDAY

    async def test_last_sent_date_separates_kinds(self, repo: Repository, tenant_id: int) -> None:
        """У разных правил свои последние даты."""
        await repo.dispatch_log.create(tenant_id, "day_before", MONDAY)

        assert await repo.dispatch_log.last_sent_date(tenant_id, "morning_of") is None

    async def test_list_by_date(self, repo: Repository, tenant_id: int) -> None:
        """Выборка за дату возвращает только записи этой даты."""
        await repo.dispatch_log.create(tenant_id, "day_before", MONDAY)
        await repo.dispatch_log.create(tenant_id, "morning_of", MONDAY)
        await repo.dispatch_log.create(tenant_id, "day_before", TUESDAY)

        entries = await repo.dispatch_log.list_by_date(tenant_id, MONDAY)

        assert len(entries) == 2
        assert all(entry.dispatch_date == MONDAY for entry in entries)

    async def test_list_by_date_scoped(self, repo: Repository, tenant_id: int) -> None:
        """Чужой тенант не видит записи за дату."""
        await repo.dispatch_log.create(tenant_id, "day_before", MONDAY)
        other = await repo.tenants.create(name="Другой", telegram_user_id=6402)
        await repo.session.flush()

        assert await repo.dispatch_log.list_by_date(other.id, MONDAY) == []

    async def test_delete_by_date_resets_journal(self, repo: Repository, tenant_id: int) -> None:
        """Сброс журнала за дату позволяет отправить повторно."""
        await repo.dispatch_log.create(tenant_id, "day_before", MONDAY)
        await repo.dispatch_log.create(tenant_id, "morning_of", MONDAY)

        removed = await repo.dispatch_log.delete_by_date(tenant_id, MONDAY)

        assert removed == 2
        assert await repo.dispatch_log.was_sent(tenant_id, "day_before", MONDAY) is False

    async def test_delete_by_date_keeps_other_dates(self, repo: Repository, tenant_id: int) -> None:
        """Сброс одной даты не затрагивает другие."""
        await repo.dispatch_log.create(tenant_id, "day_before", MONDAY)
        await repo.dispatch_log.create(tenant_id, "day_before", TUESDAY)

        await repo.dispatch_log.delete_by_date(tenant_id, MONDAY)

        assert await repo.dispatch_log.was_sent(tenant_id, "day_before", TUESDAY) is True


class TestRepositoryFacade:
    """Проверки фасада Repository."""

    async def test_facade_exposes_all_repositories(self, repo: Repository) -> None:
        """Фасад предоставляет доступ ко всем репозиториям."""
        assert repo.tenants is not None
        assert repo.entities is not None
        assert repo.lessons is not None
        assert repo.payments is not None
        assert repo.schedule is not None
        assert repo.schedule_exceptions is not None
        assert repo.reminders is not None
        assert repo.share_links is not None
        assert repo.admin_sessions is not None
        assert repo.audit is not None
        assert repo.dispatch_log is not None

    async def test_facade_shares_single_session(self, repo: Repository) -> None:
        """Все репозитории работают через одну сессию."""
        assert repo.tenants.session is repo.session
        assert repo.entities.session is repo.session
        assert repo.lessons.session is repo.session
        assert repo.payments.session is repo.session
        assert repo.schedule.session is repo.session
        assert repo.schedule_exceptions.session is repo.session
        assert repo.reminders.session is repo.session
        assert repo.share_links.session is repo.session
        assert repo.admin_sessions.session is repo.session
        assert repo.audit.session is repo.session
        assert repo.dispatch_log.session is repo.session
