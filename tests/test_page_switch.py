# -*- coding: utf-8 -*-
"""Click through every sidebar page and verify each renders real content."""
import importlib.util, tempfile, sys, os, time
sys.stdout.reconfigure(encoding="utf-8")
from pathlib import Path

BASE = str(Path(__file__).resolve().parent.parent / "weread")
sys.path.insert(0, BASE)
spec = importlib.util.spec_from_file_location("desktop_app", Path(BASE) / "desktop_app.py")
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)
tmp = tempfile.TemporaryDirectory(prefix="weread-pages-")
app.LOG_DIR = Path(tmp.name) / "logs"
app.OUTPUT = Path(tmp.name) / "exports"
app.weread_login.CREDENTIAL_STATE = Path(tmp.name) / "session-cookies.json"

# seed a fake finished book so 书架/统计 have something to show
out = Path(app.OUTPUT)
(out / "测试书-12345").mkdir(parents=True)
(out / "测试书-12345" / "测试书 - full.epub").write_bytes(b"x" * (3 * 1024 * 1024))
# seed a DONE line + an annotation note for 记录/划线
app.LOG_DIR.mkdir(parents=True, exist_ok=True)
log = Path(app.LOG_DIR) / "desktop-general.log"
log.write_text(
    'DONE {"title": "测试书", "chapters": 1483}\n'
    '[ANNOTATION] chapter UID 7: text kept, 2 of 54 underline(s) not placed\n',
    encoding="utf-8")

import customtkinter as ctk


def _make_instance():
    """A fresh window on the module-level temp dirs."""
    root = ctk.CTk()
    instance = app.DesktopApp(root)
    root.geometry("1120x680")
    root.update()
    root.update()
    return root, instance


def _seed_exports(tmp_dir):
    """Build the three folder kinds the real machine showed.

      done    -- 成品书-111    (2 MB, has an .epub)
      partial -- 半成品-222.work (5 MB, resume scratch)
      stale   -- _stale-333   (1 MB, nothing reads it)
    """
    base = Path(tmp_dir)
    (base / "成品书-111").mkdir(parents=True, exist_ok=True)
    (base / "成品书-111" / "成品书 - full.epub").write_bytes(b"x" * (2 << 20))
    (base / "半成品-222.work").mkdir(parents=True, exist_ok=True)
    (base / "半成品-222.work" / "chapter-uid-1.body").write_bytes(b"x" * (5 << 20))
    (base / "_stale-333").mkdir(parents=True, exist_ok=True)
    (base / "_stale-333" / "junk.body").write_bytes(b"x" * (1 << 20))
    return base


def _row_for(inst, name):
    return next(r for r in inst._scan_exports() if r[0] == name)


def test_scan_classifies_every_folder_kind():
    """done / partial / stale must not be blurred -- deleting them differs."""
    root, inst = _make_instance()
    try:
        base = _seed_exports(app.OUTPUT)
        rows = inst._scan_exports()
        kinds = {r[0]: r[3] for r in rows}
        assert kinds.get("成品书-111") == "done", kinds
        assert kinds.get("半成品-222.work") == "partial", kinds
        assert kinds.get("_stale-333") == "stale", kinds
        sizes = {r[0]: r[1] for r in rows}
        assert sizes["成品书-111"] >= 1.9, sizes
        assert sizes["半成品-222.work"] >= 4.9, sizes
    finally:
        root.destroy()


def test_deleting_a_finished_book_removes_it():
    root, inst = _make_instance()
    try:
        base = _seed_exports(app.OUTPUT)
        inst._ask = lambda *a, **k: True          # confirm
        inst._delete_exports([_row_for(inst, "成品书-111")])
        assert not (base / "成品书-111").exists(), "the folder was not deleted"
        assert "成品书-111" not in [r[0] for r in inst._scan_exports()]
    finally:
        root.destroy()


def test_cancelling_the_confirmation_deletes_nothing():
    """A 'no' must leave the disk untouched -- this is a destructive action."""
    root, inst = _make_instance()
    try:
        base = _seed_exports(app.OUTPUT)
        inst._ask = lambda *a, **k: False         # cancel
        inst._delete_exports([_row_for(inst, "成品书-111")])
        assert (base / "成品书-111").exists(), "cancelling still deleted it"
        assert (base / "成品书-111" / "成品书 - full.epub").exists()
    finally:
        root.destroy()


