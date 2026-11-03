/* WebView renderer: draws state from web_app.py and calls its API. */
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const esc = (value) => String(value ?? "");

const State = {
  page: "workshop",
  books: new Map(),
  order: [],
  checked: new Set(),
  selected: null,
  detail: null,
  shelf: [],
  shelfChecked: new Set(),
  exports: [],
  storageRows: [],
  exportChecked: new Set(),
  sources: { output: "", extra: [] },
  exportTarget: "",
  exportRunning: false,
  queue: { queue: [], running: false, paused: false, current: null },
  busy: false,
  auth: false,
  annotation: "注释和想法",
  historyPage: 0,
  historyPages: 1,
  shelfLoaded: false,
  exportsLoaded: false,
};

function api(name, ...args) {
  if (!window.pywebview || !window.pywebview.api) {
    return Promise.resolve({ ok: false, error: "界面桥接尚未就绪" });
  }
  /* In a frozen bundle pywebview exposes the api *object* a moment before it
     injects the methods, so calling one throws a TypeError synchronously and
     the whole startup chain dies. Treat that as "not ready yet". */
  const fn = window.pywebview.api[name];
  if (typeof fn !== "function") {
    return Promise.resolve({ ok: false, error: "界面桥接尚未就绪" });
  }
  try {
    return fn(...args).catch((error) => ({
      ok: false, error: String(error && error.message || error),
    }));
  } catch (error) {
    return Promise.resolve({ ok: false, error: String(error) });
  }
}

function toast(message, kind = "") {
  const node = $("#toast");
  node.textContent = message;
  node.className = "toast " + kind;
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => node.classList.add("hidden"), 3200);
}

function setStatus(text, kind = "info") {
  $("#status-text").textContent = text;
  const dot = $("#status-dot");
  dot.className = "status-dot" + (kind === "ok" ? " ok" :
    kind === "error" ? " error" : kind === "busy" ? " busy" : "");
  if (kind === "error") toast(text, "error");
}

function setBusy(busy) {
  State.busy = !!busy;
  $("#search-btn").disabled = !!busy;
  $("#download-btn").disabled = !!busy || !State.selected;
  $("#queue-start").disabled = !!busy || !State.queue.queue.length;
}

function showPage(name) {
  State.page = name;
  $$(".nav-item").forEach((node) =>
    node.classList.toggle("active", node.dataset.page === name));
  $$(".page").forEach((node) =>
    node.classList.toggle("active", node.dataset.page === name));
  if (name === "shelf" && !State.shelfLoaded) loadShelf();
  if (name === "shelf" && State.exportsLoaded === false) loadExports();
  if (name === "history") loadHistory(State.historyPage);
  if (name === "storage") loadStorage();
  if (name === "settings") fillSettings();
}

/* ---------------- results ---------------- */

/* A cover box keeps its placeholder text and drops the real image on top.
   The <img> has to enter the DOM immediately: with loading="lazy" a detached
   image never starts its request, so onload never fires and the image that
   would have been appended by onload is never appended -- covers stayed
   blank forever. */
function fillCover(box, url, label) {
  box.innerHTML = "";
  box.classList.remove("has-image");
  const placeholder = document.createElement("span");
  placeholder.className = "cover-ph";
  placeholder.textContent = label;
  box.append(placeholder);
  if (!url) return;
  const img = document.createElement("img");
  img.alt = "";
  img.decoding = "async";
  img.src = url;
  img.addEventListener("load", () => box.classList.add("has-image"));
  img.addEventListener("error", () => { img.remove(); placeholder.textContent = "暂无封面"; });
  box.append(img);
}

function coverNode(url, label = "封面", cls = "result-cover") {
  const box = document.createElement("div");
  box.className = cls;
  fillCover(box, url, label);
  return box;
}

function resultCard(book) {
  const card = document.createElement("div");
  card.className = "result-card" + (State.selected === book.bookId ? " selected" : "");
  card.dataset.id = book.bookId;

  const check = document.createElement("button");
  check.className = "check" + (State.checked.has(book.bookId) ? " on" : "");
  check.textContent = "✓";
  check.title = "加入队列";
  check.addEventListener("click", (event) => {
    event.stopPropagation();
    if (State.checked.has(book.bookId)) State.checked.delete(book.bookId);
    else State.checked.add(book.bookId);
    check.classList.toggle("on", State.checked.has(book.bookId));
    refreshEnqueueButton();
  });

  const main = document.createElement("div");
  main.className = "result-main";
  const title = document.createElement("div");
  title.className = "result-title";
  title.textContent = book.title || book.bookId;
  const sub = document.createElement("div");
  sub.className = "result-sub";
  sub.textContent = [book.author, book.category, book.ratingText]
    .filter(Boolean).join(" · ") || "—";
  main.append(title, sub);

  const dl = document.createElement("button");
  dl.className = "result-dl";
  dl.textContent = "下载";
  dl.addEventListener("click", (event) => {
    event.stopPropagation();
    api("download", book).then(afterAction);
  });

  card.append(check, coverNode(book.cover), main, dl);
  card.addEventListener("click", () => selectBook(book));
  return card;
}

