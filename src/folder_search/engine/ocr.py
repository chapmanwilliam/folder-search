"""Optional OCR for scanned PDFs, via ocrmypdf.

Used only when ocrmypdf is installed (`brew install ocrmypdf`). Without it,
a scanned PDF is listed as "no searchable text" rather than indexed empty.

The indexed folder is never written to. Each OCR'd copy goes in a cache in
the app's data folder, named by the source file's content hash: a changed
file re-OCRs, and rebuilding an index reuses the slow OCR work.
`--skip-text` means only pages lacking a text layer are processed.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

# A Finder-launched app gets a minimal PATH, so look where Homebrew puts it.
_EXTRA_BIN = ("/opt/homebrew/bin", "/usr/local/bin")


class OcrUnavailable(Exception):
    pass


class OcrFailed(Exception):
    pass


def ocrmypdf_path() -> str | None:
    found = shutil.which("ocrmypdf")
    if found:
        return found
    for d in _EXTRA_BIN:
        p = Path(d) / "ocrmypdf"
        if p.exists():
            return str(p)
    return None


def ocr_available() -> bool:
    return ocrmypdf_path() is not None


def ocr_pdf(
    src: Path,
    sha256: str,
    cache_dir: Path,
    timeout: int = 900,
    language: str = "eng",
) -> Path:
    """Return a path to an OCR'd copy of src, building it if not cached."""
    exe = ocrmypdf_path()
    if not exe:
        raise OcrUnavailable("ocrmypdf is not installed")
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    out = cache_dir / f"{sha256}.pdf"
    if out.exists() and out.stat().st_size > 0:
        return out

    # Write to a temp name and rename, so an interrupted run never leaves a
    # truncated PDF that would later look like a valid cache hit.
    tmp = out.with_suffix(".partial.pdf")
    cmd = [
        exe,
        "--skip-text",       # only pages without a text layer
        "--rotate-pages",    # sideways scans otherwise OCR to noise
        "--output-type", "pdf",
        "--optimize", "0",   # we want the text layer, not a smaller file
        "--language", language,
        "--quiet",
        str(src),
        str(tmp),
    ]
    env = dict(os.environ)
    env["PATH"] = os.pathsep.join([env.get("PATH", ""), *_EXTRA_BIN])
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False, env=env
        )
    except subprocess.TimeoutExpired:
        tmp.unlink(missing_ok=True)
        raise OcrFailed(f"timed out after {timeout}s") from None

    if proc.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
        tmp.unlink(missing_ok=True)
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        raise OcrFailed(
            f"ocrmypdf exit {proc.returncode}: {detail[-1] if detail else 'no output'}"
        )

    os.replace(tmp, out)
    return out
