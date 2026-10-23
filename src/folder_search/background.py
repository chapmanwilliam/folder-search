"""Keep indexes up to date while the app is closed.

A macOS LaunchAgent runs `python -m folder_search.background` at login and
every 30 minutes. Each run checks every folder and indexes what changed; when
nothing has changed it finishes in seconds without loading the model.

Coordination with the app is by one lock file:
- the app holds the lock for as long as it is open, so a background run that
  starts meanwhile simply exits (the app is keeping things up to date);
- a background run holds it while working; when the app opens, it sends the
  run SIGTERM, which stops it after the current file (indexing is resumable),
  and takes over.

Reading Dropbox and other cloud folders from a background job can need Full
Disk Access, and without it macOS does not refuse -- listing the folder just
never returns. So each folder is listed first with a time limit, and a
folder that does not answer is reported as needing access rather than left
hanging.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import plistlib
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import library
from .settings import app_home

LABEL = "io.github.chapmanwilliam.folder-search.updater"
INTERVAL_SECONDS = 30 * 60
PREFLIGHT_SECONDS = 15


# -- the lock shared with the app ---------------------------------------------


class IndexLock:
    """An exclusive, process-wide lock on indexing, released on exit.

    flock locks die with the process, so a crash never leaves a stale lock.
    The holder's pid is written into the file so the app can ask a
    background run to stop.
    """

    def __init__(self, path: Path | None = None):
        self.path = path or app_home() / "indexing.lock"
        self._fd: int | None = None

    def acquire(self, role: str) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return False
        os.ftruncate(fd, 0)
        os.write(fd, json.dumps({"pid": os.getpid(), "role": role}).encode())
        self._fd = fd
        return True

    def holder(self) -> dict | None:
        try:
            return json.loads(self.path.read_text() or "null")
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    def release(self) -> None:
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None

    @property
    def held(self) -> bool:
        return self._fd is not None


def take_over(lock: IndexLock, wait: float = 120.0) -> bool:
    """For the app: get the lock, stopping a background run if it has it."""
    if lock.acquire("app"):
        return True
    holder = lock.holder() or {}
    if holder.get("role") == "background" and holder.get("pid"):
        try:
            os.kill(int(holder["pid"]), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, ValueError):
            pass
    end = time.time() + wait
    while time.time() < end:
        time.sleep(0.5)
        if lock.acquire("app"):
            return True
    return False


# -- status shared with the app -----------------------------------------------


def status_path() -> Path:
    return app_home() / "background.json"


def read_status() -> dict:
    try:
        return json.loads(status_path().read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _write_status(status: dict) -> None:
    p = status_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(status, indent=2))
    tmp.replace(p)


# -- one background run --------------------------------------------------------


def can_list(folder: Path, timeout: float = PREFLIGHT_SECONDS) -> bool:
    """List the folder on a side thread; False if macOS never answers."""
    done = threading.Event()

    def go():
        try:
            os.listdir(folder)
        except OSError:
            pass
        done.set()

    threading.Thread(target=go, daemon=True).start()
    return done.wait(timeout)


class LazyEmbedder:
    """Loads the model on first use, so a run with no changes stays cheap."""

    def __init__(self, factory=None):
        self._factory = factory
        self._inner = None

    def _get(self):
        if self._inner is None:
            if self._factory:
                self._inner = self._factory()
            else:
                from .engine.model import Embedder

                self._inner = Embedder()
        return self._inner

    def embed_documents(self, texts):
        return self._get().embed_documents(texts)

    def embed_queries(self, texts):
        return self._get().embed_queries(texts)

    def release_memory(self):
        if self._inner is not None:
            self._inner.release_memory()


def run_once(embedder_factory=None, log: logging.Logger | None = None) -> dict:
    """Bring every folder up to date, unless the app is open.

    Returns the status written for the app to read.
    """
    from .engine.indexer import index_folder

    log = log or logging.getLogger("folder_search.background")
    lock = IndexLock()
    if not lock.acquire("background"):
        log.info("Folder Search is open or another update is running; nothing to do")
        return read_status()

    stop = threading.Event()
    previous = signal.signal(signal.SIGTERM, lambda *_: stop.set())
    status = {
        "started": time.strftime("%Y-%m-%d %H:%M:%S"),
        "finished": None,
        "needs_access": [],
        "folders": {},
    }
    embedder = LazyEmbedder(embedder_factory)
    try:
        _write_status(status)
        for entry in library.load():
            if stop.is_set():
                break
            folder = Path(entry.path)
            if entry.paused or not folder.is_dir():
                continue
            if not can_list(folder):
                log.warning("no answer listing %s: needs Full Disk Access", folder)
                status["needs_access"].append(entry.path)
                _write_status(status)
                continue
            store = library.open_store(entry.path)
            try:
                p = index_folder(
                    folder,
                    store,
                    embedder,
                    include_online=entry.include_online,
                    should_stop=stop.is_set,
                )
                result = {k: getattr(p, k) for k in ("phase", "added", "updated", "removed", "failed")}
            except Exception as e:
                result = {"phase": "error", "error": f"{type(e).__name__}: {e}"}
            finally:
                store.close()
            status["folders"][entry.path] = result
            log.info("%s: %s", entry.name, result)
            _write_status(status)
        status["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
        status["stopped"] = stop.is_set()
        _write_status(status)
        return status
    finally:
        signal.signal(signal.SIGTERM, previous)
        lock.release()


# -- the LaunchAgent -----------------------------------------------------------


def agent_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def log_path() -> Path:
    return Path.home() / "Library" / "Logs" / "Folder Search" / "background.log"


def agent_plist(python: str | None = None) -> dict:
    python = python or sys.executable
    log_path().parent.mkdir(parents=True, exist_ok=True)
    return {
        "Label": LABEL,
        "ProgramArguments": [python, "-m", "folder_search.background"],
        "RunAtLoad": True,
        "StartInterval": INTERVAL_SECONDS,
        # Low CPU and disk priority. Measured: embedding speed is unchanged
        # (the GPU does that work), so this costs nothing but courtesy.
        "ProcessType": "Background",
        "LowPriorityIO": True,
        "EnvironmentVariables": {
            "HF_HUB_OFFLINE": "1",
            "TOKENIZERS_PARALLELISM": "false",
            "HF_HUB_DISABLE_PROGRESS_BARS": "1",
            **({"FOLDER_SEARCH_HOME": os.environ["FOLDER_SEARCH_HOME"]}
               if "FOLDER_SEARCH_HOME" in os.environ else {}),
        },
        "StandardOutPath": str(log_path()),
        "StandardErrorPath": str(log_path()),
    }


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def is_enabled() -> bool:
    return agent_path().exists()


def enable(python: str | None = None) -> None:
    """Install (or refresh) the LaunchAgent and load it."""
    path = agent_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    new = plistlib.dumps(agent_plist(python))
    if path.exists() and path.read_bytes() == new:
        return  # already installed as is; avoid re-triggering a run
    domain = f"gui/{os.getuid()}"
    if path.exists():
        _launchctl("bootout", f"{domain}/{LABEL}")
    path.write_bytes(new)
    _launchctl("bootstrap", domain, str(path))


def disable() -> None:
    _launchctl("bootout", f"gui/{os.getuid()}/{LABEL}")
    agent_path().unlink(missing_ok=True)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(message)s",
        stream=sys.stdout,
    )
    # Keep the log to one line per folder per run: no model-loading chatter.
    for noisy in ("sentence_transformers", "transformers", "huggingface_hub", "torch"):
        logging.getLogger(noisy).setLevel(logging.ERROR)
    os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
    os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
    try:
        from transformers.utils import logging as hf_logging

        hf_logging.disable_progress_bar()
    except Exception:
        pass
    status = run_once()
    for handler in logging.getLogger().handlers:
        handler.flush()
    if status.get("needs_access"):
        # A listing that never answered leaves a thread stuck in the kernel;
        # leave without waiting for it.
        os._exit(0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
