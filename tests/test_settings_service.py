"""Тесты сервисов настроек, напоминаний, аудита и цены из БД.

Проверяются:

* :class:`TenantSettingsService` — чтение/запись настроек с валидацией;
* :class:`ReminderService` — CRUD правил и расчёт момента отправки;
* :class:`AuditService` — запись действий тренера;
* :class:`PaymentService` — цена и валюта из БД с фолбэком на ``.env``.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from app.core.config import settings
from app.db.repository import Repository
from app.services.audit_service import (
    ACTION_SETTINGS_UPDATED,
    ACTION_STUDENT_CREATED,
    ACTOR_BOT,
    ACTOR_TRAINER,
    AuditService,
)
from app.services.payment_service import PaymentService
from app.services.reminder_service import (
    DEFAULT_TIME_OF_DAY,
    REMINDER_KINDS,
    ReminderService,
    _normalize_kind,
    _parse_time,
)
from app.services.settings_service import (
    MAX_LESSON_PRICE,
    TenantSettings,
    TenantSettingsService,
)

MONDAY = date(2026, 9, 14)


@pytest.fixture
def settings_service(repo: Repository) -> TenantSettingsService:
    """Возвращает сервис настроек поверх тестового репозитория."""
    return TenantSettingsService(repo)


@pytest.fixture
def reminder_service(repo: Repository) -> ReminderService:
    """Возвращает сервис напоминаний поверх тестового репозитория."""
    return ReminderService(repo)


@pytest.fixture
def audit_service(repo: Repository) -> AuditService:
    """Возвращает сервис аудита поверх тестового репозитория."""
    return AuditService(repo)


@pytest.fixture
def payment_service(repo: Repository) -> PaymentService:
    """Возвращает сервис платежей поверх тестового репозитория."""
    return PaymentService(repo)


class TestTenantSettingsService:
    """Проверки чтения и записи настроек тенанта."""

    async def test_get_returns_snapshot(
        self, settings_service: TenantSettingsService, tenant_id: int
    ) -> None:
        """Возвращает снимок настроек существующего тенанта."""
        snapshot = await settings_service.get(tenant_id)

        assert snapshot is not None
        assert isinstance(snapshot, TenantSettings)
        assert snapshot.tenant_id == tenant_id
        assert snapshot.name

    async def test_get_unknown_returns_none(self, settings_service: TenantSettingsService) -> None:
        """Возвращает None для несуществующего тенанта."""
        assert await settings_service.get(999) is None

    async def test_get_lesson_price_falls_back_to_env(
        self, settings_service: TenantSettingsService, tenant_id: int
    ) -> None:
        """Подставляет цену из .env, если в БД она не задана."""
        price = await settings_service.get_lesson_price(tenant_id)

        assert price == settings.lesson_price

    async def test_get_lesson_price_uses_db_value(
        self, settings_service: TenantSettingsService, tenant_id: int
    ) -> None:
        """Берёт цену из БД, если она задана."""
        await settings_service.update(tenant_id, lesson_price=250.0)

        assert await settings_service.get_lesson_price(tenant_id) == 250.0

    async def test_get_lesson_price_unknown_tenant(
        self, settings_service: TenantSettingsService
    ) -> None:
        """Для неизвестного тенанта возвращает цену из .env."""
        assert await settings_service.get_lesson_price(999) == settings.lesson_price

    async def test_get_currency_falls_back_to_env(
        self, settings_service: TenantSettingsService, tenant_id: int
    ) -> None:
        """Подставляет валюту из .env, если в БД она не задана."""
        assert await settings_service.get_currency(tenant_id) == settings.currency

    async def test_get_currency_uses_db_value(
        self, settings_service: TenantSettingsService, tenant_id: int
    ) -> None:
        """Берёт валюту из БД, если она задана."""
        await settings_service.update(tenant_id, currency="usd")

        assert await settings_service.get_currency(tenant_id) == "USD"

    async def test_update_changes_fields(
        self, settings_service: TenantSettingsService, tenant_id: int
    ) -> None:
        """Обновляет все редактируемые поля."""
        snapshot = await settings_service.update(
            tenant_id,
            name="Шахматная школа",
            timezone="Europe/Berlin",
            lesson_price=180.0,
            currency="eur",
            payment_instructions="Перевод на карту",
        )

        assert snapshot is not None
        assert snapshot.name == "Шахматная школа"
        assert snapshot.timezone == "Europe/Berlin"
        assert snapshot.lesson_price == 180.0
        assert snapshot.currency == "EUR"
        assert snapshot.payment_instructions == "Перевод на карту"

    async def test_update_without_fields_returns_current(
        self, settings_service: TenantSettingsService, tenant_id: int
    ) -> None:
        """Без полей возвращает текущие настройки."""
        snapshot = await settings_service.update(tenant_id)

        assert snapshot is not None
        assert snapshot.tenant_id == tenant_id

    async def test_update_unknown_tenant_returns_none(
        self, settings_service: TenantSettingsService
    ) -> None:
        """Для неизвестного тенанта возвращает None."""
        assert await settings_service.update(999, lesson_price=100.0) is None

    async def test_update_ignores_unknown_fields(
        self, settings_service: TenantSettingsService, tenant_id: int
    ) -> None:
        """Игнорирует поля, которых нет в списке редактируемых."""
        snapshot = await settings_service.update(tenant_id, telegram_user_id=1, admin_token="x")

        assert snapshot is not None
        assert snapshot.tenant_id == tenant_id

    async def test_update_rejects_empty_name(
        self, settings_service: TenantSettingsService, tenant_id: int
    ) -> None:
        """Отклоняет пустое имя."""
        with pytest.raises(ValueError, match="Имя тренера"):
            await settings_service.update(tenant_id, name="   ")

    async def test_update_rejects_unknown_timezone(
        self, settings_service: TenantSettingsService, tenant_id: int
    ) -> None:
        """Отклоняет неизвестный часовой пояс."""
        with pytest.raises(ValueError, match="Неизвестный часовой пояс"):
            await settings_service.update(tenant_id, timezone="Mars/Olympus")

    async def test_update_rejects_non_positive_price(
        self, settings_service: TenantSettingsService, tenant_id: int
    ) -> None:
        """Отклоняет нулевую и отрицательную цену."""
        with pytest.raises(ValueError, match="Цена занятия"):
            await settings_service.update(tenant_id, lesson_price=0)

    async def test_update_rejects_huge_price(
        self, settings_service: TenantSettingsService, tenant_id: int
    ) -> None:
        """Отклоняет слишком большую цену."""
        with pytest.raises(ValueError, match="слишком велика"):
            await settings_service.update(tenant_id, lesson_price=MAX_LESSON_PRICE + 1)

    async def test_update_rejects_empty_currency(
        self, settings_service: TenantSettingsService, tenant_id: int
    ) -> None:
        """Отклоняет пустую валюту."""
        with pytest.raises(ValueError, match="Валюта"):
            await settings_service.update(tenant_id, currency="  ")

    async def test_update_rejects_long_currency(
        self, settings_service: TenantSettingsService, tenant_id: int
    ) -> None:
        """Отклоняет слишком длинный код валюты."""
        with pytest.raises(ValueError, match="слишком длинный"):
            await settings_service.update(tenant_id, currency="VERYLONGCURRENCY")

    async def test_update_clears_payment_instructions(
        self, settings_service: TenantSettingsService, tenant_id: int
    ) -> None:
        """Пустая строка инструкций сохраняется как None."""
        await settings_service.update(tenant_id, payment_instructions="Текст")
        snapshot = await settings_service.update(tenant_id, payment_instructions="   ")

        assert snapshot is not None
        assert snapshot.payment_instructions is None

    async def test_ensure_admin_token_creates_token(
        self, settings_service: TenantSettingsService, tenant_id: int
    ) -> None:
        """Создаёт секретный токен кабинета."""
        token = await settings_service.ensure_admin_token(tenant_id)

        assert token
        assert await settings_service.ensure_admin_token(tenant_id) == token

    async def test_ensure_admin_token_unknown_tenant(
        self, settings_service: TenantSettingsService
    ) -> None:
        """Для неизвестного тенанта возвращает None."""
        assert await settings_service.ensure_admin_token(999) is None

    async def test_snapshot_zone_returns_zoneinfo(
        self, settings_service: TenantSettingsService, tenant_id: int
    ) -> None:
        """Свойство zone возвращает ZoneInfo."""
        snapshot = await settings_service.get(tenant_id)

        assert snapshot is not None
        assert str(snapshot.zone) == snapshot.timezone

    async def test_snapshot_zone_falls_back_on_bad_timezone(
        self, settings_service: TenantSettingsService, tenant_id: int
    ) -> None:
        """При неизвестном поясе zone отдаёт пояс из .env."""
        snapshot = await settings_service.get(tenant_id)
        assert snapshot is not None

        broken = TenantSettings(
            tenant_id=snapshot.tenant_id,
            name=snapshot.name,
            timezone="Nowhere/Nothing",
            lesson_price=snapshot.lesson_price,
            currency=snapshot.currency,
            payment_instructions=None,
        )

        assert str(broken.zone) == settings.timezone


class TestReminderServiceHelpers:
    """Проверки вспомогательных функций сервиса напоминаний."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("08:00", (8, 0)), ("19:30", (19, 30)), ("07:05:00", (7, 5))],
    )
    def test_parse_time_valid(self, raw: str, expected: tuple[int, int]) -> None:
        """Разбирает корректные строки времени."""
        parsed = _parse_time(raw)
        assert parsed is not None
        assert (parsed.hour, parsed.minute) == expected

    @pytest.mark.parametrize("raw", [None, "", "abc", "24:00"])
    def test_parse_time_invalid(self, raw: str | None) -> None:
        """Возвращает None для некорректных строк."""
        assert _parse_time(raw) is None

    @pytest.mark.parametrize("kind", REMINDER_KINDS)
    def test_normalize_kind_valid(self, kind: str) -> None:
        """Принимает все допустимые виды правил."""
        assert _normalize_kind(kind.upper()) == kind

    def test_normalize_kind_invalid(self) -> None:
        """Отклоняет неизвестный вид правила."""
        with pytest.raises(ValueError, match="Неизвестный вид напоминания"):
            _normalize_kind("weekly")


