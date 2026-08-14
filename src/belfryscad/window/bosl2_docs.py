"""Lookup into the BOSL2 wiki at https://github.com/BelfrySCAD/BOSL2/wiki.

A GitHub wiki is a git repo of markdown files, and every page is served raw
at raw.githubusercontent.com/wiki/<owner>/<repo>/<Page>.md -- so the whole
wiki is readable as markdown without scraping the rendered HTML.

Two pages carry the indices everything else hangs off:
  AlphaIndex.md  every function/module/constant -> page, anchor, one-line
                 synopsis. This is the name -> URL map.
  Topics.md      topics -> the entries filed under them.
Both, and the per-file pages, are cached under ~/.cache for a week.

Qt-free by design, same as ai_tools: this runs on the AI worker thread.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.request import Request, urlopen

WIKI = "https://github.com/BelfrySCAD/BOSL2/wiki"
_RAW = "https://raw.githubusercontent.com/wiki/BelfrySCAD/BOSL2/{}.md"
_CACHE_DIR = Path.home() / ".cache" / "BelfrySCAD" / "bosl2-wiki"
_MAX_AGE = 7 * 24 * 3600
_TIMEOUT = 15

_KINDS = {"Func": "Function", "Mod": "Module",
          "Func/Mod": "Function/Module", "Const": "Constant"}


@dataclass(frozen=True)
class Entry:
    name: str        # cuboid, $slop
    kind: str        # Function, Module, Function/Module, Constant
    page: str        # shapes3d.scad
    anchor: str      # module-cuboid
    summary: str

    @property
    def url(self) -> str:
        return f"{WIKI}/{self.page}#{self.anchor}"


def _fetch(page: str) -> str:
    """The page's markdown, from the week-old-at-most disk cache or the
    wiki. A cached copy that is merely stale beats failing outright when
    the network is down."""
    cached = _CACHE_DIR / f"{page}.md"
    if cached.exists() and time.time() - cached.stat().st_mtime < _MAX_AGE:
        return cached.read_text(encoding="utf-8")
    try:
        req = Request(_RAW.format(page), headers={"User-Agent": "BelfrySCAD"})
        from belfryscad.window.library_manager import _SSL_CTX
        with urlopen(req, timeout=_TIMEOUT, context=_SSL_CTX) as resp:
            text = resp.read().decode("utf-8")
    except Exception:
        if cached.exists():
            return cached.read_text(encoding="utf-8")
        raise
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cached.write_text(text, encoding="utf-8")
    return text


# - [`cuboid()`](shapes3d.scad#module-cuboid) Mod – Creates a cube with ...
_INDEX_LINE = re.compile(
    r"^- \[`([^`]+)`\]\(([^)#]+)#([^)]+)\)\s+(\S+)\s+[–-]\s*(.*)$")
_index: dict[str, Entry] | None = None


def index() -> dict[str, Entry]:
    """Every documented name, lowercased, from the wiki's AlphaIndex page."""
    global _index
    if _index is None:
        entries = {}
        for line in _fetch("AlphaIndex").splitlines():
            m = _INDEX_LINE.match(line)
            if not m:
                continue
            name = m.group(1).replace("\\", "").rstrip("()")
            entries[name.lower()] = Entry(
                name=name, kind=_KINDS.get(m.group(4), m.group(4)),
                page=m.group(2), anchor=m.group(3),
                summary=_plain(m.group(5)))
        _index = entries
    return _index


def lookup(name: str) -> Entry | None:
    return index().get(name.strip().rstrip("()").lower())


def find(text: str, limit: int = 25) -> list[Entry]:
    """Entries whose name or synopsis mentions `text`, name matches first."""
    t = text.strip().lower()
    named = [e for k, e in index().items() if t in k]
    other = [e for k, e in index().items() if t not in k and t in e.summary.lower()]
    return (sorted(named, key=lambda e: (len(e.name), e.name))
            + sorted(other, key=lambda e: e.name))[:limit]


_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*$")


def _section(markdown: str, matches) -> str | None:
    """The chunk of `markdown` from the first heading `matches` accepts to
    the next heading at that level or above."""
    lines = markdown.splitlines()
    start = level = None
    for i, line in enumerate(lines):
        m = _HEADING.match(line)
        if not m:
            continue
        if start is None:
            if matches(m.group(2).replace("\\", "").strip()):
                start, level = i, len(m.group(1))
        elif len(m.group(1)) <= level:
            return "\n".join(lines[start:i]).strip()
    return "\n".join(lines[start:]).strip() if start is not None else None


def _plain(text: str) -> str:
    """Index synopses carry the docsgen [Geom]/[Trans] badges as raw HTML."""
    return re.sub(r"\s*<sup.*?</sup>", "", text).strip()


def entry_doc(entry: Entry) -> str:
    """The wiki's full write-up of one entry -- usage, description,
    arguments, examples."""
    title = entry.name.lower()
    body = _section(
        _fetch(entry.page),
        lambda h: ":" in h and h.split(":", 1)[1].strip().rstrip("()").lower() == title,
    )
    if body is None:
        body = f"(Section not found on the page; read it at {entry.url})"
    return f"{entry.url}\n\n{body}"


def topics() -> list[str]:
    return re.findall(r"^### (.+)$", _fetch("Topics"), re.M)


def topic_doc(topic: str) -> str | None:
    t = topic.strip().lower()
    body = _section(_fetch("Topics"), lambda h: h.lower() == t)
    if body is None:
        return None
    anchor = re.sub(r"[^a-z0-9]+", "-", t).strip("-")
    return f"{WIKI}/Topics#{anchor}\n\n{body}"


# 1. [Transforms Tutorial](Tutorial-Transforms)
_TUTORIAL_LINK = re.compile(r"\[([^\]]+)\]\((Tutorial-[^)]+)\)")


def tutorials() -> list[tuple[str, str]]:
    """(title, page) for every tutorial listed on the wiki's Tutorials page."""
    return _TUTORIAL_LINK.findall(_fetch("Tutorials"))


def tutorial_doc(name: str) -> str | None:
    n = name.strip().lower().replace(" ", "-")
    for title, page in tutorials():
        if n in title.lower() or n in page.lower():
            return f"{WIKI}/{page}\n\n{_fetch(page)}"
    return None
