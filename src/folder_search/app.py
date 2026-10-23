"""The desktop app: a pywebview window over the engine.

The front end (ui/index.html) calls the methods of `Api` through
`window.pywebview.api`. pywebview runs each call on its own thread, so:
- each call opens its own SQLite connection (cheap; WAL lets reads and the
  indexer's writes overlap);
- the embedding model is shared, behind a lock, and the indexer embeds in
  small batches so a search never waits long behind a 2,000-page bundle.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import background, library
from .background import IndexLock, take_over
from .settings import app_home
from .engine.cloudfiles import scan
from .engine.ocr import ocr_available
from .jobs import IndexQueue

UI_DIR = Path(__file__).parent / "ui"
ICON = Path(__file__).parent / "assets" / "FolderSearch.icns"
EMBED_BATCH = 32


class SharedEmbedder:
    """One model for the indexer and the search box, used one batch at a time."""

    def __init__(self):
        from .engine.model import Embedder

        self._inner = Embedder()
        self._lock = threading.Lock()

    def embed_documents(self, texts):
        import numpy as np

        out = []
        for i in range(0, len(texts), EMBED_BATCH):
            with self._lock:
                out.append(self._inner.embed_documents(texts[i : i + EMBED_BATCH]))
        return np.concatenate(out) if out else np.zeros((0, self._inner.dimensions))

    def embed_queries(self, texts):
        with self._lock:
            return self._inner.embed_queries(texts)

    def release_memory(self):
        with self._lock:
            self._inner.release_memory()


class Api:
    def __init__(self, embedder_factory=None, watch: bool = True):
        self.window = None
        self._embedder = None
        self._embedder_factory = embedder_factory or SharedEmbedder
        self._embedder_lock = threading.Lock()
        self._model_state = {"phase": "unknown", "done": 0, "total": 0, "error": ""}
        self._gate = threading.Event()
        self.queue = IndexQueue(self._get_embedder, gate=self._gate)
        self._watch = watch
        self.watcher = None
        self._background_started = False
        self._lock = IndexLock()
        self._lock_problem = ""

    # -- model ----------------------------------------------------------

    def _get_embedder(self) -> SharedEmbedder:
        with self._embedder_lock:
            if self._embedder is None:
                self._embedder = self._embedder_factory()
            return self._embedder

    def model_status(self) -> dict:
        if self._model_state["phase"] == "unknown":
            from .engine.model import model_is_cached

            self._model_state["phase"] = "ready" if model_is_cached() else "missing"
        return dict(self._model_state)

    def download_model(self) -> dict:
        if self._model_state["phase"] in ("downloading", "ready"):
            return self.model_status()
        self._model_state.update(phase="downloading", done=0, total=0, error="")

        def run():
            from .engine.model import download_model

            def prog(done, total):
                self._model_state.update(done=done, total=total)

            try:
                download_model(prog)
                self._model_state["phase"] = "loading"
                self._get_embedder()
                self._model_state["phase"] = "ready"
                self.start_background()
            except Exception as e:
                self._model_state.update(phase="error", error=f"{type(e).__name__}: {e}")

        threading.Thread(target=run, daemon=True).start()
        return self.model_status()

    def model_loaded(self) -> bool:
        """Whether the model is in memory, so a search will be quick."""
        return self._embedder is not None

    def warm_up(self) -> bool:
        """Load the model in the background so the first search is quick."""
        threading.Thread(target=self._get_embedder, daemon=True).start()
        return True

    # -- folders --------------------------------------------------------

    def state(self) -> dict:
        folders = []
        for e in library.load():
            try:
                store = library.open_store(e.path)
                stats = store.stats()
                subs = store.subfolders()
                store.close()
            except Exception as ex:  # a damaged index should not hide the rest
                stats, subs = {"error": str(ex)}, []
            folders.append(
                {
                    "path": e.path,
                    "name": e.name,
                    "include_online": e.include_online,
                    "paused": e.paused,
                    "exists": Path(e.path).is_dir(),
                    "stats": stats,
                    "subfolders": [s for s, _ in subs],
                }
            )
        return {
            "folders": folders,
            "job": self.progress(),
            "ocr": ocr_available(),
            "model": self.model_status(),
            "auto_update": self.watcher is not None,
            "background": self.background_info(),
        }

    def choose_folder(self) -> str | None:
        import webview

        result = self.window.create_file_dialog(
            webview.FileDialog.FOLDER, directory=str(Path.home())
        )
        if not result:
            return None
        return result[0] if isinstance(result, (list, tuple)) else result

    def scan_folder(self, path: str) -> dict:
        t = time.time()
        r = scan(Path(path))
        d = r.as_dict()
        d["seconds"] = round(time.time() - t, 1)
        d["path"] = path
        d["name"] = Path(path).name
        d["already"] = library.get(path) is not None
        return d

    def remove_folder(self, path: str) -> bool:
        if self.queue.busy_with(path):
            return False
        self.queue.cancel(path)
        library.remove(path)
        self._sync_watcher()
        return True

    def subfolders(self, path: str) -> list:
        """Every subfolder of an indexed folder, for the 'search in' picker."""
        store = library.open_store(path)
        try:
            return [{"dir": d, "files": n} for d, n in store.all_dirs()]
        finally:
            store.close()

    def failures(self, path: str) -> list:
        store = library.open_store(path)
        try:
            return [{"path": p, "reason": r} for p, r in store.failures()]
        finally:
            store.close()

    def skipped(self, path: str) -> list:
        store = library.open_store(path)
        try:
            return [{"path": p, "size": s} for p, s in store.skipped()]
        finally:
            store.close()

    # -- indexing -------------------------------------------------------

    def start_index(self, path: str, include_online: bool | None = None) -> dict:
        """The user asked to index (or update) a folder: run it next, visibly."""
        existing = library.get(path)
        if include_online is None:
            include_online = existing.include_online if existing else False
        entry = library.add(path, include_online=include_online)
        self.queue.submit(entry.path, auto=False, include_online=include_online)
        self._sync_watcher()
        return {"ok": True}

    def stop_index(self) -> bool:
        self.queue.stop_current()
        return True

    def progress(self) -> dict | None:
        return self.queue.progress()

    # -- automatic updates ---------------------------------------------

    def start_background(self) -> bool:
        """Once the model is available: update every folder, then watch them.

        Called by the front end after startup, and again when a first-run
        model download finishes. Safe to call more than once.
        """
        if self._background_started or self.model_status()["phase"] != "ready":
            return False
        self._background_started = True
        threading.Thread(target=self._start_background, daemon=True).start()
        return True

    def _start_background(self) -> None:
        # Take indexing over from a background run, if one is working; it
        # stops after its current file.
        if not take_over(self._lock):
            self._lock_problem = "Another copy of Folder Search is indexing."
            return
        self._gate.set()
        self._apply_background_setting()
        for e in library.load():
            if not e.paused and Path(e.path).is_dir():
                self.queue.submit(e.path, auto=True)
        if self._watch:
            try:
                from .watch import FolderWatcher

                self.watcher = FolderWatcher(lambda root: self.queue.submit(root, auto=True))
                self._sync_watcher()
                self.watcher.start()
            except Exception:
                self.watcher = None  # updates still work by hand

    # -- updates while the app is closed ----------------------------------

    def _settings(self) -> dict:
        try:
            return json.loads((app_home() / "settings.json").read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _apply_background_setting(self) -> None:
        # On unless the user turned it off. Re-installing on each start keeps
        # the LaunchAgent pointing at the Python this copy runs on.
        # A copy run from a source checkout (development) leaves the
        # LaunchAgent alone, so it keeps pointing at the installed app.
        try:
            if self._settings().get("background", True):
                if not _running_from_source():
                    background.enable()
            else:
                background.disable()
        except Exception:
            pass

    def set_background(self, on: bool) -> dict:
        s = self._settings()
        s["background"] = bool(on)
        p = app_home() / "settings.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(s, indent=2))
        self._apply_background_setting()
        return self.background_info()

    def background_info(self) -> dict:
        status = background.read_status()
        return {
            "enabled": background.is_enabled(),
            "last_run": status.get("finished") or status.get("started"),
            "needs_access": status.get("needs_access", []),
            "lock_problem": self._lock_problem,
        }

    def open_privacy_settings(self) -> bool:
        subprocess.Popen(
            ["open", "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"]
        )
        return True

    def reveal_python(self) -> bool:
        """Show the program to add under Full Disk Access."""
        subprocess.Popen(["open", "-R", sys.executable])
        return True

    def _sync_watcher(self) -> None:
        if self.watcher is not None:
            self.watcher.sync([e.path for e in library.load()])

    # -- search ---------------------------------------------------------

    def search(self, query: str, path: str | None = None, subfolder: str | None = None,
               limit: int = 30) -> dict:
        from .engine.search import group_by_file, search_chunks

        query = (query or "").strip()
        if not query:
            return {"results": [], "ms": 0}
        t = time.time()
        embedder = self._get_embedder()
        qvec = embedder.embed_queries([query.replace('"', "")])[0]
        entries = [e for e in library.load() if path in (None, "", e.path)]
        results = []
        for e in entries:
            store = library.open_store(e.path)
            try:
                hits = search_chunks(store, qvec, query, within=subfolder or None)
            finally:
                store.close()
            for fr in group_by_file(hits, max_files=limit):
                results.append(
                    {
                        "root": e.path,
                        "root_name": e.name,
                        "path": fr.path,
                        "abs": str(Path(e.path) / fr.path),
                        "filename": fr.filename,
                        "folder": fr.folder,
                        "score": fr.score,
                        "hits": [
                            {
                                "id": h.chunk_id,
                                "text": h.text,
                                "page_start": h.page_start,
                                "page_end": h.page_end,
                            }
                            for h in fr.hits
                        ],
                    }
                )
        # RRF scores share a scale across indexes, so results from several
        # folders can be merged by score directly.
        results.sort(key=lambda r: -r["score"])
        return {"results": results[:limit], "ms": round((time.time() - t) * 1000)}

    # -- results --------------------------------------------------------

    def preview(self, abs_path: str, page: int = 0, passage: str = "") -> dict:
        from .preview import render_pdf_page, text_preview

        p = Path(abs_path)
        if p.suffix.lower() == ".pdf":
            try:
                return render_pdf_page(p, page or 1, passage)
            except Exception as e:
                return {"kind": "error", "error": f"Could not show this page: {e}"}
        return text_preview(passage)

    def open_file(self, abs_path: str) -> bool:
        subprocess.Popen(["open", abs_path])
        return True

    def reveal(self, abs_path: str) -> bool:
        subprocess.Popen(["open", "-R", abs_path])
        return True


def _running_from_source() -> bool:
    """True in a git checkout (src/folder_search/app.py beside pyproject.toml);
    False for an installed copy, which lives in site-packages."""
    return (Path(__file__).resolve().parents[2] / "pyproject.toml").exists()


def _set_dock_icon() -> None:
    """Show our icon in the Dock even when run as a plain command.

    Launched from Folder Search.app, macOS takes the icon from the bundle;
    run as `folder-search` or `uv run folder-search`, it would otherwise
    show the generic Python rocket.
    """
    try:
        from AppKit import NSApplication, NSImage

        image = NSImage.alloc().initWithContentsOfFile_(str(ICON))
        if image is not None:
            NSApplication.sharedApplication().setApplicationIconImage_(image)
    except Exception:
        pass


def main() -> None:
    import webview

    # Keep torch's "Redirects are not supported" notice and tokenizer
    # parallelism warnings out of the console.
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    api = Api()
    window = webview.create_window(
        "Folder Search",
        url=str(UI_DIR / "index.html"),
        js_api=api,
        width=1280,
        height=820,
        min_size=(860, 560),
        text_select=True,
    )
    api.window = window
    _set_dock_icon()
    webview.start(debug=bool(os.environ.get("FOLDER_SEARCH_DEBUG")))


if __name__ == "__main__":
    main()