function renderResults() {
  const list = $("#result-list");
  list.innerHTML = "";
  if (!State.order.length) {
    list.innerHTML = `<div class="empty-state"><div class="empty-glyph">⌕</div>
      <p>在上面搜索一本书，结果会出现在这里。</p></div>`;
    $("#result-count").textContent = "0 本";
    return;
  }
  const fragment = document.createDocumentFragment();
  State.order.forEach((id) => {
    const book = State.books.get(id);
    if (book) fragment.append(resultCard(book));
  });
  list.append(fragment);
  $("#result-count").textContent = State.order.length + " 本";
  $("#search-meta").textContent = `找到 ${State.order.length} 本书 · 点击查看详情`;
}

function refreshEnqueueButton() {
  const button = $("#enqueue-btn");
  const count = State.checked.size;
  button.textContent = `加入队列 ${count} 本`;
  button.disabled = count === 0;
}

function selectBook(book) {
  State.selected = book.bookId;
  State.detail = { ...book };
  $$(".result-card").forEach((node) =>
    node.classList.toggle("selected", node.dataset.id === book.bookId));
  $("#detail-empty").classList.add("hidden");
  $("#detail-body").classList.remove("hidden");
  renderDetail(State.detail);
  $("#download-btn").disabled = State.busy;
  api("book_info", book.bookId).then((result) => {
    if (!result || result.ok === false) toast(result.error || "详情读取失败", "error");
  });
}

function renderDetail(book) {
  $("#detail-title").textContent = book.title || "暂无书名";
  $("#detail-author").textContent = "作者：" + (book.author || "暂无信息");
  $("#detail-category").textContent = book.category || "";
  $("#detail-rating").textContent = book.ratingText || "";
  $("#detail-id").textContent = "书籍 ID：" + (book.bookId || "—");

  const box = $("#detail-cover");
  fillCover(box, book.cover, book.cover ? "封面" : "暂无封面");
  $("#detail-intro").textContent = book.intro || "暂无简介。";
}

/* ---------------- search ---------------- */

function startSearch() {
  const keyword = $("#search-input").value.trim();
  if (!keyword) {
    setStatus("请先在搜索框输入书名或关键词。", "error");
    return;
  }
  State.books.clear();
  State.order = [];
  State.checked.clear();
  State.selected = null;
  State.detail = null;
  renderResults();
  refreshEnqueueButton();
  $("#detail-body").classList.add("hidden");
  $("#detail-empty").classList.remove("hidden");
  $("#download-btn").disabled = true;
  setStatus("正在搜索。稍等片刻，结果会出现在左侧。", "busy");
  $("#search-meta").textContent = "正在搜索微信读书…";
  showPage("workshop");
  api("search", keyword).then((result) => {
    if (result && result.ok === false) setStatus(result.error, "error");
  });
}

/* ---------------- detail / download ---------------- */

function downloadSelected() {
  if (!State.selected) return;
  const book = State.books.get(State.selected) || State.detail;
  if (!book) return;
  api("download", book).then(afterAction);
}

function afterAction(result) {
  if (!result) return;
  if (result.ok === false) {
    setStatus(result.error || "操作没有开始", "error");
  }
}

/* ---------------- queue ---------------- */

function renderQueue() {
  const q = State.queue;
  const total = q.queue.length + (q.current && q.running && !q.paused ? 1 : 0);
  $("#queue-count").textContent = total;
  $("#queue-current").innerHTML = "";
  if (q.current && q.running && !q.paused) {
    const row = document.createElement("div");
    row.className = "running";
    row.innerHTML = `<b style="color:var(--green)">正在下载</b>
      <span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">
      ${esc(q.current.title || q.current.bookId)}</span>`;
    $("#queue-current").append(row);
  }
  const list = $("#queue-list");
  list.innerHTML = "";
  if (!q.queue.length) {
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.style.height = "90px";
    empty.innerHTML = `<p>${q.current && q.running ? "队列里没有等待的书。" : "队列是空的。先勾选书再加入队列。"}</p>`;
    list.append(empty);
  }
  q.queue.forEach((book, index) => {
    const row = document.createElement("div");
    row.className = "queue-row";
    const idx = document.createElement("span");
    idx.className = "idx";
    idx.textContent = String(index + 1).padStart(2, "0");
    const title = document.createElement("span");
    title.className = "title";
    title.textContent = book.title || book.bookId;
    const remove = document.createElement("button");
    remove.className = "btn tiny";
    remove.textContent = "移除";
    remove.addEventListener("click", () => api("remove_queue", book.bookId).then((r) => {
      if (r && r.queue) applyQueue(r.queue);
    }));
    row.append(idx, title, remove);
    list.append(row);
  });
  const start = $("#queue-start");
  if (q.running) { start.textContent = "下载中…"; start.disabled = true; }
  else if (q.paused) { start.textContent = "继续队列"; start.disabled = false; }
  else { start.textContent = "开始队列下载"; start.disabled = State.busy || !q.queue.length; }
  $("#pause-btn").disabled = !(q.running || q.current);
  $("#queue-clear").disabled = q.running;
}

function applyQueue(payload) {
  if (!payload) return;
  State.queue = {
    queue: payload.queue || [],
    running: !!payload.running,
    paused: !!payload.paused,
    current: payload.current || null,
  };
  renderQueue();
}

