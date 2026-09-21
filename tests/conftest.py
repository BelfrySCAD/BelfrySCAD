"""One QApplication for the whole test session.

Several tests build Qt objects that need an application instance to exist
-- QFontInfo aborts the process outright without one, rather than raising.
Nothing created it deliberately; the suite only worked because some other
test file happened to construct one first, so running a single file on its
own could abort where the full run passed.

This was a QGuiApplication, on the reasoning that it is enough for fonts
and text layout and that "a test that genuinely needs widgets makes its own
QApplication (which is a subclass, so `instance()` still finds it)".

That advice killed the process. `QApplication.instance()` is really
`QCoreApplication.instance()`: it returns whatever application object
exists, NOT one of its own class. So the idiomatic guard

    app = QApplication.instance() or QApplication([])

found the session's QGuiApplication, short-circuited, never created the
QApplication, and the first QWidget constructed then aborted --
`Fatal Python error: Aborted`, SIGABRT, taking the whole pytest run with
it and leaving a macOS crash report behind. Being a subclass is exactly
what makes the guard fail: `instance()` finds the BASE-class object and
reports success.

A QApplication is a strict superset, so making the session app one costs
nothing and removes that trap: a plain widget test now works in-process.

Still not in-process: anything needing a GL context (QOpenGLWidget, the
viewport, MainWindow). Those belong in a subprocess driver -- see
test_render_reframe.py for the pattern the suite already uses, which 20
test files follow.
"""
import pytest


@pytest.fixture(scope="session", autouse=True)
def _qt_app():
    from PySide6.QtWidgets import QApplication
    # Deliberately NOT `QApplication.instance() or ...`: that is the very
    # bug described above. Check the type, not mere existence.
    app = QApplication.instance()
    if not isinstance(app, QApplication):
        app = QApplication([])
    yield app
