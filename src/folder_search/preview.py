"""Render a result for the preview pane.

For a PDF: the page as a PNG with the matched passage highlighted, drawn by
PyMuPDF, so no PDF viewer has to be bundled. For anything else: the
passage's text, which the front end shows with the match marked.
"""

from __future__ import annotations

import base64
import re
from pathlib import Path

import pymupdf


def _lines(passage: str) -> list[str]:
    """Search fragments: the passage's lines, long enough to be unambiguous.

    Short lines are left out: they are mostly running headers ("Lord
    Carswell") that repeat on every page and would highlight the wrong place.
    """
    out = []
    for line in passage.splitlines():
        # Broken ligatures ("de\x01ned") come out of some PDFs as control
        # characters that page search cannot match, so search either side.
        for piece in re.split(r"[\x00-\x08\x0b-\x1f]", line):
            piece = " ".join(piece.split())
            if len(piece) >= 30:
                out.append(piece[:120])
    return out[:60]


def render_pdf_page(path: Path, page_no: int, passage: str = "", zoom: float = 1.6) -> dict:
    with pymupdf.open(path) as doc:
        page_no = max(1, min(page_no or 1, doc.page_count))
        page = doc[page_no - 1]
        rects = []
        for frag in _lines(passage):
            rects.extend(page.search_for(frag))
        pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
        w, h = page.rect.width, page.rect.height
        # Highlight boxes as fractions of the page, drawn by the front end so
        # the colour follows the theme.
        boxes = [[r.x0 / w, r.y0 / h, r.width / w, r.height / h] for r in rects]
        return {
            "kind": "pdf",
            "page": page_no,
            "pages": doc.page_count,
            "image": "data:image/png;base64," + base64.b64encode(pix.tobytes("png")).decode(),
            "boxes": boxes,
            # Centre of the matched block, not its first box, so one stray
            # match elsewhere on the page does not decide where we scroll.
            "scroll_to": sorted(b[1] for b in boxes)[len(boxes) // 2] if boxes else 0.0,
        }


def text_preview(passage: str) -> dict:
    return {"kind": "text", "text": re.sub(r"\n{3,}", "\n\n", passage)}
