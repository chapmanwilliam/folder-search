"""Stopping part-way through a slow file: OCR of a big scan, or a long bundle."""

import os
import time

import pytest

from folder_search import library
from folder_search.engine import indexer as ix
from folder_search.engine import ocr


def test_ocr_stops_promptly_and_kills_its_children(tmp_path, monkeypatch):
    # A stand-in ocrmypdf that would run for a minute, with a child process,
    # as the real one runs tesseract.
    fake = tmp_path / "ocrmypdf"
    pidfile = tmp_path / "child.pid"
    fake.write_text(f"#!/bin/sh\nsleep 60 &\necho $! > {pidfile}\nwait\n")
    fake.chmod(0o755)
    monkeypatch.setattr(ocr, "ocrmypdf_path", lambda: str(fake))
    src = tmp_path / "scan.pdf"
    src.write_bytes(b"%PDF-1.4")

    asked = time.monotonic() + 1.0
    start = time.monotonic()
    with pytest.raises(ocr.Stopped):
        ocr.ocr_pdf(src, "abc", tmp_path / "cache", should_stop=lambda: time.monotonic() > asked)
    assert time.monotonic() - start < 4
    child = int(pidfile.read_text())
    time.sleep(0.2)
    with pytest.raises(ProcessLookupError):
        os.kill(child, 0)
    assert not list((tmp_path / "cache").glob("*.pdf"))


def test_stop_during_a_long_document_leaves_it_for_next_time(tmp_path, embedder, monkeypatch):
    root = tmp_path / "cases"
    root.mkdir()
    (root / "bundle.txt").write_text("\n\n".join(f"Paragraph {i} about limitation." * 40 for i in range(300)))
    monkeypatch.setattr(ix, "EMBED_BATCH", 8)

    calls = {"n": 0}
    real = embedder.embed_documents

    def counting(texts):
        calls["n"] += 1
        return real(texts)

    monkeypatch.setattr(embedder, "embed_documents", counting)
    store = library.open_store(root)
    p = ix.index_folder(root, store, embedder, should_stop=lambda: calls["n"] >= 2)
    assert (p.phase, p.added, p.failed) == ("stopped", 0, 0)
    assert store.known_files() == {}
    assert store.db.execute("select count(*) from failures").fetchone()[0] == 0

    p = ix.index_folder(root, store, embedder)
    assert (p.phase, p.added) == ("done", 1)
