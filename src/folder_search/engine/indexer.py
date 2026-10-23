"""Index a folder: walk, extract, chunk, embed, store. Incremental and resumable.

The folder is opened read-only; nothing in it is ever written. Each file is
committed as soon as it is embedded, so stopping (or quitting, or a crash)
loses at most the file in progress, and the next run carries on from there:
a file is redone only when its size or modification time has changed and
its content hash then differs too.

Cloud-only files are handled by the caller's choice:
- include_online=False: they are listed in the `skipped` table and never
  read. The kernel is told to refuse downloads for the duration, so a slip
  fails safe instead of downloading.
- include_online=True: they are read, which makes the cloud provider
  download them.
A file indexed earlier and since evicted to the cloud by Dropbox stays in
the index: its text has not changed just because the bytes left the disk.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..settings import IndexSettings, ocr_cache_dir
from .chunk import chunk_pages
from .cloudfiles import Entry, allow_downloads, reset_download_policy, walk
from .extract import Extracted, UnsupportedFormat, extract
from .ocr import OcrFailed, OcrUnavailable, ocr_available, ocr_pdf
from .store import Store


class FolderMissing(Exception):
    pass


@dataclass
class Progress:
    phase: str = "scanning"  # scanning | indexing | done | stopped | error
    total: int = 0           # files needing work this run
    done: int = 0
    total_bytes: int = 0
    done_bytes: int = 0
    current: str = ""
    added: int = 0
    updated: int = 0
    unchanged: int = 0
    failed: int = 0
    ocred: int = 0
    removed: int = 0
    skipped_online: int = 0
    started: float = field(default_factory=time.time)
    error: str = ""

    def as_dict(self) -> dict:
        d = asdict(self)
        d["elapsed"] = time.time() - self.started
        return d


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while block := f.read(1 << 20):
            h.update(block)
    return h.hexdigest()


def extract_with_ocr(path: Path, sha: str, settings: IndexSettings) -> tuple[Extracted, bool]:
    """Extract text, OCR'ing a PDF that has no text layer if ocrmypdf exists.

    Returns the extraction and whether OCR was used. OCR failure is not
    fatal: a document with some text already keeps that text.
    """
    extracted = extract(path)
    if path.suffix.lower() != ".pdf" or extracted.pages or not settings.ocr:
        return extracted, False
    if not ocr_available():
        return extracted, False
    try:
        sidecar = ocr_pdf(
            path, sha, ocr_cache_dir(), timeout=settings.ocr_timeout, language=settings.ocr_language
        )
    except (OcrFailed, OcrUnavailable):
        return extracted, False
    ocred = extract(sidecar)
    if ocred.text_length <= extracted.text_length:
        return extracted, False
    return ocred, True


def _top_folder(rel: Path) -> str:
    return rel.parts[0] if len(rel.parts) > 1 else ""


def index_folder(
    folder: Path,
    store: Store,
    embedder,
    *,
    include_online: bool = False,
    settings: IndexSettings | None = None,
    on_progress: Callable[[Progress], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> Progress:
    settings = settings or IndexSettings()
    folder = Path(folder).resolve()
    p = Progress()
    report = (lambda: on_progress(p)) if on_progress else (lambda: None)
    stop = should_stop or (lambda: False)
    report()

    if not folder.is_dir():
        raise FolderMissing(f"{folder} is not there (moved, renamed, or a drive not connected)")

    allow_downloads(include_online)
    try:
        entries = list(walk(folder))
        known = store.known_files()
        present = {str(e.path.relative_to(folder)) for e in entries}

        # A folder that suddenly looks empty is far more likely to be a sync
        # app that is not running, or a drive half-mounted, than a user who
        # deleted everything. Keep the index rather than wipe it.
        if known and not entries:
            raise FolderMissing(f"{folder} appears empty; the index was kept")

        for rel, rec in known.items():
            if rel not in present:
                store.remove_file(rec.id)
                p.removed += 1
        store.commit()

        todo: list[tuple[Entry, str]] = []
        skipped: list[tuple[str, int]] = []
        for e in entries:
            rel = str(e.path.relative_to(folder))
            rec = known.get(rel)
            if rec and rec.mtime == e.mtime and rec.size == e.size:
                p.unchanged += 1
            elif e.online_only and not include_online:
                if rec:
                    p.unchanged += 1  # evicted since indexing; keep it
                else:
                    skipped.append((rel, e.size))
            else:
                todo.append((e, rel))
        store.set_skipped(skipped)
        store.commit()
        p.skipped_online = len(skipped)
        p.total = len(todo)
        p.total_bytes = sum(e.size for e, _ in todo)
        p.phase = "indexing"
        report()

        for e, rel in todo:
            if stop():
                p.phase = "stopped"
                p.current = ""
                report()
                return p
            p.current = rel
            report()
            _index_one(e, rel, store, embedder, known.get(rel), settings, p)
            store.commit()
            p.done += 1
            p.done_bytes += e.size
            if p.done % 25 == 0:
                embedder.release_memory()
            report()

        store.set_meta("last_indexed", time.strftime("%Y-%m-%d %H:%M"))
        store.commit()
        p.phase = "done"
        p.current = ""
        report()
        return p
    finally:
        reset_download_policy()


def _index_one(e: Entry, rel: str, store: Store, embedder, rec, settings, p: Progress) -> None:
    try:
        sha = file_sha256(e.path)
        if rec and rec.sha256 == sha:
            store.touch_file(rec.id, e.mtime)
            p.unchanged += 1
            return
        extracted, used_ocr = extract_with_ocr(e.path, sha, settings)
        if not extracted.pages:
            if extracted.empty_pages:
                hint = "" if ocr_available() else "; install ocrmypdf to read scans"
                reason = f"scanned, no searchable text ({extracted.empty_pages} pages{hint})"
            else:
                reason = "no text"
            store.record_failure(rel, reason)
            p.failed += 1
            return
        chunks = chunk_pages(extracted.pages, settings.chunk_tokens, settings.chunk_overlap)
        vectors = embedder.embed_documents([c.text for c in chunks])
    except UnsupportedFormat:
        return
    except Exception as ex:  # unreadable, encrypted, corrupt: report, move on
        store.record_failure(rel, f"{type(ex).__name__}: {ex}")
        p.failed += 1
        return

    if rec:
        store.remove_file(rec.id)
        p.updated += 1
    else:
        p.added += 1
    paginated = extracted.pages[0].number > 0
    page_count = len(extracted.pages) + extracted.empty_pages if paginated else 0
    store.add_file(
        rel,
        _top_folder(Path(rel)),
        e.mtime,
        e.size,
        sha,
        page_count,
        extracted.empty_pages,
        chunks,
        vectors,
        ocr=used_ocr,
    )
    if used_ocr:
        p.ocred += 1
    store.clear_failure(rel)
