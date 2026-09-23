"""Сервис рассылки напоминаний по правилам ``ReminderRule``.

Работает по принципу «доставка по расписанию правила»:

* планировщик вызывает :meth:`MorningDispatchService.run_due` каждые несколько
  минут;
* сервис берёт включённые правила тенантов (``reminder_rules``) и для каждого
  правила вычисляет момент отправки через
  :meth:`ReminderService.resolve_send_at`;
* напоминание уходит, если момент отправки уже наступил и прошло не больше
  ``SEND_WINDOW`` (защита от отправки «просроченных» напоминаний после
  длительного простоя).

Правило ``morning_of`` относится к занятиям **сегодня**, правило
``day_before`` — к занятиям **завтра**.

Защита от дублирования — по журналу ``DispatchLog`` в БД: комбинация
(тенант, ученик, тип правила, дата занятия) отправляется ровно один раз.

Часовой пояс берётся у тенанта (``tenants.timezone``) с откатом на
``settings.timezone``.

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
from app.services.reminder_service import ReminderService
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

# Виды правил, для которых правило вообще может сработать.
ALL_REMINDER_KINDS = MORNING_KINDS + DAY_BEFORE_KINDS

# Смещение даты занятия относительно дня отправки для каждого вида правила.
# ``morning_of`` — занятие сегодня, ``day_before`` — занятие завтра.
KIND_DAY_OFFSET = {"morning_of": 0, "day_before": 1}

# Окно, в течение которого напоминание ещё считается актуальным.
# Если сервер лежал дольше, «просроченное» напоминание не отправляется.
SEND_WINDOW = timedelta(hours=1)


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
        skipped_not_due: Сколько напоминаний пропущено как ещё не наступившие.
        errors: Тексты ошибок отправки.
    """

    dispatch_date: date
    tenants_processed: int = 0
    entities_total: int = 0
    messages_sent: int = 0
    skipped_no_phone: int = 0
    skipped_duplicate: int = 0
    skipped_not_due: int = 0
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
        self._timezone = self._resolve_timezone()
        self._schedule = ScheduleService(repo)
        self._reminders = ReminderService(repo)

    @staticmethod
    def _resolve_timezone() -> ZoneInfo:
        """Возвращает часовой пояс по умолчанию из настроек.

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

    def _timezone_for(self, tenant: Tenant) -> ZoneInfo:
        """Возвращает часовой пояс конкретного тенанта.

        Часовой пояс персональный: тренер задаёт его в кабинете, и от него
        зависит, что считается «сегодня» и когда наступает время правила.

        Args:
            tenant: Тренер.

        Returns:
            Зона тенанта либо зона из настроек, если её не удалось разобрать.
        """
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        name = (tenant.timezone or "").strip()
        if not name:
            return self._timezone
        try:
            return ZoneInfo(name)
        except ZoneInfoNotFoundError:
            logger.warning(
                "Тенант %s: часовой пояс %r не найден, использую %s",
                tenant.id,
                name,
                settings.timezone,
            )
            return self._timezone

    def today(self, tenant: Tenant | None = None) -> date:
        """Возвращает текущую дату в часовом поясе тенанта.

        Args:
            tenant: Тренер (если известен).

        Returns:
            Сегодняшняя дата.
        """
        zone = self._timezone_for(tenant) if tenant is not None else self._timezone
        return datetime.now(zone).date()

    # ------------------------------------------------------------------
    # Публичные точки входа
    # ------------------------------------------------------------------

    async def run_due(self, *, now: datetime | None = None) -> DispatchResult:
        """Отправляет напоминания, время которых уже наступило.

        Единая точка входа для планировщика: правила берутся из БД, а момент
        отправки вычисляется как «дата занятия + ``time_of_day`` правила».
        Повторные вызовы безопасны — журнал ``DispatchLog`` не даст отправить
        одно и то же напоминание дважды.

        Args:
            now: Текущий момент (для тестов). По умолчанию — системное время.

        Returns:
            Итог рассылки.
        """
        moment = now or datetime.now(self._timezone)
        result = DispatchResult(dispatch_date=moment.date())

        tenants = await self._repo.tenants.list_active()
        logger.info(
            "Проверка правил напоминаний на %s: тенантов %s",
            moment.isoformat(),
            len(tenants),
        )

        for tenant in tenants:
            result.tenants_processed += 1
            await self._dispatch_due_for_tenant(tenant, moment, result)

        logger.info(
            "Проверка завершена: отправлено %s, без телефона %s, дублей %s, "
            "не наступило %s, ошибок %s",
            result.messages_sent,
            result.skipped_no_phone,
            result.skipped_duplicate,
            result.skipped_not_due,
            len(result.errors),
        )
        return result

    # ------------------------------------------------------------------
    # Внутренняя логика
    # ------------------------------------------------------------------

    async def _dispatch_due_for_tenant(
        self,
        tenant: Tenant,
        now: datetime,
        result: DispatchResult,
    ) -> None:
        """Отправляет созревшие напоминания одного тенанта.

        Для каждого включённого правила собираются занятия соответствующего
        дня (``morning_of`` — сегодня, ``day_before`` — завтра), после чего
        напоминание отправляется только если время правила уже наступило.

        Args:
            tenant: Тренер-получатель.
            now: Текущий момент в часовом поясе тенанта.
            result: Аккумулятор итогов.
        """
        rules = await self._enabled_rules(tenant.id, ALL_REMINDER_KINDS)
        if not rules:
            logger.debug("Тенант %s: включённых правил нет", tenant.id)
            return

        timezone = self._timezone_for(tenant)
        moment = now.astimezone(timezone)
        today = moment.date()

        # Занятия собираем один раз на дату: правило day_before смотрит на
        # завтра, правило morning_of — на сегодня.
        lessons_by_date: dict[date, list[PlannedLesson]] = {}
        for rule in rules:
            target_date = today + timedelta(days=KIND_DAY_OFFSET.get(rule.kind, 0))
            if target_date in lessons_by_date:
                continue
            lessons_by_date[target_date] = await self._schedule.get_day_schedule(
                tenant.id, target_date
            )

        for rule in rules:
            target_date = today + timedelta(days=KIND_DAY_OFFSET.get(rule.kind, 0))
            lessons = lessons_by_date.get(target_date) or []
            if not lessons:
                logger.debug(
                    "Тенант %s: занятий на %s нет",
                    tenant.id,
                    target_date.isoformat(),
                )
                continue

            due_lessons = [lesson for lesson in lessons if self._is_due(rule, lesson, moment)]
            if not due_lessons:
                result.skipped_not_due += 1
                continue

            result.entities_total += len(due_lessons)

            header = self._build_header(target_date, len(due_lessons))
            try:
                await self._bot.send_message(tenant.telegram_user_id, header)
            except TelegramAPIError as exc:
                message = f"Заголовок тенанту {tenant.id}: {exc}"
                logger.error(message)
                result.errors.append(message)
                continue

            for lesson in due_lessons:
                await self._send_rule_reminder(tenant, lesson, rule, target_date, moment, result)

    def _is_due(self, rule: ReminderRule, lesson: PlannedLesson, now: datetime) -> bool:
        """Проверяет, наступил ли момент отправки правила для занятия.

        Args:
            rule: Правило напоминания.
            lesson: Занятие из расписания.
            now: Текущий момент в часовом поясе тенанта.

        Returns:
            ``True``, если напоминание пора отправлять.
        """
        schedule = self._reminders.resolve_send_at(
            rule,
            lesson.lesson_date,
            lesson.start_time,
        )
        if schedule is None:
            return False

        send_at = schedule.send_at.replace(tzinfo=now.tzinfo)
        if now < send_at:
            return False
        if now >= send_at + SEND_WINDOW:
            logger.debug(
                "Правило %s для занятия %s: время %s прошло — пропуск",
                rule.kind,
                lesson.lesson_date.isoformat(),
                send_at.isoformat(),
            )
            return False
        return True

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

    async def _send_rule_reminder(
        self,
        tenant: Tenant,
        lesson: PlannedLesson,
        rule: ReminderRule,
        target_date: date,
        now: datetime,
        result: DispatchResult,
    ) -> None:
        """Отправляет одно напоминание по правилу с защитой от дублей.

        Args:
            tenant: Тренер-получатель.
            lesson: Занятие из расписания.
            rule: Правило напоминания.
            target_date: Дата занятия.
            now: Текущий момент в часовом поясе тенанта (для текста).
            result: Аккумулятор итогов.
        """
        entity = await self._repo.entities.get_by_id(tenant.id, lesson.entity_id)
        if entity is None:
            logger.debug("Ученик %s не найден — пропуск", lesson.entity_id)
            return

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