class TestReminderServiceCrud:
    """Проверки CRUD правил напоминаний."""

    async def test_upsert_creates_rule(
        self, reminder_service: ReminderService, tenant_id: int
    ) -> None:
        """Создаёт правило, если его ещё нет."""
        rule = await reminder_service.upsert_rule(tenant_id, "day_before", time_of_day="20:00")

        assert rule.id is not None
        assert rule.kind == "day_before"
        assert rule.time_of_day == "20:00"
        assert rule.is_enabled is True

    async def test_upsert_updates_existing_rule(
        self, reminder_service: ReminderService, tenant_id: int
    ) -> None:
        """Обновляет существующее правило вместо создания нового."""
        first = await reminder_service.upsert_rule(tenant_id, "morning_of", time_of_day="07:00")
        second = await reminder_service.upsert_rule(tenant_id, "morning_of", time_of_day="09:00")

        assert first.id == second.id
        assert second.time_of_day == "09:00"
        assert len(await reminder_service.list_rules(tenant_id)) == 1

    async def test_upsert_rejects_bad_kind(
        self, reminder_service: ReminderService, tenant_id: int
    ) -> None:
        """Отклоняет неизвестный вид правила."""
        with pytest.raises(ValueError, match="Неизвестный вид напоминания"):
            await reminder_service.upsert_rule(tenant_id, "weekly")

    async def test_upsert_rejects_bad_time(
        self, reminder_service: ReminderService, tenant_id: int
    ) -> None:
        """Отклоняет некорректное время."""
        with pytest.raises(ValueError, match="Некорректное время"):
            await reminder_service.upsert_rule(tenant_id, "day_before", time_of_day="25:00")

    async def test_upsert_rejects_bad_channel(
        self, reminder_service: ReminderService, tenant_id: int
    ) -> None:
        """Отклоняет неизвестный канал доставки."""
        with pytest.raises(ValueError, match="Неизвестный канал"):
            await reminder_service.upsert_rule(tenant_id, "day_before", channel="sms")

    async def test_get_rule_returns_rule(
        self, reminder_service: ReminderService, tenant_id: int
    ) -> None:
        """Возвращает правило по виду."""
        await reminder_service.upsert_rule(tenant_id, "day_before", time_of_day="20:00")

        rule = await reminder_service.get_rule(tenant_id, "DAY_BEFORE")

        assert rule is not None
        assert rule.time_of_day == "20:00"

    async def test_get_rule_unknown_returns_none(
        self, reminder_service: ReminderService, tenant_id: int
    ) -> None:
        """Возвращает None, если правила нет."""
        assert await reminder_service.get_rule(tenant_id, "day_before") is None

    async def test_list_rules_only_enabled(
        self, reminder_service: ReminderService, tenant_id: int
    ) -> None:
        """Фильтрует выключенные правила."""
        await reminder_service.upsert_rule(tenant_id, "day_before", is_enabled=True)
        await reminder_service.upsert_rule(tenant_id, "morning_of", is_enabled=False)

        assert len(await reminder_service.list_rules(tenant_id)) == 2
        assert len(await reminder_service.list_rules(tenant_id, only_enabled=True)) == 1

    async def test_update_rule_changes_fields(
        self, reminder_service: ReminderService, tenant_id: int
    ) -> None:
        """Обновляет поля правила по идентификатору."""
        rule = await reminder_service.upsert_rule(tenant_id, "day_before", time_of_day="20:00")

        updated = await reminder_service.update_rule(
            tenant_id, rule.id, time_of_day="21:00", is_enabled=False
        )

        assert updated is not None
        assert updated.time_of_day == "21:00"
        assert updated.is_enabled is False

    async def test_update_rule_without_fields_returns_rule(
        self, reminder_service: ReminderService, tenant_id: int
    ) -> None:
        """Без полей возвращает правило без изменений."""
        rule = await reminder_service.upsert_rule(tenant_id, "day_before")

        updated = await reminder_service.update_rule(tenant_id, rule.id)

        assert updated is not None
        assert updated.id == rule.id

    async def test_update_rule_rejects_bad_time(
        self, reminder_service: ReminderService, tenant_id: int
    ) -> None:
        """Отклоняет некорректное время при обновлении."""
        rule = await reminder_service.upsert_rule(tenant_id, "day_before")

        with pytest.raises(ValueError, match="Некорректное время"):
            await reminder_service.update_rule(tenant_id, rule.id, time_of_day="99:99")

    async def test_update_rule_rejects_bad_channel(
        self, reminder_service: ReminderService, tenant_id: int
    ) -> None:
        """Отклоняет неизвестный канал при обновлении."""
        rule = await reminder_service.upsert_rule(tenant_id, "day_before")

        with pytest.raises(ValueError, match="Неизвестный канал"):
            await reminder_service.update_rule(tenant_id, rule.id, channel="pigeon")

    async def test_delete_rule(self, reminder_service: ReminderService, tenant_id: int) -> None:
        """Удаляет правило."""
        rule = await reminder_service.upsert_rule(tenant_id, "day_before")

        assert await reminder_service.delete_rule(tenant_id, rule.id) is True
        assert await reminder_service.list_rules(tenant_id) == []

    async def test_ensure_defaults_creates_three_rules(
        self, reminder_service: ReminderService, tenant_id: int
    ) -> None:
        """Создаёт набор правил по умолчанию."""
        rules = await reminder_service.ensure_defaults(tenant_id)

        assert {rule.kind for rule in rules} == set(REMINDER_KINDS)
        day_before = next(rule for rule in rules if rule.kind == "day_before")
        assert day_before.time_of_day == DEFAULT_TIME_OF_DAY["day_before"]

    async def test_ensure_defaults_is_idempotent(
        self, reminder_service: ReminderService, tenant_id: int
    ) -> None:
        """Повторный вызов не создаёт дубликатов."""
        await reminder_service.ensure_defaults(tenant_id)
        rules = await reminder_service.ensure_defaults(tenant_id)

        assert len(rules) == len(REMINDER_KINDS)

    async def test_ensure_defaults_keeps_custom_rule(
        self, reminder_service: ReminderService, tenant_id: int
    ) -> None:
        """Не перезаписывает уже настроенное правило."""
        await reminder_service.upsert_rule(tenant_id, "day_before", time_of_day="22:00")

        rules = await reminder_service.ensure_defaults(tenant_id)
        day_before = next(rule for rule in rules if rule.kind == "day_before")

        assert day_before.time_of_day == "22:00"


