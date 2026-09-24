"""Help ▸ Check for Updates… -- is there a newer BelfrySCAD release?

Manual only: nothing here runs unless the user picks the menu item, so the
app never contacts GitHub on its own. `releases/latest` skips drafts and
prereleases, so an unpublished release is never offered.

`parse_version()` and `update_message()` are plain functions so the
verdict tests without a network or a widget.
"""
import json
import re
import sys
import threading
from urllib.request import Request, urlopen

from belfryscad.window.about import PROJECT_URL

LATEST_URL = "https://api.github.com/repos/BelfrySCAD/BelfrySCAD/releases/latest"
RELEASES_URL = PROJECT_URL + "/releases"


def parse_version(text: str) -> tuple[int, ...]:
    """'v1.45.0' -> (1, 45, 0). Stops at the first non-numeric part, so a
    local suffix ('1.45.0+dev') compares as its release."""
    m = re.match(r"[vV]?(\d+(?:\.\d+)*)", text.strip())
    return tuple(int(n) for n in m.group(1).split(".")) if m else ()


def update_message(current: str, latest_tag: str, platform: str = sys.platform) -> tuple[bool, str]:
    """(newer_available, text for the dialog)."""
    if parse_version(latest_tag) <= parse_version(current):
        return False, f"BelfrySCAD {current} is the latest version."
    latest = latest_tag.lstrip("vV")
    text = f"BelfrySCAD {latest} is available. You have {current}."
    if platform == "darwin":
        # No macOS installer is published (no signing certificate), so the
        # release page has nothing to download for a Mac user.
        text += "\n\nNo macOS installer is published; update your checkout and rebuild."
    return True, text


def _fetch_latest_tag() -> str:
    from belfryscad.window.library_manager import _SSL_CTX

    req = Request(LATEST_URL, headers={"Accept": "application/vnd.github+json"})
    with urlopen(req, timeout=10, context=_SSL_CTX) as resp:
        return json.loads(resp.read())["tag_name"]


def check_for_updates(parent=None):
    """Fetch off the UI thread, then report in a message box."""
    from PySide6.QtCore import QObject, Qt, QUrl, Signal
    from PySide6.QtGui import QDesktopServices
    from PySide6.QtWidgets import QMessageBox

    from belfryscad.versions import package_version

    class _Relay(QObject):
        finished = Signal(str, str)  # tag, error

    relay = _Relay(parent)
    current = package_version("belfryscad", default="0")

    def report(tag: str, error: str):
        relay.deleteLater()
        if error:
            QMessageBox.warning(parent, "Check for Updates",
                                f"Could not check for updates:\n{error}")
            return
        newer, text = update_message(current, tag)
        if not newer:
            QMessageBox.information(parent, "Check for Updates", text)
            return
        box = QMessageBox(QMessageBox.Icon.Information, "Check for Updates", text,
                          QMessageBox.StandardButton.Close, parent)
        view = box.addButton("View Release", QMessageBox.ButtonRole.AcceptRole)
        box.exec()
        if box.clickedButton() is view:
            QDesktopServices.openUrl(QUrl(f"{RELEASES_URL}/tag/{tag}"))

    relay.finished.connect(report, Qt.ConnectionType.QueuedConnection)

    def work():
        try:
            relay.finished.emit(_fetch_latest_tag(), "")
        except Exception as e:  # network, HTTP, bad JSON -- all just "couldn't check"
            relay.finished.emit("", str(e))

    threading.Thread(target=work, daemon=True).start()
