"""Chunk extracted pages into embedding-sized pieces.

Token counts are estimated at 4 characters per token, which is close enough
for sizing chunks. Chunks may span consecutive PDF pages, in which case both
page numbers are recorded (page_start, page_end). Oversized pages are split on paragraph boundaries
with overlap.
"""

from __future__ import annotations

from dataclasses import dataclass

from .extract import Page

CHARS_PER_TOKEN = 4


@dataclass
class Chunk:
    text: str
    page_start: int  # 0 = not paginated
    page_end: int


def chunk_pages(
    pages: list[Page], chunk_tokens: int = 400, overlap_tokens: int = 60
) -> list[Chunk]:
    max_chars = chunk_tokens * CHARS_PER_TOKEN
    overlap_chars = overlap_tokens * CHARS_PER_TOKEN

    # First split any oversized page into paragraph-boundary pieces, keeping
    # each piece tied to its page number.
    pieces: list[tuple[int, str]] = []
    for page in pages:
        if len(page.text) <= max_chars:
            pieces.append((page.number, page.text))
        else:
            pieces.extend(
                (page.number, part)
                for part in _split_text(page.text, max_chars, overlap_chars)
            )

    # Then pack consecutive pieces into chunks up to max_chars.
    chunks: list[Chunk] = []
    buf: list[tuple[int, str]] = []
    size = 0
    for number, text in pieces:
        if buf and size + len(text) > max_chars:
            chunks.append(_pack(buf))
            buf, size = [], 0
        buf.append((number, text))
        size += len(text)
    if buf:
        chunks.append(_pack(buf))
    return chunks


def _pack(buf: list[tuple[int, str]]) -> Chunk:
    return Chunk(
        text="\n\n".join(t for _, t in buf),
        page_start=buf[0][0],
        page_end=buf[-1][0],
    )


def _split_text(text: str, max_chars: int, overlap_chars: int) -> list[str]:
    paras = [p for p in text.split("\n\n") if p.strip()]
    if not paras:
        paras = [text]

    parts: list[str] = []
    buf: list[str] = []
    size = 0
    for para in paras:
        # A single paragraph longer than max_chars is hard-split.
        while len(para) > max_chars:
            if buf:
                parts.append("\n\n".join(buf))
                buf, size = [], 0
            parts.append(para[:max_chars])
            para = para[max_chars - overlap_chars :]
        if buf and size + len(para) > max_chars:
            parts.append("\n\n".join(buf))
            # Start the next part with the tail of this one as overlap.
            tail = parts[-1][-overlap_chars:]
            buf, size = [tail], len(tail)
        buf.append(para)
        size += len(para)
    if buf:
        parts.append("\n\n".join(buf))
    return parts
