"""The Help menu's About box, and the version facts behind it.

Split from main_window so the facts can be tested without building a
window: `about_info()` is a plain dict of strings and `about_html()`
formats it, neither of which touches a widget.

Issue #379 asked for this first and foremost to answer "what version am I
running?" -- so the versions are the top of the dialog, and the ones that
matter for a bug report (the evaluator and its parser, which is where
geometry actually comes from) are listed beside the app's own.
"""
import platform

#: The wiki, as linked from the issue asking for this.
DOCS_URL = "https://github.com/BelfrySCAD/BelfrySCAD/wiki"
PROJECT_URL = "https://github.com/BelfrySCAD/BelfrySCAD"
ISSUES_URL = "https://github.com/BelfrySCAD/BelfrySCAD/issues"

#: Reported alongside BelfrySCAD's own version. openscad_cpp_evaluator is
#: the one that matters most in a bug report -- it is where the geometry
#: comes from, and it moves independently of the app.
_COMPONENTS = ("openscad_cpp_evaluator", "PySide6", "moderngl", "manifold3d", "numpy")


def _version_of(package: str) -> str:
    import importlib.metadata

    try:
        return importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        return "not installed"


def about_info() -> dict:
    """Everything the About box states, as plain strings.

    Read at call time rather than import time: a bundled app and a `pip
    install -e` checkout answer differently, and the whole point of the
    dialog is to say which one you are looking at.
    """
    return {
        "name": "BelfrySCAD",
        "version": _version_of("belfryscad"),
        "description": "Hybrid OpenSCAD + WYSIWYG procedural CAD system",
        "author": "Revar Desmera",
        "license": "MIT",
        "python": f"{platform.python_version()} ({platform.system()} {platform.release()})",
        "components": {name: _version_of(name) for name in _COMPONENTS},
    }


def about_html(info: dict | None = None) -> str:
    """The dialog's body. Links are real anchors so they can be clicked."""
    info = info or about_info()
    rows = "".join(
        f"<tr><td style='padding-right:12px'>{name}</td><td>{version}</td></tr>"
        for name, version in info["components"].items()
    )
    return (
        f"<h2 style='margin-bottom:2px'>{info['name']} {info['version']}</h2>"
        f"<p style='margin-top:0'>{info['description']}</p>"
        f"<p>{info['license']} licence &middot; {info['author']}</p>"
        f"<p><a href='{PROJECT_URL}'>Project</a> &middot; "
        f"<a href='{DOCS_URL}'>Documentation</a> &middot; "
        f"<a href='{ISSUES_URL}'>Report an issue</a></p>"
        f"<p style='margin-bottom:2px'><b>Components</b></p>"
        f"<table>{rows}"
        f"<tr><td style='padding-right:12px'>Python</td><td>{info['python']}</td></tr></table>"
    )


def about_text(info: dict | None = None) -> str:
    """The same facts as plain text, for copying into a bug report."""
    info = info or about_info()
    lines = [
        f"{info['name']} {info['version']}",
        f"{info['license']} licence, {info['author']}",
        f"Python {info['python']}",
    ]
    lines += [f"{name} {version}" for name, version in info["components"].items()]
    return "\n".join(lines)


def show_about_dialog(parent=None):
    """Open the About box. Modeless, so it can sit beside the window while
    a version is copied out of it."""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QDesktopServices
    from PySide6.QtWidgets import (
        QApplication, QDialog, QDialogButtonBox, QLabel, QVBoxLayout,
    )

    dlg = QDialog(parent)
    dlg.setWindowTitle("About BelfrySCAD")
    layout = QVBoxLayout(dlg)

    body = QLabel(about_html())
    body.setTextFormat(Qt.TextFormat.RichText)
    body.setTextInteractionFlags(
        Qt.TextInteractionFlag.TextBrowserInteraction | Qt.TextInteractionFlag.TextSelectableByMouse)
    body.setOpenExternalLinks(False)
    body.linkActivated.connect(lambda url: QDesktopServices.openUrl(url))
    layout.addWidget(body)

    # "Copy" rather than making the user retype a version into an issue --
    # which is what the request was actually about.
    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
    copy_btn = buttons.addButton("Copy Versions", QDialogButtonBox.ButtonRole.ActionRole)
    copy_btn.clicked.connect(lambda: QApplication.clipboard().setText(about_text()))
    buttons.rejected.connect(dlg.close)
    layout.addWidget(buttons)

    dlg.show()
    return dlg


def open_documentation():
    """Help ▸ Documentation -- the wiki, in the user's browser."""
    from PySide6.QtCore import QUrl
    from PySide6.QtGui import QDesktopServices

    QDesktopServices.openUrl(QUrl(DOCS_URL))
