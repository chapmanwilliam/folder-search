"""Hybrid search: sqlite-vec similarity + FTS5 BM25, fused with RRF.

Embeddings find passages by meaning but are poor at exact names, reference
numbers and dates; keyword search is the reverse. Both rankings always take
part, fused by reciprocal rank so neither score scale dominates. A query in
double quotes is treated as an exact phrase on the keyword side.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import numpy as np

from .store import Store, vec_bytes

RRF_K = 60


@dataclass
class Hit:
    chunk_id: int
    score: float
    text: str
    path: str        # relative to the indexed folder
    filename: str
    folder: str      # top-level subfolder
    page_start: int
    page_end: int


@dataclass
class FileResult:
    path: str
    filename: str
    folder: str
    score: float
    hits: list[Hit] = field(default_factory=list)


def fts_query(query: str) -> str:
    """Turn free text into a safe FTS5 query.

    Quoted phrases stay phrases; everything else becomes OR'd quoted terms,
    so FTS5 operators typed by the user (NEAR, AND, brackets) are just words.
    """
    phrases = re.findall(r'"([^"]+)"', query)
    rest = re.sub(r'"[^"]*"', " ", query)
    parts = []
    for ph in phrases:
        words = re.findall(r"[\w']+", ph)
        if words:
            parts.append('"' + " ".join(words) + '"')
    parts += [f'"{t}"' for t in re.findall(r"[\w']+", rest)]
    return " OR ".join(parts)


def rrf(rankings: list[tuple[list[int], float]]) -> dict[int, float]:
    scores: dict[int, float] = {}
    for ranking, weight in rankings:
        for rank, cid in enumerate(ranking):
            scores[cid] = scores.get(cid, 0.0) + weight / (RRF_K + rank + 1)
    return scores


def vector_ranking(store: Store, qvec: np.ndarray, k: int, folder: str | None) -> list[int]:
    sql = (
        "select rowid from chunks_vec where embedding match vec_quantize_int8(?, 'unit') "
        "and k = ?"
    )
    params: list = [vec_bytes(qvec), k]
    if folder is not None:
        sql += " and folder = ?"
        params.append(folder)
    return [r[0] for r in store.db.execute(sql + " order by distance", params)]


def keyword_ranking(store: Store, query: str, k: int, folder: str | None) -> list[int]:
    fts = fts_query(query)
    if not fts:
        return []
    if folder is None:
        sql = (
            "select rowid from chunks_fts where chunks_fts match ? "
            "order by bm25(chunks_fts) limit ?"
        )
        params: tuple = (fts, k)
    else:
        sql = (
            "select chunks_fts.rowid from chunks_fts "
            "join chunks c on c.id = chunks_fts.rowid join files f on f.id = c.file_id "
            "where chunks_fts match ? and f.folder = ? "
            "order by bm25(chunks_fts) limit ?"
        )
        params = (fts, folder, k)
    try:
        return [r[0] for r in store.db.execute(sql, params)]
    except Exception:
        return []  # a query FTS5 still rejects should not kill the search


def search_chunks(
    store: Store,
    qvec: np.ndarray,
    query: str,
    folder: str | None = None,
    pool: int = 150,
    keyword_weight: float | None = None,
) -> list[Hit]:
    vec_ids = vector_ranking(store, qvec, pool, folder)
    kw_ids = keyword_ranking(store, query, pool, folder)
    if keyword_weight is None:
        # A quoted phrase means "I want these exact words".
        keyword_weight = 2.0 if '"' in query else 1.0
    fused = rrf([(vec_ids, 1.0), (kw_ids, keyword_weight)])
    ordered = sorted(fused, key=fused.get, reverse=True)

    hits = []
    for cid in ordered:
        row = store.db.execute(
            "select c.text, c.page_start, c.page_end, f.path, f.filename, f.folder "
            "from chunks c join files f on f.id = c.file_id where c.id = ?",
            (cid,),
        ).fetchone()
        if row:
            text, p1, p2, path, filename, folder_ = row
            hits.append(Hit(cid, fused[cid], text, path, filename, folder_, p1, p2))
    return hits


def group_by_file(hits: list[Hit], max_files: int = 25, per_file: int = 3) -> list[FileResult]:
    """Collapse passage hits into one result per document, best first.

    A document's score is its best passage's score plus a small bonus for
    each further matching passage, so a file that keeps coming up edges out
    one with a single lucky match.
    """
    files: dict[str, FileResult] = {}
    for h in hits:
        fr = files.get(h.path)
        if fr is None:
            fr = files[h.path] = FileResult(h.path, h.filename, h.folder, 0.0)
        fr.hits.append(h)
    for fr in files.values():
        scores = sorted((h.score for h in fr.hits), reverse=True)
        fr.score = scores[0] + 0.1 * sum(scores[1:4])
        fr.hits = sorted(fr.hits, key=lambda h: -h.score)[:per_file]
    return sorted(files.values(), key=lambda f: -f.score)[:max_files]


def search(
    store: Store,
    embedder,
    query: str,
    folder: str | None = None,
    max_files: int = 25,
) -> list[FileResult]:
    qvec = embedder.embed_queries([query.replace('"', "")])[0]
    return group_by_file(search_chunks(store, qvec, query, folder), max_files)
