"""Help ▸ Show Release Notes… -- every published release's notes, newest first.

The notes are the GitHub releases' own bodies: release.yml generates each one
(`generate_release_notes`) and the maintainer adds the Highlights and the
Evaluator/Parser sections before publishing. So nothing new has to be written
per release, and a bundled copy would always miss the release that ships it
(its notes are written after the build). Drafts, prereleases and releases
newer than the running app are skipped: these are the notes for what you
have, and Check for Updates is where newer releases are offered.

Fetched off the UI thread and cached on disk; when the fetch fails the cached
copy is shown instead, so the window still works offline once it has worked.

`notes_markdown()` is plain so the formatting tests without a network.
"""
import json
import re
import threading
from pathlib import Path
from urllib.request import Request, urlopen

RELEASES_API = "https://api.github.com/repos/BelfrySCAD/BelfrySCAD/releases"
# The raw releases, not the markdown: filtered per reader, so two installed
# versions sharing the cache each see only their own history.
_CACHE = Path.home() / ".cache" / "BelfrySCAD" / "releases.json"
_HEADING = re.compile(r"^(#{1,5})(?=\s)", re.MULTILINE)


def notes_markdown(releases: list[dict], current: str) -> str:
    """One `#` section per published release up to `current`, in the order
    given (the API's is newest first). Each body's own headings drop a level
    beneath it."""
    from belfryscad.window.update_check import parse_version

    parts = []
    for r in releases:
        if (r.get("draft") or r.get("prerelease")
                or parse_version(r.get("tag_name", "")) > parse_version(current)):
            continue
        date = (r.get("published_at") or "")[:10]
        title = r.get("tag_name", "").lstrip("vV") + (f" — {date}" if date else "")
        body = _HEADING.sub(r"#\1", (r.get("body") or "").replace("\r\n", "\n")).strip()
        parts.append(f"# {title}\n\n{body or '*No notes.*'}")
    return "\n\n---\n\n".join(parts)


def _fetch_releases() -> list[dict]:
    from belfryscad.window.library_manager import _SSL_CTX

    releases, page = [], 1
    while True:
        req = Request(f"{RELEASES_API}?per_page=100&page={page}",
                      headers={"Accept": "application/vnd.github+json"})
        with urlopen(req, timeout=15, context=_SSL_CTX) as resp:
            batch = json.loads(resp.read())
        releases += batch
        if len(batch) < 100:
            return releases
        page += 1


def show_release_notes(parent=None):
    """Open the window at once with a loading line; fill it when the fetch
    lands. Modeless and self-deleting."""
    from PySide6.QtCore import QObject, Qt, Signal
    from PySide6.QtWidgets import QDialog, QTextBrowser, QVBoxLayout

    dlg = QDialog(parent)
    dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    dlg.setWindowTitle("Release Notes")
    dlg.resize(760, 640)
    view = QTextBrowser(dlg)
    view.setOpenExternalLinks(True)
    view.setMarkdown("*Fetching release notes…*")
    QVBoxLayout(dlg).addWidget(view)

    class _Relay(QObject):
        finished = Signal(str)

    relay = _Relay(dlg)
    relay.finished.connect(view.setMarkdown, Qt.ConnectionType.QueuedConnection)

    from belfryscad.versions import package_version

    current = package_version("belfryscad", default="0")

    def work():
        try:
            releases = _fetch_releases()
            _CACHE.parent.mkdir(parents=True, exist_ok=True)
            _CACHE.write_text(json.dumps(releases), encoding="utf-8")
            md = notes_markdown(releases, current)
        except Exception as e:  # network, HTTP, bad JSON
            try:
                md = (f"*Could not fetch the latest notes ({e}); showing the copy from "
                      f"the last successful fetch.*\n\n"
                      + notes_markdown(json.loads(_CACHE.read_text(encoding="utf-8")), current))
            except Exception:
                md = f"**Could not fetch the release notes.**\n\n    {e}"
        try:
            relay.finished.emit(md)
        except RuntimeError:  # window closed before the fetch finished
            pass

    threading.Thread(target=work, daemon=True).start()
    dlg.show()
    return dlg