function openQueue() {
  $("#queue-modal").classList.remove("hidden");
  renderQueue();
}

/* ---------------- shelf ---------------- */

function loadShelf() {
  setStatus("正在读取微信书架…", "busy");
  api("load_shelf").then(afterAction);
}

function shelfState(book) {
  if (book.download === "done") return "done";
  if (book.download === "partial") return "partial";
  return "";
}

function refreshShelfCount() {
  const done = State.shelf.filter((book) => book.download === "done").length;
  const partial = State.shelf.filter((book) => book.download === "partial").length;
  const parts = [`共 ${State.shelf.length} 本`];
  if (done) parts.push(`已下载 ${done}`);
  if (partial) parts.push(`未完成 ${partial}`);
  $("#shelf-count").textContent = parts.join(" · ");
}

function markShelfCard(bookId, state) {
  const card = document.querySelector(
    `.shelf-card[data-id="${String(bookId).replace(/"/g, '')}"]`);
  if (!card) return;
  card.classList.remove("done", "partial");
  if (state) card.classList.add(state);
  const sub = card.querySelector(".shelf-sub");
  if (!sub) return;
  sub.querySelectorAll(".shelf-badge").forEach((node) => node.remove());
  if (!state) return;
  const badge = document.createElement("span");
  badge.className = "shelf-badge " + state;
  badge.textContent = state === "done" ? "已下载" : "未完成";
  sub.append(badge);
}

function applyLocalState(index) {
  State.shelf.forEach((book) => {
    const next = index[String(book.bookId || "")] || "";
    if (book.download !== next) {
      book.download = next;
      markShelfCard(book.bookId, next);
    }
  });
  refreshShelfCount();
}

function renderShelf() {
  const grid = $("#shelf-grid");
  grid.innerHTML = "";
  refreshShelfCount();
  if (!State.shelf.length) {
    grid.innerHTML = `<div class="empty-state" style="grid-column:1/-1">
      <p>书架为空，或登录状态已过期。点右上角「刷新书架」重试。</p></div>`;
    return;
  }
  State.shelf.forEach((book) => {
    const state = shelfState(book);
    const card = document.createElement("div");
    card.className = "shelf-card" + (state ? " " + state : "");
    card.dataset.id = String(book.bookId || "");
    const check = document.createElement("button");
    check.className = "check" + (State.shelfChecked.has(book.bookId) ? " on" : "");
    check.textContent = "✓";
    check.addEventListener("click", (event) => {
      event.stopPropagation();
      if (State.shelfChecked.has(book.bookId)) State.shelfChecked.delete(book.bookId);
      else State.shelfChecked.add(book.bookId);
      check.classList.toggle("on", State.shelfChecked.has(book.bookId));
      refreshShelfQueue();
    });
    const badge = state === "done"
      ? '<span class="shelf-badge done">已下载</span>'
      : state === "partial" ? '<span class="shelf-badge partial">未完成</span>' : "";
    const main = document.createElement("div");
    main.innerHTML = `<div class="shelf-title">${esc(book.title || book.bookId)}</div>
      <div class="shelf-sub"><span class="shelf-author">${esc(book.author || "")}</span>${badge}</div>`;
    card.append(coverNode(book.cover, "书", "result-cover"), main, check);
    card.addEventListener("click", () => {
      const item = { ...book, category: book.category || "", ratingText: "" };
      State.books.set(book.bookId, item);
      selectBook(item);
      showPage("workshop");
    });
    grid.append(card);
  });
  refreshShelfQueue();
}

function refreshShelfQueue() {
  const count = State.shelfChecked.size;
  $("#shelf-enqueue").textContent = `加入队列 ${count} 本`;
  $("#shelf-enqueue").disabled = count === 0;
}

function loadExports() {
  api("storage").then((data) => {
    if (!data) return;
    State.exports = data.rows || [];
    State.exportsLoaded = true;
    renderExports();
    renderStorage(data);
  });
  loadSources();
}

/* ---------------- extra source folders ---------------- */

function loadSources() {
  api("sources").then((data) => {
    if (!data || data.ok === false) return;
    State.sources = { output: data.output || "", extra: data.extra || [] };
    renderSources();
  });
}

function renderSources() {
  const strip = $("#sources-strip");
  if (!strip) return;
  strip.innerHTML = "";
  const label = document.createElement("span");
  label.className = "sources-label";
  label.textContent = "扫描目录";
  strip.append(label);
  const home = document.createElement("span");
  home.className = "source-chip home";
  home.textContent = "默认";
  home.title = State.sources.output;
  strip.append(home);
  (State.sources.extra || []).forEach((path) => {
    const chip = document.createElement("span");
    chip.className = "source-chip";
    chip.textContent = path.split(/[\\/]/).filter(Boolean).pop() || path;
    chip.title = path;
    const drop = document.createElement("button");
    drop.className = "chip-x";
    drop.textContent = "×";
    drop.title = "移出扫描列表（不会删除文件）";
    drop.addEventListener("click", () => {
      api("remove_source", path).then((result) => {
        if (result && result.ok === false) toast(result.error, "error");
        else { loadSources(); State.exportsLoaded = false; loadExports(); }
      });
    });
    chip.append(drop);
    strip.append(chip);
  });
  const add = document.createElement("button");
  add.className = "btn tiny";
  add.id = "sources-add";
  add.textContent = "附加目录…";
  add.title = "把旧版或其他位置的导出文件夹加进来，同样能被识别和导出";
  add.addEventListener("click", () => {
    api("add_source").then((result) => {
      if (!result || result.ok === false) {
        if (result && result.error && result.error !== "没有选择文件夹") {
          toast(result.error, "error");
        }
        return;
      }
      toast("已加入扫描目录。", "ok");
      loadSources();
      State.exportsLoaded = false;
      loadExports();
    });
  });
  strip.append(add);
}

