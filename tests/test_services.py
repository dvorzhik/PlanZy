"""Тесты Service Layer: EntityService, PaymentService, LessonService,
WhatsAppLinkService и DossierService.

Все тесты работают на изолированной in-memory SQLite (см. conftest.py)
и не обращаются к Gemini — парсер подменяется заглушкой.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.db.models import Entity, Lesson
from app.db.repository import Repository
from app.parsers.schemas import LessonParseResult, PaymentInfo
from app.services.dossier_service import DossierService
from app.services.entity_service import EntityService
from app.services.lesson_service import LessonService, UnknownEntityError
from app.services.payment_service import PaymentService
from app.services.whatsapp_service import MAX_MESSAGE_LENGTH, WhatsAppLinkService

TODAY = date(2026, 9, 17)


class StubParser:
    """Заглушка парсера: возвращает заранее заданный результат."""

    def __init__(self, result: LessonParseResult) -> None:
        self._result = result
        self.calls: list[str] = []

    async def parse(self, text: str, *, today: date | None = None) -> LessonParseResult:
        """Запоминает вызов и возвращает подготовленный результат."""
        self.calls.append(text)
        return self._result


# ----------------------------------------------------------------------
# EntityService
# ----------------------------------------------------------------------


class TestEntityService:
    """Проверки поиска/создания сущностей и расписания."""

    async def test_get_or_create_creates_new_student(
        self, repo: Repository, tenant_id: int
    ) -> None:
        """Новый ученик создаётся с типом student и публичным токеном."""
        service = EntityService(repo)
        entity, created = await service.get_or_create(tenant_id, "Миша")

        assert created is True
        assert entity.id is not None
        assert entity.entity_type == "student"
        assert entity.public_token

    async def test_get_or_create_is_idempotent(self, repo: Repository, tenant_id: int) -> None:
        """Повторный вызов возвращает ту же сущность без создания дубля."""
        service = EntityService(repo)
        first, created_first = await service.get_or_create(tenant_id, "Аня")
        second, created_second = await service.get_or_create(tenant_id, "аня")

        assert created_first is True
        assert created_second is False
        assert first.id == second.id

    async def test_get_or_create_detects_group(self, repo: Repository, tenant_id: int) -> None:
        """Название со словом «группа» даёт тип group."""
        service = EntityService(repo)
        entity, _ = await service.get_or_create(tenant_id, "Группа Новички")

        assert entity.entity_type == "group"

    async def test_list_for_weekday_sorted_by_time(self, repo: Repository, tenant_id: int) -> None:
        """Сущности дня сортируются по времени занятия."""
        service = EntityService(repo)
        late, _ = await service.get_or_create(tenant_id, "Поздний")
        early, _ = await service.get_or_create(tenant_id, "Ранний")
        await service.set_schedule(tenant_id, late.id, ["mon"], "18:00")
        await service.set_schedule(tenant_id, early.id, ["mon"], "09:00")

        result = await service.list_for_weekday(tenant_id, "mon")

        assert [e.name for e in result] == ["Ранний", "Поздний"]

    async def test_list_for_weekday_rejects_bad_code(
        self, repo: Repository, tenant_id: int
    ) -> None:
        """Некорректный код дня недели приводит к ValueError."""
        service = EntityService(repo)
        with pytest.raises(ValueError, match="Недопустимый день недели"):
            await service.list_for_weekday(tenant_id, "понедельник")

    async def test_set_schedule_rejects_bad_day(self, repo: Repository, tenant_id: int) -> None:
        """Некорректный день в расписании приводит к ValueError."""
        service = EntityService(repo)
        entity, _ = await service.get_or_create(tenant_id, "Миша")
        with pytest.raises(ValueError, match="Недопустимые дни недели"):
            await service.set_schedule(tenant_id, entity.id, ["mon", "xxx"])

    async def test_format_schedule(self, repo: Repository, tenant_id: int) -> None:
        """Расписание форматируется в читаемую строку."""
        service = EntityService(repo)
        entity, _ = await service.get_or_create(tenant_id, "Миша")
        await service.set_schedule(tenant_id, entity.id, ["mon", "wed"], "16:00")
        refreshed = await service.get_by_id(tenant_id, entity.id)

        assert refreshed is not None
        assert EntityService.format_schedule(refreshed) == "Пн, Ср в 16:00"

    async def test_format_schedule_empty(self, repo: Repository, tenant_id: int) -> None:
        """Без расписания возвращается заглушка."""
        service = EntityService(repo)
        entity, _ = await service.get_or_create(tenant_id, "Миша")

        assert EntityService.format_schedule(entity) == "расписание не задано"

    async def test_apply_schedule_from_dictation_fills_empty_fields(
        self, repo: Repository, tenant_id: int
    ) -> None:
        """Расписание из диктовки заполняет пустые поля ученика."""
        entities = EntityService(repo)
        entity, _ = await entities.get_or_create(tenant_id, "Мия")

        updated = await entities.apply_schedule_from_dictation(
            tenant_id, entity.id, ["wed"], "15:15"
        )

        assert updated is not None
        assert updated.schedule_days == "wed"
        assert updated.schedule_time == "15:15"

    async def test_apply_schedule_from_dictation_keeps_existing(
        self, repo: Repository, tenant_id: int
    ) -> None:
        """Уже заданное расписание не перезаписывается диктовкой."""
        entities = EntityService(repo)
        entity, _ = await entities.get_or_create(tenant_id, "Мия")
        await entities.set_schedule(tenant_id, entity.id, ["mon"], "10:00")

        updated = await entities.apply_schedule_from_dictation(
            tenant_id, entity.id, ["wed"], "15:15"
        )

        assert updated is not None
        assert updated.schedule_days == "mon"
        assert updated.schedule_time == "10:00"

    async def test_apply_schedule_from_dictation_returns_none_for_unknown(
        self, repo: Repository, tenant_id: int
    ) -> None:
        """Для несуществующего ученика возвращается None."""
        entities = EntityService(repo)

        assert await entities.apply_schedule_from_dictation(tenant_id, 999, ["wed"]) is None

    async def test_ensure_public_token_returns_existing(
        self, repo: Repository, tenant_id: int
    ) -> None:
        """Существующий токен возвращается без изменений."""
        service = EntityService(repo)
        entity, _ = await service.get_or_create(tenant_id, "Миша")

        token = await service.ensure_public_token(tenant_id, entity.id)

        assert token == entity.public_token


class TestEntityResolve:
    """Проверки неточного поиска учеников и групп по имени из диктовки.

    Тренер называет группу коротко («группа Шпринцак 4»), а в кабинете она
    записана полностью («Шпринцак 4 - начинающие»). Поиск должен это
    сопоставлять, но не угадывать при неоднозначности.
    """

    async def _seed(self, repo: Repository, tenant_id: int) -> None:
        """Готовит набор групп и учеников."""
        for name in (
            "Шпринцак 1 - опытные",
            "Шпринцак 2 - продолжающие",
            "Шпринцак 3 - продолжающие",
            "Шпринцак 4 - начинающие",
            "Пятничная группа",
        ):
            await repo.entities.create(tenant_id=tenant_id, name=name, entity_type="group")
        await repo.entities.create(tenant_id=tenant_id, name="Миша", entity_type="student")
        await repo.entities.create(tenant_id=tenant_id, name="Рон и Мия", entity_type="student")

    async def test_exact_match_wins(self, repo: Repository, tenant_id: int) -> None:
        await self._seed(repo, tenant_id)
        entity = await EntityService(repo).resolve(tenant_id, "Шпринцак 4 - начинающие")

        assert entity is not None
        assert entity.name == "Шпринцак 4 - начинающие"

    async def test_partial_group_name_with_marker(self, repo: Repository, tenant_id: int) -> None:
        """«группа Шпринцак 4» находит полное название группы."""
        await self._seed(repo, tenant_id)
        entity = await EntityService(repo).resolve(tenant_id, "группа Шпринцак 4")

        assert entity is not None
        assert entity.name == "Шпринцак 4 - начинающие"
        assert entity.is_group is True

    async def test_partial_group_name_without_marker(
        self, repo: Repository, tenant_id: int
    ) -> None:
        """Слово «группа» не обязательно: «Шпринцак 3» тоже находит."""
        await self._seed(repo, tenant_id)
        entity = await EntityService(repo).resolve(tenant_id, "Шпринцак 3")

        assert entity is not None
        assert entity.name == "Шпринцак 3 - продолжающие"

    async def test_case_insensitive(self, repo: Repository, tenant_id: int) -> None:
        await self._seed(repo, tenant_id)
        entity = await EntityService(repo).resolve(tenant_id, "группа шпринцак 1")

        assert entity is not None
        assert entity.name == "Шпринцак 1 - опытные"

    async def test_marker_after_name(self, repo: Repository, tenant_id: int) -> None:
        """«Пятничная группа» — слово «группа» в конце названия."""
        await self._seed(repo, tenant_id)
        entity = await EntityService(repo).resolve(tenant_id, "группа Пятничная")

        assert entity is not None
        assert entity.name == "Пятничная группа"

    async def test_ambiguous_query_returns_none(self, repo: Repository, tenant_id: int) -> None:
        """«группа Шпринцак» без номера неоднозначна — лучше не угадывать."""
        await self._seed(repo, tenant_id)

        assert await EntityService(repo).resolve(tenant_id, "группа Шпринцак") is None

    async def test_unknown_name_returns_none(self, repo: Repository, tenant_id: int) -> None:
        await self._seed(repo, tenant_id)

        assert await EntityService(repo).resolve(tenant_id, "Группа Новички") is None
        assert await EntityService(repo).resolve(tenant_id, "") is None

    async def test_partial_student_name(self, repo: Repository, tenant_id: int) -> None:
        """Частичное имя ученика тоже находится."""
        await self._seed(repo, tenant_id)
        entity = await EntityService(repo).resolve(tenant_id, "Мия")

        assert entity is not None
        assert entity.name == "Рон и Мия"
        assert entity.is_group is False

    async def test_find_similar_ignores_group_marker(
        self, repo: Repository, tenant_id: int
    ) -> None:
        """Запрос из одного слова «группа» не даёт шумных подсказок."""
        await self._seed(repo, tenant_id)
        similar = await EntityService(repo).find_similar(tenant_id, "Группа")

        assert similar == []

    async def test_find_similar_suggests_by_words(self, repo: Repository, tenant_id: int) -> None:
        """Подсказки подбираются по значимым словам."""
        await self._seed(repo, tenant_id)
        similar = await EntityService(repo).find_similar(tenant_id, "группа Шпринцак 05")

        names = [item.name for item in similar]
        assert "Шпринцак 4 - начинающие" in names
        # Все подсказки относятся к «Шпринцак», а не к другим группам.
        assert all("Шпринцак" in name for name in names)

    async def test_find_similar_without_matches(self, repo: Repository, tenant_id: int) -> None:
        await self._seed(repo, tenant_id)

        assert await EntityService(repo).find_similar(tenant_id, "Совсем Неизвестный") == []


# ----------------------------------------------------------------------
# PaymentService
# ----------------------------------------------------------------------


class TestPaymentService:
    """Проверки учёта платежей и баланса."""

    async def test_register_payment_adds_lessons(self, repo: Repository, tenant_id: int) -> None:
        """Платёж с lessons_added пополняет баланс."""
        entities = EntityService(repo)
        entity, _ = await entities.get_or_create(tenant_id, "Миша")
        payments = PaymentService(repo)

        payment = await payments.register_payment(
            tenant_id, entity.id, amount=500.0, lessons_added=8, payment_date=TODAY
        )

        assert payment.id is not None
        assert payment.amount == 500.0
        assert payment.currency == "ILS"
        assert payment.lessons_added == 8

    async def test_register_payment_requires_amount_or_lessons(
        self, repo: Repository, tenant_id: int
    ) -> None:
        """Платёж без суммы и без занятий отклоняется."""
        entities = EntityService(repo)
        entity, _ = await entities.get_or_create(tenant_id, "Миша")
        payments = PaymentService(repo)

        with pytest.raises(ValueError, match="хотя бы сумму или количество"):
            await payments.register_payment(tenant_id, entity.id)

    async def test_total_paid_sums_payments(self, repo: Repository, tenant_id: int) -> None:
        """Суммарная оплата складывается по всем платежам."""
        entities = EntityService(repo)
        entity, _ = await entities.get_or_create(tenant_id, "Миша")
        payments = PaymentService(repo)
        await payments.register_payment(tenant_id, entity.id, amount=300.0, lessons_added=4)
        await payments.register_payment(tenant_id, entity.id, amount=200.0, lessons_added=4)

        assert await payments.total_paid(tenant_id, entity.id) == 500.0

    @pytest.mark.parametrize(
        ("count", "expected"),
        [
            (1, "занятие"),
            (2, "занятия"),
            (4, "занятия"),
            (5, "занятий"),
            (11, "занятий"),
            (21, "занятие"),
            (22, "занятия"),
            (25, "занятий"),
        ],
    )
    def test_pluralize_lessons(self, count: int, expected: str) -> None:
        """Склонение слова «занятие» по числу."""
        assert PaymentService._pluralize_lessons(count) == expected

    @pytest.mark.parametrize(
        ("amount", "price", "expected"),
        [
            (120.0, 30.0, 4),
            (100.0, 30.0, 3),
            (30.0, 30.0, 1),
            (29.0, 30.0, None),
            (0.0, 30.0, None),
            (120.0, 0.0, None),
            (None, 30.0, None),
        ],
    )
    def test_lessons_from_amount(
        self, amount: float | None, price: float, expected: int | None
    ) -> None:
        """Сумма переводится в количество занятий по цене занятия."""
        assert PaymentService.lessons_from_amount(amount, price) == expected

    async def test_register_payment_infers_lessons_from_amount(
        self, repo: Repository, tenant_id: int
    ) -> None:
        """Если названа только сумма, занятия считаются по цене из настроек."""
        entities = EntityService(repo)
        entity, _ = await entities.get_or_create(tenant_id, "Миша")
        payments = PaymentService(repo)

        payment = await payments.register_payment(
            tenant_id, entity.id, amount=120.0, payment_date=TODAY
        )

        assert payment.lessons_added == 4

    async def test_register_payment_respects_explicit_lessons(
        self, repo: Repository, tenant_id: int
    ) -> None:
        """Явно названное число занятий имеет приоритет над расчётом из суммы."""
        entities = EntityService(repo)
        entity, _ = await entities.get_or_create(tenant_id, "Миша")
        payments = PaymentService(repo)

        payment = await payments.register_payment(
            tenant_id, entity.id, amount=120.0, lessons_added=2, payment_date=TODAY
        )

        assert payment.lessons_added == 2

    async def test_register_payment_can_disable_inference(
        self, repo: Repository, tenant_id: int
    ) -> None:
        """При infer_lessons=False баланс не меняется, если занятия не названы."""
        entities = EntityService(repo)
        entity, _ = await entities.get_or_create(tenant_id, "Миша")
        payments = PaymentService(repo)

        payment = await payments.register_payment(
            tenant_id,
            entity.id,
            amount=120.0,
            payment_date=TODAY,
            infer_lessons=False,
        )

        assert payment.lessons_added is None

    async def test_register_payment_uses_custom_currency(
        self, repo: Repository, tenant_id: int
    ) -> None:
        """Валюту платежа можно задать явно."""
        entities = EntityService(repo)
        entity, _ = await entities.get_or_create(tenant_id, "Миша")
        payments = PaymentService(repo)

        payment = await payments.register_payment(
            tenant_id, entity.id, amount=100.0, currency="USD", payment_date=TODAY
        )

        assert payment.currency == "USD"


# ----------------------------------------------------------------------
# LessonService
# ----------------------------------------------------------------------


class TestLessonService:
    """Проверки обработки диктовки и записи урока.

    Бот больше не создаёт учеников и группы: все имена должны быть
    заведены заранее, поэтому тесты готовят сущности через
    ``repo.entities.create``.
    """

    async def _seed(
        self,
        repo: Repository,
        tenant_id: int,
        name: str,
        *,
        entity_type: str = "student",
    ) -> Entity:
        """Создаёт ученика или группу и возвращает сущность."""
        return await repo.entities.create(
            tenant_id=tenant_id,
            name=name,
            entity_type=entity_type,
        )

    async def test_process_dictation_creates_lesson_and_debits_balance(
        self, repo: Repository, tenant_id: int
    ) -> None:
        """Диктовка создаёт урок и сохраняет платёж."""
        entity = await self._seed(repo, tenant_id, "Миша")
        await PaymentService(repo).register_payment(
            tenant_id, entity.id, amount=500.0, lessons_added=8
        )

        result_parse = LessonParseResult(
            entity_name="Миша",
            topic="Итальянская партия",
            homework="Решить 5 задач",
            rating=4,
            next_step="Дебютные ловушки",
            attendance="present",
            lesson_date=TODAY,
        )
        parser = StubParser(result_parse)
        service = LessonService(repo, parser=parser)

        processed = await service.process_dictation(tenant_id, "текст диктовки")

        assert parser.calls == ["текст диктовки"]
        assert processed.entity_created is False
        assert processed.lesson.topic == "Итальянская партия"
        assert processed.lesson.rating == "4"

    async def test_process_dictation_rejects_unknown_entity(
        self, repo: Repository, tenant_id: int
    ) -> None:
        """Незнакомое имя не создаёт ученика — поднимается UnknownEntityError."""
        result_parse = LessonParseResult(
            entity_name="Новый Ученик",
            topic="Мат в один ход",
            attendance="present",
            lesson_date=TODAY,
        )
        service = LessonService(repo, parser=StubParser(result_parse))

        with pytest.raises(UnknownEntityError) as exc_info:
            await service.process_dictation(tenant_id, "текст")

        assert exc_info.value.names == ["Новый Ученик"]
        # Ученик не появился и урок не записан.
        assert await repo.entities.get_by_name(tenant_id, "Новый Ученик") is None
        assert await repo.lessons.list_by_tenant(tenant_id) == []

    async def test_prepare_does_not_write_to_db(self, repo: Repository, tenant_id: int) -> None:
        """prepare разбирает текст, но ничего не записывает."""
        entity = await self._seed(repo, tenant_id, "Миша")
        result_parse = LessonParseResult(
            entity_name="Миша",
            topic="Вилка",
            rating=4,
            homework="Задачи 12–15",
            attendance="present",
            lesson_date=TODAY,
        )
        service = LessonService(repo, parser=StubParser(result_parse))

        prepared = await service.prepare(tenant_id, "текст")

        assert prepared.primary.entity.id == entity.id
        assert prepared.parse_result.topic == "Вилка"
        assert prepared.parse_result.rating == 4
        # В базе по-прежнему ни одного урока.
        assert await repo.lessons.list_by_entity(tenant_id, entity.id) == []

    async def test_prepare_rejects_unknown_entity(self, repo: Repository, tenant_id: int) -> None:
        """prepare сообщает обо всех ненайденных именах сразу."""
        await self._seed(repo, tenant_id, "Миша")
        result_parse = LessonParseResult(
            entity_name="Кто-то",
            extra_entities=["Ещё Кто-то"],
            attendance="present",
            lesson_date=TODAY,
        )
        service = LessonService(repo, parser=StubParser(result_parse))

        with pytest.raises(UnknownEntityError) as exc_info:
            await service.prepare(tenant_id, "текст")

        assert exc_info.value.names == ["Кто-то", "Ещё Кто-то"]

    async def test_prepare_then_apply_writes_lesson(self, repo: Repository, tenant_id: int) -> None:
        """После подтверждения урок записывается через apply_parse_result."""
        entity = await self._seed(repo, tenant_id, "Миша")
        result_parse = LessonParseResult(
            entity_name="Миша",
            topic="Вилка",
            attendance="present",
            lesson_date=TODAY,
        )
        service = LessonService(repo, parser=StubParser(result_parse))

        prepared = await service.prepare(tenant_id, "текст")
        processed = await service.apply_parse_result(tenant_id, prepared.parse_result)

        assert processed.lesson.topic == "Вилка"
        assert await service.count_lessons(tenant_id, entity.id) == 1

    async def test_lesson_date_is_always_today(self, repo: Repository, tenant_id: int) -> None:
        """Дата занятия проставляется текущим днём, даже если в тексте другая."""
        await self._seed(repo, tenant_id, "Миша")
        result_parse = LessonParseResult(
            entity_name="Миша",
            topic="Вилка",
            attendance="present",
            lesson_date=date(2020, 1, 1),
        )
        service = LessonService(repo, parser=StubParser(result_parse))

        processed = await service.process_dictation(tenant_id, "текст")

        assert processed.lesson.lesson_date == date.today()

    async def test_payment_in_dictation_is_registered(
        self, repo: Repository, tenant_id: int
    ) -> None:
        """Упоминание оплаты в диктовке создаёт платёж."""
        await self._seed(repo, tenant_id, "Аня")
        result_parse = LessonParseResult(
            entity_name="Аня",
            topic="Рокировка",
            attendance="present",
            lesson_date=TODAY,
            payment=PaymentInfo(has_payment=True, amount=400.0, lessons_added=4),
        )
        service = LessonService(repo, parser=StubParser(result_parse))

        processed = await service.process_dictation(tenant_id, "текст")

        assert processed.payment is not None
        assert processed.payment.amount == 400.0
        assert processed.payment.lessons_added == 4

    async def test_apply_parse_result_is_reusable(self, repo: Repository, tenant_id: int) -> None:
        """apply_parse_result можно вызвать без обращения к парсеру."""
        await self._seed(repo, tenant_id, "Миша")
        result_parse = LessonParseResult(
            entity_name="Миша",
            topic="Эндпиль",
            attendance="present",
            lesson_date=TODAY,
        )
        service = LessonService(repo, parser=StubParser(result_parse))

        processed = await service.apply_parse_result(tenant_id, result_parse)

        assert processed.lesson.id is not None
        assert await service.count_lessons(tenant_id, processed.entity.id) == 1

    async def test_list_history_returns_newest_first(
        self, repo: Repository, tenant_id: int
    ) -> None:
        """История уроков отдаётся от новых к старым."""
        entities = EntityService(repo)
        entity, _ = await entities.get_or_create(tenant_id, "Миша")
        service = LessonService(repo, parser=StubParser(LessonParseResult(entity_name="Миша")))

        for day, topic in ((date(2026, 9, 1), "Первый"), (date(2026, 9, 8), "Второй")):
            await service.apply_parse_result(
                tenant_id,
                LessonParseResult(
                    entity_name="Миша", topic=topic, attendance="present", lesson_date=day
                ),
            )

        history = await service.list_history(tenant_id, entity.id)

        assert [lesson.topic for lesson in history] == ["Второй", "Первый"]

    async def test_multiple_students_create_lesson_for_each(
        self, repo: Repository, tenant_id: int
    ) -> None:
        """Диктовка с двумя учениками создаёт урок для каждого из них."""
        await self._seed(repo, tenant_id, "Рон")
        await self._seed(repo, tenant_id, "Мия")
        result_parse = LessonParseResult(
            entity_name="Рон",
            extra_entities=["Мия"],
            subject="шахматы",
            topic="Виды матов",
            attendance="present",
            lesson_date=TODAY,
        )
        service = LessonService(repo, parser=StubParser(result_parse))

        processed = await service.process_dictation(tenant_id, "текст")

        assert processed.is_multi is True
        assert [item.entity.name for item in processed.all_entities] == ["Рон", "Мия"]
        assert processed.entity.name == "Рон"
        assert processed.others[0].entity.name == "Мия"
        # Урок создан для обоих учеников.
        assert await service.count_lessons(tenant_id, processed.entity.id) == 1
        assert await service.count_lessons(tenant_id, processed.others[0].entity.id) == 1

    async def test_multiple_students_payment_goes_to_primary_only(
        self, repo: Repository, tenant_id: int
    ) -> None:
        """Платёж из диктовки зачитывается только основному ученику."""
        await self._seed(repo, tenant_id, "Рон")
        await self._seed(repo, tenant_id, "Мия")
        result_parse = LessonParseResult(
            entity_name="Рон",
            extra_entities=["Мия"],
            topic="Виды матов",
            attendance="present",
            lesson_date=TODAY,
            payment=PaymentInfo(has_payment=True, amount=120.0),
        )
        service = LessonService(repo, parser=StubParser(result_parse))

        processed = await service.process_dictation(tenant_id, "текст")

        # Платёж зачитывается только основному ученику.
        assert processed.payment is not None
        assert processed.payment.lessons_added == 4
        # Дополнительному ученику платёж не достаётся.
        assert processed.others[0].payment is None

    async def test_duplicate_names_are_deduplicated(self, repo: Repository, tenant_id: int) -> None:
        """Повтор имени в extra_entities не создаёт второй урок."""
        await self._seed(repo, tenant_id, "Рон")
        await self._seed(repo, tenant_id, "Мия")
        result_parse = LessonParseResult(
            entity_name="Рон",
            extra_entities=["рон", "Мия"],
            topic="Виды матов",
            attendance="present",
            lesson_date=TODAY,
        )
        service = LessonService(repo, parser=StubParser(result_parse))

        processed = await service.process_dictation(tenant_id, "текст")

        assert [item.entity.name for item in processed.all_entities] == ["Рон", "Мия"]

    async def test_schedule_from_dictation_is_saved(self, repo: Repository, tenant_id: int) -> None:
        """Расписание, названное в диктовке, сохраняется у ученика."""
        await self._seed(repo, tenant_id, "Мия")
        result_parse = LessonParseResult(
            entity_name="Мия",
            topic="Виды матов",
            attendance="present",
            lesson_date=TODAY,
            schedule_days=["wed"],
            schedule_time="15:15",
        )
        service = LessonService(repo, parser=StubParser(result_parse))

        processed = await service.process_dictation(tenant_id, "текст")

        assert processed.entity.schedule_days == "wed"
        assert processed.entity.schedule_time == "15:15"

    async def test_schedule_saved_for_all_named_students(
        self, repo: Repository, tenant_id: int
    ) -> None:
        """Расписание из диктовки сохраняется у каждого названного ученика."""
        await self._seed(repo, tenant_id, "Рон")
        await self._seed(repo, tenant_id, "Мия")
        result_parse = LessonParseResult(
            entity_name="Рон",
            extra_entities=["Мия"],
            topic="Виды матов",
            attendance="present",
            lesson_date=TODAY,
            schedule_days=["wed"],
            schedule_time="15:15",
        )
        service = LessonService(repo, parser=StubParser(result_parse))

        processed = await service.process_dictation(tenant_id, "текст")

        assert processed.entity.schedule_days == "wed"
        assert processed.others[0].entity.schedule_days == "wed"

    async def test_schedule_from_dictation_creates_slot(
        self, repo: Repository, tenant_id: int
    ) -> None:
        """Расписание из диктовки пишется в таблицу schedule_slots (как кабинет)."""
        await self._seed(repo, tenant_id, "Мия")
        result_parse = LessonParseResult(
            entity_name="Мия",
            topic="Виды матов",
            attendance="present",
            lesson_date=TODAY,
            schedule_days=["wed"],
            schedule_time="15:15",
        )
        service = LessonService(repo, parser=StubParser(result_parse))

        processed = await service.process_dictation(tenant_id, "текст")

        slots = await repo.schedule.list_by_entity(tenant_id, processed.entity.id)
        assert len(slots) == 1
        assert slots[0].weekday == "wed"
        assert slots[0].start_time == "15:15"
        assert slots[0].is_active is True

    async def test_schedule_from_dictation_creates_slot_for_each_day(
        self, repo: Repository, tenant_id: int
    ) -> None:
        """Каждый названный день недели даёт отдельный слот."""
        await self._seed(repo, tenant_id, "Мия")
        result_parse = LessonParseResult(
            entity_name="Мия",
            topic="Виды матов",
            attendance="present",
            lesson_date=TODAY,
            schedule_days=["mon", "wed"],
            schedule_time="16:00",
        )
        service = LessonService(repo, parser=StubParser(result_parse))

        processed = await service.process_dictation(tenant_id, "текст")

        slots = await repo.schedule.list_by_entity(tenant_id, processed.entity.id)
        assert {(slot.weekday, slot.start_time) for slot in slots} == {
            ("mon", "16:00"),
            ("wed", "16:00"),
        }

    async def test_schedule_from_dictation_does_not_duplicate_slot(
        self, repo: Repository, tenant_id: int
    ) -> None:
        """Повторная диктовка с тем же расписанием не создаёт дубль слота."""
        await self._seed(repo, tenant_id, "Мия")
        result_parse = LessonParseResult(
            entity_name="Мия",
            topic="Виды матов",
            attendance="present",
            lesson_date=TODAY,
            schedule_days=["wed"],
            schedule_time="15:15",
        )
        service = LessonService(repo, parser=StubParser(result_parse))

        first = await service.process_dictation(tenant_id, "текст")
        await service.process_dictation(tenant_id, "текст")

        slots = await repo.schedule.list_by_entity(tenant_id, first.entity.id)
        assert len(slots) == 1

    async def test_schedule_from_dictation_without_time_creates_no_slot(
        self, repo: Repository, tenant_id: int
    ) -> None:
        """Без времени слот создать нельзя — таблица остаётся пустой."""
        await self._seed(repo, tenant_id, "Мия")
        result_parse = LessonParseResult(
            entity_name="Мия",
            topic="Виды матов",
            attendance="present",
            lesson_date=TODAY,
            schedule_days=["wed"],
        )
        service = LessonService(repo, parser=StubParser(result_parse))

        processed = await service.process_dictation(tenant_id, "текст")

        slots = await repo.schedule.list_by_entity(tenant_id, processed.entity.id)
        assert slots == []

    async def test_schedule_from_dictation_creates_slots_for_all_students(
        self, repo: Repository, tenant_id: int
    ) -> None:
        """Слоты создаются у каждого названного ученика."""
        await self._seed(repo, tenant_id, "Рон")
        await self._seed(repo, tenant_id, "Мия")
        result_parse = LessonParseResult(
            entity_name="Рон",
            extra_entities=["Мия"],
            topic="Виды матов",
            attendance="present",
            lesson_date=TODAY,
            schedule_days=["wed"],
            schedule_time="15:15",
        )
        service = LessonService(repo, parser=StubParser(result_parse))

        processed = await service.process_dictation(tenant_id, "текст")

        primary_slots = await repo.schedule.list_by_entity(tenant_id, processed.entity.id)
        other_slots = await repo.schedule.list_by_entity(tenant_id, processed.others[0].entity.id)
        assert len(primary_slots) == 1
        assert len(other_slots) == 1

    async def test_payment_without_lessons_count_is_inferred(
        self, repo: Repository, tenant_id: int
    ) -> None:
        """«Оплата 120» без числа занятий пополняет баланс по цене занятия."""
        await self._seed(repo, tenant_id, "Рон")
        result_parse = LessonParseResult(
            entity_name="Рон",
            topic="Виды матов",
            attendance="present",
            lesson_date=TODAY,
            payment=PaymentInfo(has_payment=True, amount=120.0, lessons_added=None),
        )
        service = LessonService(repo, parser=StubParser(result_parse))

        processed = await service.process_dictation(tenant_id, "текст")

        assert processed.payment is not None
        assert processed.payment.lessons_added == 4

    async def test_delete_lesson(self, repo: Repository, tenant_id: int) -> None:
        """Урок удаляется по идентификатору."""
        await self._seed(repo, tenant_id, "Миша")
        service = LessonService(repo, parser=StubParser(LessonParseResult(entity_name="Миша")))
        processed = await service.apply_parse_result(
            tenant_id,
            LessonParseResult(entity_name="Миша", topic="Тема", lesson_date=TODAY),
        )

        assert await service.delete_lesson(tenant_id, processed.lesson.id) is True
        assert await service.get_last_lesson(tenant_id, processed.entity.id) is None


# ----------------------------------------------------------------------
# WhatsAppLinkService
# ----------------------------------------------------------------------


class TestWhatsAppLinkService:
    """Проверки формирования ссылок и текстов WhatsApp."""

    async def test_normalize_phone_strips_formatting(self) -> None:
        """Номер очищается от пробелов, скобок и ведущего плюса."""
        assert WhatsAppLinkService.normalize_phone("+972 54-123-45-67") == "972541234567"

    @pytest.mark.parametrize("raw", [None, "", "123", "abc"])
    def test_normalize_phone_rejects_invalid(self, raw: str | None) -> None:
        """Некорректные номера дают None."""
        assert WhatsAppLinkService.normalize_phone(raw) is None

    async def test_build_lesson_report_contains_key_fields(
        self, repo: Repository, tenant_id: int
    ) -> None:
        """Отчёт родителю содержит тему, оценку, ДЗ и остаток."""
        entities = EntityService(repo)
        entity, _ = await entities.get_or_create(tenant_id, "Миша")
        await entities.set_parent_phone(tenant_id, entity.id, "+972541234567")
        refreshed = await entities.get_by_id(tenant_id, entity.id)
        assert refreshed is not None

        service = LessonService(repo, parser=StubParser(LessonParseResult(entity_name="Миша")))
        processed = await service.apply_parse_result(
            tenant_id,
            LessonParseResult(
                entity_name="Миша",
                topic="Итальянская партия",
                homework="Решить 5 задач",
                rating=4,
                next_step="Дебютные ловушки",
                lesson_date=TODAY,
            ),
        )

        wa = WhatsAppLinkService(base_url="https://planzy.dvorzhik.site")
        message = wa.build_lesson_report(processed.entity, processed.lesson)

        assert message.has_phone is True
        assert message.url.startswith("https://wa.me/972541234567?text=")
        assert "Итальянская партия" in message.text
        assert "4/5" in message.text
        assert "Решить 5 задач" in message.text
        assert "Дебютные ловушки" in message.text
        assert f"/l/{processed.entity.public_token}" in message.text

    async def test_build_lesson_report_without_phone(
        self, repo: Repository, tenant_id: int
    ) -> None:
        """Без телефона ссылка ведёт на выбор получателя в WhatsApp."""
        entities = EntityService(repo)
        entity, _ = await entities.get_or_create(tenant_id, "Миша")
        service = LessonService(repo, parser=StubParser(LessonParseResult(entity_name="Миша")))
        processed = await service.apply_parse_result(
            tenant_id,
            LessonParseResult(entity_name="Миша", topic="Тема", lesson_date=TODAY),
        )

        wa = WhatsAppLinkService(base_url="https://planzy.dvorzhik.site")
        message = wa.build_lesson_report(processed.entity, processed.lesson)

        assert message.has_phone is False
        assert message.url.startswith("https://wa.me/?text=")

    async def test_build_reminder_has_no_balance_warning(
        self, repo: Repository, tenant_id: int
    ) -> None:
        """В напоминании больше нет предупреждений о балансе."""
        entities = EntityService(repo)
        entity, _ = await entities.get_or_create(tenant_id, "Миша")
        refreshed = await entities.get_by_id(tenant_id, entity.id)
        assert refreshed is not None

        wa = WhatsAppLinkService(base_url="https://planzy.dvorzhik.site")
        message = wa.build_reminder(refreshed, lesson_date=TODAY)

        assert "Напоминаю о занятии" in message.text
        assert "пополнить баланс" not in message.text

    def test_build_reminder_greeting_is_dobrogo_dnya(self) -> None:
        """Приветствие в напоминании — «Доброго дня!»."""
        entity = Entity(tenant_id=1, name="Миша", parent_phone="972501234567")

        message = WhatsAppLinkService(base_url="https://planzy.duckdns.org").build_reminder(
            entity, lesson_date=TODAY, lesson_time="17:45"
        )

        assert message.text.startswith("Доброго дня!")
        assert "Здравствуйте" not in message.text

    def test_build_reminder_shows_lesson_time_after_date(self) -> None:
        """Время занятия подставляется сразу после даты."""
        entity = Entity(tenant_id=1, name="Миша", parent_phone="972501234567")

        message = WhatsAppLinkService(base_url="https://planzy.duckdns.org").build_reminder(
            entity, lesson_date=TODAY, lesson_time="17:45"
        )

        assert f"📅 {TODAY:%d.%m.%Y} в 17:45" in message.text

    def test_build_reminder_ignores_legacy_schedule_time(self) -> None:
        """Устаревшее поле Entity.schedule_time больше не используется.

        После перехода на web-first это поле пустое у всех, поэтому время
        берётся только из параметра ``lesson_time``.
        """
        entity = Entity(
            tenant_id=1,
            name="Миша",
            parent_phone="972501234567",
            schedule_time="08:00",
        )

        message = WhatsAppLinkService(base_url="https://planzy.duckdns.org").build_reminder(
            entity, lesson_date=TODAY, lesson_time="17:45"
        )

        assert "17:45" in message.text
        assert "08:00" not in message.text

    def test_build_reminder_without_time_has_no_time_part(self) -> None:
        """Без времени занятия строка даты остаётся без «в ЧЧ:ММ»."""
        entity = Entity(tenant_id=1, name="Миша", parent_phone="972501234567")

        message = WhatsAppLinkService(base_url="https://planzy.duckdns.org").build_reminder(
            entity, lesson_date=TODAY
        )

        assert f"📅 {TODAY:%d.%m.%Y}" in message.text
        assert " в " not in message.text

    async def test_build_card_url_requires_token(self, repo: Repository, tenant_id: int) -> None:
        """Ссылка на карточку строится из публичного токена."""
        entities = EntityService(repo)
        entity, _ = await entities.get_or_create(tenant_id, "Миша")

        wa = WhatsAppLinkService(base_url="https://planzy.dvorzhik.site/")

        assert wa.build_card_url(entity) == f"https://planzy.dvorzhik.site/l/{entity.public_token}"
        assert (
            wa.build_dossier_url(entity)
            == f"https://planzy.dvorzhik.site/dossier/{entity.public_token}"
        )

    def test_normalize_chat_url_accepts_invite(self) -> None:
        """Ссылка-приглашение принимается и приводится к https."""
        assert (
            WhatsAppLinkService.normalize_chat_url("chat.whatsapp.com/AbCdEf123")
            == "https://chat.whatsapp.com/AbCdEf123"
        )
        assert (
            WhatsAppLinkService.normalize_chat_url("https://chat.whatsapp.com/AbCdEf123/")
            == "https://chat.whatsapp.com/AbCdEf123"
        )

    @pytest.mark.parametrize(
        "raw",
        [
            None,
            "",
            "   ",
            "https://example.com/chat",
            "https://wa.me/972501234567",
            "https://chat.whatsapp.com",
            "https://chat.whatsapp.com/",
        ],
    )
    def test_normalize_chat_url_rejects_foreign(self, raw: str | None) -> None:
        """Всё, кроме ссылки-приглашения, отбрасывается."""
        assert WhatsAppLinkService.normalize_chat_url(raw) is None

    def test_build_reminder_for_group_uses_chat_url(self) -> None:
        """Для группы напоминание адресуется в её чат."""
        group = Entity(
            tenant_id=1,
            name="Новички",
            entity_type="group",
            whatsapp_chat_url="https://chat.whatsapp.com/AbCdEf123",
        )

        message = WhatsAppLinkService(base_url="https://planzy.dvorzhik.site").build_reminder(
            group, lesson_date=TODAY
        )

        assert message.chat_url == "https://chat.whatsapp.com/AbCdEf123"
        assert message.url == "https://chat.whatsapp.com/AbCdEf123"
        assert message.has_phone is True

    def test_build_reminder_for_group_includes_time(self) -> None:
        """Напоминание группе тоже содержит дату и время занятия."""
        group = Entity(
            tenant_id=1,
            name="Новички",
            entity_type="group",
            whatsapp_chat_url="https://chat.whatsapp.com/AbCdEf123",
        )

        message = WhatsAppLinkService(base_url="https://planzy.duckdns.org").build_reminder(
            group, lesson_date=TODAY, lesson_time="18:00"
        )

        assert message.text.startswith("Доброго дня!")
        assert f"📅 {TODAY:%d.%m.%Y} в 18:00" in message.text

    def test_group_link_has_no_prefilled_text(self) -> None:
        """Приглашение в чат не содержит ?text= — ограничение WhatsApp.

        У ссылок chat.whatsapp.com нет параметра подстановки сообщения,
        поэтому текст для группы отправляется отдельно в Telegram.
        """
        group = Entity(
            tenant_id=1,
            name="Новички",
            entity_type="group",
            whatsapp_chat_url="https://chat.whatsapp.com/AbCdEf123",
        )

        message = WhatsAppLinkService(base_url="https://planzy.duckdns.org").build_reminder(
            group, lesson_date=TODAY, lesson_time="18:00"
        )

        assert "?text=" not in message.url
        # Текст всё равно сформирован — его отправляют отдельным сообщением.
        assert "Напоминаю о занятии" in message.text

    @pytest.mark.parametrize(
        ("entity_type", "expected"),
        [
            ("student", "Доброго дня! Информация по занятиям — Новички."),
            ("group", "Доброго дня! Информация по группе «Новички»."),
        ],
    )
    def test_dossier_greeting_is_dobrogo_dnya(self, entity_type: str, expected: str) -> None:
        """Досье родителю и группе начинается с «Доброго дня!»."""
        entity = Entity(
            tenant_id=1,
            name="Новички",
            entity_type=entity_type,
            parent_phone="972501234567",
            whatsapp_chat_url="https://chat.whatsapp.com/AbCdEf123",
        )

        message = WhatsAppLinkService(base_url="https://planzy.duckdns.org").build_dossier_message(
            entity, "📁 Досье: Новички"
        )

        assert message.text.startswith(expected)
        assert "Здравствуйте" not in message.text

    def test_lesson_report_greeting_is_dobrogo_dnya(self) -> None:
        """Отчёт об уроке начинается с «Доброго дня!»."""
        entity = Entity(tenant_id=1, name="Миша", parent_phone="972501234567")
        lesson = Lesson(
            tenant_id=1,
            entity_id=1,
            lesson_date=TODAY,
            attendance="был",
        )

        message = WhatsAppLinkService(base_url="https://planzy.duckdns.org").build_lesson_report(
            entity, lesson
        )

        assert message.text.startswith("Доброго дня! Отчёт о занятии — Миша.")
        assert "Здравствуйте" not in message.text

    def test_build_dossier_message_for_group(self) -> None:
        """Сообщение досье для группы адресуется в чат и упоминает группу."""
        group = Entity(
            tenant_id=1,
            name="Новички",
            entity_type="group",
            whatsapp_chat_url="https://chat.whatsapp.com/AbCdEf123",
        )

        message = WhatsAppLinkService(
            base_url="https://planzy.dvorzhik.site"
        ).build_dossier_message(group, "📁 Досье: Новички")

        assert message.chat_url == "https://chat.whatsapp.com/AbCdEf123"
        assert "по группе «Новички»" in message.text
        assert "📁 Досье: Новички" in message.text

    def test_build_dossier_message_for_student(self) -> None:
        """Сообщение досье ученика адресуется родителю по номеру."""
        student = Entity(
            tenant_id=1,
            name="Миша",
            entity_type="student",
            parent_phone="972501234567",
        )

        message = WhatsAppLinkService(
            base_url="https://planzy.dvorzhik.site"
        ).build_dossier_message(student, "📁 Досье: Миша")

        assert message.chat_url is None
        assert message.url.startswith("https://wa.me/972501234567?text=")
        assert "по занятиям — Миша" in message.text

    async def test_truncates_long_message(self) -> None:
        """Слишком длинный текст обрезается под лимит wa.me."""
        wa = WhatsAppLinkService(base_url="https://planzy.dvorzhik.site")

        message = wa._build_message("+972541234567", "Т" * 3000)

        # Текст обрезан до лимита и помечен многоточием.
        assert len(message.text) <= MAX_MESSAGE_LENGTH
        assert message.text.endswith("…")
        # Ссылка построена по адресному номеру и содержит закодированный текст.
        assert message.url.startswith("https://wa.me/972541234567?text=")
        assert message.has_phone is True

    async def test_short_message_not_truncated(self) -> None:
        """Короткий текст не обрезается."""
        wa = WhatsAppLinkService(base_url="https://planzy.dvorzhik.site")

        message = wa._build_message("+972541234567", "Короткий отчёт")

        assert message.text == "Короткий отчёт"
        assert not message.text.endswith("…")


# ----------------------------------------------------------------------
# DossierService
# ----------------------------------------------------------------------


class TestDossierService:
    """Проверки сборки досье ученика."""

    async def _seed_student(self, repo: Repository, tenant_id: int) -> int:
        """Создаёт ученика с историей уроков и платежом.

        Returns:
            Идентификатор ученика.
        """
        entities = EntityService(repo)
        entity, _ = await entities.get_or_create(tenant_id, "Миша")
        await entities.set_schedule(tenant_id, entity.id, ["mon", "wed"], "16:00")
        await PaymentService(repo).register_payment(
            tenant_id, entity.id, amount=500.0, lessons_added=8
        )

        service = LessonService(repo, parser=StubParser(LessonParseResult(entity_name="Миша")))
        lessons = [
            (date(2026, 9, 1), "Дебют", 3, "Задачи 1-5", "Тактика"),
            (date(2026, 9, 8), "Миттельшпиль", 4, "Задачи 6-10", "Эндшпиль"),
            (date(2026, 9, 15), "Эндшпиль", 5, "Задачи 11-15", "Разбор партий"),
        ]
        for day, topic, rating, homework, next_step in lessons:
            await service.apply_parse_result(
                tenant_id,
                LessonParseResult(
                    entity_name="Миша",
                    topic=topic,
                    rating=rating,
                    homework=homework,
                    next_step=next_step,
                    attendance="present",
                    lesson_date=day,
                ),
            )
        return entity.id

    async def test_build_aggregates_data(self, repo: Repository, tenant_id: int) -> None:
        """Досье собирает баланс, количество уроков и сумму оплат."""
        entity_id = await self._seed_student(repo, tenant_id)
        dossier = await DossierService(repo).build(tenant_id, entity_id)

        assert dossier is not None
        assert dossier.name == "Миша"
        assert dossier.lessons_count == 3
        assert dossier.total_paid == 500.0
        assert dossier.last_payment is not None

    async def test_build_computes_average_and_trend(self, repo: Repository, tenant_id: int) -> None:
        """Средняя оценка и динамика рассчитываются по истории."""
        entity_id = await self._seed_student(repo, tenant_id)
        dossier = await DossierService(repo).build(tenant_id, entity_id)

        assert dossier is not None
        assert dossier.average_rating == 4.0
        # Свежие оценки (5) выше старых (3, 4) → рост.
        assert dossier.rating_trend == "up"

    async def test_build_takes_last_next_step(self, repo: Repository, tenant_id: int) -> None:
        """План берётся с самого свежего урока."""
        entity_id = await self._seed_student(repo, tenant_id)
        dossier = await DossierService(repo).build(tenant_id, entity_id)

        assert dossier is not None
        assert dossier.last_next_step == "Разбор партий"
        assert dossier.last_homework == "Задачи 11-15"

    async def test_build_collects_weak_topics(self, repo: Repository, tenant_id: int) -> None:
        """Темы с оценкой ≤ 3 попадают в слабые."""
        entity_id = await self._seed_student(repo, tenant_id)
        dossier = await DossierService(repo).build(tenant_id, entity_id)

        assert dossier is not None
        assert dossier.weak_topics == ["Дебют"]

    async def test_build_by_token(self, repo: Repository, tenant_id: int) -> None:
        """Досье собирается по публичному токену."""
        entity_id = await self._seed_student(repo, tenant_id)
        entity = await EntityService(repo).get_by_id(tenant_id, entity_id)
        assert entity is not None

        dossier = await DossierService(repo).build_by_token(entity.public_token)

        assert dossier is not None
        assert dossier.entity.id == entity_id

    async def test_build_returns_none_for_unknown(self, repo: Repository, tenant_id: int) -> None:
        """Для несуществующего ученика возвращается None."""
        assert await DossierService(repo).build(tenant_id, 9999) is None
        assert await DossierService(repo).build_by_token("нет-такого-токена") is None

    async def test_build_empty_history(self, repo: Repository, tenant_id: int) -> None:
        """Досье без уроков собирается без ошибок."""
        entities = EntityService(repo)
        entity, _ = await entities.get_or_create(tenant_id, "Новичок")

        dossier = await DossierService(repo).build(tenant_id, entity.id)

        assert dossier is not None
        assert dossier.lessons_count == 0
        assert dossier.history == []
        assert dossier.average_rating is None
        assert dossier.rating_trend is None
        assert dossier.last_next_step is None

    async def test_format_for_chat_contains_sections(
        self, repo: Repository, tenant_id: int
    ) -> None:
        """Текст для чата содержит ключевые разделы."""
        entity_id = await self._seed_student(repo, tenant_id)
        dossier = await DossierService(repo).build(tenant_id, entity_id)
        assert dossier is not None

        text = DossierService.format_for_chat(dossier)

        assert "Досье: Миша" in text
        assert "Средняя оценка: 4.0" in text
        assert "План с прошлого занятия: Разбор партий" in text
        assert "Слабые темы: Дебют" in text
        assert "История тем:" in text

    async def test_format_for_chat_empty_history(self, repo: Repository, tenant_id: int) -> None:
        """Для пустой истории выводится соответствующая заглушка."""
        entities = EntityService(repo)
        entity, _ = await entities.get_or_create(tenant_id, "Новичок")
        dossier = await DossierService(repo).build(tenant_id, entity.id)
        assert dossier is not None

        text = DossierService.format_for_chat(dossier)

        assert "История занятий пока пуста." in text
