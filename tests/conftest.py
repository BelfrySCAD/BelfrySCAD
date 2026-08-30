"""One QGuiApplication for the whole test session.

Several tests build Qt objects that need an application instance to exist
-- QFontInfo aborts the process outright without one, rather than raising.
Nothing created it deliberately; the suite only worked because some other
test file happened to construct a QApplication first, so running a single
file on its own could abort where the full run passed.

QGuiApplication rather than QApplication: it is enough for fonts and text
layout, and a test that genuinely needs widgets makes its own QApplication
(which is a subclass, so `instance()` still finds it).
"""
import pytest


@pytest.fixture(scope="session", autouse=True)
def _qt_app():
    from PySide6.QtGui import QGuiApplication
    app = QGuiApplication.instance() or QGuiApplication([])
    yield app
