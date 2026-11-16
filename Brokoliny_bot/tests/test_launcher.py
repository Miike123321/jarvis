from __future__ import annotations

import errno
import importlib.machinery
import importlib.util
import io
import os
import signal
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from cryptography.fernet import Fernet

from app import launcher


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def load_start_command():
    path = PROJECT_ROOT / "start.command"
    loader = importlib.machinery.SourceFileLoader("start_command_under_test", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    if spec is None:
        raise RuntimeError("Unable to load launcher script for unit tests.")
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class FakeChild:
    def __init__(self) -> None:
        self.returncode: int | None = None
        self.terminate = Mock(side_effect=self._terminate)
        self.kill = Mock(side_effect=self._kill)
        self.wait = Mock(side_effect=self._wait)

    def poll(self) -> int | None:
        return self.returncode

    def _terminate(self) -> None:
        self.returncode = 0

    def _kill(self) -> None:
        self.returncode = -signal.SIGKILL

    def _wait(self, timeout: float | None = None) -> int:
        del timeout
        if self.returncode is None:
            raise subprocess.TimeoutExpired("owned-test-child", 0)
        return self.returncode


def valid_env() -> dict[str, str]:
    return {
        "TELEGRAM_TOKEN": "synthetic-telegram-token",
        "ADMIN_TOKEN": "synthetic-admin-token",
        "DATA_ENCRYPTION_KEY": Fernet.generate_key().decode("ascii"),
    }


class LauncherProcessTests(unittest.TestCase):
    def test_local_start_uses_loopback_origin_and_keeps_secrets_out_of_argv(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable = root / "python"
            executable.write_bytes(b"synthetic executable")
            executable.chmod(0o700)
            admin = FakeChild()
            bot = FakeChild()
            origin = Mock()
            origin.fileno.return_value = 41
            env = valid_env()
            output = io.StringIO()

            with (
                patch.object(launcher, "_reserve_origin", return_value=origin),
                patch.object(launcher, "_wait_for_admin"),
                patch.object(launcher.subprocess, "Popen", side_effect=[admin, bot]) as popen,
                patch.object(launcher.time, "sleep", side_effect=KeyboardInterrupt),
                patch.object(launcher.signal, "signal", return_value=signal.SIG_DFL),
                patch.object(launcher.webbrowser, "open", return_value=True),
                patch(
                    "builtins.print",
                    side_effect=lambda *args, **kwargs: output.write(
                        " ".join(str(value) for value in args) + "\n"
                    ),
                ),
            ):
                launcher.start_services(executable, env, root)

            self.assertEqual(popen.call_count, 2)
            admin_args, admin_kwargs = popen.call_args_list[0]
            bot_args, bot_kwargs = popen.call_args_list[1]
            self.assertEqual(admin_args[0][1:4], ["-m", "uvicorn", "app.admin:app"])
            self.assertIn("--fd", admin_args[0])
            self.assertEqual(admin_kwargs["pass_fds"], (41,))
            self.assertEqual(bot_args[0][1:], ["-m", "app.bot"])
            for arguments in (admin_args[0], bot_args[0]):
                for secret in env.values():
                    self.assertNotIn(secret, arguments)
            self.assertEqual(admin_kwargs["env"]["ADMIN_TOKEN"], env["ADMIN_TOKEN"])
            self.assertEqual(bot_kwargs["env"]["TELEGRAM_TOKEN"], env["TELEGRAM_TOKEN"])
            self.assertEqual(admin_kwargs["stdout"], subprocess.DEVNULL)
            self.assertEqual(admin_kwargs["stderr"], subprocess.DEVNULL)
            self.assertNotIn("synthetic-", output.getvalue())
            admin.terminate.assert_called_once()
            bot.terminate.assert_called_once()
            origin.close.assert_called_once()

    def test_venv_python_symlink_is_preserved_in_child_argv(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base_python = root / "base-python"
            base_python.write_bytes(b"synthetic executable")
            base_python.chmod(0o700)
            venv_python = root / ".venv" / "bin" / "python"
            venv_python.parent.mkdir(parents=True)
            venv_python.symlink_to(base_python)
            admin, bot = FakeChild(), FakeChild()
            origin = Mock()
            origin.fileno.return_value = 42

            with (
                patch.object(launcher, "_reserve_origin", return_value=origin),
                patch.object(launcher, "_wait_for_admin"),
                patch.object(launcher.subprocess, "Popen", side_effect=[admin, bot]) as popen,
                patch.object(launcher.time, "sleep", side_effect=KeyboardInterrupt),
                patch.object(launcher.signal, "signal", return_value=signal.SIG_DFL),
                patch.object(launcher.webbrowser, "open", return_value=True),
                patch("builtins.print"),
            ):
                launcher.start_services(venv_python, valid_env(), root)

            self.assertTrue(venv_python.is_symlink())
            self.assertEqual(popen.call_args_list[0].args[0][0], os.path.abspath(venv_python))
            self.assertNotEqual(popen.call_args_list[0].args[0][0], str(base_python))

    def test_busy_port_fails_without_terminating_the_existing_listener(self) -> None:
        listener = Mock()
        listener.bind.side_effect = OSError(errno.EADDRINUSE, "address already in use")
        with patch.object(launcher.socket, "socket", return_value=listener):
            with self.assertRaisesRegex(RuntimeError, "Порт 127.0.0.1:8000 занят"):
                launcher._reserve_origin()

        listener.close.assert_called_once()
        listener.terminate.assert_not_called()
        listener.kill.assert_not_called()

    def test_unhealthy_response_never_counts_as_admin_readiness(self) -> None:
        process = FakeChild()
        with (
            patch.object(launcher, "_health_ok", return_value=False) as health,
            patch.object(launcher.time, "monotonic", side_effect=[0.0, 0.0, 21.0]),
            patch.object(launcher.time, "sleep"),
        ):
            with self.assertRaisesRegex(RuntimeError, "не достигла готовности"):
                launcher._wait_for_admin(process)
        health.assert_called_once_with()

    def test_cleanup_signals_only_processes_owned_by_the_launcher(self) -> None:
        owned_admin, owned_bot, unrelated = FakeChild(), FakeChild(), FakeChild()
        launcher._stop_processes([("Админка", owned_admin), ("Бот", owned_bot)])

        owned_admin.terminate.assert_called_once()
        owned_bot.terminate.assert_called_once()
        unrelated.terminate.assert_not_called()
        unrelated.kill.assert_not_called()

    def test_timeout_escalates_only_owned_child_and_reaps_it(self) -> None:
        owned, unrelated = FakeChild(), FakeChild()
        owned.terminate = Mock()
        launcher._stop_processes([("owned", owned)])
        owned.terminate.assert_called_once()
        owned.kill.assert_called_once()
        self.assertGreaterEqual(owned.wait.call_count, 2)
        unrelated.terminate.assert_not_called()
        unrelated.kill.assert_not_called()

    def test_unexpected_child_exit_cleans_up_other_owned_child(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable = root / "python"
            executable.write_bytes(b"synthetic executable")
            executable.chmod(0o700)
            admin, bot = FakeChild(), FakeChild()
            admin.returncode = 1
            origin = Mock()
            origin.fileno.return_value = 41
            with (
                patch.object(launcher, "_reserve_origin", return_value=origin),
                patch.object(launcher, "_wait_for_admin"),
                patch.object(launcher.subprocess, "Popen", side_effect=[admin, bot]),
                patch.object(launcher.signal, "signal", return_value=signal.SIG_DFL),
                patch.object(launcher.webbrowser, "open"),
                patch("builtins.print"),
            ):
                with self.assertRaises(RuntimeError):
                    launcher.start_services(executable, valid_env(), root)
            bot.terminate.assert_called_once()
            origin.close.assert_called_once()


class LauncherSetupAndRemoteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.start_script = load_start_command()

    def test_setup_creates_venv_and_never_runs_pip_when_dependencies_exist(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".env.example").write_text(
                "TELEGRAM_TOKEN=\nADMIN_TOKEN=\nDATA_ENCRYPTION_KEY=\n",
                encoding="utf-8",
            )
            venv_dir = root / ".venv"
            python = venv_dir / "bin" / "python"

            def create_venv(path: Path) -> None:
                executable = path / "bin" / "python"
                executable.parent.mkdir(parents=True)
                executable.write_bytes(b"synthetic executable")
                executable.chmod(0o700)

            builder = Mock()
            builder.create.side_effect = create_venv
            with (
                patch.object(self.start_script, "ROOT", root),
                patch.object(self.start_script, "ENV_FILE", root / ".env"),
                patch.object(self.start_script, "VENV_DIR", venv_dir),
                patch.object(self.start_script, "REQUIREMENTS_FILE", root / "requirements.txt"),
                patch.dict(os.environ, {"DATABASE_PATH": str(root / "test.sqlite3")}),
                patch.object(self.start_script.venv, "EnvBuilder", return_value=builder) as env_builder,
                patch.object(
                    self.start_script.subprocess,
                    "run",
                    side_effect=[SimpleNamespace(returncode=0), SimpleNamespace(returncode=0)],
                ) as run,
            ):
                result = self.start_script.ensure_environment()

            self.assertEqual(result, python)
            env_builder.assert_called_once_with(with_pip=True)
            builder.create.assert_called_once_with(venv_dir)
            self.assertEqual(run.call_count, 2)
            self.assertFalse(any("pip" in str(call_args.args[0]) for call_args in run.call_args_list))
            self.assertEqual((root / ".env").read_text(encoding="utf-8"), (root / ".env.example").read_text(encoding="utf-8"))

    def test_configuration_reuses_existing_secret_values_in_an_isolated_env(self) -> None:
        encryption_key = Fernet.generate_key().decode("ascii")
        existing = {
            "TELEGRAM_TOKEN": "old-synthetic-telegram",
            "GEMINI_API_KEY": "old-synthetic-ai-key",
            "ADMIN_TOKEN": "old-synthetic-admin",
            "DATA_ENCRYPTION_KEY": encryption_key,
        }
        with tempfile.TemporaryDirectory() as temporary:
            env_file = Path(temporary) / ".env"
            env_file.write_text(
                "\n".join(f"{key}={value}" for key, value in existing.items()) + "\n",
                encoding="utf-8",
            )
            with (
                patch.object(self.start_script, "ENV_FILE", env_file),
                patch.object(self.start_script, "ROOT", Path(temporary)),
                patch.object(self.start_script, "getpass") as getpass,
            ):
                getpass.getpass.side_effect = ["", "", ""]
                result = self.start_script.configure_env()

            self.assertEqual(result, existing)
            self.assertEqual(env_file.stat().st_mode & 0o777, 0o600)
            self.assertIn("DATA_ENCRYPTION_KEY", env_file.read_text(encoding="utf-8"))

    def test_remote_mode_blocks_before_configuration(self) -> None:
        with (
            patch.object(self.start_script, "ROOT", Path(tempfile.gettempdir())),
            patch.object(self.start_script.os, "chdir"),
            patch.object(self.start_script.sys, "argv", []),
            patch.object(self.start_script, "choose_mode", return_value="remote"),
            patch.object(self.start_script, "ensure_environment", return_value=Path(os.path.abspath(os.sys.executable))) as setup,
            patch.object(self.start_script, "configure_env", return_value=valid_env()) as configure,
            patch.object(self.start_script, "start_services") as start,
            patch.object(launcher.subprocess, "Popen") as popen,
            patch.object(launcher.webbrowser, "open") as browser,
            patch.dict(
                os.environ,
                {
                    "CLOUDFLARE_ACCESS_ENABLED": "true",
                    "ACCESS_ALLOW_CONFIRMED": "true",
                    "REMOTE_ACCESS_CHECKED": "true",
                },
            ),
            patch("builtins.print") as output,
        ):
            with self.assertRaises(RuntimeError):
                self.start_script.main()

        setup.assert_not_called()
        configure.assert_not_called()
        self.assertEqual(start.call_count, 0)
        popen.assert_not_called()
        browser.assert_not_called()
        self.assertFalse(any("https://" in str(args) for args, _ in output.call_args_list))

    def test_explicit_remote_argument_never_launches_services(self) -> None:
        with (
            patch.object(self.start_script.os, "chdir"),
            patch.object(self.start_script.sys, "argv", ["start.command", "--remote-configure"]),
            patch.object(self.start_script, "ensure_environment", return_value=Path(os.path.abspath(os.sys.executable))),
            patch.object(self.start_script, "configure_env", return_value=valid_env()),
            patch.object(self.start_script, "start_services") as local_start,
            patch.object(launcher.subprocess, "Popen") as popen,
            patch.object(launcher.webbrowser, "open") as browser,
            patch("builtins.print"),
        ):
            with self.assertRaises(RuntimeError):
                self.start_script.main()
        local_start.assert_not_called()
        popen.assert_not_called()
        browser.assert_not_called()

    def test_remote_choice_does_not_reexec_or_install(self) -> None:
        with (
            patch.object(self.start_script.os, "chdir"),
            patch.object(self.start_script.sys, "argv", ["start.command"]),
            patch.object(self.start_script, "choose_mode", return_value="remote"),
            patch.object(self.start_script, "ensure_environment", return_value=Path("/synthetic/venv/python")),
            patch.object(self.start_script.os, "execv", side_effect=RuntimeError("mock reexec")) as execute,
        ):
            with self.assertRaises(RuntimeError):
                self.start_script.main()
        execute.assert_not_called()

    def test_remote_unavailable_is_fail_closed_and_never_claims_access_verified(self) -> None:
        explanation = launcher.remote_unavailable()
        self.assertIn("заблокирован", explanation)
        self.assertIn("policy", explanation)
        self.assertIn("публичная ссылка не запускаются", explanation)
        self.assertNotIn("защищённая ссылка готова", explanation)


if __name__ == "__main__":
    unittest.main()
