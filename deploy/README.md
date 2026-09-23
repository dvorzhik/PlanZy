# PlanZy — инструкция по деплою на VPS

Пошаговое руководство по развёртыванию PlanZy на VPS под Ubuntu 24.04 LTS.

**Итоговая схема:**

```
Интернет → Nginx (443, SSL) → uvicorn 127.0.0.1:8000
                                  └── PlanZy (FastAPI + aiogram polling + APScheduler)
                                          └── SQLite (WAL) /opt/planzy/app/tutor.sqlite3
```

---

## Текущий рабочий сервер

> Раздел описывает **фактически используемый** сервер. Ниже в разделах 9.1–9.9
> приведена общая процедура первичной установки — она нужна, если поднимается
> новый сервер с нуля.

| Параметр | Значение |
|---|---|
| SSH-алиас | `oracle1` |
| Хост | `129.159.151.179` (Oracle Cloud, США) |
| Hostname | `roomie` |
| Пользователь SSH | `ubuntu` (ключ `~/.ssh/roomie_key`) |
| ОС | Ubuntu 24.04.4 LTS |
| Часовой пояс | `Asia/Jerusalem` (IDT, +0300) |
| Публичный домен | `https://planzy.duckdns.org` |
| Каталог проекта | `/opt/planzy/app` |
| Пользователь сервиса | `planzy` (система, `--home /opt/planzy`) |
| systemd-сервис | `planzy.service` (enabled, active) |
| Python | 3.12.3 (`/opt/planzy/app/.venv`) |
| Порт приложения | `127.0.0.1:8000` (uvicorn, `--workers 1`) |
| База данных | `/opt/planzy/app/tutor.sqlite3` (SQLite WAL) |
| Бэкапы | `/opt/planzy/app/backups/`, cron `0 3 * * *` |
| Логи | `journalctl -u planzy` |

Другие серверы (не этим приложением заняты, чтобы не путать):

| SSH-алиас | Что там |
|---|---|
| `timeweb` | GeminiPoint и DrawStory |
| `oracle2` | ZenRead, Telegram-боты (kakheti, nahariya, vizbor, yamometer) |

### Быстрые команды

```bash
# Статус и логи
ssh oracle1
sudo systemctl status planzy --no-pager
sudo journalctl -u planzy -f

# Обновление кода (см. подробнее ниже)
ssh oracle1 "sudo -u planzy -H bash -lc 'cd /opt/planzy/app && ./deploy/deploy.sh'"

# Health-check
curl -s https://planzy.duckdns.org/health

# Диагностика напоминаний и планировщика
ssh oracle1 "sudo -u planzy bash -c 'cd /opt/planzy/app && .venv/bin/python scripts/diag_reminders.py'"
ssh oracle1 "sudo -u planzy bash -c 'cd /opt/planzy/app && .venv/bin/python scripts/diag_scheduler.py'"
```

---

## 9.1 Подготовка VPS

Подключитесь к серверу по SSH и выполните базовую настройку.

```bash
# Обновление системы
sudo apt update && sudo apt upgrade -y

# Базовые пакеты
sudo apt install -y python3.12 python3.12-venv python3-pip \
    git nginx sqlite3 curl ufw certbot python3-certbot-nginx

# Часовой пояс (важно для корректной работы планировщика)
sudo timedatectl set-timezone Asia/Jerusalem
timedatectl

# Файрвол: только SSH, HTTP, HTTPS
sudo ufw allow OpenSSH
sudo ufw allow 'Nginx Full'
sudo ufw --force enable
sudo ufw status
```

Создайте системного пользователя, от имени которого будет работать сервис:

```bash
sudo adduser --system --group --home /opt/planzy --shell /bin/bash planzy
```

Настройте DNS: A-запись `planzy.duckdns.org` → IP вашего VPS.
Проверьте распространение: `dig +short planzy.duckdns.org`.

---

## 9.2 Клонирование репозитория и установка зависимостей

