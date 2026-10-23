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
import signal
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

# A Finder-launched app gets a minimal PATH, so look where Homebrew puts it.
_EXTRA_BIN = ("/opt/homebrew/bin", "/usr/local/bin")


class OcrUnavailable(Exception):
    pass


class OcrFailed(Exception):
    pass


class Stopped(Exception):
    """Indexing was asked to stop part-way through a file. The file is left
    unindexed (not marked as failed) and is picked up again next time."""


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
    should_stop: Callable[[], bool] | None = None,
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
    # A scanned bundle of a few hundred pages takes many minutes, so poll
    # rather than wait: a stop request (the app taking over, or Stop) ends
    # it within a second. ocrmypdf runs tesseract in child processes, so
    # it gets its own process group and the whole group is killed.
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        start_new_session=True,
    )
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                stdout, stderr = proc.communicate(timeout=0.5)
                break
            except subprocess.TimeoutExpired:
                pass
            if should_stop and should_stop():
                raise Stopped()
            if time.monotonic() > deadline:
                raise OcrFailed(f"timed out after {timeout}s")
    except BaseException:
        _kill_group(proc)
        tmp.unlink(missing_ok=True)
        raise

    if proc.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
        tmp.unlink(missing_ok=True)
        detail = (stderr or stdout or "").strip().splitlines()
        raise OcrFailed(
            f"ocrmypdf exit {proc.returncode}: {detail[-1] if detail else 'no output'}"
        )

    os.replace(tmp, out)
    return out


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    proc.wait()
