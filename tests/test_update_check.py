"""Help ▸ Check for Updates… -- the verdict, without a network."""
from belfryscad.window.update_check import parse_version, update_message


def test_parse_version():
    assert parse_version("v1.45.0") == (1, 45, 0)
    assert parse_version("1.45.0+dev") == (1, 45, 0)
    assert parse_version("1.9.0") < parse_version("1.10.0")


def test_up_to_date_and_ahead_are_not_updates():
    assert update_message("1.45.0", "v1.45.0")[0] is False
    assert update_message("1.46.0", "v1.45.0")[0] is False


def test_newer_release_is_offered():
    newer, text = update_message("1.44.0", "v1.45.0", platform="linux")
    assert newer and "1.45.0 is available" in text and "macOS" not in text


def test_macos_is_told_there_is_no_installer():
    assert "No macOS installer" in update_message("1.44.0", "v1.45.0", platform="darwin")[1]


def test_startup_check_at_most_once_a_day_and_only_when_enabled():
    from belfryscad.window.update_check import STARTUP_INTERVAL, startup_check_due
    assert startup_check_due(True, 0.0, 1e9)                      # never checked
    assert not startup_check_due(True, 1e9, 1e9 + STARTUP_INTERVAL - 1)
    assert startup_check_due(True, 1e9, 1e9 + STARTUP_INTERVAL)
    assert not startup_check_due(False, 0.0, 1e9)                 # preference off