def test_reclaim_clears_only_the_unfinished_folders():
    """The whole point: free the .work space, keep the finished books."""
    root, inst = _make_instance()
    try:
        base = _seed_exports(app.OUTPUT)
        reclaimable = [r for r in inst._scan_exports() if r[3] in ("partial", "stale")]
        assert len(reclaimable) == 2, reclaimable
        inst._ask = lambda *a, **k: True
        inst._delete_exports(reclaimable)
        assert not (base / "半成品-222.work").exists()
        assert not (base / "_stale-333").exists()
        assert (base / "成品书-111").exists(), "a finished book was destroyed"
        assert (base / "成品书-111" / "成品书 - full.epub").exists()
    finally:
        root.destroy()


def test_a_finished_looking_folder_with_no_epub_is_marked_failed():
    """MEASURED: 无名之町 sat there empty and still looked like a book."""
    root, inst = _make_instance()
    try:
        base = _seed_exports(app.OUTPUT)
        (base / "失败书-444").mkdir(parents=True, exist_ok=True)
        (base / "失败书-444" / "state.json").write_text("{}")
        kinds = {r[0]: r[3] for r in inst._scan_exports()}
        assert kinds["失败书-444"] == "failed", kinds
        assert kinds["成品书-111"] == "done", kinds
    finally:
        root.destroy()


def test_deleting_thoughts_keeps_the_epub():
    """The 183 MB case: give back the notes, keep the readable book."""
    root, inst = _make_instance()
    try:
        base = _seed_exports(app.OUTPUT)
        book = base / "成品书-111"
        (book / "reviews.jsonl").write_bytes(b"x" * (8 << 20))
        (book / "成品书 - full.weread.zip").write_bytes(b"x" * (2 << 20))
        inst._ask = lambda *a, **k: True
        inst._delete_thoughts(book)
        assert not (book / "reviews.jsonl").exists(), "note data survived"
        assert not (book / "成品书 - full.weread.zip").exists()
        assert (book / "成品书 - full.epub").exists(), "the EPUB was destroyed"
    finally:
        root.destroy()


def test_cancelling_the_thoughts_delete_keeps_the_notes():
    root, inst = _make_instance()
    try:
        base = _seed_exports(app.OUTPUT)
        book = base / "成品书-111"
        (book / "reviews.jsonl").write_bytes(b"x" * (8 << 20))
        inst._ask = lambda *a, **k: False
        inst._delete_thoughts(book)
        assert (book / "reviews.jsonl").exists(), "cancelling still deleted"
    finally:
        root.destroy()


def test_clean_all_spares_finished_books_even_with_a_failed_one():
    root, inst = _make_instance()
    try:
        base = _seed_exports(app.OUTPUT)
        (base / "失败书-444").mkdir(parents=True, exist_ok=True)
        (base / "失败书-444" / "state.json").write_text("{}")
        rows = [r for r in inst._scan_exports() if r[3] in ("partial", "stale", "failed")]
        inst._ask = lambda *a, **k: True
        inst._delete_exports(rows)
        assert not (base / "半成品-222.work").exists()
        assert not (base / "_stale-333").exists()
        assert not (base / "失败书-444").exists()
        assert (base / "成品书-111").exists(), "a finished book was destroyed"
        assert (base / "成品书-111" / "成品书 - full.epub").exists()
    finally:
        root.destroy()


def test_a_failed_delete_is_reported_not_silently_ignored():
    """If Windows holds a file, the user must hear about it."""
    root, inst = _make_instance()
    try:
        base = _seed_exports(app.OUTPUT)
        inst._ask = lambda *a, **k: True
        real_rmtree = app.shutil.rmtree

        def locked(*a, **k):
            raise OSError("locked by another process")

        app.shutil.rmtree = locked
        try:
            inst._delete_exports([_row_for(inst, "成品书-111")])
        finally:
            app.shutil.rmtree = real_rmtree
        assert (base / "成品书-111").exists()
        assert "失败" in inst.status.get(), inst.status.get()
    finally:
        root.destroy()