```bash
sudo -u planzy -H bash
cd /opt/planzy/app

# Клонируем в текущую директорию (точка в конце обязательна)
git clone <URL_РЕПОЗИТОРИЯ> .

# Виртуальное окружение
python3.12 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e .

# Проверка установки
.venv/bin/python -c "import app.main; print('OK')"
```

---

## 9.3 Настройка .env на сервере

```bash
cd /opt/planzy/app
cp .env.example .env
nano .env
```

Обязательно заполните:

| Переменная | Значение |
|---|---|
| `TELEGRAM_BOT_TOKEN` | реальный токен от @BotFather |
| `OWNER_TELEGRAM_ID` | ваш Telegram user_id (узнать у @userinfobot) |
| `GEMINI_API_KEY` | ключ из Google AI Studio |
| `BASE_URL` | `https://planzy.duckdns.org` |
| `APP_ENV` | `production` |
| `LOG_LEVEL` | `INFO` |

Закройте файл от посторонних:

```bash
chmod 600 .env
```

> **Важно:** при `APP_ENV=production` схема БД **не** создаётся автоматически —
> только через Alembic (следующий шаг).

---

## 9.4 Миграции Alembic и первый тенант

```bash
cd /opt/planzy/app

# Применяем миграции
.venv/bin/alembic upgrade head

# Проверяем, что таблицы созданы
sqlite3 tutor.sqlite3 ".tables"
# Ожидаем: alembic_version  entities  lessons  payments  tenants

# Создаём тенанта-владельца (без демо-данных)
.venv/bin/python -m scripts.seed
```

Для проверки на тестовых данных можно добавить `--demo`:

```bash
.venv/bin/python -m scripts.seed --demo
```

---

## 9.5 systemd-юнит

```bash
sudo cp /opt/planzy/app/deploy/planzy.service /etc/systemd/system/planzy.service
sudo systemctl daemon-reload
sudo systemctl enable planzy
sudo systemctl start planzy

# Проверка
sudo systemctl status planzy --no-pager
sudo journalctl -u planzy -n 50 --no-pager
```

Ожидаемые строки в логе:

```
Запуск PlanZy (env=production)
Polling Telegram-бота запущен фоновой задачей
PlanZy готов к работе
```

Проверьте локально:

```bash
curl -s http://127.0.0.1:8000/health
# {"status":"ok","app":"PlanZy","env":"production","database":"ok"}
```

---

## 9.6 Nginx как reverse proxy

```bash
sudo cp /opt/planzy/app/deploy/nginx-planzy.conf /etc/nginx/sites-available/planzy
sudo ln -sf /etc/nginx/sites-available/planzy /etc/nginx/sites-enabled/planzy
sudo rm -f /etc/nginx/sites-enabled/default

sudo nginx -t
```

На этом этапе конфиг ссылается на ещё не существующий SSL-сертификат,
поэтому сначала получите сертификат (шаг 9.7), а затем перезагрузите Nginx.

> **Права для отдачи статики.** Каталог `/opt/planzy` закрыт для посторонних
> (`drwxr-x--- planzy:planzy`), а Nginx работает от `www-data`. Чтобы Nginx мог
> отдавать `favicon.ico`, `favicon.svg` и `apple-touch-icon.png` напрямую,
> добавьте его в группу проекта **до** перезапуска сервиса:
>
> ```bash
> sudo usermod -aG planzy www-data
> sudo systemctl restart nginx
> ```
>
> Без этого Nginx вернёт `403 Forbidden` на `/favicon.ico` и `/static/*`.
> Ограничиваться `chmod o+rx /opt/planzy` не рекомендуется — доступ получат
> все локальные пользователи.

---

## 9.7 SSL через Let's Encrypt

```bash
sudo certbot --nginx -d planzy.duckdns.org \
    --agree-tos -m your@email.com --redirect --non-interactive

# Проверка автопродления
sudo systemctl status certbot.timer --no-pager
sudo certbot renew --dry-run

# Перезагрузка Nginx с готовым сертификатом
sudo nginx -t && sudo systemctl reload nginx
```

Проверьте снаружи:

```bash
curl -sI https://planzy.duckdns.org/health
```

