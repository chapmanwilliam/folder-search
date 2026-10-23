"""The desktop app: a pywebview window over the engine.

The front end (ui/index.html) calls the methods of `Api` through
`window.pywebview.api`. pywebview runs each call on its own thread, so:
- each call opens its own SQLite connection (cheap; WAL lets reads and the
  indexer's writes overlap);
- the embedding model is shared, behind a lock, and the indexer embeds in
  small batches so a search never waits long behind a 2,000-page bundle.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path

from . import library
from .engine.cloudfiles import scan
from .engine.ocr import ocr_available

UI_DIR = Path(__file__).parent / "ui"
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
    def __init__(self):
        self.window = None
        self._embedder: SharedEmbedder | None = None
        self._embedder_lock = threading.Lock()
        self._model_state = {"phase": "unknown", "done": 0, "total": 0, "error": ""}
        self._job: dict | None = None  # the running (or last) index job
        self._stop = threading.Event()

    # -- model ----------------------------------------------------------

    def _get_embedder(self) -> SharedEmbedder:
        with self._embedder_lock:
            if self._embedder is None:
                self._embedder = SharedEmbedder()
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
            except Exception as e:
                self._model_state.update(phase="error", error=f"{type(e).__name__}: {e}")

        threading.Thread(target=run, daemon=True).start()
        return self.model_status()

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
        if self._job and self._job["path"] == path and self._job["p"].get("phase") in (
            "scanning",
            "indexing",
        ):
            return False
        library.remove(path)
        return True

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
        if self._job and self._job["p"].get("phase") in ("scanning", "indexing", "loading"):
            return {"ok": False, "error": "Another folder is being indexed."}
        existing = library.get(path)
        if include_online is None:
            include_online = existing.include_online if existing else False
        entry = library.add(path, include_online=include_online)
        self._stop.clear()
        job = {"path": entry.path, "name": entry.name, "p": {"phase": "loading"}}
        self._job = job

        def run():
            from .engine.indexer import index_folder

            try:
                embedder = self._get_embedder()
                store = library.open_store(entry.path)
                try:
                    index_folder(
                        Path(entry.path),
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

        threading.Thread(target=run, daemon=True).start()
        return {"ok": True}

    def stop_index(self) -> bool:
        self._stop.set()
        return True

    def progress(self) -> dict | None:
        if not self._job:
            return None
        return {"path": self._job["path"], "name": self._job["name"], **self._job["p"]}

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
                hits = search_chunks(store, qvec, query, folder=subfolder or None)
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
    webview.start(debug=bool(os.environ.get("FOLDER_SEARCH_DEBUG")))


if __name__ == "__main__":
    main()
