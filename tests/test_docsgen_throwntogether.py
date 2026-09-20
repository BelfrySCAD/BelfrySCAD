"""ThrownTogether is honoured by the docs renderer (issue #524).

It was lumped in with `Render` as "same as preview, the evaluator always
does full CSG anyway", which treats it as a cheaper preview mode. It is
not: it is a different picture, one that exposes face backsides in magenta
so an example can show that a surface is open or a mesh non-manifold. BOSL2
uses it exactly that way -- isosurface()'s gyroid examples and
vnf_vertex_array()'s open-shape examples -- and every one of them was
rendering as a plain preview.

Verified against OpenSCAD 2026.02.01 on BOSL2's own gyroid example:
`--preview ""` renders it entirely in the object colour, `--preview
throwntogether` renders the backfaces magenta. Ours matched the first in
both cases, which was the bug.

The pixels are checked outside pytest -- a GL context in the suite is a
known way to take the whole run down -- so these cover the plumbing that
was actually broken: the flag was never parsed and never reached the
renderer.
"""
import pytest

from belfryscad.docsgen.imagemanager import ImageRequest
from belfryscad.headless_render import _RenderOptions


def _request(meta):
    return ImageRequest("f.scad", 1, "img.png", ["cube(1);"], meta)


class TestFlagIsParsed:
    @pytest.mark.parametrize("meta", [
        "3D,ThrownTogether",
        "3D,ThrownTogether,NoAxes",
        "3D,NoAxes,ThrownTogether,VPD=183",
        # BOSL2 spells it both ways; vnf.scad's vnf_halfspace example uses
        # the explicit form.
        "3D,ThrownTogether=true,VPD=350",
    ])
    def test_recognised(self, meta):
        assert _request(meta).thrown_together is True

    @pytest.mark.parametrize("meta", [
        "3D", "3D,NoAxes", "3D,Render", "3D,Edges,Med", "2D",
    ])
    def test_absent(self, meta):
        assert _request(meta).thrown_together is False

    def test_render_flag_is_still_independent(self):
        """Render clears $preview; ThrownTogether does not. They were
        conflated, and must not be again."""
        req = _request("3D,ThrownTogether")
        assert req.preview is True
        assert _request("3D,Render").preview is False


class TestFlagReachesTheRenderer:
    def _opts(self, **kw):
        return _RenderOptions(imgsize="100,100", camera=None, autocenter=True,
                              viewall=True, projection="p", view="",
                              colorscheme=None, **kw)

    def test_defaults_off(self):
        """Every other caller -- the CLI, the GUI preview -- must keep the
        plain-preview behaviour it has today."""
        assert self._opts().thrown_together is False

    def test_carries_through(self):
        assert self._opts(thrown_together=True).thrown_together is True

    def test_light_backfaces_is_the_inverse(self):
        """apply_view_options turns the flag into SceneRenderer.
        light_backfaces, which is what actually suppresses the magenta."""
        class FakeCamera:
            orthographic = False
        class FakeRenderer:
            light_backfaces = None
            camera = FakeCamera()
            def set_viewport(self, w, h): pass
            def __setattr__(self, k, v): object.__setattr__(self, k, v)
        from belfryscad.headless_render import apply_view_options

        plain, thrown = FakeRenderer(), FakeRenderer()
        apply_view_options(plain, self._opts())
        apply_view_options(thrown, self._opts(thrown_together=True))
        assert plain.light_backfaces is True     # open surface in object colour
        assert thrown.light_backfaces is False   # backsides shown magenta
