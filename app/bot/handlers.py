"""Хендлеры Telegram-бота PlanZy.

Содержит обработчики команд (/start, /help, /admin, /today, /student,
/group), списки учеников и групп с переходом в досье по кнопке, а также
основной сценарий — приём надиктованного текста об уроке, его разбор
через Gemini, сохранение в БД и ответ с кнопкой WhatsApp.

Команды публикуются в меню Telegram функцией :func:`set_bot_commands`
(вызывается при старте приложения в ``app/main.py``).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BotCommand, CallbackQuery, Message

from app.bot.keyboards import (
    ENTITY_CALLBACK_PREFIX,
    ENTITY_PAGE_CALLBACK_PREFIX,
    LESSON_CANCEL_CALLBACK,
    LESSON_CONFIRM_CALLBACK,
    admin_keyboard,
    dossier_keyboard,
    entity_list_keyboard,
    is_public_url,
    lesson_confirm_keyboard,
    lesson_report_keyboard,
)
from app.bot.middlewares import get_repo, get_tenant
from app.core.config import settings
from app.core.logging import get_logger
from app.db.models import Entity, Payment
from app.parsers.gemini_client import GeminiParseError
from app.parsers.schemas import LessonParseResult
from app.services.auth_service import AuthService
from app.services.dossier_service import DossierService
from app.services.entity_service import EntityService
from app.services.lesson_service import (
    ATTENDANCE_LABELS,
    LessonService,
    PreparedLesson,
    ProcessedEntity,
    ProcessedLesson,
    UnknownEntityError,
)
from app.services.schedule_service import PlannedLesson, ScheduleService
from app.services.settings_service import TenantSettingsService
from app.services.share_link_service import ShareLinkService
from app.services.whatsapp_service import WhatsAppLinkService

logger = get_logger(__name__)

# Имя роутера — используется при сборке Dispatcher в app/bot/dispatcher.py.
ROUTER_NAME = "planzy"

# Минимальная длина текста, которую имеет смысл отправлять в Gemini.
MIN_DICTATION_LENGTH = 3

# Типы сущностей, для которых работает список в меню.
ENTITY_TYPES = frozenset({"student", "group"})

# Команды, публикуемые в меню Telegram (кнопка слева от поля ввода).
BOT_COMMANDS: tuple[tuple[str, str], ...] = (
    ("start", "Приветствие и справка"),
    ("today", "Занятия на сегодня и на завтра"),
    ("student", "Список учеников — открыть досье"),
    ("group", "Список групп — открыть досье"),
    ("admin", "Ссылка на веб-кабинет тренера"),
    ("help", "Справка по диктовке"),
)


class LessonFlow(StatesGroup):
    """Состояния сценария записи занятия из диктовки.

    Пока тренер не подтвердил запись, разобранный результат лежит в FSM —
    в базу ничего не попадает.
    """

    confirm = State()


# Русские подписи дней недели для отчёта (коды хранятся в Entity.schedule_days).
WEEKDAY_LABELS: dict[str, str] = {
    "mon": "понедельник",
    "tue": "вторник",
    "wed": "среда",
    "thu": "четверг",
    "fri": "пятница",
    "sat": "суббота",
    "sun": "воскресенье",
}

HELP_TEXT = """Привет! 👋

♟ <b>PlanZy — помощник шахматного тренера</b>

Надиктуйте или напишите текст об уроке — я разберу его, покажу, как понял,
и запишу только после вашего подтверждения.

<b>Примеры диктовки:</b>
• <i>Миша, шахматы, разбирали вилку, оценка 4, ДЗ — задачи 12–15</i>
• <i>Аня, тема — рокировка, не пришла</i>
• <i>Группа Новички, эндшпиль, были все кроме Леонида, оплатили 500, 8 занятий</i>

<b>Что я распознаю:</b>
• имя ученика или название группы — из уже заведённых в кабинете;
• тему занятия;
• кто был (сохраню в примечании);
• оценку занятия;
• домашнее задание.

Дата занятия всегда ставится сегодняшняя.

