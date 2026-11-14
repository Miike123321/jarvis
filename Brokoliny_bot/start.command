#!/usr/bin/env python3
from __future__ import annotations

import getpass
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import venv
import webbrowser
from pathlib import Path


ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT / ".env"
REQUIREMENTS_FILE = ROOT / "requirements.txt"
VENV_DIR = ROOT / ".venv"


def ensure_environment() -> Path:
    if sys.version_info < (3, 11):
        raise RuntimeError("Нужен Python 3.11 или новее. Установите его и запустите файл снова.")

    if not ENV_FILE.exists():
        example = ROOT / ".env.example"
        if not example.exists():
            raise RuntimeError("Не найден .env.example.")
        ENV_FILE.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")

    if not (VENV_DIR / "bin" / "python").exists():
        print("Подготавливаю Python-окружение...")
        venv.EnvBuilder(with_pip=True).create(VENV_DIR)

    python = VENV_DIR / "bin" / "python"
    imports = (
        "import aiogram, apscheduler, cryptography, fastapi, litellm, "
        "PIL, pydantic, dotenv, reportlab, uvicorn"
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

    updates: dict[str, str] = {}
    for key, prompt in (
        ("TELEGRAM_TOKEN", "Токен Telegram-бота (получен у BotFather)"),
        ("GEMINI_API_KEY", "API-ключ Gemini"),
        ("ADMIN_TOKEN", "Пароль для входа в админ-панель"),
    ):
        existing = current.get(key, "")
        while True:
            suffix = " (Enter — оставить текущий)" if existing else ""
            value = getpass.getpass(f"{prompt}{suffix}: ").strip()
            if value:
                updates[key] = value
                break
            if existing:
                updates[key] = existing
                break
            print("Поле обязательное — введите значение.")

    encryption_key = current.get("DATA_ENCRYPTION_KEY", "")
    if encryption_key:
        try:
            Fernet(encryption_key.encode("ascii"))
        except (ValueError, UnicodeEncodeError) as exc:
            raise RuntimeError(
                "DATA_ENCRYPTION_KEY в .env некорректен. Не заменяйте его, "
                "если уже сохраняли зашифрованные данные."
            ) from exc
    else:
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


def start_services(python: Path, env_values: dict[str, str]) -> None:
    runtime_env = os.environ.copy()
    runtime_env.update(env_values)
    processes: list[tuple[str, subprocess.Popen[bytes]]] = []

    try:
        admin = subprocess.Popen(
            [
                str(python), "-m", "uvicorn", "app.admin:app",
                "--host", "127.0.0.1", "--port", "8000",
            ],
            cwd=ROOT,
            env=runtime_env,
        )
        processes.append(("Админка", admin))

        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if admin.poll() is not None:
                raise RuntimeError(f"Админка завершилась с кодом {admin.returncode}.")
            try:
                with urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=1):
                    break
            except (urllib.error.URLError, TimeoutError):
                time.sleep(0.25)
        else:
            raise RuntimeError("Админка не ответила за 20 секунд.")

        bot = subprocess.Popen(
            [str(python), "-m", "app.bot"],
            cwd=ROOT,
            env=runtime_env,
        )
        processes.append(("Бот", bot))

        print("\nАдминка: http://127.0.0.1:8000/admin")
        print("Бот запущен. Не закрывайте это окно; Ctrl+C остановит оба процесса.\n")
        webbrowser.open("http://127.0.0.1:8000/admin")

        while True:
            for name, process in processes:
                return_code = process.poll()
                if return_code is not None:
                    raise RuntimeError(f"{name} завершился с кодом {return_code}.")
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nОстанавливаю бота и админку...")
    finally:
        for _, process in processes:
            if process.poll() is None:
                process.terminate()
        for _, process in processes:
            if process.poll() is None:
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


def main() -> None:
    os.chdir(ROOT)
    python = ensure_environment()
    env_values = configure_env()
    print("\nНастройки сохранены в .env. Запускаю сервисы...")
    start_services(python, env_values)


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"\nНе удалось запустить Brokoliny Bot: {exc}", file=sys.stderr)
        sys.exit(1)
