#!/usr/bin/env bash
# Проверка конфигурации и импорта приложения PlanZy.
set -eu
cd /opt/planzy/app
.venv/bin/python - <<'PY'
from app.core.config import settings
print("env       :", settings.app_env)
print("production:", settings.is_production)
print("base_url  :", settings.base_url)
print("web       :", settings.web_host, settings.web_port)
print("timezone  :", settings.timezone)
print("sqlite    :", settings.sqlite_path)
import app.main as m
print("IMPORT_OK :", m.app.title)
PY