> **Внимание:** Certbot перезаписывает блок `server` и может потерять
> заголовки безопасности и `client_max_body_size` из исходного конфига.
> После выпуска сертификата сверьте их наличие:
>
> ```bash
> curl -sI https://planzy.duckdns.org/ | grep -iE 'x-content-type|x-frame|referrer'
> ```

---

## 9.8 Автоматический бэкап SQLite

```bash
chmod +x /opt/planzy/app/deploy/backup.sh
sudo mkdir -p /opt/planzy/app/backups
sudo chown planzy:planzy /opt/planzy/app/backups

# Ручной прогон
sudo -u planzy /opt/planzy/app/deploy/backup.sh

# Ежедневно в 03:00
sudo crontab -e
```

Добавьте строку:

```cron
0 3 * * * /opt/planzy/app/deploy/backup.sh >> /var/log/planzy-backup.log 2>&1
```

Скрипт использует `sqlite3 .backup` (безопасен при WAL), проверяет
целостность через `PRAGMA integrity_check`, сжимает gzip и хранит
последние 14 копий в `/opt/planzy/app/backups/`.

> **Совет:** дополнительно настройте выгрузку каталога `backups/`
> на внешнее хранилище (S3, Яндекс.Диск) — локальные бэкапы не спасут
> при потере сервера.

---

## 9.9 Проверка работы 24/7

```bash
# Сервис поднимается после перезагрузки
sudo reboot
# ... после перезагрузки:
sudo systemctl is-active planzy          # active
sudo systemctl is-enabled planzy         # enabled

# Проверка отказоустойчивости: убиваем процесс — systemd перезапустит
sudo pkill -f "uvicorn app.main:app"
sleep 8
sudo systemctl status planzy --no-pager  # снова active

# Проверка бота: напишите боту в Telegram — он должен ответить
# Проверка веба: откройте https://planzy.duckdns.org/
```

Мониторинг:

```bash
# Логи в реальном времени
sudo journalctl -u planzy -f

# Ошибки за сутки
sudo journalctl -u planzy --since "24 hours ago" -p err --no-pager
```

---

## Обновление кода

```bash
sudo -u planzy -H bash -lc 'cd /opt/planzy/app && ./deploy/deploy.sh'
```

Скрипт выполняет `git pull --ff-only`, обновляет зависимости, применяет
миграции, перезапускает сервис и проверяет health-check.

> **Путь к скрипту.** Проект развёрнут в `/opt/planzy/app`, поэтому
> скрипт лежит в `/opt/planzy/app/deploy/deploy.sh`. Перед запуском
> сверьте фактическое расположение: `sudo -u planzy git -C
> /opt/planzy/app rev-parse --show-toplevel`.

### Перезапуск сервиса без ввода пароля

Шаг перезапуска использует `sudo -n systemctl restart planzy`. По SSH TTY
нет, поэтому `sudo` не может запросить пароль — без дополнительной
настройки шаг упадёт. Нужно установить sudoers-правило для пользователя
`planzy` (разово, при настройке сервера):

```bash
sudo install -m 0440 -o root -g root \
    /opt/planzy/app/deploy/sudoers-planzy-deploy \
    /etc/sudoers.d/planzy-deploy
sudo visudo -c          # проверка синтаксиса
```

Правило узкое: разрешает только `restart`/`status`/`is-active` **своего**
сервиса, плюс снимает требование TTY (`!requiretty`). Произвольные команды
от `planzy` выполнять по-прежнему нельзя — убедиться можно так:

```bash
sudo -u planzy sudo -n -l
```

> ⚠️ Файлы в `/etc/sudoers.d/` должны быть доступны на запись только root
> (режим `0440`), иначе `sudo` их молча проигнорирует. Всегда проверяйте
> результат командой `sudo visudo -c`.

> ⚠️ **Почему не `sudo bash deploy/deploy.sh`.** Запуск от root ломает
> шаг 1: git отказывается работать в репозитории, принадлежащем `planzy`
> («detected dubious ownership»). Поэтому деплой запускается от
> пользователя `planzy`, а права на рестарт даёт sudoers-правило.

