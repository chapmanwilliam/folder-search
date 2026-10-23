"""The list of folders the user has indexed, kept in folders.json."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from .settings import DIMENSIONS, app_home, index_path


@dataclass
class FolderEntry:
    path: str
    name: str
    added: str
    include_online: bool = False


def _file() -> Path:
    return app_home() / "folders.json"


def load() -> list[FolderEntry]:
    try:
        data = json.loads(_file().read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    return [FolderEntry(**d) for d in data]


def save(entries: list[FolderEntry]) -> None:
    f = _file()
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps([asdict(e) for e in entries], indent=2))
    tmp.replace(f)


def get(path: str | Path) -> FolderEntry | None:
    p = str(Path(path).expanduser().resolve())
    return next((e for e in load() if e.path == p), None)


def add(path: str | Path, include_online: bool = False) -> FolderEntry:
    p = Path(path).expanduser().resolve()
    if not p.is_dir():
        raise NotADirectoryError(str(p))
    entries = load()
    for e in entries:
        if e.path == str(p):
            e.include_online = include_online
            save(entries)
            return e
    e = FolderEntry(str(p), p.name, time.strftime("%Y-%m-%d"), include_online)
    entries.append(e)
    save(entries)
    return e


def remove(path: str | Path) -> None:
    p = str(Path(path).expanduser().resolve())
    save([e for e in load() if e.path != p])
    db = index_path(Path(p))
    for suffix in ("", "-wal", "-shm"):
        Path(str(db) + suffix).unlink(missing_ok=True)


def open_store(path: str | Path, check_same_thread: bool = True):
    from .engine.store import Store

    return Store(index_path(Path(path).expanduser().resolve()), DIMENSIONS, check_same_thread)