def test_all_sidebar_pages_keep_content_on_revisit():
    root, inst = _make_instance()
    try:
        PAGES = ["我的书架", "下载记录", "存储占用", "设置", "下载工坊"]
        ok = True
        for name in PAGES:
            inst._switch_page(name)
            root.update(); root.update()
            page = inst.pages.get(name)
            children = page.winfo_children() if page else []
            texts = []
            def collect(w):
                for c in w.winfo_children():
                    try:
                        t = c.cget("text")
                        if isinstance(t, str) and t:
                            texts.append(t[:30])
                    except Exception:
                        pass
                    collect(c)
            collect(page)
            has_content = len(children) > 0
            is_active = inst._current_page == name
            # back on the workshop, the old widgets must still exist
            workshop_ok = True
            if name == "下载工坊":
                workshop_ok = (inst.results.winfo_exists() and inst.intro.winfo_exists()
                               and inst.export_button.winfo_exists())
            passed = has_content and is_active and workshop_ok
            ok = ok and passed
            print(("  OK   " if passed else "  FAIL ") + name
                  + "  widgets=%d" % len(children)
                  + ("  | " + " ; ".join(texts[:3]) if texts else ""))
            # a second visit must not duplicate the page
            inst._switch_page(name)
            root.update()
            assert inst.pages[name] is page, name + " page got rebuilt on revisit"
        assert ok, "a sidebar page lost its content"
    finally:
        root.destroy()


def test_history_page_pages_instead_of_building_every_row():
    """下载记录一页只建 40 行，不把 200 条一次性铺成 1400 个控件。

    旧实现每行 3 个 CTk 控件；窗口缩放时 Tk 要重排全部，单次接近 1 秒。
    """
    log = Path(app.LOG_DIR) / "desktop-history.log"
    log.write_text("\n".join('DONE {"title": "历史书%d", "chapters": 10}' % i
                            for i in range(200)) + "\n", encoding="utf-8")
    root, inst = _make_instance()
    try:
        inst.load_shelf = lambda: None
        inst._switch_page("下载记录")
        root.update()

        texts, labels, buttons = [], [], []

        def collect(widget):
            for child in widget.winfo_children():
                try:
                    text = child.cget("text")
                    if isinstance(text, str) and text:
                        texts.append(text)
                        if type(child).__name__ == "Label":
                            labels.append(text)
                        if type(child).__name__ == "CTkButton":
                            buttons.append((text, child))
                except Exception:
                    pass
                collect(child)

        collect(inst.pages["下载记录"])
        joined = "\n".join(texts)
        assert "第 1 /" in joined, joined[:500]
        assert "共 20" in joined, joined[:500]
        assert len(labels) < 120, "too many row labels built at once: %d" % len(labels)
        next_button = next(button for label, button in buttons if label == "下一页")
        next_button.invoke()
        root.update()
        texts.clear(); collect(inst.pages["下载记录"])
        assert any("第 2 /" in text for text in texts), texts[:20]
    finally:
        log.unlink(missing_ok=True)
        root.destroy()


def test_pages_are_stacked_instead_of_repacked():
    """切页只该把页面提到最上层，不能让 Tk 重新计算整页几何。

    旧实现每次切换都 pack_forget 旧页再 pack 新页，页面高度随下载面板收起/
    展开而变，视觉上就是整屏闪一下、像在重新排布窗口。
    """
    root, inst = _make_instance()
    try:
        inst.load_shelf = lambda: None
        inst._switch_page("我的书架")
        root.update()
        shelf = inst.pages["我的书架"]
        assert shelf.winfo_manager() == "place", shelf.winfo_manager()
        size = (shelf.winfo_width(), shelf.winfo_height())

        inst._switch_page("下载工坊")
        root.update()
        assert not shelf.winfo_ismapped()
        assert inst.workshop_footer.winfo_manager() == "place"

        inst._switch_page("我的书架")
        root.update()
        assert inst.pages["我的书架"] is shelf
        assert (shelf.winfo_width(), shelf.winfo_height()) == size, (
            "the page was re-laid out by the switch")
        assert not inst.workshop_footer.winfo_ismapped()
    finally:
        root.destroy()


if __name__ == "__main__":
    for name, case in tuple(globals().items()):
        if name.startswith("test_"):
            case()
            print("PASS", name)
