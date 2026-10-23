"""A queue of index jobs, run one at a time on a background thread.

Two kinds of job share it:
- the user's ("Add folder", "Update index"): go to the front, show a
  progress banner, and pre-empt an automatic update that is running;
- automatic ones (app start, folder changed): go to the back and run quietly.

Pre-empting is safe because indexing is resumable: a stopped job keeps every
file it finished, and the pre-empted folder is queued again to finish.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import library

RUNNING = ("loading", "scanning", "indexing")


@dataclass
class Request:
    path: str
    auto: bool
    include_online: bool | None = None  # None: use the folder's saved choice


class IndexQueue:
    def __init__(self, get_embedder: Callable, on_finished: Callable[[dict], None] | None = None):
        self._get_embedder = get_embedder
        self._on_finished = on_finished
        self._cv = threading.Condition()
        self._pending: list[Request] = []
        self._current: dict | None = None
        self._last: dict | None = None
        self._stop = threading.Event()
        self._user_stopped = False
        self._thread: threading.Thread | None = None

    # -- submitting -----------------------------------------------------

    def submit(self, path: str, auto: bool, include_online: bool | None = None) -> None:
        path = str(Path(path).expanduser().resolve())
        with self._cv:
            cur = self._current
            if cur and cur["path"] == path and not auto:
                # Already running for this folder: just make it visible.
                cur["auto"] = False
                return
            # One pending request per folder; a user request wins over an
            # automatic one and keeps its include_online choice.
            existing = next((r for r in self._pending if r.path == path), None)
            if existing:
                if auto:
                    return
                self._pending.remove(existing)
            req = Request(path, auto, include_online)
            if auto:
                self._pending.append(req)
            else:
                self._pending.insert(0, req)
                if cur and cur["auto"]:
                    self._stop.set()  # pre-empt; requeued in _run_one
            self._ensure_thread()
            self._cv.notify()

    def stop_current(self) -> None:
        """The user's Stop button: stop now and pause the folder."""
        with self._cv:
            if self._current:
                self._user_stopped = True
                self._stop.set()

    def cancel(self, path: str) -> None:
        """Forget queued work for a folder (it is being removed)."""
        path = str(Path(path).expanduser().resolve())
        with self._cv:
            self._pending = [r for r in self._pending if r.path != path]

    def busy_with(self, path: str) -> bool:
        cur = self._current
        return bool(cur and cur["path"] == str(Path(path).expanduser().resolve()))

    def progress(self) -> dict | None:
        with self._cv:
            job = self._current or self._last
            if not job:
                return None
            return {
                "path": job["path"],
                "name": job["name"],
                "auto": job["auto"],
                "queued": len(self._pending),
                **job["p"],
            }

    def wait_idle(self, timeout: float = 60) -> bool:
        """For tests: block until nothing is running or queued."""
        end = time.time() + timeout
        while time.time() < end:
            with self._cv:
                if not self._pending and not self._current:
                    return True
            time.sleep(0.05)
        return False

    # -- running --------------------------------------------------------

    def _ensure_thread(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._loop, daemon=True, name="index-queue")
            self._thread.start()

    def _loop(self) -> None:
        while True:
            with self._cv:
                while not self._pending:
                    self._cv.wait()
                req = self._pending.pop(0)
                entry = library.get(req.path)
                if entry is None or (req.auto and entry.paused):
                    continue
                include_online = (
                    entry.include_online if req.include_online is None else req.include_online
                )
                job = {
                    "path": entry.path,
                    "name": entry.name,
                    "auto": req.auto,
                    "p": {"phase": "loading"},
                }
                self._current = job
                self._stop.clear()
                self._user_stopped = False
            if not req.auto:
                library.set_paused(entry.path, False)
            self._run_one(job, include_online)

    def _run_one(self, job: dict, include_online: bool) -> None:
        from .engine.indexer import index_folder

        try:
            embedder = self._get_embedder()
            store = library.open_store(job["path"])
            try:
                index_folder(
                    Path(job["path"]),
                    store,
                    embedder,
                    include_online=include_online,
                    on_progress=lambda p: job.__setitem__("p", p.as_dict()),
                    should_stop=self._stop.is_set,
                )
            finally:
                store.close()
        except Exception as e:
            job["p"] = {**job["p"], "phase": "error", "error": f"{type(e).__name__}: {e}"}

        with self._cv:
            stopped = job["p"].get("phase") == "stopped"
            if stopped and self._user_stopped:
                library.set_paused(job["path"], True)
            elif stopped:
                # Pre-empted by a user job: finish this folder afterwards.
                self._pending.append(Request(job["path"], True))
            self._current = None
            self._last = job
        if self._on_finished:
            try:
                self._on_finished(job)
            except Exception:
                pass
