"""The AppImage library fixes, on a fake AppDir."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from fix_appimage_libs import (DEPLOY_LIB, WHEEL_QT_LIB, dedupe,   # noqa: E402
                               drop_split_libraries)


def _appdir(tmp_path, wheel_libs, deployed_libs):
    appdir = tmp_path / "BelfrySCAD.AppDir"
    (appdir / WHEEL_QT_LIB).mkdir(parents=True)
    (appdir / DEPLOY_LIB).mkdir(parents=True)
    for name, body in wheel_libs.items():
        (appdir / WHEEL_QT_LIB / name).write_bytes(body)
    for name, body in deployed_libs.items():
        (appdir / DEPLOY_LIB / name).write_bytes(body)
    return appdir


def test_a_duplicated_library_becomes_a_symlink_to_the_deployed_copy(tmp_path):
    appdir = _appdir(tmp_path,
                     {"libQt6Core.so.6": b"x" * 1000},
                     {"libQt6Core.so.6": b"y" * 800})
    count, saved = dedupe(appdir)
    assert (count, saved) == (1, 1000)

    link = appdir / WHEEL_QT_LIB / "libQt6Core.so.6"
    assert link.is_symlink()
    # Resolves to linuxdeploy's copy, and is relative so it survives being
    # mounted anywhere.
    assert not os.path.isabs(os.readlink(link))
    assert link.resolve() == (appdir / DEPLOY_LIB / "libQt6Core.so.6").resolve()
    assert link.read_bytes() == b"y" * 800


def test_a_library_only_the_wheel_has_is_left_alone(tmp_path):
    appdir = _appdir(tmp_path, {"libQt6Unique.so.6": b"z" * 10}, {})
    assert dedupe(appdir) == (0, 0)
    assert not (appdir / WHEEL_QT_LIB / "libQt6Unique.so.6").is_symlink()


def test_rerunning_is_a_no_op(tmp_path):
    appdir = _appdir(tmp_path,
                     {"libQt6Core.so.6": b"x" * 1000},
                     {"libQt6Core.so.6": b"y" * 800})
    assert dedupe(appdir)[0] == 1
    # An already-linked file must not be counted again, nor turned into a
    # link to itself.
    assert dedupe(appdir) == (0, 0)
    assert (appdir / WHEEL_QT_LIB / "libQt6Core.so.6").read_bytes() == b"y" * 800


def test_a_moved_layout_reports_nothing_rather_than_crashing(tmp_path):
    empty = tmp_path / "BelfrySCAD.AppDir"
    empty.mkdir()
    assert dedupe(empty) == (0, 0)


def test_the_bundled_libxkbcommon_is_dropped(tmp_path):
    """Its -x11 companion is not bundled, so pairing a bundled core half with
    the host's current -x11 half segfaults Qt at startup (#430)."""
    appdir = _appdir(tmp_path, {}, {"libxkbcommon.so.0": b"old", "libQt6Core.so.6": b"q"})
    removed = drop_split_libraries(appdir)
    assert removed == [f"{DEPLOY_LIB}/libxkbcommon.so.0"]
    assert not (appdir / DEPLOY_LIB / "libxkbcommon.so.0").exists()
    # Nothing else is touched.
    assert (appdir / DEPLOY_LIB / "libQt6Core.so.6").exists()


def test_glib_is_left_alone(tmp_path):
    """Bundled as a complete set, so no half comes from the host."""
    appdir = _appdir(tmp_path, {}, {"libglib-2.0.so.0": b"g", "libgio-2.0.so.0": b"g"})
    assert drop_split_libraries(appdir) == []
    assert (appdir / DEPLOY_LIB / "libglib-2.0.so.0").exists()


def test_dropping_is_idempotent(tmp_path):
    appdir = _appdir(tmp_path, {}, {"libxkbcommon.so.0": b"old"})
    assert len(drop_split_libraries(appdir)) == 1
    assert drop_split_libraries(appdir) == []
