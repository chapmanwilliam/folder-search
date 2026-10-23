import numpy as np

from folder_search.engine.chunk import Chunk
from folder_search.engine.search import fts_query, group_by_file, rrf, search
from folder_search.engine.store import Store


def add(store, embedder, path, folder, texts):
    chunks = [Chunk(t, i + 1, i + 1) for i, t in enumerate(texts)]
    vecs = embedder.embed_documents(texts)
    fid = store.add_file(path, folder, 1.0, 10, "sha-" + path, len(texts), 0, chunks, vecs)
    store.commit()
    return fid


def test_roundtrip_and_remove(tmp_path, embedder):
    store = Store(tmp_path / "i.db")
    fid = add(store, embedder, "A/a.pdf", "A", ["employer vicariously liable", "deposit"])
    assert store.stats()["chunks"] == 2
    store.remove_file(fid)
    store.commit()
    assert store.stats()["files"] == 0
    assert store.db.execute("select count(*) from chunks_vec").fetchone()[0] == 0
    assert store.db.execute(
        "select count(*) from chunks_fts where chunks_fts match 'vicariously'"
    ).fetchone()[0] == 0


def test_search_finds_and_filters_by_subfolder(tmp_path, embedder):
    store = Store(tmp_path / "i.db")
    add(store, embedder, "Smith/advice.pdf", "Smith", ["date of knowledge for limitation purposes"])
    add(store, embedder, "Jones/letter.pdf", "Jones", ["date of knowledge for limitation purposes again"])
    add(store, embedder, "Jones/other.pdf", "Jones", ["tenancy deposit scheme"])
    results = search(store, embedder, "limitation date of knowledge")
    assert {r.path for r in results[:2]} == {"Smith/advice.pdf", "Jones/letter.pdf"}
    only = search(store, embedder, "limitation date of knowledge", folder="Smith")
    assert [r.path for r in only] == ["Smith/advice.pdf"]
    assert only[0].hits[0].page_start == 1


def test_stemming_matches_word_forms(tmp_path, embedder):
    store = Store(tmp_path / "i.db")
    add(store, embedder, "a.pdf", "", ["the surgeon was negligent"])
    rows = store.db.execute(
        "select count(*) from chunks_fts where chunks_fts match ?", (fts_query("negligence"),)
    ).fetchone()[0]
    assert rows == 1  # porter: negligent/negligence share a stem
    rows = store.db.execute(
        "select count(*) from chunks_fts where chunks_fts match ?", (fts_query("negligently"),)
    ).fetchone()[0]
    assert rows == 1


def test_fts_query_escapes_syntax_and_keeps_phrases():
    q = fts_query('vicarious NEAR liability "date of knowledge" (grouped)')
    assert '"date of knowledge"' in q
    assert '"NEAR"' in q
    assert "(" not in q.replace('"', "")
    assert fts_query("  ") == ""


def test_rrf_rewards_agreement():
    s = rrf([([1, 2, 3], 1.0), ([3, 4], 1.0)])
    assert s[3] > s[2]


def test_group_by_file_one_result_per_document():
    from folder_search.engine.search import Hit

    hits = [
        Hit(1, 0.03, "x", "a.pdf", "a.pdf", "", 1, 1),
        Hit(2, 0.02, "y", "b.pdf", "b.pdf", "", 1, 1),
        Hit(3, 0.02, "z", "a.pdf", "a.pdf", "", 2, 2),
    ]
    out = group_by_file(hits)
    assert [f.path for f in out] == ["a.pdf", "b.pdf"]
    assert [h.chunk_id for h in out[0].hits] == [1, 3]
