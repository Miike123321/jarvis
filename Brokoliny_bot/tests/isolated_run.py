"""Run the project unittest command in a disposable, offline copy.

Usage: JWT_PACKAGE=/path/to/installed/jwt .venv/bin/python tests/isolated_run.py
JWT_PACKAGE is only needed when the project's venv lacks PyJWT; the installed
package is copied into the disposable tree, not installed or changed.
"""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

root = Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory(prefix="brokoliny-offline-") as directory:
    target = Path(directory)
    for name in ("app", "tests", "scss"):
        shutil.copytree(
            root / name, target / name,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".env*", "*.sqlite*", "*.db"),
            symlinks=True,
        )
    for name in ("admin.html", "start.command", "README.md", "requirements.txt"):
        shutil.copy2(root / name, target / name, follow_symlinks=False)
    if any(p.is_symlink() for p in target.rglob("*")):
        raise RuntimeError("Symlinks are not allowed in the isolated copy")
    harness = target / "harness"
    harness.mkdir()
    shutil.copy2(target / "tests" / "isolation_sitecustomize.py", harness / "sitecustomize.py")
    package = os.environ.get("JWT_PACKAGE")
    if package:
        shutil.copytree(package, harness / "jwt", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    env = {
        "PATH": str(root / ".venv" / "bin") + ":/usr/bin:/bin",
        "HOME": str(target), "TMPDIR": str(target),
        "PYTHONPATH": str(harness),
        "PYTHON_DOTENV_DISABLED": "1", "PYTHONDONTWRITEBYTECODE": "1",
        "LITELLM_LOCAL_MODEL_COST_MAP": "true",
        "DATABASE_PATH": str(target / "isolated.sqlite3"),
    }
    print("Running: python -m unittest discover -s tests -v (isolated copy)", flush=True)
    result = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
        cwd=target, env=env, check=False,
    )
    sys.exit(result.returncode)
