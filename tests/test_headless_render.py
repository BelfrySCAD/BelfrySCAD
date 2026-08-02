"""Tests for belfryscad.headless_render -- the -o out.png / --imgsize /
--view / --camera / --projection / --colorscheme / --autocenter /
--viewall / --animate CLI path.

Unlike belfryscad.headless's mesh export, this DOES touch Qt (QGuiApplication,
offscreen platform) and a real (standalone/headless) OpenGL context via
moderngl -- confirmed directly (multiple runs, alongside the full existing
test suite) that this does NOT crash pytest in this sandbox the way a real
windowed QWidget/QApplication does (see feedback_gl_qt_tests_crash_pytest
memory, which predates this finding -- QGuiApplication + a standalone
moderngl context is a different combination than what crashed before).
"""

import os
import struct
import zlib

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from belfryscad.headless_render import (
    _apply_camera, _parse_imgsize, _parse_projection, _parse_view,
    render_png, render_png_animation,
)


def _read_png(path):
    """Minimal reader matching belfryscad.png_writer.write_png's own
    output exactly (single IDAT, filter type 0 on every scanline, no
    interlacing) -- not a general PNG decoder."""
    data = open(path, "rb").read()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    pos = 8
    width = height = None
    idat = b""
    while pos < len(data):
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        tag = data[pos + 4:pos + 8]
        chunk = data[pos + 8:pos + 8 + length]
        if tag == b"IHDR":
            width, height = struct.unpack(">II", chunk[:8])
        elif tag == b"IDAT":
            idat += chunk
        pos += 8 + length + 4  # length + tag + data + crc
    raw = zlib.decompress(idat)
    stride = width * 4
    rows = []
    for y in range(height):
        row_start = y * (stride + 1)
        filter_type = raw[row_start]
        assert filter_type == 0
        rows.append(raw[row_start + 1:row_start + 1 + stride])
    return width, height, b"".join(rows)


def _pixel(width, height, pixels, x, y):
    stride = width * 4
    off = y * stride + x * 4
    return tuple(pixels[off:off + 4])


class TestParseHelpers:
    def test_imgsize_valid(self):
        assert _parse_imgsize("640,480") == (640, 480)

    def test_imgsize_invalid_format(self):
        with pytest.raises(ValueError):
            _parse_imgsize("640")

    def test_imgsize_non_positive(self):
        with pytest.raises(ValueError):
            _parse_imgsize("0,480")

    def test_view_valid(self):
        assert _parse_view("axes,wireframe") == {"axes", "wireframe"}

    def test_view_unknown_key(self):
        with pytest.raises(ValueError):
            _parse_view("axes,nonsense")

    @pytest.mark.parametrize("spec,expected", [
        ("o", True), ("ortho", True), ("orthographic", True),
        ("p", False), ("perspective", False),
    ])
    def test_projection(self, spec, expected):
        assert _parse_projection(spec) == expected

    def test_projection_invalid(self):
        with pytest.raises(ValueError):
            _parse_projection("nonsense")


class TestApplyCamera:
    class _FakeCamera:
        target = None
        elevation = None
        azimuth = None
        distance = None
        orthographic = False

    def test_translate_rot_dist_form(self):
        cam = self._FakeCamera()
        _apply_camera(cam, "0,0,0,0,0,0,50")
        assert cam.distance == 50
        assert list(cam.target) == [0, 0, 0]

    def test_eye_center_form(self):
        cam = self._FakeCamera()
        _apply_camera(cam, "10,0,0,0,0,0")  # eye on +X axis, looking at origin
        assert cam.distance == pytest.approx(10.0)
        assert cam.azimuth == pytest.approx(0.0, abs=1e-6)
        assert cam.elevation == pytest.approx(0.0, abs=1e-6)

    def test_eye_equals_center_fails(self):
        cam = self._FakeCamera()
        with pytest.raises(ValueError):
            _apply_camera(cam, "1,1,1,1,1,1")

    def test_wrong_value_count_fails(self):
        cam = self._FakeCamera()
        with pytest.raises(ValueError):
            _apply_camera(cam, "1,2,3")

    def test_non_numeric_fails(self):
        cam = self._FakeCamera()
        with pytest.raises(ValueError):
            _apply_camera(cam, "a,b,c,d,e,f,g")