class TestReminderResolveSendAt:
    """Проверки расчёта момента отправки напоминания."""

    async def test_day_before_sends_evening_before(
        self, reminder_service: ReminderService, tenant_id: int
    ) -> None:
        """Правило day_before отправляется вечером накануне."""
        rule = await reminder_service.upsert_rule(tenant_id, "day_before", time_of_day="19:00")

        schedule = ReminderService.resolve_send_at(rule, MONDAY, "17:30")

        assert schedule is not None
        assert schedule.send_at == datetime(2026, 9, 13, 19, 0)
        assert schedule.target_date == MONDAY

    async def test_morning_of_sends_same_day(
        self, reminder_service: ReminderService, tenant_id: int
    ) -> None:
        """Правило morning_of отправляется утром в день занятия."""
        rule = await reminder_service.upsert_rule(tenant_id, "morning_of", time_of_day="08:00")

        schedule = ReminderService.resolve_send_at(rule, MONDAY, "17:30")

        assert schedule is not None
        assert schedule.send_at == datetime(2026, 9, 14, 8, 0)
        assert "17:30" in schedule.description

    async def test_disabled_rule_returns_none(
        self, reminder_service: ReminderService, tenant_id: int
    ) -> None:
        """Выключенное правило не даёт момента отправки."""
        rule = await reminder_service.upsert_rule(tenant_id, "day_before", is_enabled=False)

        assert ReminderService.resolve_send_at(rule, MONDAY, "17:30") is None

    async def test_describe_day_before(
        self, reminder_service: ReminderService, tenant_id: int
    ) -> None:
        """Описание правила day_before содержит время."""
        rule = await reminder_service.upsert_rule(tenant_id, "day_before", time_of_day="19:00")

        assert "19:00" in ReminderService.describe(rule)