function renderExports() {
  const list = $("#export-list");
  list.innerHTML = "";
  $("#exports-count").textContent = `共 ${State.exports.length} 项`;
  if (!State.exports.length) {
    list.innerHTML = `<div class="empty-state"><p>本机还没有导出的书。</p></div>`;
    return;
  }
  State.exports.forEach((row) => list.append(exportRow(row, "exports")));
  refreshExportDelete();
}

function exportRow(row, scope) {
  const key = row.key || row.name;
  const node = document.createElement("div");
  node.className = "export-row" + (row.external ? " external" : "");
  const check = document.createElement("button");
  check.className = "check" + (State.exportChecked.has(key) ? " on" : "");
  check.textContent = "✓";
  check.addEventListener("click", () => {
    if (State.exportChecked.has(key)) State.exportChecked.delete(key);
    else State.exportChecked.add(key);
    check.classList.toggle("on", State.exportChecked.has(key));
    refreshExportDelete();
  });
  const name = document.createElement("div");
  name.className = "export-name";
  name.textContent = row.name;
  if (row.external) {
    const tag = document.createElement("span");
    tag.className = "source-tag";
    tag.textContent = "外部";
    tag.title = row.root || "";
    name.append(tag);
  }
  const kind = document.createElement("span");
  kind.className = "kind " + row.kind;
  kind.textContent = { done: "成品", failed: "未完成", partial: "半成品", stale: "残留" }[row.kind] || row.kind;
  const size = document.createElement("div");
  size.className = "export-size";
  size.textContent = `${row.mb} MB`;
  const open = document.createElement("button");
  open.className = "btn tiny";
  open.textContent = "打开";
  open.addEventListener("click", () => api("open_export", key));
  const parts = [check, name, kind, size, open];
  if (!row.external) {
    const del = document.createElement("button");
    del.className = "btn tiny";
    del.textContent = "删除";
    del.addEventListener("click", () => {
      if (!confirm(`删除「${row.name}」？文件会被永久移除。`)) return;
      api("delete_exports", [row.name]).then((result) => {
        if (result && result.ok) {
          toast(`已删除 ${result.removed.length} 项，回收约 ${result.reclaim_mb} MB。`, "ok");
          State.exportChecked.delete(key);
          State.exportsLoaded = false;
          loadExports();
        } else toast(result.error || "删除失败", "error");
      });
    });
    parts.push(del);
  }
  node.append(...parts);
  return node;
}

function findExportRow(key) {
  const all = (State.exports || []).concat(State.storageRows || []);
  return all.find((item) => (item.key || item.name) === key) || null;
}

function refreshExportDelete() {
  const keys = [...State.exportChecked];
  const internal = keys.filter((key) => {
    const row = findExportRow(key);
    return row && !row.external;
  });
  const del = $("#exports-delete");
  del.textContent = `删除选中 ${internal.length} 项`;
  del.disabled = internal.length === 0;
  const exp = $("#exports-export");
  if (exp) {
    exp.textContent = `导出 EPUB ${keys.length} 本`;
    exp.disabled = keys.length === 0 || State.exportRunning;
  }
}

/* ---------------- export to a folder ---------------- */

function checkedExportKeys() {
  return [...State.exportChecked];
}

function openExportModal() {
  const keys = checkedExportKeys();
  if (!keys.length) {
    toast("先在列表里勾选要导出的书。", "error");
    return;
  }
  if (!State.exportTarget) {
    const fallback = (State.boot && State.boot.paths && State.boot.paths.export_default) || "";
    if (fallback) State.exportTarget = fallback;
  }
  $("#export-target").value = State.exportTarget || "";
  $("#export-progress").classList.add("hidden");
  $("#export-modal").classList.remove("hidden");
  updateExportPreview();
}

function updateExportPreview() {
  const keys = checkedExportKeys();
  const pattern = $("#export-pattern").value;
  if (!keys.length) {
    $("#export-preview").textContent = "示例：—";
    return;
  }
  api("preview_export", keys, pattern).then((result) => {
    if (!result || result.ok === false || !result.names || !result.names.length) {
      $("#export-preview").textContent = "示例：—";
      return;
    }
    $("#export-preview").textContent = "示例：" + result.names.join("  ·  ") +
      "（按已缓存的书名/作者生成，导出时会补取出版社）";
  });
}

