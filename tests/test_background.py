import os
import plistlib
import signal
import subprocess
import sys
import threading
import time

from folder_search import background as bg
from folder_search import library


def make_folder(root, n=3):
    (root / "Case A").mkdir(parents=True)
    for i in range(n):
        (root / "Case A" / f"note{i}.md").write_text(f"note {i} on limitation")
    return root


def files_in(root):
    s = library.open_store(root)
    try:
        return s.stats()["files"]
    finally:
        s.close()


def test_run_once_indexes_and_writes_status(tmp_path, embedder):
    a = make_folder(tmp_path / "a")
    library.add(a)
    status = bg.run_once(embedder_factory=lambda: embedder)
    assert files_in(a) == 3
    assert status["folders"][str(a.resolve())]["added"] == 3
    assert status["finished"] and not status["needs_access"]
    assert bg.read_status()["finished"] == status["finished"]


def test_run_with_no_changes_never_loads_the_model(tmp_path, embedder):
    a = make_folder(tmp_path / "a")
    library.add(a)
    bg.run_once(embedder_factory=lambda: embedder)
    loads = []
    bg.run_once(embedder_factory=lambda: loads.append(1) or embedder)
    assert loads == []


def test_skips_paused_and_missing_folders(tmp_path, embedder):
    a = make_folder(tmp_path / "a")
    library.add(a)
    library.set_paused(a, True)
    status = bg.run_once(embedder_factory=lambda: embedder)
    assert status["folders"] == {} and files_in(a) == 0


def test_does_nothing_while_the_app_holds_the_lock(tmp_path, embedder):
    a = make_folder(tmp_path / "a")
    library.add(a)
    app_lock = bg.IndexLock()
    assert app_lock.acquire("app")
    try:
        bg.run_once(embedder_factory=lambda: embedder)
        assert files_in(a) == 0
    finally:
        app_lock.release()


def test_folder_that_never_answers_is_reported(tmp_path, embedder, monkeypatch):
    a = make_folder(tmp_path / "a")
    library.add(a)
    monkeypatch.setattr(bg, "can_list", lambda folder, timeout=0: False)
    status = bg.run_once(embedder_factory=lambda: embedder)
    assert status["needs_access"] == [str(a.resolve())]
    assert files_in(a) == 0


def test_can_list_times_out(tmp_path, monkeypatch):
    real = os.listdir
    monkeypatch.setattr(bg.os, "listdir", lambda p: time.sleep(5) or real(p))
    t = time.time()
    assert bg.can_list(tmp_path, timeout=0.3) is False
    assert time.time() - t < 2
    monkeypatch.setattr(bg.os, "listdir", real)
    assert bg.can_list(tmp_path, timeout=2) is True


def test_app_takes_over_from_a_background_run(tmp_path, app_home):
    """A real second process holds the lock as 'background'; take_over stops it."""
    script = (
        "import time, signal, sys\n"
        "from folder_search.background import IndexLock\n"
        "l = IndexLock(); assert l.acquire('background')\n"
        "signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))\n"
        "print('held', flush=True)\n"
        "time.sleep(60)\n"
    )
    env = dict(os.environ, FOLDER_SEARCH_HOME=str(app_home))
    proc = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True, env=env)
    try:
        assert proc.stdout.readline().strip() == "held"
        lock = bg.IndexLock()
        assert not lock.acquire("app")
        t = time.time()
        assert bg.take_over(lock, wait=10)
        assert time.time() - t < 5
        assert proc.wait(5) == 0
        lock.release()
    finally:
        if proc.poll() is None:
            proc.kill()


def test_agent_plist_runs_this_python_quietly(app_home):
    p = bg.agent_plist("/x/bin/python")
    assert p["ProgramArguments"] == ["/x/bin/python", "-m", "folder_search.background"]
    assert p["RunAtLoad"] is True and p["StartInterval"] == 1800
    assert p["ProcessType"] == "Background"
    assert p["EnvironmentVariables"]["FOLDER_SEARCH_HOME"] == str(app_home)
    plistlib.dumps(p)  # serialisable
