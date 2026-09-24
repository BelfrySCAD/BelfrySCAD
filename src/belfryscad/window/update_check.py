"""Help ▸ Check for Updates… -- is there a newer BelfrySCAD release?

Also run quietly at startup (`check_at_startup`), unless the preference
`app/checkForUpdates` is off: at most once a day, and it says nothing unless
a newer release exists that the user has not chosen to skip. A failed or
up-to-date startup check is silent. `releases/latest` skips drafts and
prereleases, so an unpublished release is never offered.

`parse_version()`, `update_message()` and `startup_check_due()` are plain
functions so the verdicts test without a network or a widget.
"""
import json
import re
import sys
import threading
import time
from urllib.request import Request, urlopen

from belfryscad.window.about import PROJECT_URL

LATEST_URL = "https://api.github.com/repos/BelfrySCAD/BelfrySCAD/releases/latest"
RELEASES_URL = PROJECT_URL + "/releases"

#: Seconds between startup checks. GitHub allows 60 unauthenticated API
#: calls an hour per address, so this is about politeness, not the limit.
STARTUP_INTERVAL = 24 * 60 * 60


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


def startup_check_due(enabled: bool, last_check: float, now: float) -> bool:
    return enabled and now - last_check >= STARTUP_INTERVAL


def _fetch_latest_tag() -> str:
    from belfryscad.window.library_manager import _SSL_CTX

    req = Request(LATEST_URL, headers={"Accept": "application/vnd.github+json"})
    with urlopen(req, timeout=10, context=_SSL_CTX) as resp:
        return json.loads(resp.read())["tag_name"]


def check_at_startup(parent=None):
    """The quiet, once-a-day version, called once per process from main."""
    from belfryscad.settings import app_settings

    s = app_settings()
    now = time.time()
    if not startup_check_due(s.value("app/checkForUpdates", True, type=bool),
                             s.value("app/lastUpdateCheck", 0.0, type=float), now):
        return
    s.setValue("app/lastUpdateCheck", now)
    check_for_updates(parent, quiet=True)


def check_for_updates(parent=None, quiet=False):
    """Fetch off the UI thread, then report in a message box. `quiet` says
    nothing unless there is a newer release the user has not skipped."""
    from PySide6.QtCore import QObject, Qt, QUrl, Signal
    from PySide6.QtGui import QDesktopServices
    from PySide6.QtWidgets import QMessageBox

    from belfryscad.versions import package_version

    class _Relay(QObject):
        finished = Signal(str, str)  # tag, error

    relay = _Relay(parent)
    current = package_version("belfryscad", default="0")

    def report(tag: str, error: str):
        from belfryscad.settings import app_settings

        relay.deleteLater()
        settings = app_settings()
        if quiet and (error or tag == settings.value("app/skippedUpdate", "")):
            return
        if error:
            QMessageBox.warning(parent, "Check for Updates",
                                f"Could not check for updates:\n{error}")
            return
        newer, text = update_message(current, tag)
        if not newer:
            if quiet:
                return
            QMessageBox.information(parent, "Check for Updates", text)
            return
        box = QMessageBox(QMessageBox.Icon.Information, "Check for Updates", text,
                          QMessageBox.StandardButton.Close, parent)
        view = box.addButton("View Release", QMessageBox.ButtonRole.AcceptRole)
        # Only offered when the app asked unprompted: skipping is how the
        # daily check stops repeating itself about a release you don't want.
        skip = box.addButton("Skip This Version", QMessageBox.ButtonRole.RejectRole) if quiet else None
        box.exec()
        if skip is not None and box.clickedButton() is skip:
            settings.setValue("app/skippedUpdate", tag)
        elif box.clickedButton() is view:
            QDesktopServices.openUrl(QUrl(f"{RELEASES_URL}/tag/{tag}"))

    relay.finished.connect(report, Qt.ConnectionType.QueuedConnection)

    def work():
        try:
            relay.finished.emit(_fetch_latest_tag(), "")
        except Exception as e:  # network, HTTP, bad JSON -- all just "couldn't check"
            relay.finished.emit("", str(e))

    threading.Thread(target=work, daemon=True).start()