⚠️ Новых учеников и группы я не создаю: сначала заведите их в кабинете.

<b>Команды:</b>
/admin — ссылка на веб-кабинет тренера
/today — занятия на сегодня и на завтра
/student — список учеников, нажмите на имя, чтобы открыть досье
/group — список групп, нажмите на группу, чтобы открыть досье
/help — эта справка

После записи урока я пришлю кнопку для отправки отчёта родителю в WhatsApp.

Все команды доступны в меню слева от поля ввода."""


async def cmd_start(message: Message, **data: Any) -> None:
    """Приветствие при первом запуске.

    Имя тенанта не подставляется: в базе оно хранится как название аккаунта
    («Мой аккаунт»), и обращение по нему выглядело странно.
    """
    await message.answer(HELP_TEXT)


async def cmd_help(message: Message, **data: Any) -> None:
    """Выводит справку по форматам диктовки."""
    await message.answer(HELP_TEXT)


async def cmd_admin(message: Message, **data: Any) -> None:
    """Присылает тренеру секретную ссылку на веб-кабинет.

    Ссылка одноразовая по смыслу: перейдя по ней, тренер получает
    cookie-сессию на 30 дней. Пересылать ссылку третьим лицам нельзя.

    Ссылка дублируется кнопкой: если адрес локальный (``localhost``),
    Telegram подсветит её как текст, но не откроет по нажатию — кнопка
    отбрасывается, а текст остаётся.
    """
    tenant = get_tenant(data)
    repo = get_repo(data)

    url = await _build_admin_url(repo, tenant.id)
    if not url:
        await message.answer("Не удалось подготовить ссылку на кабинет. Попробуйте позже.")
        return

    await message.answer(
        "🖥 <b>Веб-кабинет тренера</b>\n\n"
        "Откройте ссылку — вход выполнится автоматически, "
        "и кабинет запомнит вас на 30 дней:\n\n"
        f'<a href="{url}">{url}</a>\n\n'
        "⚠️ Не пересылайте эту ссылку: любой, кто её откроет, "
        "получит доступ к вашим данным.\n\n"
        "В кабинете удобнее вести расписание, учеников, занятия и напоминания.",
        reply_markup=admin_keyboard(url),
        disable_web_page_preview=True,
    )


async def cmd_today(message: Message, **data: Any) -> None:
    """Показывает занятия на сегодня и на завтра с учётом отклонений.

    Дата берётся в часовом поясе тренера, поэтому «сегодня» совпадает
    с его календарём, а не с UTC-сервером.
    """
    tenant = get_tenant(data)
    repo = get_repo(data)

    schedule = ScheduleService(repo)
    settings_service = TenantSettingsService(repo)
    snapshot = await settings_service.get(tenant.id)
    zone = snapshot.zone if snapshot is not None else ZoneInfo(settings.timezone)

    today = datetime.now(zone).date()
    tomorrow = today + timedelta(days=1)

    today_lessons = await schedule.get_day_schedule(tenant.id, today)
    tomorrow_lessons = await schedule.get_day_schedule(tenant.id, tomorrow)

    lines: list[str] = [
        "🗓 <b>Расписание занятий</b>",
        "",
        *_format_day_block("Сегодня", today, today_lessons),
        "",
        *_format_day_block("Завтра", tomorrow, tomorrow_lessons),
        "",
        "Подробнее — в веб-кабинете: /admin",
    ]
    await message.answer("\n".join(lines))


def _format_day_block(day_title: str, target_date: date, lessons: list[PlannedLesson]) -> list[str]:
    """Формирует блок сообщения об одном дне.

    Args:
        day_title: Подпись дня («Сегодня» или «Завтра»).
        target_date: Дата дня.
        lessons: Занятия этого дня.

    Returns:
        Список строк для итогового сообщения.
    """
    lines = [f"<b>{day_title} — {target_date:%d.%m.%Y}</b>"]
    if not lessons:
        lines.append("• занятий нет")
        return lines

    for lesson in lessons:
        icon = "➡️" if lesson.kind == "moved" else "•"
        time_range = f"{lesson.start_time}–{lesson.end_time}"
        line = f"{icon} <b>{time_range}</b> — {lesson.entity_name}"
        if lesson.location:
            line += f" ({lesson.location})"
        lines.append(line)
        if lesson.note:
            lines.append(f"   <i>{lesson.note}</i>")

    lines.append(f"Всего занятий: {len(lessons)}")
    return lines


async def cmd_student(message: Message, **data: Any) -> None:
    """Показывает список учеников с кнопками-досье."""
    await _send_entity_list(message, data, entity_type="student")


async def cmd_group(message: Message, **data: Any) -> None:
    """Показывает список групп с кнопками-досье."""
    await _send_entity_list(message, data, entity_type="group")


async def _send_entity_list(
    message: Message,
    data: dict[str, Any],
    *,
    entity_type: str,
    page: int = 0,
) -> None:
    """Отправляет список учеников или групп инлайн-кнопками.

    Нажатие на кнопку открывает досье — это аналог прежней команды
    ``/dossier Имя`` без необходимости набирать имя вручную.

    Args:
        message: Сообщение тренера.
        data: Контекст хендлера (тенант и репозиторий).
        entity_type: Тип сущностей: ``"student"`` или ``"group"``.
        page: Номер страницы списка (с нуля).
    """
    tenant = get_tenant(data)
    repo = get_repo(data)

    entities = EntityService(repo)
    if entity_type == "group":
        items = await entities.list_groups(tenant.id)
        title = "👥 <b>Группы</b>"
        hint = "Нажмите на название группы, чтобы открыть досье:"
        empty_hint = (
            "Групп пока нет.\n\n"
            "Группа появится здесь, если надиктовать урок с названием группы "
            "(например, «Группа Новички, эндшпиль») или создать её в кабинете: /admin"
        )
    else:
        items = await entities.list_students(tenant.id)
        title = "🧑‍🎓 <b>Ученики</b>"
        hint = "Нажмите на имя, чтобы открыть досье:"
        empty_hint = (
            "Учеников пока нет.\n\n"
            "Ученик появится здесь, если надиктовать урок с его именем "
            "(например, «Миша, вилка, оценка 4») или создать его в кабинете: /admin"
        )

    if not items:
        await message.answer(empty_hint)
        return

    lines = [title, f"Всего: {len(items)}", "", hint]
    keyboard = entity_list_keyboard(items, entity_type=entity_type, page=page)
    await message.answer("\n".join(lines), reply_markup=keyboard)


async def callback_open_dossier(callback: CallbackQuery, **data: Any) -> None:
    """Открывает досье по нажатию на кнопку из списка учеников или групп.

    Формат callback_data: ``entity:<тип>:<entity_id>:<страница списка>``.

    Args:
        callback: Нажатие на инлайн-кнопку.
        data: Контекст хендлера (тенант и репозиторий).
    """
    tenant = get_tenant(data)
    repo = get_repo(data)

    payload = (callback.data or "").split(":")
    if len(payload) != 4:
        await callback.answer("Не удалось открыть досье.", show_alert=True)
        return

    _, entity_type, raw_entity_id, raw_page = payload
    try:
        entity_id = int(raw_entity_id)
        page = int(raw_page)
    except ValueError:
        await callback.answer("Не удалось открыть досье.", show_alert=True)
        return

    entity = await EntityService(repo).get_by_id(tenant.id, entity_id)
    if entity is None:
        await callback.answer("Запись не найдена.", show_alert=True)
        return

    dossier = await DossierService(repo).build(tenant.id, entity.id)
    if dossier is None:
        await callback.answer("Не удалось собрать досье.", show_alert=True)
        return

    dossier_text = DossierService.format_for_chat(dossier)
    whatsapp = WhatsAppLinkService()
    back_callback = (
        f"{ENTITY_PAGE_CALLBACK_PREFIX}:{entity_type}:{page}"
        if entity_type in ENTITY_TYPES
        else None
    )
    keyboard = dossier_keyboard(
        card_url=whatsapp.build_card_url(entity),
        whatsapp=whatsapp.build_dossier_message(entity, dossier_text),
        admin_url=await _build_admin_url(repo, tenant.id),
        back_callback=back_callback,
    )

    await callback.answer()
    if callback.message is not None:
        await callback.message.answer(
            dossier_text,
            reply_markup=keyboard,
        )


async def callback_entity_page(callback: CallbackQuery, **data: Any) -> None:
    """Переключает страницу списка учеников или групп.

    Формат callback_data: ``entity_page:<тип>:<страница>``.

    Args:
        callback: Нажатие на инлайн-кнопку.
        data: Контекст хендлера (тенант и репозиторий).
    """
    payload = (callback.data or "").split(":")
    if len(payload) != 3:
        await callback.answer("Не удалось переключить страницу.", show_alert=True)
        return

    _, entity_type, raw_page = payload
    if entity_type not in ENTITY_TYPES:
        await callback.answer("Не удалось переключить страницу.", show_alert=True)
        return

    try:
        page = int(raw_page)
    except ValueError:
        await callback.answer("Не удалось переключить страницу.", show_alert=True)
        return

    if callback.message is None:
        await callback.answer()
        return

    await callback.answer()
    await _send_entity_list(callback.message, data, entity_type=entity_type, page=page)


async def callback_lesson_confirm(callback: CallbackQuery, state: FSMContext, **data: Any) -> None:
    """Записывает занятие по нажатию «Да, записать».

    Разобранный результат берётся из FSM: повторно к Gemini не обращаемся.
    После записи бот присылает обычный отчёт об уроке с кнопками WhatsApp.

    Args:
        callback: Нажатие на инлайн-кнопку.
        state: FSM-контекст с распознанным результатом.
        data: Контекст хендлера (тенант и репозиторий).
    """
    tenant = get_tenant(data)
    repo = get_repo(data)

    payload = await state.get_data()
    raw_result = payload.get("parse_result")
    if not raw_result:
        await callback.answer("Данные потерялись — пришлите текст заново.", show_alert=True)
        await state.clear()
        return

    try:
        parse_result = LessonParseResult.model_validate(raw_result)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Повреждённый результат разбора в FSM: %s", exc)
        await callback.answer("Данные потерялись — пришлите текст заново.", show_alert=True)
        await state.clear()
        return

    await callback.answer("Записываю…")
    await state.clear()

    lessons = LessonService(repo)
    try:
        result = await lessons.apply_parse_result(tenant.id, parse_result)
    except UnknownEntityError as exc:
        logger.info("При записи не найдены сущности: %s", exc.names)
        if callback.message is not None:
            await callback.message.answer(_build_unknown_entities_hint(exc))
        return
    except Exception as exc:  # noqa: BLE001
        logger.exception("Ошибка записи урока: %s", exc)
        if callback.message is not None:
            await callback.message.answer(
                "⚠️ Не удалось записать занятие. Попробуйте ещё раз или проверьте кабинет."
            )
        return

    if callback.message is not None:
        await _send_lesson_report(callback.message, result, repo=repo, tenant_id=tenant.id)


async def callback_lesson_cancel(callback: CallbackQuery, state: FSMContext, **data: Any) -> None:
    """Отменяет запись: сбрасывает результат и ждёт новый текст.

    Args:
        callback: Нажатие на инлайн-кнопку.
        state: FSM-контекст с распознанным результатом.
        data: Контекст хендлера (не используется, но приходит от middleware).
    """
    await state.clear()
    await callback.answer("Отменено")
    if callback.message is not None:
        await callback.message.answer(
            "Хорошо, ничего не записал. 👌\n\nПришлите исправленный текст — я разберу его заново."
        )


async def cmd_dossier(message: Message, command: CommandObject, **data: Any) -> None:
    """Открывает досье по имени — текстовый способ помимо списков.

    Основной путь — списки ``/student`` и ``/group``; команда оставлена
    для привычки набирать имя вручную.

    Args:
        message: Сообщение тренера.
        command: Объект команды с аргументами (имя ученика).
        data: Контекст хендлера (тенант и репозиторий).
    """
    tenant = get_tenant(data)
    repo = get_repo(data)

    name = (command.args or "").strip()
    if not name:
        await message.answer(
            "Выберите ученика или группу в списке: /student или /group.\n"
            "Можно и текстом: <code>/dossier Миша</code>"
        )
        return

    entity = await EntityService(repo).get_by_name(tenant.id, name)
    if entity is None:
        await message.answer(f"Ученик «{name}» не найден. Проверьте имя.")
        return

    dossier = await DossierService(repo).build(tenant.id, entity.id)
    if dossier is None:
        await message.answer(f"Не удалось собрать досье для «{name}».")
        return

    dossier_text = DossierService.format_for_chat(dossier)
    whatsapp = WhatsAppLinkService()
    keyboard = dossier_keyboard(
        card_url=whatsapp.build_card_url(entity),
        whatsapp=whatsapp.build_dossier_message(entity, dossier_text),
        admin_url=await _build_admin_url(repo, tenant.id),
    )
    await message.answer(dossier_text, reply_markup=keyboard)


async def _build_admin_url(repo: Any, tenant_id: int) -> str | None:
    """Собирает ссылку на веб-кабинет тренера.

    Args:
        repo: Фасад репозиториев.
        tenant_id: Идентификатор тенанта.

    Returns:
        Абсолютный URL входа в кабинет или None, если токен недоступен.
    """
    token = await AuthService(repo).ensure_admin_token(tenant_id)
    if not token:
        return None
    return f"{settings.base_url.rstrip('/')}/admin/{token}"


async def _build_lesson_url(
    repo: Any | None,
    tenant_id: int | None,
    lesson_id: int,
) -> str | None:
    """Собирает публичную ссылку на веб-карточку занятия.

    Ссылка создаётся (или переиспользуется) через :class:`ShareLinkService`,
    чтобы тренер мог сразу отправить родителю страницу с домашним заданием
    и планом следующего урока.

    Args:
        repo: Фасад репозиториев.
        tenant_id: Идентификатор тенанта.
        lesson_id: Идентификатор занятия.

    Returns:
        Абсолютный URL карточки занятия или None, если ссылку создать нельзя.
    """
    if repo is None or tenant_id is None:
        return None
    link = await ShareLinkService(repo).ensure_link(tenant_id, lesson_id)
    if link is None:
        return None
    return f"{settings.base_url.rstrip('/')}/lesson/{link.token}"


async def handle_dictation(message: Message, state: FSMContext, **data: Any) -> None:
    """Основной сценарий: разбор диктовки и запись урока после подтверждения.

    Тренер присылает текст, бот показывает распознанные поля и спрашивает
    подтверждение. В базу ничего не пишется до нажатия «Да, записать».

    Args:
        message: Сообщение тренера с текстом об уроке.
        state: FSM-контекст для хранения распознанного результата.
        data: Контекст хендлера (тенант и репозиторий).
    """
    tenant = get_tenant(data)
    repo = get_repo(data)

    text = (message.text or "").strip()
    if len(text) < MIN_DICTATION_LENGTH:
        await message.answer("Текст слишком короткий. Напишите, пожалуйста, подробнее.")
        return

    await message.bot.send_chat_action(message.chat.id, "typing")

    lessons = LessonService(repo)
    try:
        prepared = await lessons.prepare(tenant.id, text)
    except GeminiParseError as exc:
        logger.warning("Не удалось разобрать диктовку: %s", exc)
        await message.answer(
            "🤔 Не удалось разобрать текст.\n\n"
            "Попробуйте переформулировать, например:\n"
            "<i>Миша, шахматы, тема — вилка, оценка 4, ДЗ — задачи 12–15</i>"
        )
        return
    except UnknownEntityError as exc:
        logger.info("Диктовка ссылается на неизвестных: %s", exc.names)
        await message.answer(_build_unknown_entities_hint(exc))
        return
    except Exception as exc:  # noqa: BLE001
        logger.exception("Ошибка разбора диктовки: %s", exc)
        await message.answer("⚠️ Произошла ошибка при разборе текста. Попробуйте позже.")
        return

    # Распознанный результат живёт в FSM до нажатия кнопки: повторный
    # запрос к Gemini не нужен, а в БД пока ничего не записано.
    await state.set_state(LessonFlow.confirm)
    await state.update_data(parse_result=prepared.parse_result.model_dump(mode="json"))

    await message.answer(
        _format_preview(prepared),
        reply_markup=lesson_confirm_keyboard(),
    )


def _build_unknown_entities_hint(error: UnknownEntityError) -> str:
    """Формирует подсказку про ненайденных учеников и группы.

    Бот не создаёт сущности сам: их заводит тренер в кабинете. Если в базе
    есть похожие названия, они показываются как варианты.

    Args:
        error: Ошибка с ненайденными именами и подсказками.

    Returns:
        Текст ответа тренеру.
    """
    lines: list[str] = ["🚫 <b>Не нашёл в базе:</b>"]

    for name in error.names:
        lines.append(f"• {name}")
        for variant in error.suggestions.get(name, []):
            lines.append(f"   — может, «{variant}»?")

    lines.append("")
    lines.append(
        "Проверьте написание и пришлите текст заново. "
        "Новый ученик или группа создаются только в веб-кабинете: /admin"
    )
    return "\n".join(lines)


def _format_preview(prepared: PreparedLesson) -> str:
    """Формирует предпросмотр распознанного занятия.

    Args:
        prepared: Результат разбора диктовки.

    Returns:
        Текст с распознанными полями и вопросом о записи.
    """
    parse = prepared.parse_result
    lines: list[str] = ["🔍 <b>Так я понял занятие:</b>", ""]

    if prepared.is_multi:
        lines.append(f"👥 <b>Ученики:</b> {', '.join(prepared.names)}")
    else:
        item = prepared.primary
        kind = "Группа" if item.is_group else "Ученик"
        lines.append(f"👤 <b>{kind}:</b> {item.name}")

    lines.append(f"📅 <b>Дата:</b> {parse.lesson_date.strftime('%d.%m.%Y')} (сегодня)")

    if parse.topic:
        lines.append(f"📚 <b>Тема:</b> {parse.topic}")
    if parse.rating is not None:
        lines.append(f"⭐ <b>Оценка:</b> {parse.rating}/5")
    if parse.homework:
        lines.append(f"📝 <b>ДЗ:</b> {parse.homework}")
    if parse.attendance_note:
        lines.append(f"👥 <b>Кто был:</b> {parse.attendance_note}")
    elif parse.attendance != "present":
        label = ATTENDANCE_LABELS.get(parse.attendance, parse.attendance)
        lines.append(f"🚫 <b>Посещаемость:</b> {label}")
    if parse.internal_notes:
        lines.append(f"🔒 <b>Заметка:</b> {parse.internal_notes}")

    lines.append("")
    lines.append("<b>Записать занятие?</b>")
    return "\n".join(lines)


def _format_payment_line(payment: Payment) -> str | None:
    """Собирает строку про оплату: сумма, валюта и зачисленные занятия.

    Args:
        payment: Зарегистрированный платёж.

    Returns:
        Готовая строка для отчёта или None, если показывать нечего.
    """
    parts: list[str] = []
    if payment.amount is not None:
        parts.append(f"{payment.amount:g} {payment.currency}")
    if payment.lessons_added:
        parts.append(f"+{payment.lessons_added} {_pluralize_lessons(payment.lessons_added)}")
    if not parts:
        return None
    return f"💰 Оплата: {', '.join(parts)}"


def _pluralize_lessons(count: int) -> str:
    """Склоняет слово «занятие» по числу.

    Args:
        count: Количество занятий.

    Returns:
        Форма слова, согласованная с числом.
    """
    remainder_100 = count % 100
    remainder_10 = count % 10
    if 11 <= remainder_100 <= 14:
        return "занятий"
    if remainder_10 == 1:
        return "занятие"
    if 2 <= remainder_10 <= 4:
        return "занятия"
    return "занятий"


async def _format_schedule_line(repo: Any, tenant_id: int, entity: Entity) -> str | None:
    """Собирает строку про расписание ученика из регулярных слотов.

    Расписание берётся из таблицы ``schedule_slots`` — того же источника,
    что использует веб-кабинет и рассылка напоминаний. Устаревшие поля
    ``Entity.schedule_days``/``schedule_time`` не читаются: после перехода
    на web-first они пусты, и расписание всегда выглядело незаданным.

    Args:
        repo: Фасад репозиториев (без него расписание недоступно).
        tenant_id: Идентификатор тенанта.
        entity: Ученик или группа.

    Returns:
        Строка с расписанием или None, если репозитория нет
        либо у ученика нет активных слотов.
    """
    if repo is None or tenant_id is None:
        return None

    schedule = await ScheduleService(repo).format_entity_schedule(tenant_id, entity.id)
    if schedule == "расписание не задано":
        return None
    return f"🗓 Расписание: {schedule}"


async def _build_entity_block(
    processed: ProcessedEntity,
    parse: LessonParseResult,
    *,
    is_primary: bool,
    repo: Any,
    tenant_id: int,
) -> list[str]:
    """Формирует блок отчёта по одному ученику.

    Args:
        processed: Результат обработки по конкретному ученику.
        parse: Структурированный результат разбора диктовки.
        is_primary: True для основного ученика (того, кто назван первым).
        repo: Фасад репозиториев (для чтения расписания из слотов).
        tenant_id: Идентификатор тенанта.

    Returns:
        Список строк для итогового сообщения.
    """
    entity = processed.entity
    lesson = processed.lesson

    if is_primary:
        header = f"✅ <b>Урок сохранён</b> — {entity.name}"
    else:
        header = f"✅ <b>Урок сохранён</b> — {entity.name} <i>(доп. ученик)</i>"

    lines = [header]
    if processed.entity_created:
        lines.append("<i>(ученик добавлен в базу впервые)</i>")

    lines.append("")
    lines.append(f"📅 {lesson.lesson_date.strftime('%d.%m.%Y')}")
    if lesson.topic and lesson.topic != "—":
        lines.append(f"📚 Тема: {lesson.topic}")
    if lesson.rating:
        lines.append(f"⭐ Оценка: {lesson.rating}/5")
    if lesson.homework:
        lines.append(f"📝 ДЗ: {lesson.homework}")
    if lesson.next_step:
        lines.append(f"➡️ Далее: {lesson.next_step}")
    if parse.attendance != "present":
        lines.append(f"🚫 Посещаемость: {parse.attendance}")

    schedule_line = await _format_schedule_line(repo, tenant_id, entity)
    if schedule_line:
        lines.append(schedule_line)

    if processed.payment is not None:
        payment_line = _format_payment_line(processed.payment)
        if payment_line:
            lines.append("")
            lines.append(payment_line)

    return lines


async def _send_lesson_report(
    message: Message,
    result: ProcessedLesson,
    *,
    repo: Any | None = None,
    tenant_id: int | None = None,
) -> None:
    """Отправляет отчёт об обработанном уроке с кнопками действий.

    Если в диктовке было названо несколько учеников, в отчёте показывается
    блок по каждому из них, а кнопка WhatsApp ведёт на основного ученика.

    Args:
        message: Исходное сообщение тренера.
        result: Результат обработки диктовки.
        repo: Фасад репозиториев (для публичной ссылки на занятие).
        tenant_id: Идентификатор тенанта (для публичной ссылки на занятие).
    """
    parse = result.parse_result
    entities = result.all_entities

    lines: list[str] = []
    for index, processed in enumerate(entities):
        if index > 0:
            lines.append("")
            lines.append("➖➖➖➖➖")
            lines.append("")
        block = await _build_entity_block(
            processed,
            parse,
            is_primary=index == 0,
            repo=repo,
            tenant_id=tenant_id,
        )
        lines.extend(block)

    if result.is_multi:
        lines.append("")
        lines.append(
            f"👥 В диктовке названо учеников: {len(entities)}. Оплата зачтена основному ученику."
        )

    entity = result.entity
    lesson = result.lesson

    whatsapp = WhatsAppLinkService()
    report = whatsapp.build_lesson_report(
        entity,
        lesson,
        payment=result.payment,
    )
    card_url = whatsapp.build_card_url(entity)
    dossier_url = whatsapp.build_dossier_url(entity)
    lesson_url = await _build_lesson_url(repo, tenant_id, lesson.id)
    keyboard = lesson_report_keyboard(
        whatsapp=report,
        card_url=card_url,
        dossier_url=dossier_url,
        lesson_url=lesson_url,
    )

    # Telegram не принимает локальные адреса в кнопках — предупреждаем тренера.
    if not is_public_url(card_url):
        lines.append("")
        lines.append(
            "ℹ️ Ссылки на карточку и досье скрыты: <code>BASE_URL</code> "
            "указывает на локальный адрес. Для публичных ссылок нужен "
            "внешний HTTPS-адрес (например, через туннель)."
        )

    await message.answer("\n".join(lines), reply_markup=keyboard)


def create_router(name: str = ROUTER_NAME) -> Router:
    """Собирает новый роутер со всеми хендлерами бота.

    Роутер нельзя подключить к двум диспетчерам: aiogram выбрасывает
    ``RuntimeError: Router is already attached``. Раньше хендлеры висели
    на модульном синглтоне, из-за чего повторная сборка приложения падала.
    Теперь роутер собирается фабрикой, а хендлеры привязываются к нему
    при каждой сборке.

    Args:
        name: Имя роутера.

    Returns:
        Роутер с зарегистрированными хендлерами команд и callback'ов.
    """
    fresh = Router(name=name)
    _register_handlers(fresh)
    return fresh


def _register_handlers(target: Router) -> None:
    """Привязывает хендлеры к указанному роутеру.

    Args:
        target: Роутер, к которому подключаются хендлеры.
    """
    target.message.register(cmd_start, CommandStart())
    target.message.register(cmd_help, Command("help"))
    target.message.register(cmd_admin, Command("admin"))
    target.message.register(cmd_today, Command("today"))
    target.message.register(cmd_student, Command("student"))
    target.message.register(cmd_group, Command("group"))
    target.message.register(cmd_dossier, Command("dossier"))
    target.callback_query.register(
        callback_open_dossier, F.data.startswith(f"{ENTITY_CALLBACK_PREFIX}:")
    )
    target.callback_query.register(
        callback_entity_page, F.data.startswith(f"{ENTITY_PAGE_CALLBACK_PREFIX}:")
    )
    target.callback_query.register(callback_lesson_confirm, F.data == LESSON_CONFIRM_CALLBACK)
    target.callback_query.register(callback_lesson_cancel, F.data == LESSON_CANCEL_CALLBACK)
    # Диктовка — последней: команды должны перехватываться раньше.
    target.message.register(handle_dictation, F.text)


def register_handlers(dispatcher: Any) -> None:
    """Подключает роутер с хендлерами к диспетчеру.

    Args:
        dispatcher: Экземпляр aiogram Dispatcher.
    """
    dispatcher.include_router(create_router())


async def set_bot_commands(bot: Any) -> None:
    """Публикует список команд в меню Telegram.

    После этого команды видны в кнопке слева от поля ввода — тренеру
    не нужно помнить их наизусть. Ошибка сети не критична: бот продолжает
    работать, команды просто остаются от предыдущей публикации.

    Args:
        bot: Экземпляр aiogram Bot.
    """
    try:
        await bot.set_my_commands(
            [
                BotCommand(command=name, description=description)
                for name, description in BOT_COMMANDS
            ]
        )
        logger.info("Меню команд бота опубликовано (%d команд)", len(BOT_COMMANDS))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Не удалось опубликовать меню команд: %s", exc)


# Роутер по умолчанию: используется, если приложение собирается один раз.
router = create_router()


__all__ = [
    "BOT_COMMANDS",
    "HELP_TEXT",
    "ROUTER_NAME",
    "create_router",
    "register_handlers",
    "router",
    "set_bot_commands",
]
