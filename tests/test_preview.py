import pymupdf

from folder_search.preview import _lines, render_pdf_page


def test_short_header_lines_are_not_highlight_targets():
    assert _lines("Lord Carswell\nA v Hoare (HL(E))") == []


def test_broken_ligatures_split_into_searchable_pieces():
    out = _lines("the actions in question were de\x01ned as those for negligence and nuisance")
    assert out == ["the actions in question were de", "ned as those for negligence and nuisance"]


def test_render_highlights_passage_lines(tmp_path):
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Running header text that is long enough here")
    page.insert_text((72, 600), "The limitation period runs from the date of knowledge.")
    p = tmp_path / "x.pdf"
    doc.save(p)
    out = render_pdf_page(p, 1, "The limitation period runs from the date of knowledge.")
    assert out["kind"] == "pdf" and out["pages"] == 1
    assert len(out["boxes"]) == 1
    assert out["scroll_to"] > 0.5  # lower half of the page, not the header
