"""Конфигурация приложения.

Все настройки читаются из переменных окружения (файл .env).
Используется pydantic-settings для валидации типов и значений.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Корень проекта (директория, содержащая папку app/)
BASE_DIR = Path(__file__).resolve().parent.parent.parent


class Settings(BaseSettings):
    """Настройки приложения, загружаемые из .env."""

    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Telegram ---
    telegram_bot_token: str = Field(
        ...,
        description="Токен Telegram-бота от @BotFather",
    )
    owner_telegram_id: int = Field(
        ...,
        description="Telegram user_id владельца (тренера), единственный, кому отвечает бот",
    )

    # --- Gemini Flash ---
    gemini_api_key: str = Field(
        ...,
        description="API-ключ Google AI Studio для Gemini",
    )
    gemini_model: str = Field(
        default="gemini-2.0-flash",
        description="Имя модели Gemini для парсинга диктовки",
    )

    # --- База данных ---
    database_url: str = Field(
        default="sqlite+aiosqlite:///./tutor.sqlite3",
        description="URL подключения к БД (SQLAlchemy async)",
    )

    # --- Оплата ---
    lesson_price: float = Field(
        default=0.0,
        ge=0.0,
        description=(
            "Стоимость одного занятия. Если задана, бот сам считает, сколько "
            "занятий даёт названная сумма (например, 120 при цене 30 → 4 занятия). "
            "0 отключает автоматический расчёт."
        ),
    )
    currency: str = Field(
        default="ILS",
        description="Валюта по умолчанию для платежей (ILS, USD, EUR, RUB)",
    )

    # --- Веб ---
    base_url: str = Field(
        default="http://localhost:8000",
        description="Публичный базовый URL для генерации ссылок на смарт-карточки",
    )
    web_host: str = Field(default="127.0.0.1", description="Хост FastAPI")
    web_port: int = Field(default=8000, description="Порт FastAPI")

    # --- Планировщик ---
    timezone: str = Field(
        default="Asia/Jerusalem",
        description="Часовой пояс для утренней рассылки",
    )
    morning_dispatch_time: str = Field(
        default="08:30",
        description="Время утренней рассылки в формате ЧЧ:ММ",
    )
    day_before_dispatch_time: str = Field(
        default="19:00",
        description="Время вечерней рассылки накануне занятий в формате ЧЧ:ММ",
    )

    # --- Логирование ---
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(
        default="INFO",
        description="Уровень логирования",
    )

    # --- Режим работы ---
    app_env: Literal["development", "production"] = Field(
        default="development",
        description="Режим работы приложения",
    )

    @field_validator("morning_dispatch_time", "day_before_dispatch_time")
    @classmethod
    def validate_dispatch_time(cls, value: str) -> str:
        """Проверяет, что время рассылки задано в формате ЧЧ:ММ."""
        parts = value.split(":")
        if len(parts) != 2:
            raise ValueError("Время должно быть в формате ЧЧ:ММ, например 08:30")
        hours, minutes = parts
        if not (hours.isdigit() and minutes.isdigit()):
            raise ValueError("Часы и минуты должны быть числами")
        if not (0 <= int(hours) <= 23):
            raise ValueError("Часы должны быть в диапазоне 0–23")
        if not (0 <= int(minutes) <= 59):
            raise ValueError("Минуты должны быть в диапазоне 0–59")
        return value

    @field_validator("base_url")
    @classmethod
    def strip_trailing_slash(cls, value: str) -> str:
        """Убирает завершающий слэш, чтобы не было двойных слэшей в ссылках."""
        return value.rstrip("/")

    @property
    def is_production(self) -> bool:
        """Признак боевого режима работы."""
        return self.app_env == "production"

    @property
    def dispatch_hour(self) -> int:
        """Час утренней рассылки."""
        return int(self.morning_dispatch_time.split(":")[0])

    @property
    def dispatch_minute(self) -> int:
        """Минуты утренней рассылки."""
        return int(self.morning_dispatch_time.split(":")[1])

    @property
    def day_before_hour(self) -> int:
        """Час вечерней рассылки накануне занятий."""
        return int(self.day_before_dispatch_time.split(":")[0])

    @property
    def day_before_minute(self) -> int:
        """Минуты вечерней рассылки накануне занятий."""
        return int(self.day_before_dispatch_time.split(":")[1])

    @property
    def sqlite_path(self) -> Path | None:
        """Путь к файлу SQLite, если используется SQLite. Иначе None."""
        prefix = "sqlite+aiosqlite:///"
        if self.database_url.startswith(prefix):
            raw_path = self.database_url[len(prefix) :]
            path = Path(raw_path)
            if not path.is_absolute():
                path = BASE_DIR / path
            return path
        return None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Возвращает единственный экземпляр настроек (кэшируется)."""
    return Settings()  # type: ignore[call-arg]


# Удобный глобальный доступ к настройкам
settings = get_settings()
