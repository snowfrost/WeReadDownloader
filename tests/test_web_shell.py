# -*- coding: utf-8 -*-
"""Guard rails for the WebView shell (weread/web_app.py + webui assets).

The Tk frontend has its own tests; this file covers the things that broke while
building the WebView shell: a frozen bundle that cannot find index.html, a
WebView2 profile shared with a killed instance (which silently leaves an empty
JS bridge), and a CSS grid that does not match the row's child order.
"""
import importlib.util
import sys
import time
import types
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

BASE = Path(__file__).resolve().parent.parent / "weread"
sys.path.insert(0, str(BASE))

# web_app imports pywebview; the test interpreter usually has no GUI stack.
if "webview" not in sys.modules:
    try:
        import webview  # noqa: F401
    except Exception:
        stub = types.ModuleType("webview")
        stub.create_window = lambda *args, **kwargs: None
        stub.start = lambda *args, **kwargs: None
        sys.modules["webview"] = stub

spec = importlib.util.spec_from_file_location("web_app", BASE / "web_app.py")
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)


def test_the_webui_assets_ship_with_the_package():
    index = app._webui_index()
    assert index.is_file(), index
    text = index.read_text(encoding="utf-8")
    assert "app.js" in text and "style.css" in text
    for name in ("app.js", "style.css"):
        asset = index.with_name(name)
        assert asset.is_file(), asset
        assert asset.stat().st_size > 2 * 1024, (name, asset.stat().st_size)


def test_each_run_gets_its_own_webview_profile():
    """WebView2 locks its user-data folder; a killed instance must not poison
    the next launch. Every run therefore gets run-<pid>, and stale ones go."""
    base = app.core.PRIVATE / "webview"
    stale = base / "run-1"
    stale.mkdir(parents=True, exist_ok=True)
    old = time.time() - 3 * 24 * 3600
    import os
    os.utime(stale, (old, old))

    profile = app._webview_profile()
    assert profile.is_dir(), profile
    assert profile.name == "run-%d" % os.getpid(), profile
    # Two runs must never share a folder.
    assert profile != stale
    assert not stale.exists(), "a profile older than a day should be pruned"


def test_result_row_grid_matches_the_child_order():
    """check, cover, text, button: four columns, cover pinned at 40px."""
    import re
    css = (BASE / "webui" / "style.css").read_text(encoding="utf-8")
    block = css.split(".result-card {", 1)[1].split("}", 1)[0]
    match = re.search(r"grid-template-columns:\s*([^;]+);", block)
    assert match, block
    columns = " ".join(match.group(1).split())
    assert columns == "18px 40px minmax(0, 1fr) auto", columns


def test_the_bridge_helper_never_throws_synchronously():
    """An api method that is not injected yet must resolve to ok:false.

    In a frozen bundle pywebview exposes the api object a moment before the
    methods, and the old helper called the missing function directly, which
    killed boot() and left every button dead.
    """
    js = (BASE / "webui" / "app.js").read_text(encoding="utf-8")
    body = js.split("function api(name, ...args) {", 1)[1].split("\n}", 1)[0]
    assert "typeof fn !== \"function\"" in body, body
    assert "界面桥接尚未就绪" in body


def test_covers_are_attached_before_they_load():
    """A lazy, detached <img> never starts its request.

    The first WebView build set loading="lazy" and only appended the image from
    its onload handler, so every cover stayed blank forever: no request without
    being in the document, no onload without a request.
    """
    js = (BASE / "webui" / "app.js").read_text(encoding="utf-8")
    body = js.split("function fillCover(", 1)[1].split("\n}", 1)[0]
    assert 'loading = "lazy"' not in body, "lazy loading deadlocks a detached image"
    assert "box.append(img)" in body, body
    assert 'addEventListener("load"' in body, "onload must not gate the append"
    assert 'addEventListener("error"' in body, "a failed cover needs a placeholder"


def test_shelf_title_cannot_push_past_the_card():
    """1fr keeps the title's min-content width; long book names overflowed."""
    css = (BASE / "webui" / "style.css").read_text(encoding="utf-8")
    block = css.split(".shelf-card {", 1)[1].split("}", 1)[0]
    assert "minmax(0, 1fr)" in block, block
    assert "position: relative" in css.split(".result-cover {", 1)[1].split("}", 1)[0], (
        "the cover image is absolutely positioned, its box needs a containing block")


