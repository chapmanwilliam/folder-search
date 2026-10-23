import os
from pathlib import Path

from folder_search import library
from folder_search.engine import indexer as ix
from folder_search.engine.cloudfiles import Entry, scan, walk
from folder_search.engine.search import search


def make_folder(root: Path) -> Path:
    (root / "Smith v Jones").mkdir(parents=True)
    (root / "Smith v Jones" / "advice.md").write_text("Limitation runs from the date of knowledge.")
    (root / "Smith v Jones" / "~$advice.docx").write_text("lock file")
    (root / ".hidden").mkdir()
    (root / ".hidden" / "x.txt").write_text("hidden")
    (root / "notes.txt").write_text("Quantum of general damages for PSLA.")
    (root / "photo.jpg").write_bytes(b"\xff\xd8")
    return root


def test_walk_skips_hidden_lock_and_unsupported(tmp_path):
    root = make_folder(tmp_path / "cases")
    names = sorted(e.path.name for e in walk(root))
    assert names == ["advice.md", "notes.txt"]


def test_index_is_incremental_and_tracks_deletes(tmp_path, embedder):
    root = make_folder(tmp_path / "cases")
    store = library.open_store(root)
    p = ix.index_folder(root, store, embedder)
    assert (p.phase, p.added, p.failed) == ("done", 2, 0)
    assert store.subfolders() == [("Smith v Jones", 1)]

    p = ix.index_folder(root, store, embedder)
    assert (p.added, p.updated, p.unchanged, p.total) == (0, 0, 2, 0)

    f = root / "notes.txt"
    f.write_text("Quantum of special damages.")
    os.utime(f, (1, 1))
    (root / "Smith v Jones" / "advice.md").unlink()
    p = ix.index_folder(root, store, embedder)
    assert (p.updated, p.removed) == (1, 1)
    assert store.stats()["files"] == 1


def test_touched_but_unchanged_file_is_not_reembedded(tmp_path, embedder):
    root = make_folder(tmp_path / "cases")
    store = library.open_store(root)
    ix.index_folder(root, store, embedder)
    os.utime(root / "notes.txt", (5, 5))
    p = ix.index_folder(root, store, embedder)
    assert (p.updated, p.unchanged) == (0, 2)


def test_online_only_files_are_skipped_not_read(tmp_path, embedder, monkeypatch):
    root = make_folder(tmp_path / "cases")
    real_walk = ix.walk

    def fake_walk(folder):
        for e in real_walk(folder):
            if e.path.name == "notes.txt":
                e = Entry(e.path, 123456, e.mtime, online_only=True)
            yield e

    monkeypatch.setattr(ix, "walk", fake_walk)
    opened = []
    real_sha = ix.file_sha256
    monkeypatch.setattr(ix, "file_sha256", lambda p: opened.append(p.name) or real_sha(p))

    store = library.open_store(root)
    p = ix.index_folder(root, store, embedder, include_online=False)
    assert p.skipped_online == 1 and p.added == 1
    assert "notes.txt" not in opened
    assert store.skipped() == [("notes.txt", 123456)]

    p = ix.index_folder(root, store, embedder, include_online=True)
    assert p.added == 1 and store.skipped() == []


def test_stop_resumes_where_it_left_off(tmp_path, embedder):
    root = make_folder(tmp_path / "cases")
    store = library.open_store(root)
    calls = {"n": 0}

    def stop_after_one():
        calls["n"] += 1
        return calls["n"] > 1

    p = ix.index_folder(root, store, embedder, should_stop=stop_after_one)
    assert p.phase == "stopped" and p.done == 1
    p = ix.index_folder(root, store, embedder)
    assert (p.added, p.unchanged) == (1, 1)


def test_scan_counts(tmp_path):
    root = make_folder(tmp_path / "cases")
    r = scan(root)
    assert (r.local_files, r.online_files) == (2, 0)
    assert r.by_type == {".md": 1, ".txt": 1}


def test_library_end_to_end(tmp_path, embedder):
    root = make_folder(tmp_path / "cases")
    library.add(root)
    store = library.open_store(root)
    ix.index_folder(root, store, embedder)
    res = search(store, embedder, "date of knowledge limitation")
    assert res[0].path == "Smith v Jones/advice.md"
    store.close()
    library.remove(root)
    assert library.load() == []


def test_missing_or_emptied_folder_keeps_the_index(tmp_path, embedder):
    import pytest

    root = make_folder(tmp_path / "cases")
    store = library.open_store(root)
    ix.index_folder(root, store, embedder)
    for f in list(walk(root)):
        f.path.unlink()
    with pytest.raises(ix.FolderMissing):
        ix.index_folder(root, store, embedder)
    assert store.stats()["files"] == 2
    with pytest.raises(ix.FolderMissing):
        ix.index_folder(tmp_path / "gone", store, embedder)
