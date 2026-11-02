# -*- coding: utf-8 -*-
"""Regression guards for the 2026-10-29 responsiveness pass.

These pin the four things that made the window feel heavy, each of which is
easy to undo by accident:

  1. a burst of search rows renders the page ONCE, not once per row;
  2. one cover URL is fetched once even when several rows ask for it, and a
     decoded cover is reused instead of re-downloaded on every page turn;
  3. ticking an export checkbox no longer walks every export tree on disk;
  4. one malformed worker event cannot swallow the "done" behind it.

Timing assertions are deliberately loose: CI machines are not this one, and a
flaky perf test is worse than no perf test.
"""
import importlib.util
import json
import queue
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "weread"))
SCRIPT = Path(__file__).resolve().parent.parent / "weread" / "desktop_app.py"
spec = importlib.util.spec_from_file_location("desktop_app", SCRIPT)
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)

_TMP = tempfile.TemporaryDirectory(prefix="weread-perf-")
app.LOG_DIR = Path(_TMP.name) / "logs"
app.OUTPUT = Path(_TMP.name) / "exports"
app.weread_login.CREDENTIAL_STATE = Path(_TMP.name) / "session-cookies.json"


def _make_gui():
    import customtkinter as ctk
    root = ctk.CTk()
    root.geometry("1120x680")
    instance = app.DesktopApp(root)
    root.update()
    instance.current_action = "search"
    return root, instance


def _row(index: int) -> str:
    return ("%d\t书名%d\t作者%d\thttps://example.invalid/%d.jpg\t885\t%d\t分类"
            % (700000 + index, index, index, index, 10 + index))


def test_a_burst_of_results_renders_the_page_once():
    root, instance = _make_gui()
    try:
        calls = []
        original = instance._render_page
        instance._render_page = lambda: (calls.append(1), original())[1]
        instance._request_card_cover = lambda *args, **kwargs: None
        events = queue.Queue()
        for index in range(15):
            events.put(("line", _row(index)))
        instance.events = events
        instance._drain_events()
        assert len(calls) == 1, (
            "15 rows must coalesce into one page render, got %d" % len(calls))
        assert len(instance.card_widgets) == 15, len(instance.card_widgets)
        assert instance.events.qsize() == 0
    finally:
        root.destroy()


def test_one_cover_url_is_fetched_once_for_many_rows():
    root, instance = _make_gui()
    try:
        fetched = []
        instance._fetch_cover = lambda url: fetched.append(url)
        for book_id in ("a", "b", "c", "d", "e"):
            instance._request_card_cover(book_id, "https://example.invalid/same.jpg")
        deadline = time.time() + 1
        while not fetched and time.time() < deadline:
            time.sleep(0.01)
        assert fetched == ["https://example.invalid/same.jpg"], fetched
        waiters = instance._cover_waiters["https://example.invalid/same.jpg"]
        assert len(waiters) == 5, waiters
    finally:
        root.destroy()


def test_a_decoded_cover_is_reused_without_another_fetch_or_thread():
    root, instance = _make_gui()
    try:
        fetched = []
        applied = []
        instance._fetch_cover = lambda url: fetched.append(url)
        instance._apply_card_cover = lambda book_id, image: applied.append(
            (book_id, image))
        image = object()
        instance._cover_images[("https://example.invalid/c.jpg",
                                instance.COVER_W, instance.COVER_H)] = image
        instance._request_card_cover("book", "https://example.invalid/c.jpg")
        assert fetched == [], "a cached cover must not hit the network again"
        assert applied == [("book", image)], applied
    finally:
        root.destroy()


def test_ticking_an_export_checkbox_does_not_rescan_the_disk():
    root, instance = _make_gui()
    try:
        instance._scan_exports = lambda: (_ for _ in ()).throw(
            AssertionError("checkbox must not rescan exports"))
        instance._local_rows_snapshot = [
            ("成品书-111", 1.5, None, "done", Path(_TMP.name) / "成品书-111"),
        ]
        instance._local_checks["成品书-111"] = app.tk.BooleanVar(value=True)

        class Button:
            def __init__(self):
                self.state = None
                self.text = ""

            def configure(self, **kwargs):
                self.state = kwargs.get("state", self.state)
                self.text = kwargs.get("text", self.text)

        instance.local_delete_button = Button()
        instance._on_local_check("成品书-111")
        assert instance.local_delete_button.state == "normal"
        rows = instance._selected_local_rows()
        assert [row[0] for row in rows] == ["成品书-111"], rows
    finally:
        root.destroy()


def test_one_bad_event_does_not_swallow_the_done_behind_it():
    root, instance = _make_gui()
    try:
        handled = []

        def boom(_value):
            raise ValueError("synthetic malformed line")

        instance._handle_line = boom
        instance._handle_done = lambda job, code: handled.append((job, code))
        instance.events = queue.Queue()
        instance.events.put(("line", "first bad line"))
        instance.events.put(("line", "second bad line"))
        instance.events.put(("done", (42, 0)))
        instance._drain_events()
        assert handled == [(42, 0)], handled
    finally:
        root.destroy()


def test_history_reader_only_touches_the_log_tail():
    with tempfile.TemporaryDirectory() as temp:
        log = Path(temp) / "desktop-1.log"
        filler = ("x" * 200 + "\n") * 5000          # ~1 MB of old noise
        log.write_text(filler + "DONE C:/exports/某书/xxx.epub\n", encoding="utf-8")
        text = app.DesktopApp._read_log_tail(log, limit=64 * 1024)
        assert "DONE C:/exports/" in text
        assert len(text.encode("utf-8")) <= 70 * 1024


def test_the_service_client_terminates_without_waiting_for_a_request_lock():
    class FakeProcess:
        def __init__(self):
            self.killed = 0
            self.waited = 0

        def kill(self):
            self.killed += 1

        def wait(self, timeout=None):
            self.waited += 1
            return 0

    client = app._ServiceClient()
    process = FakeProcess()
    client._process = process
    started = time.perf_counter()
    client.terminate()
    elapsed = time.perf_counter() - started
    assert process.killed == 1
    assert client._process is None
    assert process.waited == 0, "closing must not block on process.wait()"
    assert elapsed < 0.5


def test_a_spawn_in_flight_is_registered_before_it_is_adopted():
    root, instance = _make_gui()
    try:
        class FakeProcess:
            def __init__(self):
                self.killed = 0

            def kill(self):
                self.killed += 1

            def wait(self, timeout=None):
                return 0

        process = FakeProcess()
        instance._track_worker(process)
        instance._terminate_all_workers()
        assert process.killed == 1
        assert not instance._live_workers
    finally:
        root.destroy()


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("PASS", name)
