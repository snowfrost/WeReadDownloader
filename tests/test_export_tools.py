# -*- coding: utf-8 -*-
"""Guard rails for the export shelf: extra source folders and EPUB renames.

Covers the rules that are easy to get wrong: a rename pattern must not leave a
dangling dash when the publisher is unknown, two books must never overwrite each
other inside one batch, and a folder the user pointed at must be scanned but
never cleaned up.
"""
import sys
import tempfile
import time
import types
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

BASE = Path(__file__).resolve().parent.parent / "weread"
sys.path.insert(0, str(BASE))

if "webview" not in sys.modules:
    try:
        import webview  # noqa: F401
    except Exception:
        stub = types.ModuleType("webview")
        stub.create_window = lambda *a, **k: None
        stub.start = lambda *a, **k: None
        stub.FOLDER_DIALOG = "folder"
        sys.modules["webview"] = stub

import importlib.util  # noqa: E402


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


library = _load("library", BASE / "library.py")
# web_app does `import library`; register our instance so both see the same
# module, otherwise path patches land on a copy the bridge never reads.
sys.modules["library"] = library
app = _load("web_app", BASE / "web_app.py")

TESTS = []


def test(func):
    TESTS.append(func)
    return func


def _export_folder(root: Path, title: str, book_id: str, epub_bytes=b"epub",
                   suffix: str = "") -> Path:
    folder = root / ("%s-%s%s" % (title, book_id, suffix))
    folder.mkdir(parents=True, exist_ok=True)
    if epub_bytes is not None:
        (folder / ("%s - full.epub" % title)).write_bytes(epub_bytes)
    return folder


@test
def test_pattern_drops_fields_that_have_no_value():
    values = {"title": "书名", "author": "作者", "publisher": ""}
    assert library.render_pattern("{title}-{author}-{publisher}", values) == "书名-作者"
    values["publisher"] = "某某出版社"
    assert library.render_pattern("{title}-{author}-{publisher}", values) == "书名-作者-某某出版社"
    # An unknown placeholder disappears instead of being printed literally.
    assert library.render_pattern("{title}-{nope}", values) == "书名"


@test
def test_pattern_survives_hostile_titles():
    values = {"title": 'a/b\\c:d*e?"f<g>h|i', "author": "", "publisher": ""}
    name = library.render_pattern("{title}-{author}", values)
    assert not set(name) & set('<>:"/\\|?*'), name
    long_title = "书" * 300
    assert len(library.render_pattern("{title}", {"title": long_title})) <= 120


@test
def test_conflict_policies():
    entries = [
        {"values": {"title": "同名书"}, "epub": "a.epub"},
        {"values": {"title": "同名书"}, "epub": "b.epub"},
    ]
    skipped = library.plan_names(entries, "{title}", set(), "skip")
    assert [p["action"] for p in skipped] == ["copy", "skip"], skipped

    suffixed = library.plan_names(entries, "{title}", {"同名书.epub"}, "suffix")
    assert [p["target"] for p in suffixed] == ["同名书 (2).epub", "同名书 (3).epub"], suffixed

    overwritten = library.plan_names(entries, "{title}", {"同名书.epub"}, "overwrite")
    assert overwritten[0]["action"] == "overwrite"


@test
def test_folder_state_and_epub_discovery():
    with tempfile.TemporaryDirectory(prefix="weread-lib-") as temp:
        root = Path(temp)
        done = _export_folder(root, "成品书", "100001", b"x" * 10)
        (done / "old.epub").write_bytes(b"x" * 2)
        assert library.folder_state(done) == "done"
        assert library.find_epub(done).stat().st_size == 10, "the largest epub wins"

        half = _export_folder(root, "半本", "100002", None, suffix=".work")
        assert library.folder_state(half) == "partial"

        stale = root / "_stale-书-100003"
        stale.mkdir()
        assert library.folder_state(stale) == "partial"

        empty = _export_folder(root, "空目录", "100004", None)
        assert library.folder_state(empty) == "failed"

        assert library.book_id_from_name("成品书-100001") == "100001"
        assert library.book_id_from_name("半本-100002.work") == "100002"
        assert library.book_id_from_name("没有书号") == ""


@test
def test_extra_sources_are_scanned_but_never_cleaned():
    with tempfile.TemporaryDirectory(prefix="weread-src-") as temp:
        base = Path(temp)
        default_root = base / "exports"
        legacy = base / "legacy-exports"
        default_root.mkdir()
        legacy.mkdir()
        _export_folder(legacy, "旧版书", "200001")

        originals = (app.OUTPUT, library.SOURCES_FILE)
        app.OUTPUT = default_root
        app.core.OUTPUT = default_root
        library.SOURCES_FILE = base / "sources.json"
        try:
            assert library.add_source(legacy)[0] is True
            assert library.add_source(legacy)[0] is False, "duplicates must be refused"
            index = app.WebApi()._export_index()
            assert index.get("200001") == "done", index
            rows = app.WebApi()._scan_exports()
            external = [r for r in rows if r["external"]]
            assert [r["name"] for r in external] == ["旧版书-200001"], rows
            # A read-only folder must not be offered for cleanup.
            stats = app.WebApi().storage()
            assert stats["reclaim"] == 0, stats
        finally:
            app.OUTPUT, library.SOURCES_FILE = originals
            app.core.OUTPUT = originals[0]


@test
def test_export_copies_and_renames_selected_books():
    with tempfile.TemporaryDirectory(prefix="weread-export-") as temp:
        base = Path(temp)
        source = base / "exports"
        source.mkdir()
        _export_folder(source, "第一本", "300001", b"one")
        _export_folder(source, "第二本", "300002", b"two")
        destination = base / "out"

        originals = (app.OUTPUT, library.META_FILE)
        app.OUTPUT = source
        app.core.OUTPUT = source
        library.META_FILE = base / "book-meta.json"
        try:
            library.remember_meta("300001", title="第一本", author="作者甲",
                                  publisher="甲出版社")
            library.remember_meta("300002", title="第二本", author="作者乙")
            api = app.WebApi()
            rows = api._scan_exports()
            keys = [r["key"] for r in rows]
            result = api.export_epubs(keys, destination, "{title}-{author}-{publisher}", "skip")
            assert result["ok"] is True, result

            done = None
            deadline = time.time() + 20
            while time.time() < deadline:
                for item in api.poll_events()["events"]:
                    if item["name"] == "export_done":
                        done = item["data"]
                if done:
                    break
                time.sleep(0.05)
        finally:
            app.OUTPUT, library.META_FILE = originals
            app.core.OUTPUT = originals[0]

        assert done is not None, "the export thread never reported back"
        assert done["exported"] == 2, done
        names = sorted(item.name for item in destination.glob("*.epub"))
        assert names == ["第一本-作者甲-甲出版社.epub", "第二本-作者乙.epub"], names
        assert (destination / "第一本-作者甲-甲出版社.epub").read_bytes() == b"one"


@test
def test_export_refuses_an_unusable_target():
    api = app.WebApi()
    assert api.export_epubs([], "/tmp/x")["ok"] is False
    # No such export row: it must say so rather than pretend to start.
    error = api.export_epubs(["a"], "   ")["error"]
    assert error, error


def main() -> int:
    failures = 0
    for func in TESTS:
        try:
            func()
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print("FAIL %s: %r" % (func.__name__, exc))
        else:
            print("ok   %s" % func.__name__)
    print("%d passed, %d failed" % (len(TESTS) - failures, failures))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