class TestRenderPng:
    def test_basic_render_succeeds_with_requested_size(self, tmp_path):
        src = tmp_path / "in.scad"
        src.write_text("cube([10, 5, 3]);\n")
        out = tmp_path / "out.png"
        code = render_png(str(src), str(out), imgsize="200,150")
        assert code == 0
        w, h, _pixels = _read_png(out)
        assert (w, h) == (200, 150)

    def test_colorscheme_changes_background(self, tmp_path):
        src = tmp_path / "in.scad"
        src.write_text("cube(1);\n")
        out_default = tmp_path / "default.png"
        out_themed = tmp_path / "themed.png"
        assert render_png(str(src), str(out_default), imgsize="50,50") == 0
        assert render_png(str(src), str(out_themed), imgsize="50,50", colorscheme="Nature") == 0
        w, h, default_px = _read_png(out_default)
        _, _, themed_px = _read_png(out_themed)
        # top-left corner pixel is background in both -- different theme, different color
        assert _pixel(w, h, default_px, 0, 0) != _pixel(w, h, themed_px, 0, 0)

    def test_unknown_colorscheme_fails(self, tmp_path):
        src = tmp_path / "in.scad"
        src.write_text("cube(1);\n")
        code = render_png(str(src), str(tmp_path / "out.png"), colorscheme="NotARealTheme")
        assert code == 1

    def test_invalid_imgsize_fails(self, tmp_path):
        src = tmp_path / "in.scad"
        src.write_text("cube(1);\n")
        code = render_png(str(src), str(tmp_path / "out.png"), imgsize="bad")
        assert code == 1

    def test_missing_input_file_fails(self, tmp_path):
        code = render_png(str(tmp_path / "nope.scad"), str(tmp_path / "out.png"))
        assert code == 1

    def test_summary_camera_and_geometry(self, tmp_path):
        src = tmp_path / "in.scad"
        src.write_text("cube([10, 5, 3]);\n")
        out = tmp_path / "out.png"
        summary_path = tmp_path / "summary.json"
        code = render_png(str(src), str(out), imgsize="50,50", summary="camera,geometry",
                           summary_file=str(summary_path))
        assert code == 0
        import json
        data = json.loads(summary_path.read_text())
        assert data["geometry"] == {"bodies": 1, "facets": 12, "vertices": 8}
        assert set(data["camera"]) == {"translate", "distance", "azimuth", "elevation"}

    def test_define_applies(self, tmp_path):
        src = tmp_path / "in.scad"
        src.write_text("x = 10;\ncube([x, x, x]);\n")
        out = tmp_path / "out.png"
        # Just confirm the define doesn't break the render pipeline --
        # pixel-level geometry verification is covered by test_headless.py
        # (mesh export) since STL vertex data is far easier to assert on
        # than rendered pixels.
        code = render_png(str(src), str(out), defines=["x=1"])
        assert code == 0


class TestRenderPngAnimation:
    def test_frame_filenames(self, tmp_path):
        src = tmp_path / "in.scad"
        src.write_text("cube(1);\n")
        out = tmp_path / "out.png"
        code = render_png_animation(str(src), str(out), 3, imgsize="50,50")
        assert code == 0
        names = sorted(p.name for p in tmp_path.glob("out*.png"))
        assert names == [f"out{i:05d}.png" for i in range(3)]

    def test_camera_refits_every_frame_not_just_first(self, tmp_path):
        # Regression: an earlier version fit the camera once from frame 0
        # and held it steady -- a model that orbits through space (common
        # animation pattern) drifted out of that fixed framing, leaving
        # middle frames blank. Confirmed directly against real
        # OpenSCAD.app that --viewall re-fits every frame, not just the
        # first. A cube that translates far from the origin partway
        # through the cycle must still be visible (non-background pixels
        # present) in every frame if the camera is correctly re-fit.
        src = tmp_path / "in.scad"
        src.write_text("translate([$t*100, 0, 0]) cube(2);\n")
        out = tmp_path / "out.png"
        code = render_png_animation(str(src), str(out), 4, imgsize="80,80", viewall=True)
        assert code == 0
        for i in range(4):
            w, h, pixels = _read_png(tmp_path / f"out{i:05d}.png")
            bg = _pixel(w, h, pixels, 0, 0)
            center = _pixel(w, h, pixels, w // 2, h // 2)
            assert center != bg, f"frame {i}: model not visible at center -- camera didn't track it"

    def test_animate_dir_routes_frames(self, tmp_path):
        src = tmp_path / "in.scad"
        src.write_text("cube(1);\n")
        out_dir = tmp_path / "out"
        frames_dir = tmp_path / "frames"
        code = render_png_animation(str(src), str(out_dir / "out.png"), 2,
                                     imgsize="50,50", animate_dir=str(frames_dir))
        assert code == 0
        assert sorted(p.name for p in frames_dir.glob("*.png")) == ["out00000.png", "out00001.png"]
        assert not out_dir.exists()

    def test_zero_steps_fails(self, tmp_path):
        src = tmp_path / "in.scad"
        src.write_text("cube(1);\n")
        code = render_png_animation(str(src), str(tmp_path / "out.png"), 0)
        assert code == 1