class TestAuditService:
    """Проверки журнала действий тренера."""

    async def test_log_writes_entry(self, audit_service: AuditService, tenant_id: int) -> None:
        """Записывает произвольное действие."""
        entry = await audit_service.log(
            tenant_id, ACTION_STUDENT_CREATED, actor=ACTOR_TRAINER, payload={"name": "Миша"}
        )

        assert entry.id is not None
        assert entry.action == ACTION_STUDENT_CREATED
        assert entry.actor == ACTOR_TRAINER
        assert entry.payload_dict == {"name": "Миша"}

    async def test_log_defaults_to_trainer_actor(
        self, audit_service: AuditService, tenant_id: int
    ) -> None:
        """По умолчанию действие приписывается тренеру."""
        entry = await audit_service.log(tenant_id, ACTION_SETTINGS_UPDATED)

        assert entry.actor == ACTOR_TRAINER

    async def test_log_student_created(self, audit_service: AuditService, tenant_id: int) -> None:
        """Записывает создание ученика."""
        entry = await audit_service.log_student_created(tenant_id, 42, name="Миша")

        assert entry.action == ACTION_STUDENT_CREATED
        assert entry.payload_dict["entity_id"] == 42

    async def test_log_student_updated(self, audit_service: AuditService, tenant_id: int) -> None:
        """Записывает изменение ученика."""
        entry = await audit_service.log_student_updated(tenant_id, 42, changes={"name": "Миша"})

        assert entry.payload_dict["entity_id"] == 42
        assert entry.payload_dict["changes"] == {"name": "Миша"}

    async def test_log_student_deleted(self, audit_service: AuditService, tenant_id: int) -> None:
        """Записывает удаление ученика."""
        entry = await audit_service.log_student_deleted(tenant_id, 42, name="Миша")

        assert entry.payload_dict["name"] == "Миша"

    async def test_log_payment_registered(
        self, audit_service: AuditService, tenant_id: int
    ) -> None:
        """Записывает внесение оплаты."""
        entry = await audit_service.log_payment_registered(
            tenant_id, 42, amount=300.0, lessons_added=3
        )

        assert entry.payload_dict["entity_id"] == 42
        assert entry.payload_dict["amount"] == 300.0
        assert entry.payload_dict["lessons_added"] == 3

    async def test_log_settings_updated(self, audit_service: AuditService, tenant_id: int) -> None:
        """Записывает изменение настроек."""
        entry = await audit_service.log_settings_updated(tenant_id, changes={"lesson_price": 100.0})

        assert entry.action == ACTION_SETTINGS_UPDATED
        assert entry.payload_dict["changes"] == {"lesson_price": 100.0}

    async def test_log_lesson_created(self, audit_service: AuditService, tenant_id: int) -> None:
        """Записывает создание занятия."""
        entry = await audit_service.log_lesson_created(tenant_id, 7, entity_id=42)

        assert entry.payload_dict["lesson_id"] == 7
        assert entry.payload_dict["entity_id"] == 42

    async def test_log_lesson_deleted(self, audit_service: AuditService, tenant_id: int) -> None:
        """Записывает удаление занятия."""
        entry = await audit_service.log_lesson_deleted(tenant_id, 7, entity_id=42)

        assert entry.payload_dict["lesson_id"] == 7

    async def test_log_respects_bot_actor(
        self, audit_service: AuditService, tenant_id: int
    ) -> None:
        """Позволяет указать бота как источник действия."""
        entry = await audit_service.log(tenant_id, ACTION_STUDENT_CREATED, actor=ACTOR_BOT)

        assert entry.actor == ACTOR_BOT

    async def test_list_entries_newest_first(
        self, audit_service: AuditService, tenant_id: int
    ) -> None:
        """Возвращает записи от новых к старым."""
        await audit_service.log(tenant_id, ACTION_STUDENT_CREATED)
        await audit_service.log(tenant_id, ACTION_SETTINGS_UPDATED)

        entries = await audit_service.list_entries(tenant_id)

        assert [entry.action for entry in entries] == [
            ACTION_SETTINGS_UPDATED,
            ACTION_STUDENT_CREATED,
        ]

    async def test_list_entries_respects_limit(
        self, audit_service: AuditService, tenant_id: int
    ) -> None:
        """Ограничивает число записей."""
        for _ in range(5):
            await audit_service.log(tenant_id, ACTION_STUDENT_CREATED)

        assert len(await audit_service.list_entries(tenant_id, limit=2)) == 2

    async def test_list_entries_scoped_to_tenant(
        self, audit_service: AuditService, repo: Repository, tenant_id: int
    ) -> None:
        """Не показывает записи других тенантов."""
        other = await repo.tenants.create(telegram_user_id=999_001, name="Другой")
        await audit_service.log(tenant_id, ACTION_STUDENT_CREATED)
        await audit_service.log(other.id, ACTION_SETTINGS_UPDATED)

        entries = await audit_service.list_entries(tenant_id)

        assert len(entries) == 1
        assert entries[0].action == ACTION_STUDENT_CREATED

    async def test_describe_returns_label(
        self, audit_service: AuditService, tenant_id: int
    ) -> None:
        """Описание действия — человекочитаемая строка."""
        entry = await audit_service.log(tenant_id, ACTION_STUDENT_CREATED)

        assert AuditService.describe(entry)


