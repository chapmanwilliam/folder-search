"""SQLite store for one indexed folder: files, chunks, FTS5 keywords, vectors.

One database file holds everything. SQLite gives BM25 keyword search (FTS5)
and vector search (sqlite-vec) in one file with one dependency.

Vectors are stored as int8 (vec_quantize_int8 over unit vectors), a quarter
the size of float32, so a 10,000-file folder's vectors fit in well under a
gigabyte. Retrieval quality loss from int8 on unit vectors is negligible.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

import numpy as np

SCHEMA_VERSION = 1

SCHEMA = """
create table if not exists meta (
    key text primary key,
    value text not null
);

create table if not exists files (
    id integer primary key,
    path text not null unique,       -- relative to the indexed folder
    filename text not null,
    folder text not null default '', -- top-level subfolder, '' at the root
    mtime real not null,
    size integer not null default 0,
    sha256 text not null,
    pages integer not null default 0,
    empty_pages integer not null default 0,
    ocr integer not null default 0,
    indexed_at text not null default (datetime('now'))
);
create index if not exists files_folder on files(folder);

create table if not exists chunks (
    id integer primary key,
    file_id integer not null references files(id) on delete cascade,
    page_start integer not null,
    page_end integer not null,
    text text not null
);
create index if not exists chunks_file on chunks(file_id);

create virtual table if not exists chunks_fts using fts5(
    text, content='chunks', content_rowid='id',
    tokenize='porter unicode61'
);

-- Files that could not be indexed, and why. Cleared when a file succeeds.
create table if not exists failures (
    path text primary key,
    reason text not null,
    at text not null default (datetime('now'))
);

