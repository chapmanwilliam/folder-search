import threading
import time

from folder_search import library
from folder_search.jobs import IndexQueue
from folder_search.watch import Debouncer, FolderWatcher, relevant


def make_folder(root, n=3):
    (root / "Case A").mkdir(parents=True)
    for i in range(n):
        (root / "Case A" / f"note{i}.md").write_text(f"note {i} about limitation and quantum")
    return root


def files_in(root):
    s = library.open_store(root)
    try:
        return s.stats()["files"]
    finally:
        s.close()


# -- queue ---------------------------------------------------------------


def test_queue_runs_jobs_in_order(tmp_path, embedder):
    a = make_folder(tmp_path / "a")
    b = make_folder(tmp_path / "b")
    library.add(a)
    library.add(b)
    q = IndexQueue(lambda: embedder)
    q.submit(str(a), auto=True)
    q.submit(str(b), auto=True)
    assert q.wait_idle(30)
    assert files_in(a) == 3 and files_in(b) == 3
    assert q.progress()["phase"] == "done"


def test_auto_updates_skip_paused_folders(tmp_path, embedder):
    a = make_folder(tmp_path / "a")
    library.add(a)
    library.set_paused(a, True)
    q = IndexQueue(lambda: embedder)
    q.submit(str(a), auto=True)
    assert q.wait_idle(10)
    assert files_in(a) == 0
    q.submit(str(a), auto=False)  # the user resuming clears the pause
    assert q.wait_idle(30)
    assert files_in(a) == 3 and not library.get(a).paused


class SlowEmbedder:
    """Wraps the fake embedder so a job is still running when we act."""

    def __init__(self, inner, delay=0.15):
        self.inner, self.delay = inner, delay

    def embed_documents(self, texts):
        time.sleep(self.delay)
        return self.inner.embed_documents(texts)

    def embed_queries(self, texts):
        return self.inner.embed_queries(texts)

    def release_memory(self):
        pass


def wait_for(cond, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_user_stop_pauses_folder(tmp_path, embedder):
    a = make_folder(tmp_path / "a", n=20)
    library.add(a)
    q = IndexQueue(lambda: SlowEmbedder(embedder))
    q.submit(str(a), auto=False)
    assert wait_for(lambda: (q.progress() or {}).get("done", 0) >= 1)
    q.stop_current()
    assert q.wait_idle(10)
    assert q.progress()["phase"] == "stopped"
    assert library.get(a).paused


def test_user_job_preempts_auto_job_which_then_finishes(tmp_path, embedder):
    a = make_folder(tmp_path / "a", n=20)
    b = make_folder(tmp_path / "b", n=2)
    library.add(a)
    library.add(b)
    order = []
    q = IndexQueue(lambda: SlowEmbedder(embedder), on_finished=lambda j: order.append((j["name"], j["p"]["phase"])))
    q.submit(str(a), auto=True)
    assert wait_for(lambda: (q.progress() or {}).get("done", 0) >= 1)
    q.submit(str(b), auto=False)
    assert q.wait_idle(30)
    assert order[0] == ("a", "stopped")
    assert order[1] == ("b", "done")
    assert order[2] == ("a", "done")
    assert files_in(a) == 20 and not library.get(a).paused


# -- watching ------------------------------------------------------------


def test_relevant_filters_noise():
    root = "/x/Cases"
    assert relevant("/x/Cases/A/advice.pdf", root)
    assert not relevant("/x/Cases/A/~$advice.docx", root)
    assert not relevant("/x/Cases/.dropbox.cache/a.pdf", root)
    assert not relevant("/x/Cases/A/photo.jpg", root)
    assert not relevant("/elsewhere/a.pdf", root)
    assert relevant("/x/Cases/New case", root, is_dir=True)


def test_debouncer_waits_for_quiet_and_spaces_runs():
    now = [0.0]
    d = Debouncer(quiet=60, min_gap=120, clock=lambda: now[0])
    d.note("/r")
    now[0] = 30
    d.note("/r")          # still changing
    now[0] = 80
    assert d.due() == []  # only 50 s quiet
    now[0] = 91
    assert d.due() == ["/r"]
    assert d.due() == []  # consumed
    d.note("/r")
    now[0] = 160          # quiet, but only 69 s since last run
    assert d.due() == []
    now[0] = 212
    assert d.due() == ["/r"]


def test_watcher_sees_a_new_file(tmp_path):
    root = make_folder(tmp_path / "w")
    root = str(root.resolve())
    hits = []
    seen = threading.Event()
    w = FolderWatcher(lambda r: (hits.append(r), seen.set()), quiet=0.5, min_gap=0, tick=0.2)
    w.sync([root])
    w.start()
    try:
        time.sleep(1.0)  # FSEvents needs a moment before it reports
        (tmp_path / "w" / "Case A" / "new.md").write_text("fresh document")
        assert seen.wait(15), "no update requested after a new file"
        assert hits == [root]
        w.sync([])
        assert w.watching() == []
    finally:
        w.stop()
