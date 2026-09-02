"""One place every `QSettings` in the app comes from.

Qt's own `QSettings.setDefaultFormat()` looked like the zero-refactor hook for
`--testing`: flip the default format, point `setPath` at a temp directory, and
every existing two-argument `QSettings("BelfrySCAD", "BelfrySCAD")` follows.
It does not work on macOS -- measured, `defaultFormat()` duly reports
`IniFormat` while `fileName()` still ends in
`Library/Preferences/com.belfryscad.BelfrySCAD.plist`. The two-argument
constructor keeps resolving to the native store, so there is no global switch
to flip and the call sites have to route through something this project owns.

Hence this module: every settings read or write in the GUI goes through
`app_settings()`, so testing mode is one function call rather than a flag
threaded to two dozen places (and stays that way as call sites are added).
"""
from PySide6.QtCore import QSettings

ORG = "BelfrySCAD"
APP = "BelfrySCAD"

#: Set by `use_scratch_settings()`; None in normal operation.
_scratch_dir: str | None = None


def app_settings() -> QSettings:
    """The application's settings, honouring testing mode.

    Always a fresh object, matching how the call sites already used
    `QSettings(...)` directly -- QSettings is cheap to construct and reads
    through to the same backing store, so there is nothing to share.
    """
    if _scratch_dir is None:
        return QSettings(ORG, APP)
    return QSettings(QSettings.Format.IniFormat, QSettings.Scope.UserScope,
                     ORG, APP)


def use_scratch_settings(tmpdir: str, seed: bool = True) -> str:
    """Redirect `app_settings()` into `tmpdir` and return the file it will use.

    With `seed` (the `--testing` case) the current settings are copied in
    rather than starting from an empty slate: testing mode is for exercising
    the real app, and an install with no recent files, no configured AI
    provider and default preferences is not the app being tested. Reads
    therefore behave normally; only writes are discarded, along with `tmpdir`.

    `seed=False` gives a genuinely empty store, which is what a *test* wants --
    one asserting "no custom themes by default" has to not see the developer's
    own themes. Tests use this rather than monkeypatching `QSettings` inside
    some particular module: that is how `test_color_themes.py`'s fixture was
    written, and when the call sites moved behind `app_settings()` the patch
    silently stopped isolating anything and the suite started reading and
    writing the developer's real preferences. Going through the same switch
    the app uses means an isolation fixture cannot drift out of date again.
    Restore the previous value of `_scratch_dir` when done.
    """
    global _scratch_dir
    snapshot = {}
    if seed:
        real = QSettings(ORG, APP)
        for key in real.allKeys():
            snapshot[key] = real.value(key)

    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope,
                      tmpdir)
    _scratch_dir = tmpdir

    scratch = app_settings()
    for key, value in snapshot.items():
        scratch.setValue(key, value)
    scratch.sync()
    return scratch.fileName()
