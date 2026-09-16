"""The gizmo's size is one number, read by both the drawing and the picking.

It was a bare `0.14` in four places -- three in the renderer, one in the
viewport's scale drag -- which is how a handle and the region that picks it
drift apart.
"""
import re
from pathlib import Path

from belfryscad.engine.renderer import GIZMO_SCALE

_SRC = Path(__file__).resolve().parent.parent / "src" / "belfryscad"


def test_nothing_hard_codes_the_gizmo_size_any_more():
    """Four copies of `camera.distance * 0.14`, now one named constant."""
    uses = 0
    for path in (_SRC / "engine" / "renderer.py", _SRC / "window" / "viewport.py"):
        # Code only: a comment is allowed to mention the old number, and a
        # test that greps prose finds its own explanation.
        code = "\n".join(re.sub(r"#.*", "", ln)
                         for ln in path.read_text().splitlines())
        assert "0.14" not in code, path
        uses += len(re.findall(r"distance \* GIZMO_SCALE", code))
    assert uses == 4, uses


def test_the_viewport_reads_the_renderer_s_constant():
    text = (_SRC / "window" / "viewport.py").read_text()
    assert "GIZMO_SCALE" in text
    assert "import GIZMO_SCALE" in text or "GIZMO_SCALE," in text


def test_the_pick_tolerance_does_not_scale_with_the_handle():
    """A smaller handle must stay just as easy to grab: the tolerance is in
    screen pixels, so it is unaffected by GIZMO_SCALE."""
    text = (_SRC / "engine" / "renderer.py").read_text()
    picks = [m for m in re.finditer(r"def _pick_\w+_axis", text)]
    assert len(picks) == 2
    for m in picks:
        body = text[m.start():m.start() + 2500]
        assert "best_dist, best_axis = 12.0" in body, m.group()
        assert "GIZMO_SCALE" not in body.split("best_dist")[1], (
            "tolerance must not be derived from the handle size")


def test_the_gizmo_is_a_fraction_of_the_camera_distance():
    """Apparent size stays put as you zoom, which is the whole reason it is
    a fraction rather than a length."""
    assert 0.0 < GIZMO_SCALE < 0.5