class TestPaymentServiceDbPrice:
    """Проверки перехода PaymentService на цену и валюту из БД."""

    async def test_resolve_lesson_price_falls_back_to_env(
        self, payment_service: PaymentService, tenant_id: int
    ) -> None:
        """Без цены в БД берёт значение из .env."""
        assert await payment_service.resolve_lesson_price(tenant_id) == settings.lesson_price

    async def test_resolve_lesson_price_uses_db(
        self, payment_service: PaymentService, repo: Repository, tenant_id: int
    ) -> None:
        """Берёт цену из БД, если она задана."""
        await repo.tenants.update_settings(tenant_id, lesson_price=300.0)

        assert await payment_service.resolve_lesson_price(tenant_id) == 300.0

    async def test_resolve_lesson_price_unknown_tenant(
        self, payment_service: PaymentService
    ) -> None:
        """Для неизвестного тенанта возвращает цену из .env."""
        assert await payment_service.resolve_lesson_price(999) == settings.lesson_price

    async def test_resolve_lesson_price_prefers_entity_price(
        self, payment_service: PaymentService, repo: Repository, tenant_id: int
    ) -> None:
        """Индивидуальная цена ученика важнее цены тенанта."""
        await repo.tenants.update_settings(tenant_id, lesson_price=300.0)
        entity = await repo.entities.create(tenant_id=tenant_id, name="Аня", lesson_price=120.0)

        assert await payment_service.resolve_lesson_price(tenant_id, entity.id) == 120.0

    async def test_resolve_lesson_price_falls_back_to_tenant_when_entity_blank(
        self, payment_service: PaymentService, repo: Repository, tenant_id: int
    ) -> None:
        """Без индивидуальной цены берёт цену тенанта."""
        await repo.tenants.update_settings(tenant_id, lesson_price=300.0)
        entity = await repo.entities.create(tenant_id=tenant_id, name="Аня")

        assert await payment_service.resolve_lesson_price(tenant_id, entity.id) == 300.0

    async def test_resolve_lesson_price_entity_without_tenant_price(
        self, payment_service: PaymentService, repo: Repository, tenant_id: int
    ) -> None:
        """Без цены ученика и тенанта берёт цену из .env."""
        entity = await repo.entities.create(tenant_id=tenant_id, name="Аня")

        assert (
            await payment_service.resolve_lesson_price(tenant_id, entity.id)
            == settings.lesson_price
        )

    async def test_resolve_lesson_price_unknown_entity_falls_back(
        self, payment_service: PaymentService, repo: Repository, tenant_id: int
    ) -> None:
        """Для неизвестного ученика берёт цену тенанта."""
        await repo.tenants.update_settings(tenant_id, lesson_price=300.0)

        assert await payment_service.resolve_lesson_price(tenant_id, 999) == 300.0

    async def test_resolve_currency_falls_back_to_env(
        self, payment_service: PaymentService, tenant_id: int
    ) -> None:
        """Без валюты в БД берёт значение из .env."""
        assert await payment_service.resolve_currency(tenant_id) == settings.currency

    async def test_resolve_currency_uses_db(
        self, payment_service: PaymentService, repo: Repository, tenant_id: int
    ) -> None:
        """Берёт валюту из БД, если она задана."""
        await repo.tenants.update_settings(tenant_id, currency="USD")

        assert await payment_service.resolve_currency(tenant_id) == "USD"

    async def test_register_payment_infers_lessons_from_db_price(
        self, payment_service: PaymentService, repo: Repository, tenant_id: int
    ) -> None:
        """Считает занятия по цене из БД, а не из .env."""
        await repo.tenants.update_settings(tenant_id, lesson_price=100.0)
        entity = await repo.entities.create(tenant_id=tenant_id, name="Миша")

        payment = await payment_service.register_payment(tenant_id, entity.id, amount=300.0)

        assert payment.lessons_added == 3

    async def test_register_payment_uses_entity_price(
        self, payment_service: PaymentService, repo: Repository, tenant_id: int
    ) -> None:
        """Считает занятия по индивидуальной цене ученика, а не тенанта."""
        await repo.tenants.update_settings(tenant_id, lesson_price=100.0)
        entity = await repo.entities.create(tenant_id=tenant_id, name="Аня", lesson_price=150.0)

        payment = await payment_service.register_payment(tenant_id, entity.id, amount=300.0)

        assert payment.lessons_added == 2

    async def test_register_payment_uses_db_currency(
        self, payment_service: PaymentService, repo: Repository, tenant_id: int
    ) -> None:
        """Сохраняет платёж в валюте тенанта из БД."""
        await repo.tenants.update_settings(tenant_id, currency="USD")
        entity = await repo.entities.create(tenant_id=tenant_id, name="Миша")

        payment = await payment_service.register_payment(tenant_id, entity.id, lessons_added=2)

        assert payment.currency == "USD"

    async def test_register_payment_explicit_currency_wins(
        self, payment_service: PaymentService, repo: Repository, tenant_id: int
    ) -> None:
        """Явно переданная валюта имеет приоритет над настройками."""
        await repo.tenants.update_settings(tenant_id, currency="USD")
        entity = await repo.entities.create(tenant_id=tenant_id, name="Миша")

        payment = await payment_service.register_payment(
            tenant_id, entity.id, lessons_added=1, currency="EUR"
        )

        assert payment.currency == "EUR"

    async def test_register_payment_requires_amount_or_lessons(
        self, payment_service: PaymentService, repo: Repository, tenant_id: int
    ) -> None:
        """Без суммы и без количества занятий платёж не создаётся."""
        entity = await repo.entities.create(tenant_id=tenant_id, name="Миша")

        with pytest.raises(ValueError, match="сумм"):
            await payment_service.register_payment(tenant_id, entity.id)

    async def test_register_payment_without_infer_keeps_lessons_none(
        self, payment_service: PaymentService, repo: Repository, tenant_id: int
    ) -> None:
        """При infer_lessons=False количество занятий не вычисляется."""
        await repo.tenants.update_settings(tenant_id, lesson_price=100.0)
        entity = await repo.entities.create(tenant_id=tenant_id, name="Миша")

        payment = await payment_service.register_payment(
            tenant_id, entity.id, amount=300.0, infer_lessons=False
        )

        assert payment.lessons_added is None

    async def test_register_payment_explicit_lessons_win_over_price(
        self, payment_service: PaymentService, repo: Repository, tenant_id: int
    ) -> None:
        """Явно переданное количество занятий имеет приоритет над расчётом."""
        await repo.tenants.update_settings(tenant_id, lesson_price=100.0)
        entity = await repo.entities.create(tenant_id=tenant_id, name="Миша")

        payment = await payment_service.register_payment(
            tenant_id, entity.id, amount=300.0, lessons_added=5
        )

        assert payment.lessons_added == 5


