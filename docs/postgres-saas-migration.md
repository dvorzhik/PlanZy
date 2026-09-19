# План миграции на PostgreSQL и SaaS

Документ описывает, как превратить текущий однопользовательский MVP
на SQLite в многопользовательский SaaS на PostgreSQL.

**Триггер миграции:** появление второго платящего тренера.

---

## Почему именно этот момент

SQLite с WAL отлично держит нагрузку одного тренера: десятки записей
в день, единичные читатели публичных страниц. Но у него есть жёсткие
пределы:

| Ограничение SQLite | Последствие для SaaS |
|---|---|
| Один писатель за раз (блокировка на уровне БД) | При 10+ активных тренерах начнутся `database is locked` |
| Файл на одном сервере | Нельзя горизонтально масштабироваться |
| Нет ролей и прав | Изоляция тенантов держится только на коде |
| Нет репликации из коробки | Бэкап = копия файла, точка восстановления — на момент копии |

PostgreSQL снимает все четыре ограничения.

---

## Что уже готово к миграции

Архитектура изначально закладывалась под этот переход:

1. **`tenant_id` во всех таблицах данных** — схема не изменится,
   изменится только способ контроля доступа.
2. **Весь SQL — в [`app/db/repository.py`](../app/db/repository.py)** —
   каждый метод уже принимает `tenant_id`. Замена СУБД не затронет
   Service Layer, бота, веб и планировщик.
3. **SQLAlchemy 2.x async ORM** — диалект меняется одной строкой
   в `DATABASE_URL`.
4. **Alembic с `render_as_batch=True`** — миграции переносимы.

---

## Этап A. Переезд на PostgreSQL (1–2 дня)

### A.1 Подготовка окружения

```bash
# На сервере
sudo apt install -y postgresql postgresql-contrib
sudo -u postgres psql

CREATE DATABASE planzy;
CREATE USER planzy WITH ENCRYPTED PASSWORD '<сильный_пароль>';
GRANT ALL PRIVILEGES ON DATABASE planzy TO planzy;
\c planzy
GRANT ALL ON SCHEMA public TO planzy;
```

### A.2 Зависимости

В [`pyproject.toml`](../pyproject.toml) добавить:

```toml
dependencies = [
    # ...
    "asyncpg>=0.30.0",
]
```

```bash
.venv/bin/pip install -e .
```

### A.3 Правки в коде

**`DATABASE_URL`** в `.env`:

```env
DATABASE_URL=postgresql+asyncpg://planzy:<пароль>@127.0.0.1:5432/planzy
```

**`app/db/base.py`** — `_configure_sqlite()` уже вызывается условно
(только для SQLite), поэтому PRAGMA не помешают. Проверить, что
`create_engine()` корректно выбирает пул для PostgreSQL:

```python
engine_kwargs: dict = {"echo": False, "future": True}
if settings.database_url.startswith("sqlite"):
    engine_kwargs["connect_args"] = {"check_same_thread": False}
    engine_kwargs["poolclass"] = StaticPool
else:
    engine_kwargs["pool_size"] = 10
    engine_kwargs["max_overflow"] = 20
    engine_kwargs["pool_pre_ping"] = True
```

**`app/core/config.py`** — свойство `sqlite_path` должно возвращать
`None` для не-SQLite URL (уже так и работает).

### A.4 Миграции

```bash
# На чистой БД PostgreSQL
.venv/bin/alembic upgrade head
```

Миграция `ebb54b7015bc` использует `batch_alter_table`, что на PostgreSQL
работает как обычный `ALTER TABLE` — проблем не будет.

### A.5 Перенос данных

```bash
# 1. Выгружаем из SQLite
sqlite3 tutor.sqlite3 .dump > /tmp/planzy-sqlite.sql

# 2. Конвертируем (утилита pgloader делает это автоматически)
sudo apt install -y pgloader
pgloader sqlite:///opt/planzy/tutor.sqlite3 \
         postgresql://planzy:<пароль>@127.0.0.1/planzy
```

`pgloader` сам приведёт типы и перенесёт данные. После переноса
обязательно сверить количество строк:

```sql
SELECT 'tenants' AS t, count(*) FROM tenants
UNION ALL SELECT 'entities', count(*) FROM entities
UNION ALL SELECT 'lessons',  count(*) FROM lessons
UNION ALL SELECT 'payments', count(*) FROM payments;
```

### A.6 Переключение

```bash
sudo systemctl stop planzy
# ... перенос данных ...
sudo systemctl start planzy
curl -s http://127.0.0.1:8000/health
```

**Откат:** вернуть `DATABASE_URL` на SQLite и перезапустить сервис —
файл `tutor.sqlite3` остаётся нетронутым до подтверждения успеха.

### A.7 Бэкапы

[`deploy/backup.sh`](../deploy/backup.sh) заменить на `pg_dump`:

```bash
pg_dump -Fc -U planzy planzy > "/opt/planzy/backups/planzy-$(date +%F-%H%M).dump"
```

---

## Этап B. Изоляция тенантов на уровне БД (2–3 дня)

Сейчас изоляция держится на том, что каждый метод Repository принимает
`tenant_id`. Это надёжно, но одна забытая проверка = утечка данных.
PostgreSQL позволяет продублировать защиту на уровне СУБД.

### B.1 Row-Level Security

```sql
ALTER TABLE entities ENABLE ROW LEVEL SECURITY;
ALTER TABLE lessons  ENABLE ROW LEVEL SECURITY;
ALTER TABLE payments ENABLE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation ON entities
    USING (tenant_id = current_setting('app.tenant_id')::int);
-- аналогично для lessons и payments
```

В начале каждой сессии:

```python
await session.execute(text("SET LOCAL app.tenant_id = :tid"), {"tid": tenant_id})
```

Это можно централизовать в `get_session()` через контекстный менеджер,
принимающий `tenant_id`.

### B.2 Тесты

Добавить тест, который пытается прочитать данные чужого тенанта
и ожидает пустой результат — даже если в запросе забыт фильтр.

---

## Этап C. SaaS-функциональность (1–2 недели)

### C.1 Онбординг тренеров

Сейчас тенант создаётся скриптом [`scripts/seed.py`](../scripts/seed.py).
Для SaaS нужен самостоятельный вход:

- команда `/start` в боте создаёт нового тенанта, если `user_id` неизвестен;
- приветственное сообщение с инструкцией;
- ограничение: N дней бесплатно, затем подписка.

### C.2 Биллинг

Варианты для израильского рынка:

| Провайдер | Плюсы |
|---|---|
| **Stripe** | зрелый API, подписки, вебхуки |
| **Paddle** | берёт на себя налоги (merchant of record) |
| **ЮKassa** | если целевая аудитория в РФ |

Схема: таблица `subscriptions` (`tenant_id`, `status`, `plan`,
`current_period_end`, `provider_customer_id`). Middleware бота проверяет
статус и блокирует доступ при истечении.

### C.3 Лимиты и квоты

- число учеников на тарифе;
- число разборов Gemini в месяц (защита от перерасхода);
- хранение истории (например, 12 месяцев на базовом тарифе).

Счётчики — в отдельной таблице `usage_counters`, инкремент атомарный.

### C.4 Админ-панель

Минимум: список тенантов, статус подписки, потребление Gemini,
возможность заблокировать. Достаточно простой страницы на FastAPI
с HTTP Basic Auth.

### C.5 Наблюдаемость

- **Sentry** — трейсинг исключений (в боте и вебе);
- **Prometheus + Grafana** — метрики: запросы/сек, латентность Gemini,
  число активных тенантов;
- **Алерты** — падение сервиса, ошибки Gemini > 5%, диск > 80%.

---

## Этап D. Масштабирование (по мере роста)

| Проблема | Решение |
|---|---|
| Один процесс не тянет | Несколько uvicorn-воркеров + вынести бота в отдельный процесс |
| Два процесса бота конфликтуют | Один polling-процесс, остальные — только веб |
| Планировщик дублирует рассылку | Вынести в отдельный сервис или использовать `pg_advisory_lock` |
| Медленные запросы | Индексы на `(tenant_id, lesson_date)`, `(tenant_id, entity_id)` |
| Рост БД | Партиционирование `lessons` по дате |
| Геораспределённость | Read-replica для публичных страниц |

---

## Чек-лист готовности к SaaS

- [ ] PostgreSQL в проде, SQLite только для локальной разработки
- [ ] RLS включён на всех таблицах данных
- [ ] Тест на изоляцию тенантов (попытка чтения чужих данных)
- [ ] Самостоятельная регистрация через `/start`
- [ ] Биллинг с вебхуками и обработкой отмены
- [ ] Квоты на Gemini с алертом при приближении к лимиту
- [ ] Sentry + метрики + алерты
- [ ] `pg_dump` в cron + выгрузка на внешнее хранилище
- [ ] Юридические документы: оферта, политика конфиденциальности
- [ ] Инструкция для нового тренера (онбординг-гайд)

---

## Оценка трудозатрат

| Этап | Срок | Риск |
|---|---|---|
| A. Переезд на PostgreSQL | 1–2 дня | низкий (архитектура готова) |
| B. Row-Level Security | 2–3 дня | средний (нужны тесты) |
| C. SaaS-функциональность | 1–2 недели | средний (биллинг, edge-cases) |
| D. Масштабирование | по потребности | низкий |

**Вывод:** архитектура MVP позволяет начать миграцию в любой момент
без переписывания бизнес-логики. Ключевое решение — не откладывать
`tenant_id` и Repository-слой, что уже сделано.
