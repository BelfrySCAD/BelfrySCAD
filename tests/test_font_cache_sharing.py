"""#434: two tabs sharing a cache swapped each other's fonts.

text() put the resolved FontHandle in its CSG params, and a handle numbers
faces within ONE FontProvider -- which every Evaluator makes for itself. Two
evaluators sharing a ManifoldCache, which is what two GUI tabs are, handed
the cache the same number for different fonts, and the second render was
served the first one's glyphs. text() puts the font SPEC in the params too
now, so the key tells them apart.

Lives here rather than in the evaluator's C++ suite because this is where
the condition reproduces: the same scenario written against the C++ test
helpers does not collide, so a test there would pass either way.
"""
import struct

import pytest

from openscad_cpp_evaluator import Evaluator, ManifoldCache, parse
from belfryscad import exporters

FONTS = ["Arial:style=Regular", "Arial:style=Bold",
         "Courier New:style=Regular", "Courier New:style=Bold"]


def _ink_width(stl):
    with open(stl, "rb") as f:
        f.seek(80)
        n = struct.unpack("<I", f.read(4))[0]
        body = f.read()
    xs = [struct.unpack_from("<f", body[i * 50:(i + 1) * 50], 12 + v * 12)[0]
          for i in range(n) for v in range(3)]
    return round(max(xs) - min(xs), 3)


def _render(tmp_path, font, tag, cache):
    """One render in its OWN Evaluator -- a fresh FontProvider each time,
    exactly like a GUI render."""
    src = tmp_path / f"{tag}.scad"
    src.write_text(f'linear_extrude(1) text("G", size=10, font="{font}");\n')
    parse(str(src))
    ev = Evaluator(manifold_cache=cache, echo_fn=lambda m: None)
    ev.evaluate(str(src), {}, generate=True)
    out = tmp_path / f"{tag}.stl"
    exporters.export_model(str(out), ev.geometry)
    return _ink_width(str(out))


@pytest.fixture
def truth(tmp_path):
    """Each font rendered in a cache of its own -- what each SHOULD look
    like. Skips if this machine cannot tell the fonts apart."""
    widths = {f: _render(tmp_path, f, f"own{i}", ManifoldCache())
              for i, f in enumerate(FONTS)}
    if len(set(widths.values())) < len(FONTS):
        pytest.skip(f"fonts not distinguishable here: {widths}")
    return widths


def test_a_shared_cache_does_not_swap_fonts(tmp_path, truth):
    shared = ManifoldCache()
    for i, font in enumerate(FONTS):
        got = _render(tmp_path, font, f"sh{i}", shared)
        assert got == truth[font], (
            f"{font} came back {got}, expected {truth[font]} -- served another "
            f"font's cached glyphs")


def test_the_same_font_twice_still_hits_the_cache(tmp_path):
    """The fix must not defeat caching: one spec, one entry."""
    shared = ManifoldCache()
    a = _render(tmp_path, FONTS[0], "same_a", shared)
    b = _render(tmp_path, FONTS[0], "same_b", shared)
    assert a == b