function pickExportTarget() {
  api("pick_folder").then((result) => {
    if (result && result.path) {
      State.exportTarget = result.path;
      $("#export-target").value = result.path;
    } else if (result && result.error && result.error !== "没有选择文件夹") {
      toast(result.error, "error");
    }
  });
}

function startExport() {
  const keys = checkedExportKeys();
  if (!keys.length) {
    toast("没有勾选任何书。", "error");
    return;
  }
  if (!State.exportTarget) {
    toast("先选择目标文件夹。", "error");
    return;
  }
  const pattern = $("#export-pattern").value.trim() || "{title}-{author}-{publisher}";
  const conflict = $("#export-conflict").value;
  State.exportRunning = true;
  refreshExportDelete();
  $("#export-start").disabled = true;
  $("#export-progress").classList.remove("hidden");
  $("#export-bar").style.width = "0%";
  $("#export-progress-text").textContent = "正在导出…";
  api("export_epubs", keys, State.exportTarget, pattern, conflict).then((result) => {
    if (!result || result.ok === false) {
      State.exportRunning = false;
      $("#export-start").disabled = false;
      refreshExportDelete();
      toast((result && result.error) || "导出失败", "error");
    }
  });
}

/* ---------------- history / storage ---------------- */

function loadHistory(page) {
  api("history", page).then((data) => {
    if (!data) return;
    State.historyPage = data.page;
    State.historyPages = data.pages;
    const list = $("#history-list");
    list.innerHTML = "";
    if (!data.rows.length) {
      list.innerHTML = `<div class="empty-state"><p>还没有下载记录。</p></div>`;
    } else {
      data.rows.forEach(([stamp, line]) => {
        const row = document.createElement("div");
        row.className = "history-row";
        const a = document.createElement("div");
        a.className = "stamp";
        a.textContent = stamp;
        const b = document.createElement("div");
        b.className = "line";
        b.textContent = line;
        row.append(a, b);
        list.append(row);
      });
    }
    $("#history-page").textContent =
      `第 ${data.page + 1} / ${data.pages} 页 · 共 ${data.total} 条`;
    $("#history-prev").disabled = data.page <= 0;
    $("#history-next").disabled = data.page + 1 >= data.pages;
    $("#history-pager").classList.toggle("hidden", data.pages <= 1);
  });
}

function loadStorage() {
  api("storage").then((data) => {
    if (!data) return;
    State.exports = data.rows || [];
    renderStorage(data);
  });
}

function renderStorage(data) {
  State.storageRows = (data && data.rows) || [];
  const row = $("#stat-row");
  row.innerHTML = "";
  const stats = [
    ["导出总占用", `${data.total_mb} MB`, ""],
    ["成品书", `${data.done} 本`, "green"],
    ["可回收", `${data.reclaim_mb} MB`, "gold"],
    ["可清理项", `${data.reclaim} 项`, ""],
  ];
  stats.forEach(([label, value, cls]) => {
    const card = document.createElement("div");
    card.className = "stat-card";
    card.innerHTML = `<div class="label">${label}</div>
      <div class="value ${cls}">${esc(value)}</div>`;
    row.append(card);
  });
  const list = $("#storage-list");
  list.innerHTML = "";
  if (!State.exports.length) {
    list.innerHTML = `<div class="empty-state"><p>还没有导出的书。</p></div>`;
    return;
  }
  State.exports.forEach((item) => list.append(exportRow(item, "storage")));
}

/* ---------------- settings / auth ---------------- */

function fillSettings() {
  const boot = State.boot || {};
  $("#path-output").textContent = boot.paths?.output || "—";
  $("#path-logs").textContent = boot.paths?.logs || "—";
  $("#settings-version").textContent = boot.version || "—";
  $("#settings-auth").textContent = State.auth ? "已登录" : "未登录 / 已过期";
  $("#settings-auth").style.color = State.auth ? "var(--green)" : "var(--red)";
}

function updateAuth(ok) {
  State.auth = !!ok;
  const chip = $("#auth-chip");
  chip.textContent = ok ? "已登录" : "未登录";
  chip.className = "chip " + (ok ? "ok" : "bad");
  fillSettings();
}

function openLogin() {
  $("#login-modal").classList.remove("hidden");
  $("#login-qr").style.display = "none";
  $("#qr-placeholder").style.display = "block";
  $("#qr-placeholder").textContent = "正在获取二维码…";
  $("#login-status").textContent = "请用手机微信读书「扫一扫」。";
  $("#otp-row").classList.add("hidden");
  api("login_begin").then((result) => {
    if (result && result.ok === false) toast(result.error, "error");
  });
}

/* ---------------- events ---------------- */

/* The page pulls events instead of letting Python push them. pywebview's
   evaluate_js blocks on a semaphore released from the UI thread with no
   timeout, so a push loop can hang the window for good. Pulling keeps every
   call on the JS side and Python only ever answers. */
let pumpStarted = false;

async function pumpEvents() {
  try {
    const result = await api("poll_events", 80);
    if (result && result.ok !== false) {
      (result.events || []).forEach((item) => {
        try {
          window.WR.event(item);
        } catch (error) {
          console.error("event failed", item && item.name, error);
        }
      });
    }
  } catch (error) {
    console.error("poll failed", error);
  } finally {
    setTimeout(pumpEvents, 120);
  }
}