class TestPaymentServiceMonthlyDebt:
    """Проверки расчёта долга группы за текущий календарный месяц."""

    async def test_monthly_debt_counts_conducted_lessons(
        self, payment_service: PaymentService, repo: Repository, tenant_id: int
    ) -> None:
        """Долг = проведённые занятия месяца × цена (без платежей)."""
        await repo.tenants.update_settings(tenant_id, lesson_price=100.0)
        group = await repo.entities.create(tenant_id=tenant_id, name="Группа", entity_type="group")
        today = date(2026, 9, 18)
        for day in (1, 8, 15):
            await repo.lessons.create(
                tenant_id=tenant_id,
                entity_id=group.id,
                lesson_date=date(2026, 9, day),
                topic="Занятие",
                attendance="Аня, Миша",
            )

        assert await payment_service.monthly_debt(tenant_id, group.id, today=today) == 300.0

    async def test_monthly_debt_ignores_unconducted_lessons(
        self, payment_service: PaymentService, repo: Repository, tenant_id: int
    ) -> None:
        """Занятия без «Примечания» не считаются проведёнными."""
        await repo.tenants.update_settings(tenant_id, lesson_price=100.0)
        group = await repo.entities.create(tenant_id=tenant_id, name="Группа", entity_type="group")
        today = date(2026, 9, 18)
        await repo.lessons.create(
            tenant_id=tenant_id,
            entity_id=group.id,
            lesson_date=date(2026, 9, 10),
            topic="Проведено",
            attendance="Аня",
        )
        await repo.lessons.create(
            tenant_id=tenant_id,
            entity_id=group.id,
            lesson_date=date(2026, 9, 11),
            topic="Не проведено",
            attendance=None,
        )

        assert await payment_service.monthly_debt(tenant_id, group.id, today=today) == 100.0

    async def test_monthly_debt_ignores_other_months(
        self, payment_service: PaymentService, repo: Repository, tenant_id: int
    ) -> None:
        """Занятия прошлого месяца не попадают в долг текущего."""
        await repo.tenants.update_settings(tenant_id, lesson_price=100.0)
        group = await repo.entities.create(tenant_id=tenant_id, name="Группа", entity_type="group")
        today = date(2026, 9, 18)
        await repo.lessons.create(
            tenant_id=tenant_id,
            entity_id=group.id,
            lesson_date=date(2026, 8, 31),
            topic="Август",
            attendance="Аня",
        )
        await repo.lessons.create(
            tenant_id=tenant_id,
            entity_id=group.id,
            lesson_date=date(2026, 9, 1),
            topic="Сентябрь",
            attendance="Аня",
        )

        assert await payment_service.monthly_debt(tenant_id, group.id, today=today) == 100.0

    async def test_monthly_debt_subtracts_payments(
        self, payment_service: PaymentService, repo: Repository, tenant_id: int
    ) -> None:
        """Платежи текущего месяца уменьшают долг."""
        await repo.tenants.update_settings(tenant_id, lesson_price=100.0)
        group = await repo.entities.create(tenant_id=tenant_id, name="Группа", entity_type="group")
        today = date(2026, 9, 18)
        for day in (1, 8, 15, 22):
            await repo.lessons.create(
                tenant_id=tenant_id,
                entity_id=group.id,
                lesson_date=date(2026, 9, day),
                topic="Занятие",
                attendance="Аня",
            )
        await repo.payments.create(
            tenant_id=tenant_id,
            entity_id=group.id,
            amount=250.0,
            payment_date=date(2026, 9, 5),
        )

        assert await payment_service.monthly_debt(tenant_id, group.id, today=today) == 150.0

    async def test_monthly_debt_ignores_payments_of_other_months(
        self, payment_service: PaymentService, repo: Repository, tenant_id: int
    ) -> None:
        """Платёж прошлого месяца не уменьшает долг текущего."""
        await repo.tenants.update_settings(tenant_id, lesson_price=100.0)
        group = await repo.entities.create(tenant_id=tenant_id, name="Группа", entity_type="group")
        today = date(2026, 9, 18)
        await repo.lessons.create(
            tenant_id=tenant_id,
            entity_id=group.id,
            lesson_date=date(2026, 9, 10),
            topic="Занятие",
            attendance="Аня",
        )
        await repo.payments.create(
            tenant_id=tenant_id,
            entity_id=group.id,
            amount=500.0,
            payment_date=date(2026, 8, 20),
        )

        assert await payment_service.monthly_debt(tenant_id, group.id, today=today) == 100.0

    async def test_monthly_debt_uses_entity_price(
        self, payment_service: PaymentService, repo: Repository, tenant_id: int
    ) -> None:
        """Индивидуальная цена группы важнее цены тенанта."""
        await repo.tenants.update_settings(tenant_id, lesson_price=100.0)
        group = await repo.entities.create(
            tenant_id=tenant_id,
            name="Группа",
            entity_type="group",
            lesson_price=250.0,
        )
        today = date(2026, 9, 18)
        for day in (1, 8):
            await repo.lessons.create(
                tenant_id=tenant_id,
                entity_id=group.id,
                lesson_date=date(2026, 9, day),
                topic="Занятие",
                attendance="Аня",
            )

        assert await payment_service.monthly_debt(tenant_id, group.id, today=today) == 500.0

    async def test_monthly_debt_can_be_negative_when_overpaid(
        self, payment_service: PaymentService, repo: Repository, tenant_id: int
    ) -> None:
        """Переплата даёт отрицательный долг (аванс)."""
        await repo.tenants.update_settings(tenant_id, lesson_price=100.0)
        group = await repo.entities.create(tenant_id=tenant_id, name="Группа", entity_type="group")
        today = date(2026, 9, 18)
        await repo.lessons.create(
            tenant_id=tenant_id,
            entity_id=group.id,
            lesson_date=date(2026, 9, 10),
            topic="Занятие",
            attendance="Аня",
        )
        await repo.payments.create(
            tenant_id=tenant_id,
            entity_id=group.id,
            amount=300.0,
            payment_date=date(2026, 9, 12),
        )

        assert await payment_service.monthly_debt(tenant_id, group.id, today=today) == -200.0

    async def test_monthly_summary_returns_breakdown(
        self, payment_service: PaymentService, repo: Repository, tenant_id: int
    ) -> None:
        """Разбор долга содержит проведённые занятия, цену, начислено и оплачено."""
        await repo.tenants.update_settings(tenant_id, lesson_price=100.0)
        group = await repo.entities.create(tenant_id=tenant_id, name="Группа", entity_type="group")
        today = date(2026, 9, 18)
        for day in (1, 8, 15):
            await repo.lessons.create(
                tenant_id=tenant_id,
                entity_id=group.id,
                lesson_date=date(2026, 9, day),
                topic="Занятие",
                attendance="Аня",
            )
        await repo.payments.create(
            tenant_id=tenant_id,
            entity_id=group.id,
            amount=120.0,
            payment_date=date(2026, 9, 5),
        )

        summary = await payment_service.monthly_summary(tenant_id, group.id, today=today)

        assert summary["conducted"] == 3
        assert summary["price"] == 100.0
        assert summary["charged"] == 300.0
        assert summary["paid"] == 120.0
        assert summary["debt"] == 180.0

    async def test_monthly_summary_empty_month(
        self, payment_service: PaymentService, repo: Repository, tenant_id: int
    ) -> None:
        """Без занятий и платежей долг равен нулю."""
        await repo.tenants.update_settings(tenant_id, lesson_price=100.0)
        group = await repo.entities.create(tenant_id=tenant_id, name="Группа", entity_type="group")

        summary = await payment_service.monthly_summary(
            tenant_id, group.id, today=date(2026, 9, 18)
        )

        assert summary["conducted"] == 0
        assert summary["charged"] == 0.0
        assert summary["paid"] == 0.0
        assert summary["debt"] == 0.0

    async def test_monthly_debt_december_rolls_over(
        self, payment_service: PaymentService, repo: Repository, tenant_id: int
    ) -> None:
        """В декабре конец месяца вычисляется корректно (переход года)."""
        await repo.tenants.update_settings(tenant_id, lesson_price=100.0)
        group = await repo.entities.create(tenant_id=tenant_id, name="Группа", entity_type="group")
        today = date(2026, 12, 15)
        await repo.lessons.create(
            tenant_id=tenant_id,
            entity_id=group.id,
            lesson_date=date(2026, 12, 31),
            topic="Последний день",
            attendance="Аня",
        )
        await repo.lessons.create(
            tenant_id=tenant_id,
            entity_id=group.id,
            lesson_date=date(2027, 1, 1),
            topic="Новый год",
            attendance="Аня",
        )

        assert await payment_service.monthly_debt(tenant_id, group.id, today=today) == 100.0


