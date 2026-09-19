# ============================================================
# PlanZy — production configuration (oracle1)
# ============================================================

# --- Telegram ---
TELEGRAM_BOT_TOKEN=__TELEGRAM_BOT_TOKEN__
OWNER_TELEGRAM_ID=__OWNER_TELEGRAM_ID__

# --- Gemini Flash ---
GEMINI_API_KEY=__GEMINI_API_KEY__
GEMINI_MODEL=gemini-3.6-flash

# --- База данных ---
DATABASE_URL=sqlite+aiosqlite:///./tutor.sqlite3

# --- Настройки тренера (значения по умолчанию) ---
LESSON_PRICE=30
CURRENCY=ILS

# --- Веб ---
BASE_URL=https://planzy.dvorzhik.site
WEB_HOST=127.0.0.1
WEB_PORT=8000

# --- Планировщик ---
TIMEZONE=Asia/Jerusalem
MORNING_DISPATCH_TIME=08:30
DAY_BEFORE_DISPATCH_TIME=19:00

# --- Логирование ---
LOG_LEVEL=INFO

# --- Режим работы ---
APP_ENV=production