function startEventPump() {
  if (pumpStarted) return;
  pumpStarted = true;
  pumpEvents();
}

window.WR = {
  event(payload) {
    const { name, data } = payload || {};
    switch (name) {
      case "status":
        setStatus(data.text, data.kind);
        break;
      case "busy":
        setBusy(data.busy);
        break;
      case "search_start":
        break;
      case "search_row": {
        State.books.set(data.bookId, data);
        State.order.push(data.bookId);
        renderResults();
        break;
      }
      case "search_meta": {
        const book = State.books.get(data.bookId);
        if (book && data.category) book.category = data.category;
        const node = $(`.result-card[data-id="${CSS.escape(data.bookId)}"] .result-sub`);
        if (node) {
          node.textContent = [book?.author, data.category].filter(Boolean).join(" · ");
        }
        break;
      }
      case "search_done":
        if (!State.order.length) {
          setStatus("搜索没有结果，换个关键词试试。", "error");
        } else {
          setStatus(`找到 ${State.order.length} 本书。`, "ok");
        }
        break;
      case "book_info": {
        const id = data.bookId || State.selected;
        const merged = { ...(State.books.get(id) || {}), ...data };
        State.books.set(id, merged);
        if (id === State.selected) {
          State.detail = merged;
          renderDetail(merged);
        }
        break;
      }
      case "probe":
        setStatus(data.ok ? "第一章可读，可以开始下载。" : (data.detail || "第一章检查失败"),
          data.ok ? "ok" : "error");
        break;
      case "book_info_failed":
        setStatus("详情读取失败，仍可尝试下载。", "error");
        break;
      case "auth":
        updateAuth(data.ok);
        break;
      case "shelf":
        State.shelf = data.rows || [];
        State.shelfLoaded = true;
        renderShelf();
        break;
      case "queue":
        applyQueue(data);
        break;
      case "download_start":
        setStatus(`正在准备《${data.title || data.bookId}》…`, "busy");
        break;
      case "progress":
        $("#progress-fill").style.width = `${data.percent}%`;
        setStatus(`正在下载 ${data.current}/${data.total} 个章节…`, "busy");
        break;
      case "download_done": {
        const finished = (data && data.book) || {};
        const id = String(finished.bookId || "");
        const row = State.shelf.find((item) => String(item.bookId) === id);
        if (row) {
          row.download = "done";
          markShelfCard(id, "done");
          refreshShelfCount();
        }
        break;
      }
      case "download_failed":
        setStatus(data.error || "下载未完成", "error");
        break;
      case "download_end":
        if (data.code === 0) {
          setStatus(`《${data.book?.title || data.book?.bookId || ""}》下载完成。`, "ok");
          State.exportsLoaded = false;
        }
        break;
      case "log":
        appendLog(data.line);
        break;
      case "sources_changed":
        State.sources = { output: data.output || "", extra: data.extra || [] };
        renderSources();
        break;
      case "export_start":
        State.exportRunning = true;
        $("#export-progress").classList.remove("hidden");
        $("#export-bar").style.width = "0%";
        $("#export-progress-text").textContent = `准备导出 ${data.total} 本…`;
        break;
      case "export_progress": {
        const total = data.total || 1;
        const percent = Math.round((data.done / total) * 100);
        $("#export-bar").style.width = percent + "%";
        const mark = data.state === "skip" ? "跳过" : data.state === "error" ? "失败" : "已导出";
        $("#export-progress-text").textContent =
          `${data.done}/${total} · ${mark} ${data.name}` + (data.detail ? `（${data.detail}）` : "");
        break;
      }
      case "export_done":
        State.exportRunning = false;
        $("#export-start").disabled = false;
        refreshExportDelete();
        $("#export-bar").style.width = "100%";
        $("#export-progress-text").textContent =
          `完成：${data.exported} 本已复制，${data.skipped} 本跳过，${data.failed} 本失败 → ${data.target}`;
        toast(`导出完成：${data.exported} 本。`, data.failed ? "error" : "ok");
        State.exportsLoaded = false;
        loadExports();
        break;
      case "storage_changed":
        State.exports = data.rows || [];
        State.exportsLoaded = true;
        renderExports();
        renderStorage(data);
        /* A delete can change what counts as "already downloaded"; refresh the
           marks from the disk index instead of re-reading the whole shelf. */
        if (State.shelf.length) {
          api("local_state").then((result) => {
            if (result && result.ok && result.index) applyLocalState(result.index);
          });
        }
        break;
      case "login":
        handleLogin(data);
        break;
      default:
        break;
    }
  },
};

function appendLog(line) {
  const drawer = $("#log-drawer");
  if (!drawer.classList.contains("open")) return;
  drawer.textContent += line + "\n";
  if (drawer.textContent.length > 12000) {
    drawer.textContent = drawer.textContent.slice(-10000);
  }
  drawer.scrollTop = drawer.scrollHeight;
}

