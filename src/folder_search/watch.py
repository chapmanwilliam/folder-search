"""Notice changes in indexed folders and ask for an update once they settle.

macOS reports file changes through FSEvents (via the watchdog package), so
watching costs nothing while nothing changes, even for a folder of 80,000
files. A change does not trigger an update straight away: a document being
saved, or a batch of files being copied in, produces a burst of events.
Instead each folder waits until it has been quiet for `quiet` seconds, and
is not updated more often than every `min_gap` seconds.

Only events for documents the app can read count, so Dropbox shuffling its
own hidden files, or Word's "~$" lock files, do not cause updates.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path

from .engine.extract import SUPPORTED

QUIET_SECONDS = 60
MIN_GAP_SECONDS = 120
TICK_SECONDS = 5


def relevant(path: str, root: str, is_dir: bool = False) -> bool:
    """Is a changed path a document (or a folder) inside root that we index?

    A whole subfolder being moved in, out or deleted counts too.
    """
    try:
        rel = Path(path).relative_to(root)
    except ValueError:
        return False
    if not rel.parts:
        return False
    if any(part.startswith(".") for part in rel.parts):
        return False
    if is_dir:
        return True
    name = rel.parts[-1]
    if name.startswith("~$"):
        return False
    return Path(name).suffix.lower() in SUPPORTED


class Debouncer:
    """Decides when a folder with recent changes is due for an update."""

    def __init__(self, quiet: float = QUIET_SECONDS, min_gap: float = MIN_GAP_SECONDS,
                 clock: Callable[[], float] = time.monotonic):
        self.quiet = quiet
        self.min_gap = min_gap
        self.clock = clock
        self._last_event: dict[str, float] = {}
        self._last_run: dict[str, float] = {}
        self._lock = threading.Lock()

    def note(self, root: str) -> None:
        with self._lock:
            self._last_event[root] = self.clock()

    def forget(self, root: str) -> None:
        with self._lock:
            self._last_event.pop(root, None)
            self._last_run.pop(root, None)

    def due(self) -> list[str]:
        now = self.clock()
        out = []
        with self._lock:
            for root, t in list(self._last_event.items()):
                if now - t < self.quiet:
                    continue
                if now - self._last_run.get(root, -1e18) < self.min_gap:
                    continue
                out.append(root)
                del self._last_event[root]
                self._last_run[root] = now
        return out


class FolderWatcher:
    def __init__(self, on_due: Callable[[str], None], quiet: float = QUIET_SECONDS,
                 min_gap: float = MIN_GAP_SECONDS, tick: float = TICK_SECONDS):
        from watchdog.observers import Observer

        self.on_due = on_due
        self.debouncer = Debouncer(quiet, min_gap)
        self.tick = tick
        self._observer = Observer()
        self._watches: dict[str, object] = {}
        self._lock = threading.Lock()
        self._stopped = threading.Event()
        self._ticker: threading.Thread | None = None

    def _handler(self, root: str):
        from watchdog.events import FileSystemEventHandler

        debouncer = self.debouncer

        class Handler(FileSystemEventHandler):
            def on_any_event(self, event):
                if event.event_type not in ("created", "modified", "moved", "deleted"):
                    return
                if event.is_directory and event.event_type == "modified":
                    return
                paths = [event.src_path, getattr(event, "dest_path", "") or ""]
                if any(p and relevant(p, root, event.is_directory) for p in paths):
                    debouncer.note(root)

        return Handler()

    def sync(self, roots: list[str]) -> None:
        """Watch exactly these folders (add new ones, drop removed ones)."""
        wanted = {r for r in roots if Path(r).is_dir()}
        with self._lock:
            for root in list(self._watches):
                if root not in wanted:
                    self._observer.unschedule(self._watches.pop(root))
                    self.debouncer.forget(root)
            for root in wanted - set(self._watches):
                try:
                    self._watches[root] = self._observer.schedule(
                        self._handler(root), root, recursive=True
                    )
                except Exception:
                    pass  # unreadable folder: it simply is not watched

    def watching(self) -> list[str]:
        with self._lock:
            return sorted(self._watches)

    def start(self) -> None:
        self._observer.start()
        self._ticker = threading.Thread(target=self._tick_loop, daemon=True, name="watch-tick")
        self._ticker.start()

    def stop(self) -> None:
        self._stopped.set()
        self._observer.stop()

    def _tick_loop(self) -> None:
        while not self._stopped.wait(self.tick):
            for root in self.debouncer.due():
                try:
                    self.on_due(root)
                except Exception:
                    pass
