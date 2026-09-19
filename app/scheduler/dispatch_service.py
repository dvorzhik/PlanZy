"""Сервис рассылки напоминаний по правилам ``ReminderRule``.

Работает в двух режимах:

* **Утренняя рассылка** (``run``) — собирает занятия на текущий день из
  регулярных слотов ``ScheduleSlot`` с учётом разовых отклонений
  ``ScheduleException`` (через :class:`ScheduleService`) и отправляет тренеру
  напоминания по включённому правилу ``morning_of``.
* **Вечерняя рассылка** (``run_day_before``) — собирает занятия на завтра и
  отправляет напоминания по правилу ``day_before``.

Защита от дублирования — по журналу ``DispatchLog`` в БД: комбинация
(тенант, ученик, тип правила, дата рассылки) отправляется ровно один раз.

Сервис не знает о транспорте напрямую: он получает ``Bot`` и ``Repository``
извне, что упрощает тестирование.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from app.core.config import settings
from app.core.logging import get_logger
from app.db.models import Entity, ReminderRule, Tenant
from app.db.repository import Repository
from app.services.schedule_service import PlannedLesson, ScheduleService
from app.services.whatsapp_service import WhatsAppLinkService

if TYPE_CHECKING:
    from zoneinfo import ZoneInfo

logger = get_logger(__name__)

# Коды дней недели в порядке, совпадающем с ``date.weekday()``.
WEEKDAY_CODES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

# Русские названия дней недели для заголовка рассылки.
WEEKDAY_LABELS = (
    "понедельник",
    "вторник",
    "среда",
    "четверг",
    "пятница",
    "суббота",
    "воскресенье",
)

# Виды правил, обрабатываемые утренней рассылкой.
MORNING_KINDS = ("morning_of",)

# Виды правил, обрабатываемые вечерней рассылкой (накануне занятия).
DAY_BEFORE_KINDS = ("day_before",)


@dataclass(slots=True)
class DispatchResult:
    """Итог одной рассылки.

    Attributes:
        dispatch_date: Дата рассылки.
        tenants_processed: Сколько тенантов обработано.
        entities_total: Сколько занятий попало в выборку.
        messages_sent: Сколько сообщений успешно отправлено.
        skipped_no_phone: Сколько занятий пропущено из-за отсутствия телефона.
        skipped_duplicate: Сколько напоминаний пропущено как уже отправленные.
        errors: Тексты ошибок отправки.
    """

    dispatch_date: date
    tenants_processed: int = 0
    entities_total: int = 0
    messages_sent: int = 0
    skipped_no_phone: int = 0
    skipped_duplicate: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def has_errors(self) -> bool:
        """True, если при рассылке были ошибки."""
        return bool(self.errors)


class MorningDispatchService:
    """Формирует и отправляет напоминания тренерам по правилам."""

    def __init__(
        self,
        repo: Repository,
        bot: Bot,
        *,
        whatsapp: WhatsAppLinkService | None = None,
        timezone: ZoneInfo | None = None,
    ) -> None:
        """Инициализирует сервис рассылки.

        Args:
            repo: Фасад репозиториев.
            bot: Экземпляр aiogram-бота для отправки сообщений тренеру.
            whatsapp: Сервис ссылок WhatsApp (по умолчанию создаётся сам).
            timezone: Часовой пояс для определения «сегодня».
        """
        self._repo = repo
        self._bot = bot
        self._whatsapp = whatsapp or WhatsAppLinkService()
        self._timezone = timezone or self._resolve_timezone()
        self._schedule = ScheduleService(repo)

    @staticmethod
    def _resolve_timezone() -> ZoneInfo:
        """Возвращает часовой пояс из настроек.

        Returns:
            ZoneInfo для настроенной таймзоны; UTC при ошибке.
        """
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            return ZoneInfo(settings.timezone)
        except ZoneInfoNotFoundError:
            logger.warning(
                "Часовой пояс %r не найден, использую UTC",
                settings.timezone,
            )
            return ZoneInfo("UTC")

    def today(self) -> date:
        """Возвращает текущую дату в настроенном часовом поясе.

        Returns:
            Сегодняшняя дата.
        """
        return datetime.now(self._timezone).date()

    # ------------------------------------------------------------------
    # Публичные точки входа
    # ------------------------------------------------------------------

    async def run(self, *, dispatch_date: date | None = None) -> DispatchResult:
        """Выполняет утреннюю рассылку по всем активным тенантам.

        Обрабатывает правило ``morning_of`` для занятий на указанную дату.

        Args:
            dispatch_date: Дата рассылки. По умолчанию — сегодня.

        Returns:
            Итог рассылки.
        """
        target_date = dispatch_date or self.today()
        return await self._run_for_date(target_date, kinds=MORNING_KINDS)

    async def run_day_before(self, *, dispatch_date: date | None = None) -> DispatchResult:
        """Выполняет вечернюю рассылку накануне занятий.

        Обрабатывает правило ``day_before`` для занятий на следующий день.

        Args:
            dispatch_date: Дата рассылки (день, в который отправляем).
                По умолчанию — сегодня; занятия берутся на завтра.

        Returns:
            Итог рассылки.
        """
        base_date = dispatch_date or self.today()
        target_date = base_date + timedelta(days=1)
        return await self._run_for_date(target_date, kinds=DAY_BEFORE_KINDS)

    # ------------------------------------------------------------------
    # Внутренняя логика
    # ------------------------------------------------------------------

    async def _run_for_date(
        self,
        target_date: date,
        *,
        kinds: tuple[str, ...],
    ) -> DispatchResult:
        """Выполняет рассылку по всем тенантам на указанную дату.

        Args:
            target_date: Дата занятий, по которым шлём напоминания.
            kinds: Виды правил, которые нужно обработать.

        Returns:
            Итог рассылки.
        """
        weekday_code = WEEKDAY_CODES[target_date.weekday()]
        result = DispatchResult(dispatch_date=target_date)

        tenants = await self._repo.tenants.list_active()
        logger.info(
            "Рассылка %s (%s): тенантов %s, правила %s",
            target_date.isoformat(),
            weekday_code,
            len(tenants),
            ", ".join(kinds),
        )

        for tenant in tenants:
            result.tenants_processed += 1
            await self._dispatch_for_tenant(tenant, target_date, kinds, result)

        logger.info(
            "Рассылка завершена: отправлено %s, без телефона %s, дублей %s, ошибок %s",
            result.messages_sent,
            result.skipped_no_phone,
            result.skipped_duplicate,
            len(result.errors),
        )
        return result

    async def _dispatch_for_tenant(
        self,
        tenant: Tenant,
        target_date: date,
        kinds: tuple[str, ...],
        result: DispatchResult,
    ) -> None:
        """Отправляет напоминания по одному тенанту.

        Args:
            tenant: Тренер-получатель.
            target_date: Дата занятий.
            kinds: Виды правил для обработки.
            result: Аккумулятор итогов.
        """
        rules = await self._enabled_rules(tenant.id, kinds)
        if not rules:
            logger.debug("Тенант %s: включённых правил нет", tenant.id)
            return

        lessons = await self._schedule.get_day_schedule(tenant.id, target_date)
        if not lessons:
            logger.debug("Тенант %s: занятий на %s нет", tenant.id, target_date.isoformat())
            return

        result.entities_total += len(lessons)

        header = self._build_header(target_date, len(lessons))
        try:
            await self._bot.send_message(tenant.telegram_user_id, header)
        except TelegramAPIError as exc:
            message = f"Заголовок тенанту {tenant.id}: {exc}"
            logger.error(message)
            result.errors.append(message)
            return

        for lesson in lessons:
            await self._send_lesson_reminders(tenant, lesson, rules, target_date, result)

    async def _enabled_rules(
        self,
        tenant_id: int,
        kinds: tuple[str, ...],
    ) -> list[ReminderRule]:
        """Возвращает включённые правила указанных видов.

        Args:
            tenant_id: Идентификатор тенанта.
            kinds: Виды правил.

        Returns:
            Список включённых правил.
        """
        rules = await self._repo.reminders.list_by_tenant(tenant_id, only_enabled=True)
        return [rule for rule in rules if rule.kind in kinds]

    async def _send_lesson_reminders(
        self,
        tenant: Tenant,
        lesson: PlannedLesson,
        rules: list[ReminderRule],
        target_date: date,
        result: DispatchResult,
    ) -> None:
        """Отправляет напоминания по одному занятию для каждого правила.

        Args:
            tenant: Тренер-получатель.
            lesson: Занятие из расписания.
            rules: Включённые правила тенанта.
            target_date: Дата занятия.
            result: Аккумулятор итогов.
        """
        entity = await self._repo.entities.get_by_id(tenant.id, lesson.entity_id)
        if entity is None:
            logger.debug("Ученик %s не найден — пропуск", lesson.entity_id)
            return

        for rule in rules:
            await self._send_rule_reminder(tenant, entity, lesson, rule, target_date, result)

    async def _send_rule_reminder(
        self,
        tenant: Tenant,
        entity: Entity,
        lesson: PlannedLesson,
        rule: ReminderRule,
        target_date: date,
        result: DispatchResult,
    ) -> None:
        """Отправляет одно напоминание по правилу с защитой от дублей.

        Args:
            tenant: Тренер-получатель.
            entity: Ученик или группа.
            lesson: Занятие из расписания.
            rule: Правило напоминания.
            target_date: Дата занятия.
            result: Аккумулятор итогов.
        """
        if not entity.parent_phone:
            result.skipped_no_phone += 1
            logger.debug("Ученик %s без телефона родителя — пропуск", entity.id)
            return

        already_sent = await self._repo.dispatch_log.was_sent(
            tenant.id,
            rule.kind,
            target_date,
            entity.id,
        )
        if already_sent:
            result.skipped_duplicate += 1
            logger.debug(
                "Напоминание %s для %s уже отправлено — пропуск",
                rule.kind,
                entity.name,
            )
            return

        message = self._whatsapp.build_reminder(entity, lesson_date=target_date)
        text = self._build_entity_text(entity, lesson, rule, message.text)

        keyboard = self._build_keyboard(entity, message.url)
        try:
            await self._bot.send_message(
                tenant.telegram_user_id,
                text,
                reply_markup=keyboard,
            )
        except TelegramAPIError as exc:
            error = f"Напоминание {entity.name!r}: {exc}"
            logger.error(error)
            result.errors.append(error)
            return

        await self._repo.dispatch_log.create(
            tenant.id,
            rule.kind,
            target_date,
            entity.id,
        )
        result.messages_sent += 1

    def _build_header(self, target_date: date, count: int) -> str:
        """Формирует заголовок рассылки.

        Args:
            target_date: Дата занятий.
            count: Количество занятий.

        Returns:
            HTML-текст заголовка.
        """
        weekday = WEEKDAY_LABELS[target_date.weekday()]
        return (
            f"☀️ <b>Доброе утро!</b>\n"
            f"Сегодня {weekday}, {target_date.strftime('%d.%m.%Y')}.\n"
            f"Занятий по расписанию: <b>{count}</b>.\n\n"
            "Напоминания родителям — по кнопкам ниже."
        )

    @staticmethod
    def _build_entity_text(
        entity: Entity,
        lesson: PlannedLesson,
        rule: ReminderRule,
        reminder_text: str,
    ) -> str:
        """Формирует текст сообщения по ученику.

        Args:
            entity: Ученик или группа.
            lesson: Занятие из расписания.
            rule: Правило, по которому отправляется напоминание.
            reminder_text: Текст напоминания для родителя.

        Returns:
            HTML-текст для Telegram.
        """
        time_line = f"🕒 Время: <b>{lesson.start_time}</b>"
        if lesson.kind == "moved":
            time_line += " (перенесено)"
        elif lesson.kind == "extra":
            time_line += " (доп. занятие)"

        return f"👤 <b>{entity.name}</b>\n{time_line}\n\n<i>{reminder_text}</i>"

    @staticmethod
    def _build_keyboard(entity: Entity, whatsapp_url: str):
        """Собирает inline-клавиатуру с кнопкой WhatsApp.

        Args:
            entity: Ученик или группа.
            whatsapp_url: Ссылка wa.me с текстом напоминания.

        Returns:
            Inline-клавиатура или None, если ссылка недоступна.
        """
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

        if not whatsapp_url:
            return None
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=f"📲 Написать родителю ({entity.name})",
                        url=whatsapp_url,
                    )
                ]
            ]
        )
