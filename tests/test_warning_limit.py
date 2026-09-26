"""Preferences > Render > Stop rendering after is the one warning limit
(#566): the Design-menu toggle that duplicated it is gone, and its setting is
carried into the preference once."""
import pytest

from belfryscad.settings import app_settings, use_scratch_settings
from belfryscad.window.main_window import MainWindow


@pytest.fixture
def settings(tmp_path):
    use_scratch_settings(str(tmp_path), seed=False)
    return app_settings()


def _limit(settings):
    return MainWindow._warning_limit(None)


@pytest.mark.parametrize("stored,limit", [(0, None), (1, 1), (7, 7)])
def test_the_preference_alone_sets_the_limit(settings, stored, limit):
    settings.setValue("render/stopAfterWarnings", stored)
    settings.setValue("stopOnFirstWarning", True)  # the old toggle no longer counts
    assert _limit(settings) == limit


@pytest.mark.parametrize("toggle,pref,after", [(True, 0, 1), (True, 5, 5), (False, 0, 0)])
def test_the_old_toggle_is_migrated_once(settings, toggle, pref, after):
    settings.setValue("stopOnFirstWarning", toggle)
    settings.setValue("render/stopAfterWarnings", pref)
    MainWindow._migrate_stop_on_first_warning()
    assert settings.value("render/stopAfterWarnings", type=int) == after
    assert not settings.contains("stopOnFirstWarning")