def test_the_page_pulls_events_instead_of_python_pushing_them():
    """Python must never call evaluate_js: it blocks without a timeout.

    pywebview's evaluate_js waits on a semaphore released from the UI thread;
    a push loop could hang the window for good. The page polls instead.
    """
    source = (BASE / "web_app.py").read_text(encoding="utf-8")
    assert ".evaluate_js(" not in source, (
        "a Python->JS push reappeared; it can deadlock the UI thread")
    js = (BASE / "webui" / "app.js").read_text(encoding="utf-8")
    assert "poll_events" in js and "startEventPump" in js, "the page never polls"


def test_poll_events_drains_the_queue_and_respects_the_limit():
    instance = app.WebApi()
    for index in range(5):
        instance._emit("status", {"text": "t%d" % index})
    first = instance.poll_events(3)
    assert first["ok"] is True
    assert len(first["events"]) == 3, first
    assert first["events"][0]["data"] == {"text": "t0"}
    second = instance.poll_events(10)
    assert [item["data"]["text"] for item in second["events"]] == ["t3", "t4"]
    assert instance.poll_events(10)["events"] == []


def test_the_page_reports_its_own_readiness():
    import tempfile
    with tempfile.TemporaryDirectory(prefix="weread-boot-") as temp:
        original = app.LOG_DIR
        app.LOG_DIR = Path(temp)
        try:
            instance = app.WebApi()
            assert instance.report_ready({"keys": 27, "rail": "v1.0.8",
                                          "elapsed": 1234})["ok"] is True
            text = (Path(temp) / "boot.log").read_text(encoding="utf-8")
        finally:
            app.LOG_DIR = original
    assert "page ready keys=27" in text, text
    assert "v1.0.8" in text


def test_the_bridge_only_exposes_methods():
    """pywebview walks every public attribute of js_api, recursively.

    Handing it the WebApi instance meant it walked `window` (a live WebView2
    Window) and other state on the UI thread; when that walk failed the
    generated bridge had zero methods and every button was dead.
    """
    import inspect
    facade = app._JsApiFacade(app.WebApi())
    names = dir(facade)
    for name in names:
        assert inspect.ismethod(getattr(facade, name)) or inspect.isfunction(
            getattr(facade, name)), "%s is not callable" % name
    assert "boot" in names and "poll_events" in names and "report_ready" in names
    for hidden in ("window", "service", "events", "queue", "selected"):
        assert hidden not in names, "%s leaks into the JS bridge" % hidden


def test_the_shelf_knows_which_books_are_already_on_disk():
    """Export folders are `<title>-<bookId>`; a `.work` suffix means unfinished."""
    import tempfile
    with tempfile.TemporaryDirectory(prefix="weread-exports-") as temp:
        root = Path(temp)
        done = root / "书甲-1234"
        done.mkdir()
        (done / "书甲 - full.epub").write_bytes(b"x")
        half = root / "书乙-5678.work"
        half.mkdir()
        (half / "chapter-1.body").write_bytes(b"x")
        stale = root / "_stale-书丙-9999"
        stale.mkdir()
        (root / "没有书号").mkdir()

        original = app.OUTPUT
        app.OUTPUT = root
        try:
            index = app.WebApi()._export_index()
        finally:
            app.OUTPUT = original

    assert index.get("1234") == "done", index
    assert index.get("5678") == "partial", index
    assert index.get("9999") == "partial", index
    assert len(index) == 3, index


def test_the_shelf_marks_downloaded_books_visually():
    js = (BASE / "webui" / "app.js").read_text(encoding="utf-8")
    assert "shelfState" in js and "已下载" in js and "未完成" in js
    css = (BASE / "webui" / "style.css").read_text(encoding="utf-8")
    assert ".shelf-card.done" in css and ".shelf-badge.done" in css, (
        "a downloaded book must be distinguishable at a glance")
    assert ".shelf-card.partial" in css, "an unfinished export needs its own state"


def main() -> int:
    for name, func in sorted(globals().items()):
        if name.startswith("test_") and callable(func):
            func()
            print("ok", name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
