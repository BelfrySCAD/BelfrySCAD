"""An uncoloured difference() follows the live theme (#884's follow-up).

The evaluator marks triangles with no colour of their own with stand-ins
(negative RGB: -1 body, -2 cut face) instead of baking yellow and green;
the renderer draws them in the theme's colour, and a cut a shade darker."""
from types import SimpleNamespace

from belfryscad.engine.renderer import CUT_FACE_SHADE, SceneRenderer


def test_buffer_color_follows_the_theme_and_shades_a_cut():
    r = SceneRenderer()
    r._default_color = (0.5, 0.25, 1.0, 1.0)            # the theme
    body = SimpleNamespace(color=None, theme_cut=False)
    cut = SimpleNamespace(color=None, theme_cut=True)
    own = SimpleNamespace(color=(1.0, 0.0, 0.0, 1.0), theme_cut=True)
    assert r.buffer_color(body) == (0.5, 0.25, 1.0, 1.0)
    assert r.buffer_color(cut) == (0.5 * CUT_FACE_SHADE, 0.25 * CUT_FACE_SHADE, CUT_FACE_SHADE, 1.0)
    assert r.buffer_color(own) == (1.0, 0.0, 0.0, 1.0)  # an explicit colour wins


def test_an_uncoloured_cut_carries_the_theme_stand_ins(tmp_path):
    from openscad_cpp_evaluator import Evaluator, to_renderable_bodies
    p = tmp_path / "cut.scad"
    p.write_text("difference() { cube(10); translate([5,5,-1]) cylinder(h=12, d=6, $fn=16); }")
    bodies, _ = Evaluator(echo_fn=lambda m: None).evaluate(str(p), {})
    tc = to_renderable_bodies(bodies)[0].tri_colors
    assert sorted({float(c[0]) for c in tc}) == [-2.0, -1.0]