class TestPaymentServiceMonthlyOverview:
    """Проверки пакетного расчёта месячных показателей для списка учеников."""

    async def test_overview_returns_entry_for_each_entity(
        self, payment_service: PaymentService, repo: Repository, tenant_id: int
    ) -> None:
        """В результате есть запись для каждой переданной сущности."""
        student = await repo.entities.create(
            tenant_id=tenant_id, name="Ученик", entity_type="student"
        )
        group = await repo.entities.create(tenant_id=tenant_id, name="Группа", entity_type="group")

        overview = await payment_service.monthly_overview(
            tenant_id, [student, group], today=date(2026, 9, 15)
        )

        assert set(overview) == {student.id, group.id}

    async def test_overview_counts_conducted_and_paid(
        self, payment_service: PaymentService, repo: Repository, tenant_id: int
    ) -> None:
        """Проведённые занятия и платежи месяца попадают в разбор."""
        await repo.tenants.update_settings(tenant_id, lesson_price=100.0)
        group = await repo.entities.create(tenant_id=tenant_id, name="Группа", entity_type="group")
        await repo.lessons.create(
            tenant_id=tenant_id,
            entity_id=group.id,
            lesson_date=date(2026, 9, 10),
            topic="Занятие",
            attendance="Аня",
        )
        await repo.lessons.create(
            tenant_id=tenant_id,
            entity_id=group.id,
            lesson_date=date(2026, 9, 12),
            topic="Занятие",
            attendance="Аня",
        )
        await repo.payments.create(
            tenant_id=tenant_id,
            entity_id=group.id,
            payment_date=date(2026, 9, 13),
            amount=150.0,
            currency="ILS",
            lessons_added=1,
        )

        overview = await payment_service.monthly_overview(
            tenant_id, [group], today=date(2026, 9, 15)
        )
        entry = overview[group.id]

        assert entry["conducted"] == 2
        assert entry["charged"] == 200.0
        assert entry["paid"] == 150.0
        assert entry["debt"] == 50.0

    async def test_overview_uses_entity_price(
        self, payment_service: PaymentService, repo: Repository, tenant_id: int
    ) -> None:
        """Индивидуальная цена ученика имеет приоритет над ценой тенанта."""
        await repo.tenants.update_settings(tenant_id, lesson_price=100.0)
        student = await repo.entities.create(
            tenant_id=tenant_id,
            name="Особый",
            entity_type="student",
            lesson_price=250.0,
        )
        await repo.lessons.create(
            tenant_id=tenant_id,
            entity_id=student.id,
            lesson_date=date(2026, 9, 10),
            topic="Занятие",
            attendance="был",
        )

        overview = await payment_service.monthly_overview(
            tenant_id, [student], today=date(2026, 9, 15)
        )

        assert overview[student.id]["price"] == 250.0
        assert overview[student.id]["charged"] == 250.0

    async def test_overview_ignores_other_months(
        self, payment_service: PaymentService, repo: Repository, tenant_id: int
    ) -> None:
        """Занятия и платежи других месяцев не учитываются."""
        await repo.tenants.update_settings(tenant_id, lesson_price=100.0)
        group = await repo.entities.create(tenant_id=tenant_id, name="Группа", entity_type="group")
        await repo.lessons.create(
            tenant_id=tenant_id,
            entity_id=group.id,
            lesson_date=date(2026, 8, 20),
            topic="Прошлый месяц",
            attendance="Аня",
        )
        await repo.payments.create(
            tenant_id=tenant_id,
            entity_id=group.id,
            payment_date=date(2026, 8, 21),
            amount=500.0,
            currency="ILS",
            lessons_added=5,
        )

        overview = await payment_service.monthly_overview(
            tenant_id, [group], today=date(2026, 9, 15)
        )

        assert overview[group.id]["conducted"] == 0
        assert overview[group.id]["paid"] == 0.0
        assert overview[group.id]["debt"] == 0.0

    async def test_overview_empty_list(
        self, payment_service: PaymentService, tenant_id: int
    ) -> None:
        """Пустой список сущностей даёт пустой результат."""
        overview = await payment_service.monthly_overview(tenant_id, [])

        assert overview == {}

    async def test_overview_debt_can_be_negative(
        self, payment_service: PaymentService, repo: Repository, tenant_id: int
    ) -> None:
        """Переплата даёт отрицательный долг (аванс)."""
        await repo.tenants.update_settings(tenant_id, lesson_price=100.0)
        group = await repo.entities.create(tenant_id=tenant_id, name="Группа", entity_type="group")
        await repo.payments.create(
            tenant_id=tenant_id,
            entity_id=group.id,
            payment_date=date(2026, 9, 5),
            amount=300.0,
            currency="ILS",
            lessons_added=3,
        )

        overview = await payment_service.monthly_overview(
            tenant_id, [group], today=date(2026, 9, 15)
        )

        assert overview[group.id]["debt"] == -300.0
