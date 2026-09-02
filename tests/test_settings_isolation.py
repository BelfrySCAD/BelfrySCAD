"""`--testing` must not leave anything behind in the real settings store.

These exercise `belfryscad.settings` directly rather than launching the GUI --
the module is the whole mechanism, and the one thing worth pinning down is
that a redirected write cannot reach the user's real preferences.

Qt's own `QSettings.setDefaultFormat()` is deliberately NOT used by that
module: on macOS the two-argument `QSettings(org, app)` constructor keeps
resolving to the native plist even after the default format is changed
(`defaultFormat()` reports Ini while `fileName()` still ends in `.plist`), so
there is no global switch and the call sites have to route through
`app_settings()`. `test_every_settings_call_site_routes_through_app_settings`
is what keeps them there.
"""
import pathlib

import pytest
from PySide6.QtCore import QSettings

from belfryscad import settings as bs_settings


@pytest.fixture
def scratch(tmp_path):
    """Redirect settings for one test, then put the module back.

    `QSettings.setPath` is a global static, so leaving it set would leak into
    every later test in the same process.
    """
    before = bs_settings._scratch_dir
    yield bs_settings.use_scratch_settings(str(tmp_path))
    bs_settings._scratch_dir = before


def test_scratch_settings_carry_the_real_values_over(scratch):
    """Testing mode reads real settings -- an install with no recent files
    and default preferences is not the app under test."""
    real = QSettings(bs_settings.ORG, bs_settings.APP)
    keys = real.allKeys()
    scratch_settings = bs_settings.app_settings()

    assert sorted(scratch_settings.allKeys()) == sorted(keys)
    for key in list(keys)[:5]:
        assert str(scratch_settings.value(key)) == str(real.value(key))


def test_a_write_in_testing_mode_never_reaches_the_real_store(scratch, tmp_path):
    probe = "testing/pytest_canary"
    real_before = QSettings(bs_settings.ORG, bs_settings.APP)
    assert real_before.value(probe) is None, "a previous run leaked this key"
    n_before = len(real_before.allKeys())

    written = bs_settings.app_settings()
    written.setValue(probe, "should-not-persist")
    written.sync()
    assert bs_settings.app_settings().value(probe) == "should-not-persist"

    # The redirected file is the temp one, and the real store is untouched.
    assert str(tmp_path) in scratch
    assert pathlib.Path(scratch).exists()
    real_after = QSettings(bs_settings.ORG, bs_settings.APP)
    assert real_after.value(probe) is None
    assert len(real_after.allKeys()) == n_before


def test_app_settings_is_the_real_store_when_not_testing():
    assert bs_settings._scratch_dir is None
    assert bs_settings.app_settings().fileName() == \
        QSettings(bs_settings.ORG, bs_settings.APP).fileName()


def test_every_settings_call_site_routes_through_app_settings():
    """One stray `QSettings("BelfrySCAD", "BelfrySCAD")` silently opts that
    setting out of testing mode, and nothing else would catch it."""
    src = pathlib.Path(__file__).resolve().parent.parent / "src" / "belfryscad"
    offenders = []
    for path in src.rglob("*.py"):
        if path.name == "settings.py":
            continue          # defines ORG/APP; documents the constructor
        for n, line in enumerate(path.read_text().splitlines(), 1):
            if 'QSettings("BelfrySCAD"' in line and not line.lstrip().startswith("#"):
                offenders.append(f"{path.relative_to(src)}:{n}")
    assert not offenders, (
        "these bypass app_settings() and so ignore --testing: " + ", ".join(offenders))
