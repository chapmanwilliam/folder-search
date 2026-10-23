"""Walk a folder without downloading anything, and spot cloud-only files.

Dropbox, iCloud Drive, OneDrive and Google Drive on macOS all use the File
Provider system: a file can be a "dataless" placeholder whose contents are
still in the cloud. A placeholder reports its full size and opens like a
normal file, so nothing warns you; the first read silently downloads it.

`os.stat` does not download (verified on real Dropbox placeholders, which
report st_flags 0x40000060), so checking is free. On top of that, the kernel
can be told to refuse downloads for this whole process, which turns a missed
check into a read error rather than a surprise multi-gigabyte download.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from .extract import SUPPORTED

# <sys/stat.h>: file is a placeholder whose contents live in the cloud.
SF_DATALESS = 0x40000000

# setiopolicy_np, from <sys/resource.h>.
_IOPOL_TYPE_VFS_MATERIALIZE_DATALESS_FILES = 3
_IOPOL_SCOPE_PROCESS = 0
_IOPOL_DEFAULT = 0
_IOPOL_OFF = 1
_IOPOL_ON = 2

# Skipped wherever they appear: version control, dependency folders, and
# Office lock files ("~$Brief.docx").
_SKIP_DIRS = {"node_modules", "__pycache__"}


def _set_policy(value: int) -> bool:
    try:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        return (
            libc.setiopolicy_np(
                _IOPOL_TYPE_VFS_MATERIALIZE_DATALESS_FILES, _IOPOL_SCOPE_PROCESS, value
            )
            == 0
        )
    except (OSError, AttributeError, ValueError):
        return False


def allow_downloads(allow: bool) -> bool:
    """Let this process download cloud-only files, or forbid it outright.

    Allowing is also needed for a different reason: a process that is not
    the frontmost app (a launchd job, say) reading a placeholder otherwise
    fails with EDEADLK, "Resource deadlock avoided".
    """
    return _set_policy(_IOPOL_ON if allow else _IOPOL_OFF)


def reset_download_policy() -> bool:
    return _set_policy(_IOPOL_DEFAULT)


def is_dataless(st: os.stat_result) -> bool:
    return bool(getattr(st, "st_flags", 0) & SF_DATALESS)


@dataclass
class Entry:
    path: Path
    size: int
    mtime: float
    online_only: bool


def walk(folder: Path) -> Iterator[Entry]:
    """Every supported document under folder, in a stable order.

    Hidden files and folders (names starting with ".") are skipped, as are
    Office lock files. Symlinks are not followed, so a link cannot pull in
    a folder outside the one being indexed or loop forever.
    """
    stack = [Path(folder)]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                entries = sorted(it, key=lambda e: e.name.lower())
        except (PermissionError, FileNotFoundError, NotADirectoryError):
            continue
        subdirs = []
        for e in entries:
            name = e.name
            if name.startswith(".") or name.startswith("~$"):
                continue
            if e.is_dir(follow_symlinks=False):
                if name not in _SKIP_DIRS:
                    subdirs.append(Path(e.path))
                continue
            if not e.is_file(follow_symlinks=False):
                continue
            if Path(name).suffix.lower() not in SUPPORTED:
                continue
            try:
                st = e.stat(follow_symlinks=False)
            except OSError:
                continue
            yield Entry(Path(e.path), st.st_size, st.st_mtime, is_dataless(st))
        stack.extend(reversed(subdirs))


@dataclass
class ScanResult:
    local_files: int = 0
    local_bytes: int = 0
    online_files: int = 0
    online_bytes: int = 0
    by_type: dict[str, int] = field(default_factory=dict)

    @property
    def total_files(self) -> int:
        return self.local_files + self.online_files

    def as_dict(self) -> dict:
        d = dict(vars(self))
        d["total_files"] = self.total_files
        return d


def scan(folder: Path, progress=None) -> ScanResult:
    """Count the documents in a folder, split by on-disk and cloud-only.

    Reads only metadata, so it never downloads anything.
    """
    r = ScanResult()
    for i, e in enumerate(walk(folder), 1):
        ext = e.path.suffix.lower()
        r.by_type[ext] = r.by_type.get(ext, 0) + 1
        if e.online_only:
            r.online_files += 1
            r.online_bytes += e.size
        else:
            r.local_files += 1
            r.local_bytes += e.size
        if progress and i % 500 == 0:
            progress(i)
    return r
