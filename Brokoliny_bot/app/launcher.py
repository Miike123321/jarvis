"""Local service supervisor. Remote publication is deliberately unavailable."""

from __future__ import annotations

import base64
import binascii
import errno
import http.client
import json
import os
import signal
import socket
import subprocess
import threading
import time
import webbrowser
from pathlib import Path


_HOST = "127.0.0.1"
_PORT = 8000
_STARTUP_TIMEOUT = 20.0
_STOP_TIMEOUT = 5.0


def remote_unavailable() -> str:
    """Explain the fail-closed remote-mode decision without exposing settings."""
    return (
        "Удалённый запуск заблокирован: в приложении реализована проверка "
        "Access JWT на каждом запросе, но нет авторитетного проверяющего "
        "актуальную эффективную Cloudflare Access policy, hostname, bypass- и "
        "конфликтные правила и фактические tunnel ingress. Локальные настройки, "
        "валидный JWT или доступность JWKS этого не доказывают. Проверенный "
        "безопасный способ первичного создания/передачи named-tunnel "
        "credential для установленной версии cloudflared также не подтверждён. "
        "Владелец должен отдельно подготовить команду и домен, Access "
        "application/IdP/MFA с явной Allow policy и named tunnel/ingress; "
        "локально задаются CLOUDFLARE_ACCESS_TEAM, "
        "CLOUDFLARE_ACCESS_AUDIENCE и непустой "
        "CLOUDFLARE_ACCESS_ADMIN_EMAILS. Эти действия не выполняются "
        "автоматически. До реализации авторитетной проверки tunnel, сервисы "
        "и публичная ссылка не запускаются; quick tunnel не используется. "
        "Локальный режим остаётся доступен."
    )


def start_remote_services(
    python: Path, env_values: dict[str, str], root: Path
) -> None:
    """Fail closed before side effects while Cloudflare policy proof is absent.

    The arguments intentionally remain part of the launcher contract. No
    credentials are inspected, persisted, emitted, or passed to a child
    process: a local setting cannot prove the live effective Access policy or
    tunnel ingress, so attempting first-run provisioning would be unsafe.
    """
    del python, env_values, root
    raise RuntimeError(remote_unavailable())