function handleLogin(data) {
  if (data.state === "qr") {
    $("#login-qr").src = data.image;
    $("#login-qr").style.display = "block";
    $("#qr-placeholder").style.display = "none";
    $("#login-status").textContent = "请用手机微信读书「扫一扫」扫描二维码。";
    $("#otp-row").classList.add("hidden");
  } else if (data.state === "otp") {
    $("#login-status").textContent = data.message || "请输入手机上的 4 位验证码。";
    $("#otp-row").classList.remove("hidden");
    $("#otp-input").focus();
  } else if (data.state === "done") {
    $("#login-status").textContent = "登录成功。";
    toast("登录成功。", "ok");
    updateAuth(true);
    setTimeout(() => $("#login-modal").classList.add("hidden"), 900);
  } else if (data.state === "error" || data.state === "expired") {
    $("#login-status").textContent = data.message || "登录失败。";
    toast(data.message || "登录失败", "error");
  }
}

/* ---------------- wiring ---------------- */

function bindUi() {
  if (bindUi._done) return;
  bindUi._done = true;
  $("#nav").addEventListener("click", (event) => {
    const item = event.target.closest(".nav-item");
    if (item) showPage(item.dataset.page);
  });
  $$(".nav-item").forEach((item) => {
    if (item.dataset.page === "settings") {
      item.addEventListener("click", () => showPage("settings"));
    }
  });
  $("#search-btn").addEventListener("click", startSearch);
  $("#search-input").addEventListener("keydown", (event) => {
    if (event.key === "Enter") startSearch();
  });
  $("#queue-btn").addEventListener("click", openQueue);
  $("#auth-chip").addEventListener("click", () => api("check_auth").then(afterAction));
  $("#login-btn").addEventListener("click", openLogin);
  $("#download-btn").addEventListener("click", downloadSelected);
  $("#probe-btn").addEventListener("click", () => {
    if (!State.selected) { toast("先选择一本书", "error"); return; }
    api("probe", State.selected).then(afterAction);
  });
  $("#pause-btn").addEventListener("click", () => {
    const action = State.queue.paused ? "resume" : "pause";
    api(action).then(afterAction);
  });
  $("#annotation").addEventListener("change", (event) => {
    api("set_annotation", event.target.value).then((result) => {
      if (result && result.ok) toast("标注方式已切换：" + result.annotation, "ok");
    });
  });
  $("#logs-btn").addEventListener("click", () => {
    $("#log-drawer").classList.toggle("open");
  });
  $("#open-output-btn").addEventListener("click", () => api("open_path", "output"));
  $("#logout-btn").addEventListener("click", () => {
    api("logout").then(() => { updateAuth(false); toast("已退出登录", "ok"); });
  });

  $("#enqueue-btn").addEventListener("click", () => {
    const books = [...State.checked].map((id) => State.books.get(id)).filter(Boolean);
    api("enqueue", books).then((result) => {
      if (result && result.ok) {
        toast(`已加入队列 ${result.added} 本。`, "ok");
        setStatus(`已加入队列 ${result.added} 本，共 ${State.queue.queue.length} 本。`, "ok");
        applyQueue(result.queue);
      }
    });
  });

  $("#queue-start").addEventListener("click", () => {
    const action = State.queue.paused ? "resume" : "start_queue";
    api(action).then(afterAction);
  });
  $("#queue-clear").addEventListener("click", () => api("clear_queue").then(afterAction));
  $$("[data-close]").forEach((node) => node.addEventListener("click", () => {
    node.closest(".modal-backdrop").classList.add("hidden");
  }));
  $("#queue-modal").addEventListener("click", (event) => {
    if (event.target === $("#queue-modal")) $("#queue-modal").classList.add("hidden");
  });
  $("#login-modal").addEventListener("click", (event) => {
    if (event.target === $("#login-modal")) $("#login-modal").classList.add("hidden");
  });

  $("#shelf-refresh").addEventListener("click", () => {
    State.shelfLoaded = false;
    loadShelf();
  });
  $$(".tab").forEach((tab) => tab.addEventListener("click", () => {
    $$(".tab").forEach((node) => node.classList.toggle("active", node === tab));
    const wechat = tab.dataset.tab === "wechat";
    $("#shelf-wechat").classList.toggle("hidden", !wechat);
    $("#shelf-exports").classList.toggle("hidden", wechat);
    if (!wechat) loadExports();
  }));
  $("#shelf-select-all").addEventListener("click", () => {
    const all = State.shelf.every((b) => State.shelfChecked.has(b.bookId));
    State.shelfChecked.clear();
    if (!all) State.shelf.forEach((b) => State.shelfChecked.add(b.bookId));
    renderShelf();
  });
  $("#shelf-enqueue").addEventListener("click", () => {
    const books = State.shelf.filter((b) => State.shelfChecked.has(b.bookId));
    api("enqueue", books).then((result) => {
      if (result && result.ok) {
        toast(`已加入队列 ${result.added} 本。`, "ok");
        applyQueue(result.queue);
      }
    });
  });
  $("#exports-select-all").addEventListener("click", () => {
    const all = State.exports.every((r) => State.exportChecked.has(r.key || r.name));
    State.exportChecked.clear();
    if (!all) State.exports.forEach((r) => State.exportChecked.add(r.key || r.name));
    renderExports();
  });
  $("#exports-export").addEventListener("click", openExportModal);
  $("#export-pick").addEventListener("click", pickExportTarget);
  $("#export-pattern").addEventListener("input", updateExportPreview);
  $("#export-start").addEventListener("click", startExport);
  $("#exports-delete").addEventListener("click", () => {
    const names = [...State.exportChecked].map((key) => {
      const row = findExportRow(key);
      return row && !row.external ? row.name : "";
    }).filter(Boolean);
    if (!names.length) return;
    if (!confirm(`删除选中的 ${names.length} 项？文件会被永久移除。`)) return;
    api("delete_exports", names).then((result) => {
      if (result && result.ok) {
        toast(`已删除 ${result.removed.length} 项，回收约 ${result.reclaim_mb} MB。`, "ok");
        State.exportChecked.clear();
        State.exportsLoaded = false;
        loadExports();
      }
    });
  });
  $("#exports-clean").addEventListener("click", () => {
    if (!confirm("清除半成品与残留？已完成的 EPUB 全部保留。")) return;
    api("cleanup_exports").then((result) => {
      if (result && result.ok) {
        toast(`已清除，回收约 ${result.reclaim_mb} MB。`, "ok");
        State.exportsLoaded = false;
        loadExports();
      }
    });
  });
  $("#storage-refresh").addEventListener("click", loadStorage);
  $("#history-prev").addEventListener("click", () => loadHistory(State.historyPage - 1));
  $("#history-next").addEventListener("click", () => loadHistory(State.historyPage + 1));
  $("#settings-login").addEventListener("click", openLogin);
  $("#settings-logout").addEventListener("click", () => {
    api("logout").then(() => { updateAuth(false); });
  });
  $$("[data-open]").forEach((node) => node.addEventListener("click", () =>
    api("open_path", node.dataset.open)));
  $("#login-refresh").addEventListener("click", openLogin);
  $("#otp-submit").addEventListener("click", () => {
    const code = $("#otp-input").value.trim();
    api("login_otp", code).then((result) => {
      if (result && result.ok === false) toast(result.error, "error");
      $("#otp-row").classList.add("hidden");
    });
  });
  $("#otp-input").addEventListener("keydown", (event) => {
    if (event.key === "Enter") $("#otp-submit").click();
  });
}