-- Cloud-only files left out because the user chose not to download them.
create table if not exists skipped (
    path text primary key,
    size integer not null
);
"""

VEC_SCHEMA = """
create virtual table if not exists chunks_vec using vec0(
    embedding int8[{dims}] distance_metric=cosine,
    folder text  -- copy of files.folder, so a one-subfolder search filters
                 -- inside the nearest-neighbour scan rather than after it
);
"""


@dataclass
class FileRecord:
    id: int
    path: str
    mtime: float
    size: int
    sha256: str


def vec_bytes(v: np.ndarray) -> bytes:
    return np.asarray(v, dtype=np.float32).tobytes()


class Store:
    def __init__(self, path: str | Path, dims: int = 768, check_same_thread: bool = True):
        import sqlite_vec

        path = Path(path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.db = sqlite3.connect(str(path), check_same_thread=check_same_thread)
        self.db.enable_load_extension(True)
        sqlite_vec.load(self.db)
        self.db.enable_load_extension(False)
        self.db.execute("pragma foreign_keys = on")
        # WAL lets the UI search while the indexer writes.
        self.db.execute("pragma journal_mode = wal")
        self.db.executescript(SCHEMA)
        self.db.executescript(VEC_SCHEMA.format(dims=dims))
        self.set_meta("schema_version", str(SCHEMA_VERSION))
        self.set_meta("dims", str(dims))
        self.db.commit()

    # -- metadata -------------------------------------------------------

    def set_meta(self, key: str, value: str) -> None:
        self.db.execute(
            "insert into meta (key, value) values (?, ?) "
            "on conflict(key) do update set value = excluded.value",
            (key, value),
        )

    def get_meta(self, key: str) -> str | None:
        row = self.db.execute("select value from meta where key = ?", (key,)).fetchone()
        return row[0] if row else None

    # -- files ----------------------------------------------------------

    def known_files(self) -> dict[str, FileRecord]:
        rows = self.db.execute("select id, path, mtime, size, sha256 from files").fetchall()
        return {r[1]: FileRecord(*r) for r in rows}

    def remove_file(self, file_id: int) -> None:
        chunk_ids = [
            r[0] for r in self.db.execute("select id from chunks where file_id = ?", (file_id,))
        ]
        for cid in chunk_ids:
            self.db.execute("delete from chunks_vec where rowid = ?", (cid,))
            self.db.execute(
                "insert into chunks_fts(chunks_fts, rowid, text) "
                "select 'delete', id, text from chunks where id = ?",
                (cid,),
            )
        self.db.execute("delete from chunks where file_id = ?", (file_id,))
        self.db.execute("delete from files where id = ?", (file_id,))

    def add_file(
        self,
        path: str,
        folder: str,
        mtime: float,
        size: int,
        sha256: str,
        pages: int,
        empty_pages: int,
        chunks: list,
        vectors: np.ndarray,
        ocr: bool = False,
    ) -> int:
        cur = self.db.execute(
            "insert into files (path, filename, folder, mtime, size, sha256, "
            "pages, empty_pages, ocr) values (?,?,?,?,?,?,?,?,?)",
            (path, Path(path).name, folder, mtime, size, sha256, pages, empty_pages, int(ocr)),
        )
        file_id = cur.lastrowid
        for chunk, vec in zip(chunks, vectors, strict=True):
            cur = self.db.execute(
                "insert into chunks (file_id, page_start, page_end, text) values (?,?,?,?)",
                (file_id, chunk.page_start, chunk.page_end, chunk.text),
            )
            cid = cur.lastrowid
            self.db.execute("insert into chunks_fts(rowid, text) values (?,?)", (cid, chunk.text))
            self.db.execute(
                "insert into chunks_vec(rowid, embedding, folder) "
                "values (?, vec_quantize_int8(?, 'unit'), ?)",
                (cid, vec_bytes(vec), folder),
            )
        return file_id

    def touch_file(self, file_id: int, mtime: float) -> None:
        """Record a new mtime for a file whose content hash did not change."""
        self.db.execute("update files set mtime = ? where id = ?", (mtime, file_id))

    # -- failures and skipped -------------------------------------------

    def record_failure(self, path: str, reason: str) -> None:
        self.db.execute(
            "insert into failures (path, reason) values (?,?) "
            "on conflict(path) do update set reason=excluded.reason, at=datetime('now')",
            (path, reason),
        )

    def clear_failure(self, path: str) -> None:
        self.db.execute("delete from failures where path = ?", (path,))

    def failures(self) -> list[tuple[str, str]]:
        return self.db.execute("select path, reason from failures order by path").fetchall()

    def set_skipped(self, items: list[tuple[str, int]]) -> None:
        self.db.execute("delete from skipped")
        self.db.executemany("insert or replace into skipped (path, size) values (?,?)", items)

    def skipped(self) -> list[tuple[str, int]]:
        return self.db.execute("select path, size from skipped order by path").fetchall()

    # -- reporting ------------------------------------------------------

    def stats(self) -> dict:
        one = lambda q: self.db.execute(q).fetchone()[0]  # noqa: E731
        return {
            "files": one("select count(*) from files"),
            "chunks": one("select count(*) from chunks"),
            "failures": one("select count(*) from failures"),
            "skipped": one("select count(*) from skipped"),
            "skipped_bytes": one("select coalesce(sum(size), 0) from skipped"),
            "ocr_files": one("select count(*) from files where ocr = 1"),
            "last_indexed": self.get_meta("last_indexed"),
        }

    def subfolders(self) -> list[tuple[str, int]]:
        return self.db.execute(
            "select folder, count(*) from files where folder != '' "
            "group by folder order by folder collate nocase"
        ).fetchall()

    def all_dirs(self) -> list[tuple[str, int]]:
        """Every subfolder holding indexed files, at any depth, with the
        number of files under it (including deeper subfolders)."""
        counts: dict[str, int] = {}
        for (path,) in self.db.execute("select path from files"):
            parts = path.split("/")[:-1]
            for i in range(1, len(parts) + 1):
                d = "/".join(parts[:i])
                counts[d] = counts.get(d, 0) + 1
        return sorted(counts.items(), key=lambda kv: kv[0].lower())

    def commit(self) -> None:
        self.db.commit()

    def close(self) -> None:
        self.db.commit()
        self.db.close()