def _reserve_origin() -> socket.socket:
    """Reserve the actual listening socket, not just a racy port check."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        # Permit a normal restart after connections in TIME_WAIT, not another
        # simultaneous listener (SO_REUSEPORT is deliberately not enabled).
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((_HOST, _PORT))
    except OSError as exc:
        sock.close()
        if exc.errno == errno.EADDRINUSE:
            raise RuntimeError(
                "Порт 127.0.0.1:8000 занят. Освободите его и повторите запуск; "
                "чужие процессы не останавливались."
            ) from None
        raise RuntimeError("Не удалось зарезервировать локальный порт 8000.") from None
    return sock


def _health_ok() -> bool:
    # http.client connects directly; proxy environment variables are ignored.
    connection = http.client.HTTPConnection(_HOST, _PORT, timeout=1)
    try:
        connection.request("GET", "/health")
        response = connection.getresponse()
        if response.status != 200:
            return False
        body = response.read(1025)
        return len(body) <= 1024 and json.loads(body) == {"status": "ok"}
    except (OSError, http.client.HTTPException, ValueError):
        return False
    finally:
        connection.close()


def _wait_for_admin(admin: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + _STARTUP_TIMEOUT
    while time.monotonic() < deadline:
        if admin.poll() is not None:
            raise RuntimeError("Админка завершилась до готовности. Проверьте конфигурацию и зависимости.")
        ready = _health_ok()
        if admin.poll() is not None:
            raise RuntimeError("Админка завершилась во время проверки готовности.")
        if ready:
            return
        time.sleep(0.25)
    raise RuntimeError("Админка не достигла готовности за 20 секунд.")


def _stop_processes(processes: list[tuple[str, subprocess.Popen[bytes]]]) -> None:
    # Signal all owned children first, then bound the total graceful wait.
    failed = False
    for _, process in reversed(processes):
        if process.poll() is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
            except OSError:
                failed = True
    deadline = time.monotonic() + _STOP_TIMEOUT
    for _, process in reversed(processes):
        if process.poll() is None:
            try:
                process.wait(timeout=max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                pass
            except OSError:
                failed = True
    for _, process in reversed(processes):
        if process.poll() is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            except OSError:
                failed = True
    deadline = time.monotonic() + _STOP_TIMEOUT
    for _, process in reversed(processes):
        try:
            # Always reap our own children, including those already exited.
            process.wait(timeout=max(0.0, deadline - time.monotonic()))
        except (subprocess.TimeoutExpired, OSError):
            failed = True
    if failed:
        print("Не удалось подтвердить остановку всех дочерних процессов.")


def start_services(python: Path, env_values: dict[str, str], root: Path) -> None:
    """Run and supervise the local admin and bot until interrupted or failure.

    The socket is reserved before spawning Uvicorn and inherited via --fd,
    preventing another process from satisfying our health check. This POSIX
    implementation is intended for the existing macOS launcher. Secrets are
    passed only in the environment; child output is discarded because third
    party exception and HTTP logs can contain credentials.
    """
    selected_mode = env_values.get(
        "APP_MODE", os.environ.get("APP_MODE", "local")
    ).strip().lower()
    if selected_mode == "remote":
        raise RuntimeError(
            "Удалённый режим должен запускаться через start_remote_services()."
        )
    if os.name != "posix":
        raise RuntimeError("Локальный запуск поддерживается только в POSIX-окружении; проверенная платформа — macOS.")
    # Make the executable absolute without following symlinks. A venv's
    # ``bin/python`` commonly links to the base interpreter; resolving it here
    # would discard the venv path and make child processes use the wrong prefix.
    python = Path(os.path.abspath(python))
    root = root.resolve()
    if not python.is_file() or not os.access(python, os.X_OK):
        raise RuntimeError("Исполняемый Python не найден. Проверьте локальное окружение.")
    if not root.is_dir():
        raise RuntimeError("Каталог приложения не найден.")

    runtime_env = os.environ.copy()
    runtime_env.update(env_values)
    missing = [
        key for key in ("TELEGRAM_TOKEN", "ADMIN_TOKEN", "DATA_ENCRYPTION_KEY")
        if not runtime_env.get(key, "").strip()
    ]
    if missing:
        raise RuntimeError("Не заданы обязательные настройки: " + ", ".join(missing))
    try:
        key = base64.urlsafe_b64decode(runtime_env["DATA_ENCRYPTION_KEY"].encode("ascii"))
        if len(key) != 32:
            raise ValueError
    except (ValueError, UnicodeEncodeError, binascii.Error):
        raise RuntimeError(
            "DATA_ENCRYPTION_KEY некорректен. Не заменяйте ключ существующих данных."
        ) from None

    processes: list[tuple[str, subprocess.Popen[bytes]]] = []
    previous_handlers: dict[int, object] = {}
    origin: socket.socket | None = None

    def interrupt(_signum: int, _frame: object) -> None:
        raise KeyboardInterrupt

    try:
        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                previous_handlers[sig] = signal.signal(sig, interrupt)
        origin = _reserve_origin()
        try:
            admin = subprocess.Popen(
                [str(python), "-m", "uvicorn", "app.admin:app", "--fd", str(origin.fileno())],
                cwd=root,
                env=runtime_env,
                pass_fds=(origin.fileno(),),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            raise RuntimeError("Не удалось запустить админку. Проверьте Python и зависимости.") from None
        processes.append(("Админка", admin))
        _wait_for_admin(admin)

        try:
            bot = subprocess.Popen(
                [str(python), "-m", "app.bot"],
                cwd=root,
                env=runtime_env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            raise RuntimeError("Не удалось запустить бота. Проверьте Python и зависимости.") from None
        processes.append(("Бот", bot))
        if bot.poll() is not None:
            raise RuntimeError("Бот завершился при запуске. Проверьте конфигурацию и зависимости.")
        if admin.poll() is not None:
            raise RuntimeError("Админка завершилась при запуске бота.")

        print("Локальная админка готова: http://127.0.0.1:8000/admin")
        print("Процесс бота запущен; подключение к Telegram не подтверждено.")
        print("Не закрывайте это окно; Ctrl+C остановит оба процесса.")
        try:
            webbrowser.open("http://127.0.0.1:8000/admin")
        except Exception:
            print("Не удалось открыть браузер; откройте локальный адрес вручную.")

        while True:
            for name, process in processes:
                if process.poll() is not None:
                    raise RuntimeError(f"{name} завершился; останавливаю остальные процессы.")
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nОстанавливаю локальные процессы...")
    finally:
        # Do not let a second terminal signal interrupt cleanup.
        for sig in previous_handlers:
            signal.signal(sig, signal.SIG_IGN)
        try:
            _stop_processes(processes)
        finally:
            if origin is not None:
                origin.close()
            for sig, handler in previous_handlers.items():
                signal.signal(sig, handler)
