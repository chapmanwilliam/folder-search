"""Command-line interface: `fsearch scan|index|search|list|remove`.

The desktop app does the same things; this is for scripting and for
checking the engine on its own.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from . import library


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def cmd_scan(args) -> int:
    from .engine.cloudfiles import scan

    t = time.time()
    r = scan(Path(args.folder).expanduser())
    print(f"{r.local_files:,} documents on disk ({human_bytes(r.local_bytes)})")
    print(f"{r.online_files:,} cloud-only ({human_bytes(r.online_bytes)} to download)")
    print("by type: " + ", ".join(f"{k} {v:,}" for k, v in sorted(r.by_type.items(), key=lambda kv: -kv[1])))
    print(f"[{time.time() - t:.1f}s]")
    return 0


def cmd_index(args) -> int:
    from .engine.indexer import index_folder
    from .engine.model import Embedder

    from .background import IndexLock

    lock = IndexLock()
    if not lock.acquire("cli"):
        print("Folder Search is already indexing (the app is open, or a background update is running).",
              file=sys.stderr)
        return 1
    folder = Path(args.folder).expanduser().resolve()
    entry = library.add(folder, include_online=args.include_online)
    store = library.open_store(folder)
    embedder = Embedder()
    last = [0.0]

    def show(p):
        now = time.time()
        if p.phase == "indexing" and now - last[0] < 1 and p.done < p.total:
            return
        last[0] = now
        if p.phase == "indexing":
            sys.stdout.write(f"\r  {p.done:,}/{p.total:,} files  {p.current[-60:]:<60}")
            sys.stdout.flush()

    try:
        p = index_folder(folder, store, embedder, include_online=entry.include_online, on_progress=show)
    except KeyboardInterrupt:
        print("\nstopped; run again to carry on where it left off")
        return 130
    finally:
        store.close()
    print(
        f"\n{p.phase}: {p.added} added, {p.updated} updated, {p.unchanged} unchanged, "
        f"{p.removed} removed, {p.failed} failed, {p.skipped_online} cloud-only skipped "
        f"[{time.time() - p.started:.0f}s]"
    )
    return 0


def cmd_search(args) -> int:
    from .engine.model import Embedder
    from .engine.search import search

    entries = library.load()
    if args.folder:
        e = library.get(args.folder)
        if not e:
            print(f"not indexed: {args.folder}", file=sys.stderr)
            return 1
        entries = [e]
    if not entries:
        print("nothing indexed yet; run `fsearch index <folder>`", file=sys.stderr)
        return 1
    embedder = Embedder()
    for e in entries:
        store = library.open_store(e.path)
        results = search(store, embedder, args.query, folder=args.sub, max_files=args.n)
        store.close()
        print(f"== {e.name}")
        for i, fr in enumerate(results, 1):
            print(f"{i:2}. {fr.path}")
            for h in fr.hits[: args.passages]:
                page = f"p{h.page_start}" + (f"-{h.page_end}" if h.page_end != h.page_start else "") if h.page_start else ""
                snippet = " ".join(h.text.split())[:220]
                print(f"      {page:>8}  {snippet}")
    return 0


def cmd_list(args) -> int:
    for e in library.load():
        store = library.open_store(e.path)
        s = store.stats()
        store.close()
        print(
            f"{e.path}\n    {s['files']:,} files, {s['chunks']:,} passages, "
            f"{s['failures']} failed, {s['skipped']:,} cloud-only skipped, last indexed {s['last_indexed']}"
        )
    return 0


def cmd_failures(args) -> int:
    store = library.open_store(args.folder)
    for path, reason in store.failures():
        print(f"{path}\n    {reason}")
    store.close()
    return 0


def cmd_background(args) -> int:
    from . import background as bg

    if args.action == "enable":
        bg.enable()
        print(f"on: updates every {bg.INTERVAL_SECONDS // 60} min and at login; log at {bg.log_path()}")
    elif args.action == "disable":
        bg.disable()
        print("off")
    elif args.action == "run":
        import logging

        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
        bg.run_once()
    else:
        s = bg.read_status()
        print(f"{'on' if bg.is_enabled() else 'off'}; last run {s.get('finished') or s.get('started') or 'never'}")
        for path, r in s.get("folders", {}).items():
            print(f"  {path}: {r}")
        for path in s.get("needs_access", []):
            print(f"  {path}: NEEDS FULL DISK ACCESS for {sys.executable}")
    return 0


def cmd_remove(args) -> int:
    library.remove(args.folder)
    print("removed")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="fsearch", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="count documents on disk and cloud-only")
    s.add_argument("folder")
    s.set_defaults(fn=cmd_scan)

    s = sub.add_parser("index", help="index a folder (incremental)")
    s.add_argument("folder")
    s.add_argument("--include-online", action="store_true", help="download and index cloud-only files")
    s.set_defaults(fn=cmd_index)

    s = sub.add_parser("search", help="search indexed folders")
    s.add_argument("query")
    s.add_argument("--folder", help="only this indexed folder")
    s.add_argument("--sub", help="only this top-level subfolder")
    s.add_argument("-n", type=int, default=10, help="documents to show")
    s.add_argument("--passages", type=int, default=2)
    s.set_defaults(fn=cmd_search)

    s = sub.add_parser("list", help="list indexed folders")
    s.set_defaults(fn=cmd_list)

    s = sub.add_parser("failures", help="files that could not be indexed")
    s.add_argument("folder")
    s.set_defaults(fn=cmd_failures)

    s = sub.add_parser("background", help="updates while the app is closed")
    s.add_argument("action", choices=["status", "enable", "disable", "run"], nargs="?", default="status")
    s.set_defaults(fn=cmd_background)

    s = sub.add_parser("remove", help="forget a folder and delete its index")
    s.add_argument("folder")
    s.set_defaults(fn=cmd_remove)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
