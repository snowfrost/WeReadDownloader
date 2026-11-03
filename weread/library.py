"""Local library helpers for the export shelf.

Three jobs, all of them file-system work that the GUI should not do itself:

* remember extra folders the user downloaded into before this build existed,
  so those books can be marked "already downloaded" and re-exported;
* find the finished EPUB inside an export folder;
* turn a rename pattern such as ``{title}-{author}-{publisher}`` into a safe
  file name, dropping placeholders that have no value instead of leaving
  dangling separators behind.

Nothing here touches the network; metadata comes from a small cache that the
bridge fills from the shelf and from book details.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import weread_core as core

SOURCES_FILE = core.PRIVATE / "sources.json"
META_FILE = core.PRIVATE / "book-meta.json"

# Characters Windows refuses in a file name, plus control codes.
ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
SEPARATOR_RUN = re.compile(r"[-_\s·]{2,}")
BOOK_ID = re.compile(r"-(\d{5,})$")

DEFAULT_PATTERN = "{title}-{author}-{publisher}"

# Fields a rename pattern may use; the UI lists the same keys.
FIELDS = ("title", "author", "publisher", "category", "bookId", "date")


def _read_json(path: Path, fallback):
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return fallback
    try:
        data = json.loads(raw)
    except ValueError:
        return fallback
    return data if isinstance(data, type(fallback)) else fallback


def _write_json(path: Path, payload) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(path.name + ".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                        encoding="utf-8")
        os.replace(temp, path)
    except OSError:
        pass


# -- extra source folders -------------------------------------------------

def load_sources() -> list[Path]:
    """Extra folders to treat as export roots (beyond the default output)."""
    raw = _read_json(SOURCES_FILE, {})
    paths = []
    for item in raw.get("roots", []):
        try:
            path = Path(str(item))
        except (TypeError, ValueError):
            continue
        if path.is_dir():
            paths.append(path)
    return paths


def save_sources(paths: list[Path]) -> None:
    seen = []
    for path in paths:
        text = str(path)
        if text not in seen:
            seen.append(text)
    _write_json(SOURCES_FILE, {"roots": seen})


def add_source(path) -> tuple[bool, str]:
    """Register a folder; refuses the default output and duplicates."""
    try:
        folder = Path(str(path)).expanduser().resolve()
    except (TypeError, ValueError, OSError):
        return False, "路径无效"
    if not folder.is_dir():
        return False, "文件夹不存在"
    if folder.resolve() == core.OUTPUT.resolve():
        return False, "这就是默认导出目录，不用重复添加"
    roots = load_sources()
    if any(existing.resolve() == folder for existing in roots):
        return False, "这个文件夹已经在列表里了"
    roots.append(folder)
    save_sources(roots)
    return True, ""


def remove_source(path) -> tuple[bool, str]:
    try:
        folder = Path(str(path)).resolve()
    except (TypeError, ValueError, OSError):
        return False, "路径无效"
    roots = load_sources()
    kept = [item for item in roots if item.resolve() != folder]
    if len(kept) == len(roots):
        return False, "列表里没有这个文件夹"
    save_sources(kept)
    return True, ""


def scan_roots() -> list[Path]:
    """Default output first, then every extra folder."""
    roots = [core.OUTPUT]
    seen = {core.OUTPUT.resolve()}
    for path in load_sources():
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        roots.append(path)
    return roots


# -- metadata cache -------------------------------------------------------

def load_meta() -> dict:
    raw = _read_json(META_FILE, {})
    return raw if isinstance(raw, dict) else {}


def remember_meta(book_id, **fields) -> None:
    """Cache title/author/publisher/category so renames work offline."""
    key = str(book_id or "")
    if not key:
        return
    data = load_meta()
    entry = data.get(key, {})
    if not isinstance(entry, dict):
        entry = {}
    for name, value in fields.items():
        if value not in (None, ""):
            entry[name] = value
    if entry:
        data[key] = entry
        _write_json(META_FILE, data)


def meta_for(book_id) -> dict:
    entry = load_meta().get(str(book_id or ""), {})
    return entry if isinstance(entry, dict) else {}


# -- export folders -------------------------------------------------------

def book_id_from_name(name: str) -> str:
    stem = name[:-5] if name.endswith(".work") else name
    match = BOOK_ID.search(stem)
    return match.group(1) if match else ""


def folder_state(folder: Path) -> str:
    name = folder.name
    if name.endswith(".work"):
        return "partial"
    if name.startswith("_stale-"):
        return "partial"
    return "done" if find_epub(folder) is not None else "failed"


def find_epub(folder: Path) -> Path | None:
    """The finished book: the largest .epub at the folder root."""
    try:
        candidates = [item for item in folder.glob("*.epub") if item.is_file()]
    except OSError:
        return None
    if not candidates:
        return None
    return max(candidates, key=lambda item: item.stat().st_size)


def sanitize_name(text: str, limit: int = 120) -> str:
    cleaned = ILLEGAL.sub(" ", str(text or ""))
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    if len(cleaned) > limit:
        cleaned = cleaned[:limit].rstrip(" .")
    return cleaned


def render_pattern(pattern: str, values: dict) -> str:
    """Fill a rename pattern, dropping fields that have no value.

    `{title}-{author}-{publisher}` becomes `书名-作者` when the publisher is
    unknown: the placeholder disappears together with the separator around it,
    so no name ends with a dangling dash.
    """
    template = (pattern or "").strip() or DEFAULT_PATTERN
    text = template
    for field in FIELDS:
        value = sanitize_name(values.get(field, ""))
        text = text.replace("{%s}" % field, value)
    # Any placeholder we do not know about is not worth printing.
    text = re.sub(r"\{[a-zA-Z]+\}", "", text)
    text = SEPARATOR_RUN.sub("-", text)
    text = text.strip(" -_·")
    return sanitize_name(text) or sanitize_name(values.get("title") or "未命名")


def plan_names(entries: list[dict], pattern: str, existing: set[str],
               on_conflict: str = "skip") -> list[dict]:
    """Decide the target file name for every entry, in order.

    `entries` items carry `values` (for the pattern) and `epub` (Path).
    `existing` holds names already taken in the target folder; it is updated so
    two books with the same name inside one batch also get distinct names.
    """
    taken = set(existing)
    plans = []
    for entry in entries:
        base = render_pattern(pattern, entry.get("values") or {})
        name = base + ".epub"
        if name in taken:
            if on_conflict == "overwrite":
                plans.append(dict(entry, target=name, action="overwrite"))
                continue
            if on_conflict == "suffix":
                index = 2
                while True:
                    candidate = "%s (%d).epub" % (base, index)
                    if candidate not in taken:
                        name = candidate
                        break
                    index += 1
                taken.add(name)
                plans.append(dict(entry, target=name, action="copy"))
                continue
            plans.append(dict(entry, target=name, action="skip"))
            continue
        taken.add(name)
        plans.append(dict(entry, target=name, action="copy"))
    return plans
