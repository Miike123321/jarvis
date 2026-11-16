#!/usr/bin/env python3
from __future__ import annotations

import getpass
import json
import os
import subprocess
import sys
import venv
import warnings
from pathlib import Path


ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT / ".env"
REQUIREMENTS_FILE = ROOT / "requirements.txt"
VENV_DIR = ROOT / ".venv"


def ensure_environment() -> Path:
    if sys.version_info < (3, 11):
        raise RuntimeError("Нужен Python 3.11 или новее. Установите его и запустите файл снова.")

    if not ENV_FILE.exists():
        database_path = Path(os.environ.get("DATABASE_PATH", "./data/brokoliny.sqlite3"))
        if database_path.exists():
            raise RuntimeError(
                "Найдена существующая база, но .env отсутствует. "
                "Восстановите прежний .env и ключ шифрования из защищённой копии."
            )
        example = ROOT / ".env.example"
        if not example.exists():
            raise RuntimeError("Не найден .env.example.")
        ENV_FILE.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")

    if not (VENV_DIR / "bin" / "python").exists():
        print("Подготавливаю Python-окружение...")
        venv.EnvBuilder(with_pip=True).create(VENV_DIR)

    python = VENV_DIR / "bin" / "python"
    version_check = subprocess.run(
        [str(python), "-c", "import sys; sys.exit(sys.version_info < (3, 11))"],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if version_check.returncode:
        raise RuntimeError(
            "В .venv нужен Python 3.11 или новее. Восстановите окружение и повторите запуск."
        )
    imports = (
        "import aiogram, apscheduler, cryptography, fastapi, httpx, litellm, "
        "PIL, pydantic, dotenv, reportlab, uvicorn, httpx, jwt"
        "from jwt import decode; from jwt.algorithms import RSAAlgorithm"
    )
    check = subprocess.run(
        [str(python), "-c", imports],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if check.returncode:
        print("Устанавливаю необходимые библиотеки (первый запуск может занять несколько минут)...")
        subprocess.run(
            [str(python), "-m", "pip", "install", "-r", str(REQUIREMENTS_FILE)],
            cwd=ROOT,
            check=True,
        )
    return python


def configure_env() -> dict[str, str]:
    from dotenv import dotenv_values
    from cryptography.fernet import Fernet

    current = {
        key: value
        for key, value in dotenv_values(ENV_FILE, interpolate=False).items()
        if value is not None
    }

    encryption_key = current.get("DATA_ENCRYPTION_KEY", "")
    database_path = Path(current.get("DATABASE_PATH") or os.environ.get(
        "DATABASE_PATH", "./data/brokoliny.sqlite3"
    ))
    if not encryption_key and database_path.exists():
        raise RuntimeError(
            "Для существующей базы не задан DATA_ENCRYPTION_KEY. "
            "Восстановите прежний ключ из защищённой копии; новый ключ не создан."
        )
    if encryption_key:
        try:
            Fernet(encryption_key.encode("ascii"))
        except (ValueError, UnicodeEncodeError) as exc:
            raise RuntimeError(
                "DATA_ENCRYPTION_KEY в .env некорректен. Не заменяйте его, "
                "если уже сохраняли зашифрованные данные."
            ) from exc

    updates: dict[str, str] = {}
    for key, prompt in (
        ("TELEGRAM_TOKEN", "Токен Telegram-бота (получен у BotFather)"),
        ("GEMINI_API_KEY", "API-ключ Gemini"),
        ("ADMIN_TOKEN", "Пароль для входа в админ-панель"),
    ):
        existing = current.get(key, "")
        while True:
            suffix = " (Enter — оставить текущий)" if existing else ""
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("error", getpass.GetPassWarning)
                    value = getpass.getpass(f"{prompt}{suffix}: ").strip()
            except getpass.GetPassWarning as exc:
                raise RuntimeError(
                    "Не удалось отключить отображение ввода. Откройте лаунчер в терминале."
                ) from exc
            if value:
                updates[key] = value
                break
            if existing:
                updates[key] = existing
                break
            print("Поле обязательное — введите значение.")

    if not encryption_key:
        encryption_key = Fernet.generate_key().decode("ascii")
        print("Создан ключ шифрования и сохранён в .env.")
    updates["DATA_ENCRYPTION_KEY"] = encryption_key

    lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
    for key, value in updates.items():
        replacement = f"{key}={json.dumps(value)}"
        for index, line in enumerate(lines):
            if line.split("=", 1)[0].strip() == key:
                lines[index] = replacement
                break
        else:
            lines.append(replacement)

    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    ENV_FILE.chmod(0o600)
    current.update(updates)
    return current


def choose_mode() -> str:
    print("Режим запуска: 1 — локальный (по умолчанию), 2 — удалённый.")
    while True:
        choice = input("Выберите 1 или 2: ").strip().lower()
        if choice in ("", "1", "local"):
            return "local"
        if choice in ("2", "remote"):
            return "remote"
        print("Введите 1/local или 2/remote.")


def start_services(python: Path, env_values: dict[str, str]) -> None:
    from app.launcher import start_services as supervise_services

    supervise_services(python, env_values, ROOT)


def start_remote_services(python: Path, env_values: dict[str, str]) -> None:
    try:
        from app.launcher import start_remote_services as supervise_services
    except ImportError:
        raise RuntimeError(
            "Удалённый запуск заблокирован: безопасный лаунчер недоступен."
        ) from None

    supervise_services(python, env_values, ROOT)


def main() -> None:
    os.chdir(ROOT)
    if sys.version_info < (3, 11):
        raise RuntimeError("Нужен Python 3.11 или новее. Установите его и запустите файл снова.")

    if sys.argv[1:] not in ([], ["--local-configure"], ["--remote-configure"]):
        raise RuntimeError("Неизвестный параметр запуска.")

    if not sys.argv[1:]:
        mode = choose_mode()
        configure_argument = (
            "--remote-configure" if mode == "remote" else "--local-configure"
        )
    else:
        configure_argument = sys.argv[1]

    if configure_argument == "--remote-configure":
        from app.launcher import remote_unavailable

        raise RuntimeError(remote_unavailable())

    python = ensure_environment()
    if os.path.abspath(sys.executable) != os.path.abspath(python):
        os.execv(
            str(python),
            [str(python), str(Path(__file__).resolve()), configure_argument],
        )

    env_values = configure_env()
    python = Path(sys.executable)
    if configure_argument == "--remote-configure":
        print("\nНастройки сохранены в .env. Проверяю условия удалённого запуска...")
        start_remote_services(python, env_values)
    else:
        print("\nНастройки сохранены в .env. Запускаю сервисы...")
        start_services(python, env_values)


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, subprocess.CalledProcessError, EOFError) as exc:
        print(f"\nНе удалось запустить Brokoliny Bot: {exc}", file=sys.stderr)
        sys.exit(1)
