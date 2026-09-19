"""Настройка логирования приложения.

Единая точка конфигурации логов для бота, веба и планировщика.
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from app.core.config import BASE_DIR, settings

# Директория для файлов логов
LOG_DIR = BASE_DIR / "logs"

# Формат сообщений: время | уровень | модуль | сообщение
LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# Сторонние библиотеки, чьи логи избыточны на уровне INFO
NOISY_LOGGERS = (
    "aiogram.event",
    "aiosqlite",
    "apscheduler.executors.default",
    "httpx",
    "httpcore",
    "google.generativeai",
    "urllib3",
)


def setup_logging() -> None:
    """Инициализирует логирование: консоль + ротация в файл.

    Вызывается один раз при старте приложения.
    """
    root_logger = logging.getLogger()
    root_logger.setLevel(settings.log_level)

    # Убираем ранее добавленные хендлеры, чтобы избежать дублирования
    for handler in list(root_logger.handlers):
        root_logger.removeHandler(handler)

    formatter = logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)

    # --- Консольный хендлер ---
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    console_handler.setLevel(settings.log_level)
    root_logger.addHandler(console_handler)

    # --- Файловый хендлер с ротацией ---
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            LOG_DIR / "planzy.log",
            maxBytes=5 * 1024 * 1024,  # 5 МБ
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        file_handler.setLevel(settings.log_level)
        root_logger.addHandler(file_handler)
    except OSError:
        # Если не удалось создать файл лога (например, нет прав),
        # продолжаем работать только с консолью
        root_logger.warning("Не удалось инициализировать файловый лог, работаем только с консолью")

    # Приглушаем избыточные логи сторонних библиотек
    for logger_name in NOISY_LOGGERS:
        logging.getLogger(logger_name).setLevel(logging.WARNING)

    logging.getLogger(__name__).info(
        "Логирование инициализировано (уровень: %s, режим: %s)",
        settings.log_level,
        settings.app_env,
    )


def get_logger(name: str) -> logging.Logger:
    """Возвращает логгер с указанным именем."""
    return logging.getLogger(name)
