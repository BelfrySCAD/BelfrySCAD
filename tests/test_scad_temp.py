"""Temp .scad lifecycle.

These land in the *user's own project directory*, not /tmp, because a
relative `use`/`include` inside them has to resolve. That makes anything
left behind visible clutter in someone's work, which is how this got
noticed: a stray `tmpnoz8y9v3.scad` sitting next to Dalek.scad.

Qt-free, so unlike the widget colour work this is a real pytest suite.
"""
import os
import time

import pytest

from belfryscad import scad_temp


@pytest.fixture
def script(tmp_path):
    p = tmp_path / "model.scad"
    p.write_text("cube(1);\n")
    return p


def test_temp_is_written_beside_the_script(script, tmp_path):
    # Not a detail: a relative use/include in the temp resolves against
    # whatever directory it sits in.
    path = scad_temp.write_temp_scad("cube(2);\n", near=script)
    try:
        assert os.path.dirname(path) == str(tmp_path)
        assert open(path).read() == "cube(2);\n"
    finally:
        scad_temp.remove(path)


def test_temp_without_a_script_goes_to_the_platform_temp_dir(tmp_path):
    path = scad_temp.write_temp_scad("cube(2);\n")
    try:
        assert os.path.dirname(path) != str(tmp_path)
    finally:
        scad_temp.remove(path)


def test_the_name_identifies_us(script):
    """A bare tmp*.scad is indistinguishable from a user's own file, so it
    can never be swept automatically. Ours can."""
    path = scad_temp.write_temp_scad("x\n", near=script)
    try:
        name = os.path.basename(path)
        assert name.startswith("belfryscad-") and name.endswith(".scad")
    finally:
        scad_temp.remove(path)


def test_remove_is_safe_on_none_and_on_an_already_gone_file(script):
    path = scad_temp.write_temp_scad("x\n", near=script)
    scad_temp.remove(path)
    scad_temp.remove(path)      # second time: already gone
    scad_temp.remove(None)


def _ours_in(directory):
    return [p for p in os.listdir(directory) if p.startswith("belfryscad-")]


def test_a_failed_write_leaves_nothing_behind(script, monkeypatch):
    """The window this closes: the file exists on disk from the moment it
    is created, so a write that raises must not strand it."""
    real = scad_temp.os.fdopen

    class _WriteFails:
        """Opens for real -- so the handle is genuinely closed on the way
        out -- and fails only at write. Windows will not unlink a file that
        still has an open handle, so a fake that skipped the close would
        test the OS rather than this module."""
        def __init__(self, fd, *a, **kw):
            self._f = real(fd, *a, **kw)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._f.close()
            return False

        def write(self, *_a):
            raise OSError("disk full")

    monkeypatch.setattr(scad_temp.os, "fdopen", _WriteFails)
    with pytest.raises(OSError):
        scad_temp.write_temp_scad("x\n", near=script)
    assert _ours_in(os.path.dirname(script)) == []


def test_a_failed_open_leaves_nothing_behind(script, monkeypatch):
    """If fdopen() fails the descriptor is still open and still ours. On
    Windows the file cannot be unlinked until it is closed, so failing to
    close it there strands the temp -- caught on CI, not locally."""
    def boom(fd, *_a, **_kw):
        raise OSError("out of handles")

    monkeypatch.setattr(scad_temp.os, "fdopen", boom)
    with pytest.raises(OSError):
        scad_temp.write_temp_scad("x\n", near=script)
    assert _ours_in(os.path.dirname(script)) == []


# --- the sweep --------------------------------------------------------
# try/finally covers every failure the process survives. It cannot cover
# SIGKILL or a segfault in the C++ evaluator, so orphans are swept on the
# next write into the same directory.
def _age(path, seconds):
    old = time.time() - seconds
    os.utime(path, (old, old))


def test_a_stale_orphan_is_swept(script):
    orphan = scad_temp.write_temp_scad("orphan\n", near=script)
    _age(orphan, 7200)
    fresh = scad_temp.write_temp_scad("new\n", near=script)
    try:
        assert not os.path.exists(orphan)
    finally:
        scad_temp.remove(fresh)


def test_a_concurrent_instances_live_temp_survives(script):
    """Two BelfrySCAD windows rendering out of one directory is ordinary,
    and deleting the other one's in-flight temp would break its render."""
    live = scad_temp.write_temp_scad("live\n", near=script)
    fresh = scad_temp.write_temp_scad("new\n", near=script)
    try:
        assert os.path.exists(live)
    finally:
        scad_temp.remove(live)
        scad_temp.remove(fresh)


def test_the_sweep_never_touches_a_file_that_is_not_ours(script, tmp_path):
    """Including one that merely looks temp-ish. This is exactly why the
    prefix had to change: a `tmp*.scad` glob could hit a user's own file."""
    decoy = tmp_path / "tmpABCDEF.scad"
    decoy.write_text("someone's actual work\n")
    _age(decoy, 7200)
    other = tmp_path / "notes.scad"
    other.write_text("also theirs\n")
    _age(other, 7200)

    fresh = scad_temp.write_temp_scad("new\n", near=script)
    try:
        assert decoy.exists()
        assert other.exists()
        assert script.exists()
    finally:
        scad_temp.remove(fresh)


def test_the_sweep_only_looks_in_the_one_directory(script, tmp_path):
    sibling = tmp_path.parent / "elsewhere"
    sibling.mkdir(exist_ok=True)
    stranger = sibling / "belfryscad-stale.scad"
    stranger.write_text("not our business\n")
    _age(stranger, 7200)
    fresh = scad_temp.write_temp_scad("new\n", near=script)
    try:
        assert stranger.exists()
    finally:
        scad_temp.remove(fresh)
        stranger.unlink()