> ℹ️ Скрипты `deploy.sh` и `backup.sh` хранятся в git с битом выполнения
> (`100755`), поэтому после `git pull` они сразу исполняемы. Если права
> потерялись (например, из-за монтирования без `exec`), восстановите их:
> `chmod +x /opt/planzy/app/deploy/*.sh`.

### Откат

Перед деплоем сделайте бэкап БД, чтобы можно было откатиться:

```bash
sudo -u planzy sqlite3 /opt/planzy/app/tutor.sqlite3 \
    ".backup /opt/planzy/app/backups/predeploy-$(date +%Y%m%d-%H%M%S).sqlite3"
```

Откат кода — возврат на предыдущий коммит и перезапуск:

```bash
sudo -u planzy git -C /opt/planzy/app reset --hard <предыдущий_sha>
sudo systemctl restart planzy
```

---

## Диагностика типовых проблем

| Симптом | Причина | Решение |
|---|---|---|
| `База данных недоступна` | не применены миграции | `.venv/bin/alembic upgrade head` |
| Бот не отвечает | неверный `TELEGRAM_BOT_TOKEN` или `OWNER_TELEGRAM_ID` | проверить `.env`, `journalctl -u planzy` |
| `502 Bad Gateway` | сервис не запущен | `systemctl status planzy`, смотреть лог |
| `Conflict: terminated by other getUpdates` | запущено два экземпляра | `pkill -f uvicorn`, оставить один сервис |
| Рассылка не приходит в указанное время | правило выключено или время указано неверно | `/admin/reminders`: время берётся из правила, `journalctl -u planzy \| grep 'Проверка'` |
| Рассылка не приходит вообще | нет занятий по расписанию на нужную дату | `.venv/bin/python scripts/diag_reminders.py` |
| Часовой пояс сбит | `tenants.timezone` не совпадает с реальным | `/admin/settings` → «Часовой пояс», затем `timedatectl` |
| `UnicodeEncodeError` в скриптах | локаль не UTF-8 | `PYTHONIOENCODING=utf-8` (уже задан в юните) |

---

## Как работает рассылка напоминаний

Чтобы не искать причину «напоминание пришло не вовремя» вслепую.

**Модель.** Правило (`reminder_rules`) состоит из двух значимых полей:

| Поле | Смысл |
|---|---|
| `kind` | *какой день*: `morning_of` — занятие сегодня, `day_before` — занятие завтра |
| `time_of_day` | *во сколько* отправить (локальное время тренера, `ЧЧ:ММ`) |

Больше ничего на время не влияет. Значения из `.env`
(`MORNING_DISPATCH_TIME` / `DAY_BEFORE_DISPATCH_TIME`) **рассылка не использует** —
они оставлены только для `scripts/diag_scheduler.py`.

**Механика.**

1. Планировщик просыпается по задаче-тику `reminder_tick` каждые
   `DISPATCH_TICK_MINUTES` (5) минут — см. `app/scheduler/scheduler.py`.
2. `MorningDispatchService.run_due()` берёт включённые правила всех активных
   тенантов и для каждого правила собирает занятия нужной даты.
3. Момент отправки = дата занятия + `time_of_day`, в часовом поясе тенанта
   (`tenants.timezone`, откат на `TIMEZONE` из `.env`).
4. Напоминание уходит, если момент наступил и прошло не больше
   `SEND_WINDOW` (1 час). Просроченные напоминания после простоя **не**
   отправляются — это защита, а не поломка.
5. Повторная отправка невозможна: журнал `dispatch_log` хранит уникальную
   комбинацию `tenant_id + entity_id + rule_kind + dispatch_date`.

**Проверка.** В логе видны обе строки на каждый тик:

```
Проверка правил напоминаний на 2026-09-23T20:00:00+03:00: тенантов 1
Проверка завершена: отправлено 3, без телефона 1, дублей 0, не наступило 0, ошибок 0
```

Расшифровка: `без телефона` — у ученика нет телефона родителя;
`дублей` — уже отправляли сегодня; `не наступило` — время правила ещё не пришло.

