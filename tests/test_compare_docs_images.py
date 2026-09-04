"""The docs-image comparison tool's metric behaves as its docstring claims.

These build tiny images rather than rendering anything, so they run in
milliseconds and pin the two properties the metric exists for: blind to
lighting, near-blind to thin lines, sensitive to geometry.
"""
import importlib.util
import json
import pathlib

import numpy as np
import pytest
from PySide6.QtGui import QImage


def _tool():
    path = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "compare_docs_images.py"
    spec = importlib.util.spec_from_file_location("compare_docs_images", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _png(path, draw):
    """A 96x96 image on a cream background; `draw(arr)` paints into it."""
    arr = np.full((96, 96, 3), 255, dtype=np.uint8)
    arr[:, :, 2] = 229
    draw(arr)
    img = QImage(bytes(arr.tobytes()), 96, 96, 96 * 3, QImage.Format.Format_RGB888)
    assert img.save(str(path), "PNG")


def test_lighting_alone_does_not_count_as_a_difference(tmp_path):
    tool = _tool()
    a, b = tmp_path / "a.png", tmp_path / "b.png"
    # Same square, two very different shades -- what a lighting change does.
    _png(a, lambda x: x.__setitem__((slice(20, 76), slice(20, 76)), [200, 170, 40]))
    _png(b, lambda x: x.__setitem__((slice(20, 76), slice(20, 76)), [90, 75, 15]))
    assert tool.compare_pair(a, b)["iou"] == pytest.approx(1.0)


def test_a_thin_line_across_the_frame_is_ignored(tmp_path):
    """Axes, ticks and glyph strokes are 1-2px wide; the block average is
    what stops hundreds of them drowning out the model."""
    tool = _tool()
    a, b = tmp_path / "a.png", tmp_path / "b.png"
    _png(a, lambda x: x.__setitem__((slice(20, 76), slice(20, 76)), [200, 170, 40]))

    def with_line(x):
        x[20:76, 20:76] = [200, 170, 40]
        x[10:11, :] = [0, 0, 0]      # a 1px rule right across the image
        x[:, 84:85] = [0, 0, 0]
    _png(b, with_line)
    assert tool.compare_pair(a, b)["iou"] == pytest.approx(1.0)


def test_a_moved_body_does_count(tmp_path):
    tool = _tool()
    a, b = tmp_path / "a.png", tmp_path / "b.png"
    _png(a, lambda x: x.__setitem__((slice(20, 60), slice(20, 60)), [200, 170, 40]))
    _png(b, lambda x: x.__setitem__((slice(40, 80), slice(40, 80)), [200, 170, 40]))
    assert tool.compare_pair(a, b)["iou"] < 0.4


def test_a_missing_body_is_the_worst_score(tmp_path):
    tool = _tool()
    a, b = tmp_path / "a.png", tmp_path / "b.png"
    _png(a, lambda x: x.__setitem__((slice(20, 76), slice(20, 76)), [200, 170, 40]))
    _png(b, lambda x: None)
    assert tool.compare_pair(a, b)["iou"] == 0.0


def test_delta_reports_regressions_and_exits_nonzero(tmp_path, capsys):
    """`delta` gates a change: a fix that quietly breaks another image must
    not pass silently."""
    tool = _tool()
    before = tmp_path / "before.json"
    after = tmp_path / "after.json"
    before.write_text(json.dumps([{"image": "a.png", "iou": 0.10},
                                   {"image": "b.png", "iou": 0.90}]))
    after.write_text(json.dumps([{"image": "a.png", "iou": 0.99},
                                  {"image": "b.png", "iou": 0.40}]))

    rc = tool.main(["delta", str(before), str(after)])
    out = capsys.readouterr().out
    assert "improved : 1" in out
    assert "REGRESSED: 1" in out
    assert rc == 1, "a regression must fail the check"

    # Nothing moved -> clean exit.
    assert tool.main(["delta", str(before), str(before)]) == 0
