"""Проверки инфраструктурных скриптов деплоя.

Тесты не запускают деплой (он выполняется на VPS), а проверяют статические
свойства файлов, на которых мы уже один раз споткнулись:

* бит выполнения у ``deploy.sh`` / ``backup.sh`` в git (иначе
  ``./deploy/deploy.sh`` падает с Permission denied);
* синтаксис bash (``bash -n``);
* перезапуск сервиса не требует интерактивного пароля;
* пути к скриптам в документации совпадают с реальными.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEPLOY_DIR = PROJECT_ROOT / "deploy"


def _bash_available() -> bool:
    """Проверяет, доступен ли bash для проверки синтаксиса."""
    return shutil.which("bash") is not None


requires_bash = pytest.mark.skipif(
    not _bash_available(),
    reason="bash недоступен (тест проверяет синтаксис shell-скриптов)",
)


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    """Запускает git в корне проекта."""
    return subprocess.run(
        ["git", *args],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def _bash_path(path: Path) -> str:
    """Приводит путь к виду, понятному bash.

    На Windows ``bash`` — это WSL, который не понимает ``D:\\...``,
    поэтому путь конвертируется в ``/mnt/d/...``.
    """
    if not shutil.which("bash"):
        return str(path)

    resolved = str(path)
    if len(resolved) > 1 and resolved[1] == ":":
        drive = resolved[0].lower()
        rest = resolved[2:].replace("\\", "/")
        return f"/mnt/{drive}{rest}"
    return resolved


def _code_lines(text: str) -> str:
    """Убирает строки-комментарии, чтобы искать только по коду скрипта.

    Без этого проверка ловила бы упоминание команды в поясняющем
    комментарии, а не реальный вызов.
    """
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


@pytest.fixture(scope="module")
def deploy_sh() -> str:
    """Возвращает содержимое deploy.sh."""
    return (DEPLOY_DIR / "deploy.sh").read_text(encoding="utf-8")


class TestScriptsExecutable:
    """Скрипты должны лежать в git с битом выполнения."""

    @pytest.mark.parametrize("name", ["deploy.sh", "backup.sh"])
    def test_script_is_marked_executable_in_git(self, name: str) -> None:
        """В индексе git у скрипта режим 100755, а не 100644."""
        result = _git("ls-files", "-s", f"deploy/{name}")

        if result.returncode != 0 or not result.stdout.strip():
            pytest.skip("git недоступен или файл не отслеживается")

        mode = result.stdout.split()[0]

        assert mode == "100755", f"deploy/{name} имеет режим {mode}, ожидался 100755"

    def test_sudoers_file_tracked(self) -> None:
        """Sudoers-правило для деплоя есть в репозитории."""
        assert (DEPLOY_DIR / "sudoers-planzy-deploy").is_file()


class TestDeployScriptContent:
    """Содержимое deploy.sh защищено от регрессий."""

    @requires_bash
    def test_syntax_is_valid(self) -> None:
        """Скрипт синтаксически корректен (bash -n)."""
        result = subprocess.run(
            ["bash", "-n", _bash_path(DEPLOY_DIR / "deploy.sh")],
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode == 0, f"bash -n упал: {result.stderr}"

    def test_uses_non_interactive_sudo(self, deploy_sh: str) -> None:
        """Перезапуск использует `sudo -n`, иначе по SSH упрётся в TTY."""
        assert "sudo -n systemctl restart" in _code_lines(deploy_sh)

    def test_does_not_use_plain_sudo_restart(self, deploy_sh: str) -> None:
        """Нет голого `sudo systemctl restart` без флага -n."""
        plain = re.findall(r"sudo\s+systemctl\s+restart", _code_lines(deploy_sh))

        assert plain == [], "найден sudo systemctl restart без -n: упадёт без TTY"

    def test_has_restart_service_helper(self, deploy_sh: str) -> None:
        """Есть функция перезапуска с веткой для root."""
        assert "restart_service()" in deploy_sh
        assert '[[ "$(id -u)" -eq 0 ]]' in deploy_sh

    def test_fails_with_actionable_message(self, deploy_sh: str) -> None:
        """При отсутствии прав скрипт объясняет, что делать."""
        assert "нет прав на перезапуск" in deploy_sh
        assert "sudoers-planzy-deploy" in deploy_sh

    def test_health_check_retries_are_configurable(self, deploy_sh: str) -> None:
        """Число попыток health-check настраивается через переменную."""
        assert "HEALTH_RETRIES" in deploy_sh



class TestSudoersRule:
    """Sudoers-правило должно быть узким, а не «всё без пароля»."""

    @pytest.fixture(scope="class")
    @classmethod
    def sudoers(cls) -> str:
        """Возвращает содержимое sudoers-файла."""
        return (DEPLOY_DIR / "sudoers-planzy-deploy").read_text(encoding="utf-8")

    @staticmethod
    def _rule_lines(sudoers: str) -> list[str]:
        """Возвращает значимые строки правил без комментариев и Defaults."""
        return [
            line.strip()
            for line in sudoers.splitlines()
            if line.strip()
            and not line.strip().startswith("#")
            and not line.strip().startswith("Defaults")
        ]

    def test_grants_only_planzy_service(self, sudoers: str) -> None:
        """Разрешены только restart/status/is-active сервиса planzy."""
        rules = self._rule_lines(sudoers)

        assert rules, "в sudoers-файле нет ни одного правила"

        for line in rules:
            assert "NOPASSWD:" in line
            assert "systemctl" in line
            # Каждое разрешение адресовано конкретному сервису.
            assert re.search(r"systemctl\s+(restart|status|is-active)\s+planzy", line)

    def test_no_arbitrary_command_grant(self, sudoers: str) -> None:
        """Нет разрешения на произвольные команды (ALL)."""
        for line in self._rule_lines(sudoers):
            assert "NOPASSWD: ALL" not in line
            assert "/usr/bin/*" not in line
            assert "/bin/*" not in line

    def test_disables_requiretty_for_planzy(self, sudoers: str) -> None:
        """TTY не требуется — иначе деплой по SSH не работает."""
        assert "Defaults:planzy !requiretty" in sudoers


class TestDeployDocs:
    """Документация ссылается на реальные пути и команды."""

    @pytest.fixture(scope="class")
    @classmethod
    def readme(cls) -> str:
        """Возвращает содержимое deploy/README.md."""
        return (DEPLOY_DIR / "README.md").read_text(encoding="utf-8")

    def test_documents_correct_script_path(self, readme: str) -> None:
        """Указан реальный путь /opt/planzy/app/deploy/deploy.sh."""
        assert "/opt/planzy/app/deploy/deploy.sh" in readme
        assert "/opt/planzy/deploy/deploy.sh" not in readme

    def test_documents_backup_path(self, readme: str) -> None:
        """Пути бэкапа указывают на /opt/planzy/app."""
        assert "/opt/planzy/app/deploy/backup.sh" in readme
        assert "/opt/planzy/deploy/backup.sh" not in readme

    def test_documents_passwordless_restart(self, readme: str) -> None:
        """Описано решение проблемы с паролем sudo."""
        assert "sudoers-planzy-deploy" in readme
        assert "sudo -n" in readme

    def test_documents_planzy_user_run(self, readme: str) -> None:
        """Основная команда деплоя запускается от пользователя planzy."""
        assert "sudo -u planzy" in readme
        assert "/opt/planzy/app/deploy/deploy.sh" in readme

    def test_warns_against_running_as_root(self, readme: str) -> None:
        """Предупреждает, что запуск от root ломает git pull."""
        assert "dubious ownership" in readme

    def test_does_not_recommend_sudo_bash(self, readme: str) -> None:
        """Нет совета запускать скрипт от root через sudo bash."""
        assert "sudo bash /opt/planzy/app/deploy/deploy.sh`" not in readme