async function boot() {
  if (boot._done) return;
  boot._done = true;
  bindUi();
  const info = await api("boot");
  /* In a frozen bundle the pywebview object can appear before its api methods
     are proxied, so the first call returns {ok:false}. Retry instead of
     leaving the version blank and the auth state unknown forever. */
  if (!info || info.ok === false) {
    boot._done = false;
    boot._tries = (boot._tries || 0) + 1;
    /* First launch of a freshly copied exe can take much longer than usual
       (antivirus scans the binary, WebView2 warms a new profile). Waiting a
       fixed 10s and then declaring the bridge dead produced a window whose
       buttons never worked. Poll often at first, then once a second, and keep
       waiting instead of giving up. */
    const delay = boot._tries < 40 ? 250 : 1000;
    if (boot._tries < 40) setStatus("界面初始化中…", "busy");
    else if (boot._tries === 40) {
      setStatus("界面初始化较慢，仍在等待…", "busy");
    }
    setTimeout(boot, delay);
    return;
  }
  State.boot = info || {};
  $("#rail-version").textContent = "v" + (info.version || "—");
  $("#settings-version").textContent = info.version || "—";
  const select = $("#annotation");
  select.innerHTML = "";
  (info.annotations || []).forEach((label) => {
    const option = document.createElement("option");
    option.value = label;
    option.textContent = label;
    option.selected = label === info.annotation;
    select.append(option);
  });
  State.annotation = info.annotation || State.annotation;
  $("#annotation-hint").textContent = {
    "注释和想法": "正文只留角标，注释与想法收进书末附录",
    "注释": "只在书末附注释",
    "想法": "只在书末附想法",
    "纯正文": "不要划线、注释和想法，最快",
  }[State.annotation] || "";
  updateAuth(!!(info.auth && info.auth.ready));
  applyQueue({ queue: info.queue || [], running: !!info.queue_running,
               paused: !!info.queue_paused });
  $("#status-meta").textContent = "v" + (info.version || "");
  fillSettings();
  if (!info.auth || !info.auth.ready) {
    setStatus("尚未登录。点右上角「登录 / 切换账号」扫码登录。", "error");
  } else if (info.queue_restored) {
    /* The queue outlives the window now; say so, and say it is paused. */
    setStatus(`已恢复上次队列 ${info.queue_restored} 本（已暂停），点「队列」里的继续下载接着跑。`,
      "info");
  } else {
    setStatus("准备就绪。", "ok");
  }
  startEventPump();
  api("report_ready", {
    keys: Object.keys(window.pywebview.api || {}).length,
    rail: $("#rail-version").textContent,
    elapsed: Math.round(performance.now()),
  });
}

window.addEventListener("pywebviewready", boot, { once: true });
/* The ready event can fire before app.js is parsed in a frozen bundle; poll
   the bridge instead of assuming the event will be caught. */
(function waitForBridge() {
  if (window.pywebview && window.pywebview.api) {
    boot();
  } else {
    setTimeout(waitForBridge, 150);
  }
})();
