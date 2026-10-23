from folder_search.engine.chunk import chunk_pages
from folder_search.engine.extract import Page


def test_small_pages_merge_with_page_range():
    pages = [Page(1, "alpha " * 50), Page(2, "beta " * 50), Page(3, "gamma " * 50)]
    chunks = chunk_pages(pages, chunk_tokens=400, overlap_tokens=60)
    assert len(chunks) == 1
    assert chunks[0].page_start == 1
    assert chunks[0].page_end == 3


def test_big_page_splits_within_page():
    paras = "\n\n".join(f"paragraph {i} " + "word " * 80 for i in range(10))
    chunks = chunk_pages([Page(5, paras)], chunk_tokens=400, overlap_tokens=60)
    assert len(chunks) > 1
    assert all(c.page_start == 5 and c.page_end == 5 for c in chunks)
    assert all(len(c.text) <= 400 * 4 + 400 for c in chunks)


def test_never_loses_text():
    pages = [Page(1, "needle-in-haystack unique marker"), Page(2, "word " * 1000)]
    chunks = chunk_pages(pages, chunk_tokens=300, overlap_tokens=40)
    joined = " ".join(c.text for c in chunks)
    assert "needle-in-haystack unique marker" in joined


def test_monster_paragraph_hard_split():
    text = "x" * 10000  # no paragraph breaks at all
    chunks = chunk_pages([Page(1, text)], chunk_tokens=400, overlap_tokens=60)
    assert len(chunks) > 1
    assert all(len(c.text) <= 400 * 4 + 400 for c in chunks)
