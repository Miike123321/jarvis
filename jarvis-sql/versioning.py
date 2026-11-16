"""Local release, changed-file, and server-start counters."""

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
BASELINE_NAME = "version_baseline.json"
STATE_NAME = ".jarvis_version_state.sqlite"
_start_lock = threading.Lock()
_process_starts: dict[tuple[int, Path, int], int] = {}


@dataclass(frozen=True)
class AppVersion:
    release: int
    changed_files: int
    starts: int

    def __str__(self) -> str:
        return f"{self.release}.{self.changed_files}.{self.starts}"


def _git_files(project_dir: Path, *args: str) -> set[str]:
    result = subprocess.run(
        ["git", *args],
        cwd=project_dir,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return {
        os.fsdecode(name)
        for name in result.stdout.split(b"\0")
        if name
    }


def _read_baseline(project_dir: Path) -> tuple[int, dict[str, str]]:
    data = json.loads((project_dir / BASELINE_NAME).read_text(encoding="utf-8"))
    if (
        not isinstance(data, dict)
        or type(data.get("release")) is not int
        or data["release"] < 1
        or not isinstance(data.get("files"), dict)
        or any(
            not isinstance(name, str) or not isinstance(digest, str)
            for name, digest in data["files"].items()
        )
    ):
        raise ValueError("Некорректные данные версии.")
    return data["release"], data["files"]


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def changed_file_count(project_dir: Path, baseline: dict[str, str]) -> int:
    names = _git_files(
        project_dir, "ls-files", "--cached", "--others", "--exclude-standard", "-z", "--", "."
    )
    names |= _git_files(project_dir, "ls-tree", "-r", "--name-only", "-z", "HEAD", "--", ".")
    names.discard(BASELINE_NAME)
    count = 0
    for name in names:
        path = project_dir / name
        if not path.is_file():
            count += name in baseline
        elif baseline.get(name) != _hash_file(path):
            count += 1
    return count


def register_start(project_dir: Path, release: int) -> int:
    key = (os.getpid(), project_dir.resolve(), release)
    with _start_lock:
        if key in _process_starts:
            return _process_starts[key]
        with sqlite3.connect(project_dir / STATE_NAME, timeout=10) as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "CREATE TABLE IF NOT EXISTS starts "
                "(id INTEGER PRIMARY KEY CHECK (id = 1), release INTEGER NOT NULL, "
                "count INTEGER NOT NULL)"
            )
            previous = db.execute(
                "SELECT release, count FROM starts WHERE id = 1"
            ).fetchone()
            count = previous[1] + 1 if previous and previous[0] == release else 1
            db.execute(
                "INSERT OR REPLACE INTO starts (id, release, count) VALUES (1, ?, ?)",
                (release, count),
            )
        _process_starts[key] = count
        return count


def get_version(project_dir: Path = PROJECT_DIR) -> AppVersion:
    release, baseline = _read_baseline(project_dir)
    return AppVersion(
        release=release,
        changed_files=changed_file_count(project_dir, baseline),
        starts=register_start(project_dir, release),
    )


def prepare_release(project_dir: Path = PROJECT_DIR) -> int:
    """Prepare a new baseline after staging all files in the release checkout."""
    release, _ = _read_baseline(project_dir)
    unstaged = subprocess.run(
        ["git", "diff", "--quiet", "--", "."],
        cwd=project_dir,
        check=False,
        stderr=subprocess.PIPE,
    )
    if unstaged.returncode != 0:
        raise RuntimeError("Сначала добавьте все изменения релиза в Git index.")
    untracked = _git_files(
        project_dir, "ls-files", "--others", "--exclude-standard", "-z", "--", "."
    )
    if untracked:
        raise RuntimeError("Сначала добавьте новые файлы релиза в Git index.")
    names = _git_files(project_dir, "ls-files", "--cached", "-z", "--", ".")
    names.discard(BASELINE_NAME)
    files = {
        name: _hash_file(project_dir / name)
        for name in sorted(names)
        if (project_dir / name).is_file()
    }
    new_release = release + 1
    (project_dir / BASELINE_NAME).write_text(
        json.dumps({"release": new_release, "files": files}, ensure_ascii=False, indent=2)
        + "\n",
        encoding="utf-8",
    )
    return new_release


if __name__ == "__main__":
    if sys.argv[1:] != ["prepare-release"]:
        raise SystemExit("Использование: python versioning.py prepare-release")
    print(f"Подготовлен релиз {prepare_release()}; добавьте version_baseline.json в commit.")
