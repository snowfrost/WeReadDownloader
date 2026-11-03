#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WebView shell for WeReadDownloader.

The Tk frontend is replaced by a WebView2 window; this module owns everything
that must stay in Python: the warm worker service, download/queue processes,
shelf/history/storage reads and QR login. The renderer only draws state and
calls these methods.

Why a rewrite instead of another Tk patch: measured on this machine, a page
with 60 CustomTkinter labels spent ~270 ms per window resize just redrawing
widgets, and every switch repainted the whole window. WebView2 composites in
the GPU process, so resizing/maximizing is a viewport change rather than a
widget-tree relayout.
"""
from __future__ import annotations

import base64
import ctypes
import io
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import uuid
from pathlib import Path

import requests
import webview

import weread_core as core
import library
import weread_login as login
from weread_core import (
    LOG_DIR,
    OUTPUT,
    WORKER_NAME,
    book_detail_lines,
    friendly_error,
    parse_book_info_message,
    parse_search_meta,
    parse_search_row,
    rating_text,
    redact_secrets,
    worker_command,
)
from version import APP_VERSION

APP_TITLE = "WeReadDownloader"
ANNOTATION_LABELS = ("注释和想法", "注释", "想法", "纯正文")
DEFAULT_ANNOTATION = "注释和想法"
HISTORY_PAGE_SIZE = 40
PROGRESS_RE = re.compile(r"^\[(\d+)/(\d+)\]")


class ServiceClient:
    """One long-lived --serve worker, same protocol as the Tk frontend."""

    def __init__(self, log=None):
        self._process = None
        self._lock = threading.Lock()
        self._log = log or (lambda _line: None)

    def _kill_locked(self):
        process, self._process = self._process, None
        if process is not None:
            try:
                process.kill()
            except Exception:
                pass

    def _ensure_locked(self):
        process = self._process
        if process is not None and process.poll() is None:
            return process
        self._process = None
        command = worker_command(["--serve"])
        if command is None:
            return None
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        try:
            process = subprocess.Popen(
                command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
                errors="replace", bufsize=1, cwd=str(Path(command[0]).parent),
                env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError as exc:
            self._log("[SERVICE] start failed: %s: %s"
                      % (type(exc).__name__, exc))
            return None
        stream = getattr(process, "stdout", None)
        if stream is None:
            self._kill_locked()
            return None
        try:
            ready = json.loads(stream.readline() or "{}")
        except (OSError, ValueError, AttributeError):
            ready = {}
        if ready.get("event") != "ready":
            self._kill_locked()
            return None
        self._process = process
        return process

    def warm(self):
        with self._lock:
            return self._ensure_locked() is not None

    def request(self, request_id, command, arguments, on_line):
        """Return exit code; None when the warm worker is unavailable."""
        with self._lock:
            process = self._ensure_locked()
            if process is None:
                return None
            payload = json.dumps({"id": request_id, "cmd": command,
                                  "args": arguments}, ensure_ascii=False)
            try:
                process.stdin.write(payload + "\n")
                process.stdin.flush()
            except (OSError, ValueError):
                self._kill_locked()
                return None
            while True:
                try:
                    raw = process.stdout.readline()
                except (OSError, ValueError):
                    raw = ""
                if not raw:
                    self._kill_locked()
                    on_line("FAILED WorkerUnavailable 常驻下载组件意外退出")
                    return -1
                try:
                    message = json.loads(raw)
                except ValueError:
                    continue
                if message.get("id") != request_id:
                    continue
                if "line" in message:
                    on_line(str(message["line"]))
                    continue
                if message.get("event") == "end":
                    return int(message.get("code", 1))

    def shutdown(self):
        process, self._process = self._process, None
        if process is None:
            return
        try:
            process.stdin.write('{"cmd": "quit"}\n')
            process.stdin.flush()
        except Exception:
            pass
        try:
            process.wait(timeout=1.0)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass


QUEUE_FILE = core.PRIVATE / "queue.json"


def _load_queue_file():
    """The queue saved by the last session, or None when unusable."""
    try:
        raw = QUEUE_FILE.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    items = [item for item in (data.get("items") or [])
             if isinstance(item, dict) and item.get("bookId")]
    results = [item for item in (data.get("results") or []) if isinstance(item, dict)]
    return {"items": items, "results": results[-50:],
            "paused": bool(data.get("paused")),
            "saved_at": data.get("saved_at")}


def _write_queue_file(payload) -> None:
    """Write atomically: a crash mid-write must not leave a broken queue."""
    try:
        QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
        temp = QUEUE_FILE.with_name(QUEUE_FILE.name + ".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                        encoding="utf-8")
        os.replace(temp, QUEUE_FILE)
    except OSError:
        pass


class _JsApiFacade:
    """The object handed to pywebview as js_api.

    pywebview enumerates everything a js_api object exposes: it walks every
    public attribute that is not callable, recursively. Handing it the WebApi
    instance made it walk `window` (a live WebView2 Window), `service`, the
    queues and dicts -- on the UI thread, while the browser was starting. That
    walk is slow and can fail, and when it does the generated bridge comes out
    with zero methods: the page loads, every button does nothing.

    This facade exposes exactly the callable surface, so enumeration is a flat
    list of methods and nothing else leaks into window.pywebview.api.
    """

    def __init__(self, api):
        object.__setattr__(self, "_api", api)

    def _names(self):
        api = object.__getattribute__(self, "_api")
        return sorted(name for name in dir(api)
                      if not name.startswith("_") and callable(getattr(api, name, None)))

    def __dir__(self):
        return self._names()

    def __getattr__(self, name):
        api = object.__getattribute__(self, "_api")
        if name in self._names():
            return getattr(api, name)
        raise AttributeError(name)


class WebApi:
    """Methods exposed to JavaScript as window.pywebview.api.*."""

    def __init__(self):
        self.window = None
        self.service = ServiceClient(self._log)
        self.events = queue.Queue()
        self._busy = False
        self._busy_lock = threading.Lock()
        self._job = 0
        self._process = None
        self._workers = set()
        self._workers_lock = threading.Lock()
        self.selected = {}
        self.queue = []
        self.queue_results = []
        self.queue_running = False
        self.queue_paused = False
        self._pause_requested = False
        self._queue_retried = set()
        self._queue_book = None
        self._restored = 0
        self._restore_queue()
        self.annotation = DEFAULT_ANNOTATION
        self._closing = False
        self._login_session = None
        self._login_jar = None
        self._login_uid = ""
        self._login_cancel = threading.Event()
        self._otp_ready = threading.Event()
        self._pending_otp = ""
        self._login_thread = None
        self._forward_thread = None
        self._ready_at = None

    # -- plumbing ------------------------------------------------------
    def bind(self, window):
        # No Python->JS push any more. pywebview's evaluate_js blocks on a
        # semaphore that is released by a callback scheduled on the UI thread,
        # with no timeout: if the UI thread is busy the caller hangs forever,
        # and if that caller ever holds a lock the whole window freezes. The
        # page pulls events instead (see poll_events), so Python never has to
        # touch the UI thread.
        self.window = window
        self._pump_started = time.time()

    def _emit(self, name, data=None):
        self.events.put((name, data))

    def _restore_queue(self):
        """Bring back last session's queue and its paused flag.

        Nothing starts on its own: a queue that was mid-run when the app closed
        comes back paused, so the user decides when to continue.
        """
        saved = _load_queue_file()
        if not saved or not saved["items"]:
            return
        seen = set()
        items = []
        for item in saved["items"]:
            book_id = str(item.get("bookId"))
            if book_id in seen:
                continue
            seen.add(book_id)
            items.append(dict(item))
        self.queue = items
        self.queue_results = saved["results"]
        self.queue_running = False
        self.queue_paused = True
        self._queue_book = None
        self._restored = len(items)

    def _save_queue(self):
        """Persist the queue, the in-flight book and the paused flag.

        Closing the app used to throw the queue away; the interrupted download
        is saved at the head so "continue" picks up where it stopped.
        """
        items = []
        seen = set()
        for item in ([self._queue_book] if self._queue_book else []) + list(self.queue):
            if not isinstance(item, dict):
                continue
            book_id = str(item.get("bookId") or "")
            if not book_id or book_id in seen:
                continue
            seen.add(book_id)
            items.append(dict(item))
        _write_queue_file({
            "version": 1,
            "items": items,
            "results": self.queue_results[-50:],
            "paused": bool(self.queue_paused),
            "saved_at": int(time.time()),
        })

    def poll_events(self, limit=80):
        """Drain queued events for the page. Called by JS about every 120ms."""
        if not isinstance(limit, int) or limit <= 0:
            limit = 80
        batch = []
        for _ in range(min(limit, 200)):
            try:
                name, data = self.events.get_nowait()
            except queue.Empty:
                break
            batch.append({"name": name, "data": data})
        return {"ok": True, "events": batch, "pending": self.events.qsize()}

    def report_ready(self, info):
        """Called once by the page after boot, so the log shows what it saw."""
        detail = info if isinstance(info, dict) else {}
        self._ready_at = time.time()
        _boot_log("page ready keys=%s rail=%s after=%sms"
                  % (detail.get("keys"), detail.get("rail"),
                     detail.get("elapsed")))
        return {"ok": True}

    def _log(self, text):
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            with (LOG_DIR / "webui.log").open("a", encoding="utf-8") as stream:
                stream.write(text + "\n")
        except OSError:
            pass

    def _status(self, text, kind="info"):
        self._emit("status", {"text": text, "kind": kind})

    def _set_busy(self, busy):
        with self._busy_lock:
            self._busy = busy
        self._emit("busy", {"busy": busy})

    def _is_busy(self):
        with self._busy_lock:
            return self._busy

    # -- boot / settings ----------------------------------------------
    def boot(self):
        status = {}
        try:
            status = login.credentials_status()
        except Exception:
            status = {}
        return {
            "ok": True,
            "version": APP_VERSION,
            "title": APP_TITLE,
            "paths": {"output": str(OUTPUT), "logs": str(LOG_DIR),
                      "export_default": str(Path.home() / "Documents" / "WeRead 导出")},
            "auth": status,
            "queue": self.queue,
            "queue_running": self.queue_running,
            "queue_paused": self.queue_paused,
            "queue_restored": self._restored,
            "annotation": self.annotation,
            "annotations": list(ANNOTATION_LABELS),
            "selected": self.selected,
        }

    def set_annotation(self, label):
        if label not in ANNOTATION_LABELS:
            return {"ok": False, "error": "未知的标注方式"}
        self.annotation = label
        return {"ok": True, "annotation": label}

    def open_path(self, which):
        target = OUTPUT if which == "output" else LOG_DIR
        try:
            target.mkdir(parents=True, exist_ok=True)
            os.startfile(str(target))
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    # -- search / detail ----------------------------------------------
    def search(self, keyword):
        keyword = (keyword or "").strip()
        if not keyword:
            return {"ok": False, "error": "请先输入书名或关键词"}
        if self._is_busy():
            return {"ok": False, "error": "当前有任务在运行，请等它结束"}
        self._set_busy(True)
        self._job += 1
        job = self._job
        self._status("正在搜索微信读书…", "busy")
        self._emit("search_start", {"keyword": keyword})

        def run():
            code = self.service.request(job, "search", {"keyword": keyword},
                                        self._on_search_line)
            if code is None:
                self._status("常驻组件不可用，正在改用独立进程重试…", "busy")
                self._emit("service_miss", {"action": "search"})
            self._emit("search_done", {"code": code if code is not None else 1})
            self._set_busy(False)

        threading.Thread(target=run, daemon=True).start()
        return {"ok": True}

    def _on_search_line(self, line):
        meta = parse_search_meta(line)
        if meta is not None:
            self._emit("search_meta", {"bookId": meta[0], "category": meta[1]})
            return
        row = parse_search_row(line)
        if row is not None:
            self._emit("search_row", row)
            return
        if line.startswith("FAILED "):
            self._status(friendly_error(line), "error")
        self._log(line)

    def book_info(self, book_id):
        book_id = str(book_id or "").strip()
        if not book_id:
            return {"ok": False, "error": "缺少书籍 ID"}
        if self._is_busy():
            return {"ok": False, "error": "当前有任务在运行，稍后再点"}
        self._set_busy(True)
        self._job += 1
        job = self._job
        self._status("正在读取书籍详情…", "busy")
        self._emit("detail_start", {"bookId": book_id})

        def run():
            holder = {}

            def on_line(line):
                info = parse_book_info_message(line)
                if info is not None:
                    holder.update(info)
                    library.remember_meta(book_id, **{
                        key: info.get(key) for key in ("title", "author",
                                                       "publisher", "category")})
                elif line.startswith("FAILED "):
                    self._status(friendly_error(line), "error")
                self._log(line)

            code = self.service.request(job, "book-info", {"bookId": book_id},
                                        on_line)
            if holder:
                self._emit("book_info", holder)
            else:
                self._emit("book_info_failed",
                           {"code": code, "bookId": book_id})
                self._status("详情读取失败，仍可尝试下载", "error")
            self._set_busy(False)

        threading.Thread(target=run, daemon=True).start()
        return {"ok": True}

    def probe(self, book_id):
        book_id = str(book_id or "").strip()
        if not book_id:
            return {"ok": False, "error": "缺少书籍 ID"}
        if self._is_busy():
            return {"ok": False, "error": "当前有任务在运行"}
        self._set_busy(True)
        self._job += 1
        job = self._job
        self._status("正在检查第一章…", "busy")

        def run():
            ok = False
            detail = ""
            def on_line(line):
                nonlocal ok, detail
                if line.startswith("PROBE_OK"):
                    ok = True
                    detail = line
                elif line.startswith("FAILED "):
                    detail = friendly_error(line)
                self._log(line)
            code = self.service.request(job, "probe", {"bookId": book_id},
                                        on_line)
            self._emit("probe", {"ok": ok, "detail": detail, "code": code})
            self._status("第一章可读，可以开始下载。" if ok else
                         (detail or "第一章检查失败"), "ok" if ok else "error")
            self._set_busy(False)

        threading.Thread(target=run, daemon=True).start()
        return {"ok": True}

    def check_auth(self):
        if self._is_busy():
            return {"ok": False, "error": "当前有任务在运行"}
        self._set_busy(True)
        self._job += 1
        job = self._job
        self._status("正在检查登录状态…", "busy")

        def run():
            result = {"ok": False, "detail": ""}

            def on_line(line):
                if line.startswith("AUTH_OK"):
                    result["ok"] = True
                    result["detail"] = line
                elif line.startswith("FAILED "):
                    result["detail"] = friendly_error(line)

            code = self.service.request(job, "auth", {}, on_line)
            if code is None:
                result["detail"] = "常驻组件不可用"
            self._emit("auth", result)
            self._set_busy(False)
            self._status("登录状态正常。" if result["ok"] else
                         (result["detail"] or "登录状态检查失败"), 
                         "ok" if result["ok"] else "error")

        threading.Thread(target=run, daemon=True).start()
        return {"ok": True}

    def load_shelf(self):
        if self._is_busy():
            return {"ok": False, "error": "当前有任务在运行"}
        self._set_busy(True)
        self._job += 1
        job = self._job
        self._status("正在读取微信书架…", "busy")

        def run():
            rows = []

            def on_line(line):
                if line.startswith("SHELF\t"):
                    payload = line.split("\t", 1)[1]
                    try:
                        value = json.loads(payload)
                        if isinstance(value, list):
                            rows.extend(value)
                    except ValueError:
                        pass
                elif line.startswith("FAILED "):
                    self._status(friendly_error(line), "error")

            code = self.service.request(job, "shelf", {}, on_line)
            local = self._export_index()
            for row in rows:
                if isinstance(row, dict):
                    row["download"] = local.get(str(row.get("bookId") or ""), "")
                    # Remember what the shelf already knows, so a rename never
                    # has to ask the network for a title that is right here.
                    library.remember_meta(row.get("bookId"),
                                          title=row.get("title"),
                                          author=row.get("author"))
            self._emit("shelf", {"rows": rows, "code": code})
            self._set_busy(False)
            self._status("已读取微信书架，共 %d 本。" % len(rows)
                         if rows else "书架为空，或登录状态已过期。",
                         "ok" if rows else "error")

        threading.Thread(target=run, daemon=True).start()
        return {"ok": True}

    # -- download / queue ---------------------------------------------
    def _annotation_flags(self):
        if self.annotation == "注释和想法":
            return ["--unified-appendix", "--appendix-scope", "both"]
        if self.annotation == "注释":
            return ["--unified-appendix", "--appendix-scope", "notes"]
        if self.annotation == "想法":
            return ["--unified-appendix", "--appendix-scope", "thoughts"]
        return ["--no-thoughts", "--no-annotations"]

    def _track(self, process):
        with self._workers_lock:
            self._workers.add(process)

    def _untrack(self, process):
        with self._workers_lock:
            self._workers.discard(process)

    def download(self, book):
        if not isinstance(book, dict) or not book.get("bookId"):
            return {"ok": False, "error": "缺少书籍 ID"}
        if self._is_busy() or self.queue_running:
            return {"ok": False, "error": "当前已有下载任务"}
        self.selected = dict(book)
        self._start_one(book)
        return {"ok": True}

    def _start_one(self, book):
        self._queue_book = dict(book)
        self._set_busy(True)
        self._pause_requested = False
        self._emit("download_start", book)
        thread = threading.Thread(target=self._run_download, args=(book,),
                                  daemon=True)
        thread.start()

    def _run_download(self, book):
        args = [str(book.get("bookId")), "--jobs", "1"] + self._annotation_flags()
        command = worker_command(args)
        if command is None:
            self._emit("download_failed", {"book": book, "error": "下载组件缺失"})
            self._download_finished(-1, book)
            return
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        try:
            process = subprocess.Popen(
                command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                cwd=str(Path(command[0]).parent), env=env,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError as exc:
            self._emit("download_failed",
                       {"book": book, "error": f"{type(exc).__name__}: {exc}"})
            self._download_finished(-1, book)
            return
        self._process = process
        self._track(process)
        code = -1
        try:
            if process.stdout is not None:
                for raw in process.stdout:
                    line = raw.rstrip("\r\n")
                    if not line:
                        continue
                    self._log(line)
                    match = PROGRESS_RE.match(line)
                    if match:
                        current, total = int(match.group(1)), max(1, int(match.group(2)))
                        self._emit("progress", {
                            "current": current, "total": total,
                            "percent": int(current / total * 1000) / 10.0,
                        })
                    if line.startswith("DONE "):
                        self._emit("download_done", {"book": book, "line": line})
                    elif line.startswith("FAILED "):
                        self._emit("download_failed", {
                            "book": book, "error": friendly_error(line)})
                    self._emit("log", {"line": redact_secrets(line)})
            code = process.wait()
        except Exception as exc:
            self._emit("download_failed",
                       {"book": book, "error": f"{type(exc).__name__}: {exc}"})
        finally:
            self._untrack(process)
            self._process = None
        self._download_finished(code, book)

    def _download_finished(self, code, book):
        self._emit("download_end", {"book": book, "code": code})
        self._set_busy(False)
        self._queue_book = None
        if not self.queue_running:
            self._status("下载完成。" if code == 0 else "下载未完成，详见技术日志。",
                         "ok" if code == 0 else "error")
            self._save_queue()
            return
        if self._pause_requested:
            self.queue_running = False
            self.queue_paused = True
            if book and not any(b.get("bookId") == book.get("bookId")
                                for b in self.queue):
                self.queue.insert(0, dict(book))
            self._emit("queue", self._queue_state())
            self._status("队列已暂停，当前这本保留在队首。", "info")
            self._save_queue()
            return
        success = code == 0
        self.queue_results.append({
            "bookId": book.get("bookId"), "title": book.get("title", ""),
            "ok": success})
        if self.queue:
            nxt = self.queue.pop(0)
            self._emit("queue", self._queue_state())
            self._status("队列继续：开始下载《%s》。" % (nxt.get("title") or
                                                      nxt.get("bookId")), "busy")
            self._start_one(nxt)
            self._save_queue()
            return
        self.queue_running = False
        failed = [r for r in self.queue_results if not r["ok"]]
        self._emit("queue", self._queue_state())
        self._status("队列结束：%d 本完成，%d 本未完成。"
                     % (len(self.queue_results) - len(failed), len(failed)),
                     "ok" if not failed else "error")
        self._save_queue()

    def enqueue(self, books):
        if not isinstance(books, list):
            return {"ok": False, "error": "参数错误"}
        existing = {str(b.get("bookId")) for b in self.queue}
        added = 0
        for book in books:
            bid = str(book.get("bookId") or "")
            if not bid or bid in existing:
                continue
            self.queue.append({"bookId": bid, "title": book.get("title", ""),
                               "author": book.get("author", "")})
            existing.add(bid)
            added += 1
        self._emit("queue", self._queue_state())
        self._save_queue()
        return {"ok": True, "added": added, "queue": self._queue_state()}

    def remove_queue(self, book_id):
        self.queue = [b for b in self.queue if str(b.get("bookId")) != str(book_id)]
        self._emit("queue", self._queue_state())
        self._save_queue()
        return {"ok": True}

    def clear_queue(self):
        if self.queue_running:
            return {"ok": False, "error": "下载中不能清空队列"}
        self.queue.clear()
        self._emit("queue", self._queue_state())
        self._save_queue()
        return {"ok": True}

    def start_queue(self):
        if self.queue_running:
            return {"ok": False, "error": "队列已在运行"}
        if not self.queue:
            return {"ok": False, "error": "队列是空的"}
        if self._is_busy():
            return {"ok": False, "error": "当前有任务在运行"}
        self.queue_running = True
        self.queue_paused = False
        self.queue_results.clear()
        self._emit("queue", self._queue_state())
        self._start_one(self.queue.pop(0))
        self._save_queue()
        return {"ok": True}

    def pause(self):
        if not self.queue_running and self._process is None:
            return {"ok": False, "error": "没有正在运行的下载"}
        self._pause_requested = True
        if self.queue_running:
            self._status("正在暂停：当前这本停下后，队列会停在这里。", "info")
        else:
            self._status("正在暂停，已写入的章节会保留。", "info")
        process = self._process
        if process is not None:
            try:
                process.terminate()
            except Exception:
                pass
        return {"ok": True}

    def resume(self):
        if not self.queue_paused:
            return {"ok": False, "error": "队列没有暂停"}
        self.queue_paused = False
        self.queue_running = True
        if not self.queue:
            self.queue_running = False
            self._save_queue()
            return {"ok": False, "error": "队列里没有书"}
        self._emit("queue", self._queue_state())
        self._start_one(self.queue.pop(0))
        self._save_queue()
        return {"ok": True}

    def _queue_state(self):
        return {"queue": self.queue, "running": self.queue_running,
                "paused": self.queue_paused, "current": self._queue_book}

    # -- history / storage --------------------------------------------
    @staticmethod
    def _read_log_tail(path: Path, limit: int = 65536) -> str:
        try:
            with path.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                start = max(0, size - limit)
                handle.seek(start)
                data = handle.read()
        except OSError:
            return ""
        text = data.decode("utf-8", errors="replace")
        if start:
            text = text.split("\n", 1)[1] if "\n" in text else ""
        return text

    def history(self, page=0):
        rows = []
        try:
            logs = sorted(LOG_DIR.glob("desktop-*.log"),
                          key=lambda p: p.stat().st_mtime, reverse=True)[:200]
            for path in logs:
                for line in self._read_log_tail(path).splitlines():
                    if not line.startswith("DONE "):
                        continue
                    body = line[5:].strip()
                    try:
                        payload = json.loads(body)
                        title = str(payload.get("title") or "未知书名")
                        chapters = payload.get("chapters") or payload.get("total") or "?"
                        rows.append(("完成", f"{title} · {chapters} 章"))
                        continue
                    except (ValueError, TypeError):
                        pass
                    p = Path(body.replace("/", "\\"))
                    book = p.parent.name or body
                    if book.endswith(".work"):
                        book = book[:-5]
                    artifact = "EPUB" if body.lower().endswith(".epub") else (
                        "导入包" if body.lower().endswith(".zip") else "文件")
                    display = re.sub(r"-\d{6,}$", "", book) or book
                    rows.append(("完成", f"{display} · {artifact}"))
        except OSError:
            pass
        rows = list(reversed(rows))
        page = max(0, int(page or 0))
        total = max(1, (len(rows) + HISTORY_PAGE_SIZE - 1) // HISTORY_PAGE_SIZE)
        page = min(page, total - 1)
        start = page * HISTORY_PAGE_SIZE
        return {"rows": rows[start:start + HISTORY_PAGE_SIZE],
                "page": page, "pages": total, "total": len(rows)}

    def _export_index(self):
        """bookId -> "done" / "partial" for everything already on this machine.

        Export folders are named `<title>-<bookId>`; an interrupted run keeps a
        `.work` suffix. Every folder the user pointed at counts -- books saved
        by an older build live in its own output folder, and they should show up
        as "already downloaded" too.
        """
        index = {}
        for root in library.scan_roots():
            try:
                entries = list(root.iterdir())
            except OSError:
                continue
            for entry in entries:
                if not entry.is_dir():
                    continue
                book_id = library.book_id_from_name(entry.name)
                if not book_id:
                    continue
                state = library.folder_state(entry)
                if state == "failed":
                    state = "partial"
                # A finished export wins over a leftover half-download.
                if index.get(book_id) != "done":
                    index[book_id] = state
        return index

    def _folder_size_mb(self, folder: Path) -> float:
        total = 0
        for root_dir, _dirs, files in os.walk(folder):
            for filename in files:
                try:
                    total += os.stat(os.path.join(root_dir, filename)).st_size
                except OSError:
                    continue
        return round(total / 1048576.0, 1)

    def _scan_exports(self):
        rows = []
        for root in library.scan_roots():
            try:
                root.mkdir(parents=True, exist_ok=True)
                entries = sorted(root.iterdir(),
                                 key=lambda p: p.stat().st_mtime, reverse=True)
            except OSError:
                continue
            try:
                external = root.resolve() != OUTPUT.resolve()
            except OSError:
                external = True
            for entry in entries:
                if not entry.is_dir():
                    continue
                kind = library.folder_state(entry)
                if entry.name.startswith("_stale-"):
                    kind = "stale"
                epub = library.find_epub(entry)
                rows.append({
                    "name": entry.name,
                    "key": str(entry),
                    "root": str(root),
                    "external": external,
                    "bookId": library.book_id_from_name(entry.name),
                    "epub": str(epub) if epub else "",
                    "mb": self._folder_size_mb(entry),
                    "mtime": int(entry.stat().st_mtime),
                    "kind": kind,
                })
        return rows

    def local_state(self):
        """bookId -> download state; lets the shelf refresh marks after a delete
        without re-reading the whole WeRead shelf."""
        return {"ok": True, "index": self._export_index()}

    def storage(self):
        rows = self._scan_exports()
        done = [r for r in rows if r["kind"] == "done"]
        # Only the app's own folder may be cleaned up; extra folders belong to
        # the user and are read-only here.
        reclaimable = [r for r in rows
                       if not r["external"] and r["kind"] in ("partial", "stale", "failed")]
        return {
            "rows": rows,
            "total_mb": round(sum(r["mb"] for r in rows), 1),
            "done": len(done),
            "reclaim_mb": round(sum(r["mb"] for r in reclaimable), 1),
            "reclaim": len(reclaimable),
        }

    def delete_exports(self, names, only_reclaimable=False):
        removed, failed, reclaim = [], [], 0.0
        for name in names or []:
            safe = Path(str(name)).name
            target = OUTPUT / safe
            if not target.is_dir():
                failed.append({"name": safe, "error": "目录不存在"})
                continue
            if only_reclaimable and not (safe.endswith(".work")
                                         or safe.startswith("_stale-")
                                         or not next(target.glob("*.epub"), None)):
                continue
            try:
                size = sum(os.path.getsize(os.path.join(r, f))
                           for r, _d, fs in os.walk(target) for f in fs)
            except OSError:
                size = 0
            try:
                shutil.rmtree(str(target))
                removed.append(safe)
                reclaim += size / 1048576.0
            except Exception as exc:
                failed.append({"name": safe, "error": str(exc)})
        self._emit("storage_changed", self.storage())
        return {"ok": True, "removed": removed, "failed": failed,
                "reclaim_mb": round(reclaim, 1)}

    def cleanup_exports(self):
        rows = self._scan_exports()
        victims = [r["name"] for r in rows
                   if not r["external"] and r["kind"] in ("partial", "stale", "failed")]
        return self.delete_exports(victims)

    # -- extra source folders / EPUB export ----------------------------
    def sources(self):
        extra = library.load_sources()
        return {"ok": True, "output": str(OUTPUT),
                "extra": [str(path) for path in extra],
                "roots": [str(path) for path in library.scan_roots()]}

    def _pick_folder(self) -> str:
        """Ask Windows for a folder; empty string when the user cancels."""
        try:
            import webview as _webview
            if self.window is None:
                return ""
            picked = self.window.create_file_dialog(_webview.FOLDER_DIALOG,
                                                    allow_multiple=False)
        except Exception:
            return ""
        if not picked:
            return ""
        if isinstance(picked, (list, tuple)):
            return str(picked[0]) if picked else ""
        return str(picked)

    def pick_folder(self):
        path = self._pick_folder()
        return {"ok": bool(path), "path": path,
                "error": "" if path else "没有选择文件夹"}

    def add_source(self, path=None):
        target = path or self._pick_folder()
        if not target:
            return {"ok": False, "error": "没有选择文件夹"}
        ok, error = library.add_source(target)
        if not ok:
            return {"ok": False, "error": error}
        sources = self.sources()
        self._emit("sources_changed", sources)
        self._emit("storage_changed", self.storage())
        return dict(sources, added=str(target))

    def remove_source(self, path):
        ok, error = library.remove_source(path)
        if not ok:
            return {"ok": False, "error": error}
        sources = self.sources()
        self._emit("sources_changed", sources)
        self._emit("storage_changed", self.storage())
        return sources

    def open_export(self, key):
        """Open one export folder in Explorer (direct children of a root only)."""
        try:
            folder = Path(str(key or "")).resolve()
        except (TypeError, ValueError, OSError):
            return {"ok": False, "error": "路径无效"}
        allowed = False
        for root in library.scan_roots():
            try:
                if folder.parent == root.resolve() and folder.is_dir():
                    allowed = True
                    break
            except OSError:
                continue
        if not allowed:
            return {"ok": False, "error": "只能打开扫描目录下的文件夹"}
        try:
            os.startfile(str(folder))
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def preview_export(self, keys, pattern=None):
        """Show what the first few names will look like, using cached metadata.

        No network here on purpose: a preview must answer instantly. The real
        export fills in a missing publisher; the UI says so.
        """
        rows = {row["key"]: row for row in self._scan_exports()}
        chosen = [rows[str(key)] for key in (keys or []) if str(key) in rows]
        template = (pattern or "").strip() or library.DEFAULT_PATTERN
        names = []
        for row in chosen[:3]:
            epub = Path(row["epub"]) if row.get("epub") else Path(row["key"])
            values = self._rename_values(row, epub)
            names.append(library.render_pattern(template, values) + ".epub")
        return {"ok": True, "names": names, "total": len(chosen)}

    def _rename_values(self, row, epub: Path) -> dict:
        """What the rename pattern can use for one book."""
        book_id = row.get("bookId") or library.book_id_from_name(row["name"])
        meta = library.meta_for(book_id)
        fallback = row["name"][:-5] if row["name"].endswith(".work") else row["name"]
        if book_id and fallback.endswith("-" + book_id):
            fallback = fallback[: -(len(book_id) + 1)]
        values = {
            "bookId": book_id,
            "title": meta.get("title") or fallback,
            "author": meta.get("author") or "",
            "publisher": meta.get("publisher") or "",
            "category": meta.get("category") or "",
            "date": time.strftime("%Y-%m-%d", time.localtime(row.get("mtime") or 0)),
        }
        values["epub"] = str(epub)
        return values

    def _fill_missing_meta(self, values: dict) -> dict:
        """Fetch title/author/publisher through the worker when the cache lacks them.

        Only done when the pattern actually asks for a field we do not have, so
        a simple `{title}` rename never waits on the network.
        """
        if values.get("publisher") and values.get("category"):
            return values
        book_id = values.get("bookId")
        if not book_id:
            return values
        holder = {}

        def on_line(line):
            info = parse_book_info_message(line)
            if info is not None:
                holder.update(info)

        try:
            self._job += 1
            self.service.request(self._job, "book-info", {"bookId": book_id}, on_line)
        except Exception:
            return values
        if not holder:
            return values
        library.remember_meta(book_id, **{key: holder.get(key)
                                         for key in ("title", "author",
                                                     "publisher", "category")})
        for key in ("title", "author", "publisher", "category"):
            if not values.get(key) and holder.get(key):
                values[key] = holder[key]
        return values

    def export_epubs(self, keys, target, pattern=None, on_conflict="skip"):
        """Copy the chosen finished EPUBs into one folder under new names."""
        if not isinstance(keys, (list, tuple)) or not keys:
            return {"ok": False, "error": "先在列表里勾选要导出的书"}
        chosen = []
        rows = {row["key"]: row for row in self._scan_exports()}
        for key in keys:
            row = rows.get(str(key))
            if row is not None:
                chosen.append(row)
        if not chosen:
            return {"ok": False, "error": "选中的条目已经不在磁盘上，刷新一下列表"}
        try:
            destination = Path(str(target or "")).expanduser()
        except (TypeError, ValueError):
            return {"ok": False, "error": "目标文件夹无效"}
        if not str(destination).strip():
            return {"ok": False, "error": "请选择目标文件夹"}
        try:
            destination.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return {"ok": False, "error": "无法创建目标文件夹：%s" % exc}
        if not os.access(str(destination), os.W_OK):
            return {"ok": False, "error": "目标文件夹不可写"}

        pattern = (pattern or "").strip() or library.DEFAULT_PATTERN
        conflict = on_conflict if on_conflict in ("skip", "overwrite", "suffix") else "skip"
        threading.Thread(target=self._run_export,
                         args=(chosen, destination, pattern, conflict),
                         daemon=True).start()
        return {"ok": True, "queued": len(chosen)}

    def _run_export(self, rows, destination: Path, pattern: str,
                    on_conflict: str) -> None:
        total = len(rows)
        exported = skipped = failed = 0
        self._emit("export_start", {"total": total, "target": str(destination)})
        entries = []
        for row in rows:
            epub = Path(row["epub"]) if row.get("epub") else library.find_epub(Path(row["key"]))
            if epub is None or not epub.is_file():
                skipped += 1
                self._emit("export_progress", {
                    "done": exported + skipped + failed, "total": total,
                    "name": row["name"], "state": "skip",
                    "detail": "没有成品 EPUB"})
                continue
            values = self._rename_values(row, epub)
            if "{publisher}" in pattern or "{category}" in pattern:
                values = self._fill_missing_meta(values)
            entries.append({"key": row["key"], "epub": str(epub),
                            "values": values})
        taken = {item.name for item in destination.glob("*.epub")}
        plans = library.plan_names(entries, pattern, taken, on_conflict)
        for plan in plans:
            label = plan["values"].get("title") or Path(plan["epub"]).stem
            if plan["action"] == "skip":
                skipped += 1
                self._emit("export_progress", {
                    "done": exported + skipped + failed, "total": total,
                    "name": label, "state": "skip", "detail": "同名文件已存在"})
                continue
            try:
                shutil.copy2(plan["epub"], destination / plan["target"])
            except Exception as exc:
                failed += 1
                self._emit("export_progress", {
                    "done": exported + skipped + failed, "total": total,
                    "name": label, "state": "error", "detail": str(exc)})
                continue
            exported += 1
            self._emit("export_progress", {
                "done": exported + skipped + failed, "total": total,
                "name": plan["target"], "state": "ok", "detail": ""})
        self._emit("export_done", {
            "total": total, "exported": exported, "skipped": skipped,
            "failed": failed, "target": str(destination)})
        self._status("导出完成：%d 本已复制，%d 本跳过，%d 本失败。"
                     % (exported, skipped, failed),
                     "ok" if not failed else "error")

    # -- QR login ------------------------------------------------------
    def login_begin(self):
        if self._login_thread is not None and self._login_thread.is_alive():
            return {"ok": False, "error": "登录流程已在进行"}
        self._login_cancel.clear()
        self._otp_ready.clear()
        self._pending_otp = ""
        self._emit("login", {"state": "starting"})

        def run():
            session = requests.Session()
            try:
                uid, jar = login.begin_login(session)
                image = login.render_qr_png(login.confirm_url(uid),
                                            box_size=6, border=3)
                buffer = io.BytesIO()
                image.save(buffer, format="PNG")
                data_url = "data:image/png;base64," + base64.b64encode(
                    buffer.getvalue()).decode("ascii")
            except Exception as exc:
                self._emit("login", {"state": "error",
                                     "message": str(exc) or type(exc).__name__})
                return
            self._login_session, self._login_jar, self._login_uid = session, jar, uid
            self._emit("login", {"state": "qr", "image": data_url,
                                 "url": login.confirm_url(uid)})
            deadline = time.monotonic() + login.LOGIN_SESSION_TIMEOUT
            while time.monotonic() < deadline and not self._login_cancel.is_set():
                otp = ""
                try:
                    result = login.poll_login(session, jar, uid, otp)
                except login.LoginPending:
                    continue
                except login.LoginError as exc:
                    self._emit("login", {"state": "error", "message": str(exc)})
                    return
                state, detail = login.interpret_poll(result)
                if state == "ok":
                    try:
                        credentials = login.complete_login(session, jar, result)
                        login.save_credentials(credentials)
                    except Exception as exc:
                        self._emit("login", {"state": "error",
                                             "message": str(exc)})
                        return
                    self._emit("login", {"state": "done",
                                         "message": "登录成功。"})
                    self._emit("auth", {"ok": True, "detail": "登录成功"})
                    self._status("登录成功。", "ok")
                    return
                if state in ("otp", "otp_expired"):
                    if state == "otp_expired":
                        self._emit("login", {"state": "expired",
                                             "message": detail})
                        return
                    self._emit("login", {"state": "otp", "message": detail})
                    self._otp_ready.clear()
                    if not self._otp_ready.wait(timeout=max(
                            1, deadline - time.monotonic())):
                        break
                    otp = self._pending_otp
                    try:
                        result = login.poll_login(session, jar, uid, otp)
                    except login.LoginPending:
                        continue
                    except login.LoginError as exc:
                        self._emit("login", {"state": "error",
                                             "message": str(exc)})
                        return
                    state, detail = login.interpret_poll(result)
                    if state == "ok":
                        try:
                            credentials = login.complete_login(session, jar, result)
                            login.save_credentials(credentials)
                        except Exception as exc:
                            self._emit("login", {"state": "error",
                                                 "message": str(exc)})
                            return
                        self._emit("login", {"state": "done",
                                             "message": "登录成功。"})
                        self._emit("auth", {"ok": True, "detail": "登录成功"})
                        return
                    self._emit("login", {"state": "error", "message": detail})
                    return
                self._emit("login", {"state": "error", "message": detail})
                return
            self._emit("login", {"state": "expired",
                                 "message": "二维码已过期，请重新获取。"})

        self._login_thread = threading.Thread(target=run, daemon=True)
        self._login_thread.start()
        return {"ok": True}

    def login_otp(self, code):
        self._pending_otp = str(code or "").strip()
        if not self._pending_otp:
            return {"ok": False, "error": "请输入验证码"}
        self._otp_ready.set()
        return {"ok": True}

    def login_cancel(self):
        self._login_cancel.set()
        self._otp_ready.set()
        return {"ok": True}

    def logout(self):
        try:
            login.clear_credentials()
        except Exception:
            pass
        self._emit("auth", {"ok": False, "detail": "已退出登录"})
        return {"ok": True}

    def shutdown(self):
        self._closing = True
        self._save_queue()
        self._login_cancel.set()
        self._otp_ready.set()
        try:
            self.service.shutdown()
        except Exception:
            pass
        with self._workers_lock:
            processes = list(self._workers)
            self._workers.clear()
        for process in processes:
            try:
                process.kill()
            except Exception:
                pass


def _webui_index() -> Path:
    """Locate index.html in source runs and in the frozen one-dir bundle."""
    candidates = [
        Path(__file__).with_name("webui") / "index.html",
        Path(getattr(sys, "_MEIPASS", "")) / "weread" / "webui" / "index.html",
        Path(sys.executable).resolve().parent / "_internal" / "weread" / "webui" / "index.html",
        Path(sys.executable).resolve().parent / "weread" / "webui" / "index.html",
    ]
    for candidate in candidates:
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return candidates[0]


def _boot_log(message: str) -> None:
    """Startup breadcrumbs for the windowed (console-less) build."""
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with (LOG_DIR / "boot.log").open("a", encoding="utf-8") as handle:
            handle.write("%s %s\n" % (time.strftime("%H:%M:%S"), message))
    except Exception:
        pass


def _pid_alive(pid: int) -> bool:
    """True when a process with this pid still exists.

    Used to avoid deleting the profile of a second instance that is genuinely
    running; when the answer is unknown we assume alive and keep the folder.
    """
    if pid <= 0:
        return False
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        kernel32.CloseHandle(handle)
        return True
    except Exception:
        return True


def _webview_profile() -> Path:
    """A fresh WebView2 profile per run.

    WebView2 keeps an exclusive handle on its user-data folder. When an earlier
    run was force-killed, its msedgewebview2 helpers can outlive it; a new
    instance that reuses the same folder then silently gets no pywebview
    bridge -- the window opens, the page renders, and every button does
    nothing. Login cookies are kept by weread_login (cookies.json), not by the
    browser profile, so a new folder per run costs only a few hundred ms.

    The name carries a random suffix on purpose: `run-<pid>` collides whenever
    Windows hands out a reused pid, and the collision lands exactly on a folder
    the orphaned helpers still hold. Folders left behind by dead runs are
    removed here; a folder owned by a live process is left alone.
    """
    base = core.PRIVATE / "webview"
    try:
        base.mkdir(parents=True, exist_ok=True)
        for old in base.iterdir():
            if not old.is_dir() or not old.name.startswith("run-"):
                continue
            parts = old.name.split("-")
            owner = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
            if owner and owner != os.getpid() and _pid_alive(owner):
                continue
            shutil.rmtree(old, ignore_errors=True)
        profile = base / ("run-%d-%s" % (os.getpid(), uuid.uuid4().hex[:6]))
        profile.mkdir(parents=True, exist_ok=True)
        return profile
    except OSError:
        import tempfile
        return Path(tempfile.mkdtemp(prefix="weread-webview-"))


def _watch_readiness(api) -> None:
    """Record the case the page never reports: that is the dead-button state."""

    def run():
        deadline = time.time() + 25
        while time.time() < deadline:
            if getattr(api, "_ready_at", None):
                return
            time.sleep(0.5)
        _boot_log("page never reported ready in 25s (bridge likely empty)")

    threading.Thread(target=run, daemon=True).start()


def main():
    _boot_log("frozen=%s exe=%s file=%s" % (getattr(core, "FROZEN", "?"),
                                            sys.executable, __file__))
    try:
        api = WebApi()
    except Exception:
        _boot_log("WebApi failed:\n" + traceback.format_exc())
        raise
    methods = [name for name in dir(api) if not name.startswith("_")
               and callable(getattr(api, name, None))]
    _boot_log("api methods=%d %s" % (len(methods), ",".join(sorted(methods)[:4])))
    index = _webui_index()
    _boot_log("index=%s exists=%s" % (index, index.is_file()))
    try:
        window = webview.create_window(
            APP_TITLE, url=index.as_uri(), js_api=_JsApiFacade(api),
            width=1240, height=820, min_size=(1000, 640),
            background_color="#0B1322", text_select=False)
    except Exception:
        _boot_log("create_window failed:\n" + traceback.format_exc())
        raise
    api.bind(window)
    _boot_log("window created")
    _watch_readiness(api)
    profile = _webview_profile()
    _boot_log("profile=%s" % profile)
    try:
        webview.start(gui="edgechromium", private_mode=False,
                      storage_path=str(profile))
    except Exception:
        _boot_log("start failed:\n" + traceback.format_exc())
        raise
    finally:
        _boot_log("start returned")
        api.shutdown()
        shutil.rmtree(profile, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
