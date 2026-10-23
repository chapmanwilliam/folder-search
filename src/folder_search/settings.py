"""Fixed settings and where the app keeps its data.

There is no config file: the defaults below are the ones chambers-search
settled on after indexing ~1,000 legal PDFs, and a user picking a folder
should not have to tune chunk sizes. Set FOLDER_SEARCH_HOME to keep the
data somewhere other than Application Support (the tests do this).
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

MODEL_NAME = "google/embeddinggemma-2"
# Full Matryoshka width. Vectors are stored as int8, so 768 dims costs
# 768 bytes per chunk rather than 3 KB.
DIMENSIONS = 768


@dataclass(frozen=True)
class IndexSettings:
    chunk_tokens: int = 400
    chunk_overlap: int = 60
    ocr: bool = True  # used only if ocrmypdf is installed
    ocr_timeout: int = 3600  # big scanned bundles; OCR stops at once if asked
    ocr_language: str = "eng"


def app_home() -> Path:
    env = os.environ.get("FOLDER_SEARCH_HOME")
    if env:
        return Path(env).expanduser()
    return Path.home() / "Library" / "Application Support" / "Folder Search"


def index_path(folder: Path) -> Path:
    """One SQLite index per folder, named by a hash of its absolute path."""
    key = hashlib.sha256(str(Path(folder).resolve()).encode()).hexdigest()[:16]
    return app_home() / "indexes" / f"{key}.db"


def ocr_cache_dir() -> Path:
    return app_home() / "ocr-cache"
