import pathlib
"""The docsgen port: example metadata parsing, APNG output, markdown
fix-ups, and an end-to-end preview.

Nothing here renders: image generation needs an OpenGL context, and Qt/GL
inside pytest takes the whole run down with it. The preview test therefore
uses gen_images=False, and the rendering path is exercised by
`belfryscad --docsgen` against a real library instead.
"""
import struct
import zlib

import pytest

from belfryscad.docsgen.imagemanager import ImageRequest
from belfryscad.docsgen.preview import build_preview, markdown_for_qt
from belfryscad.docsgen.runner import camera_spec_from_dyn
from belfryscad.png_writer import write_apng


DEMO = """\
// LibFile: demo.scad
//   A tiny library.
// Includes:
//   include <demo.scad>
// FileGroup: Testing
// FileSummary: One module.

// Section: Shapes

// Module: widget()
// Synopsis: Makes a cube.
// Topics: Shapes (3D)
// Usage:
//   widget(size);
// Description:
//   Makes a cube `size` on a side.
// Arguments:
//   size = Length of each side.
// Example: A plain widget.
//   widget(20);
module widget(size=10) cube(size, center=true);
"""


def make_request(meta, script=("cube(1);",)):
    return ImageRequest("demo.scad", 1, "out/demo.png", list(script), meta)


# -- Example/Figure metadata ------------------------------------------

def test_default_example_is_a_single_still_at_the_openscad_default_view():
    req = make_request("3D")
    assert req.imgsize == (320, 240)
    assert req.animation_frames is None
    assert req.camera == [0, 0, 0, 55, 0, 25, 444]
    assert req.orthographic and req.show_axes and req.show_scales
    assert not req.show_edges


@pytest.mark.parametrize("meta,expected", [
    ("Small", (240, 180)),
    ("Med", (480, 360)),
    ("Big", (640, 480)),
    ("Huge", (800, 600)),
    ("Size=100x50", (100, 50)),
])
def test_size_keywords(meta, expected):
    assert make_request(meta).imgsize == expected


def test_spin_becomes_an_animation_with_a_script_driven_camera():
    req = make_request("Spin")
    assert req.animation_frames == 36
    # A $t-dependent view can only be resolved by running the script, so the
    # fixed camera is dropped and $vp* assignments are prepended instead.
    assert req.camera is None
    assert req.script_lines[:4] == [
        "$vpt = [0, 0, 0];",
        "$vpr = [90-45*cos(360*$t), 0, 360*$t];",
        "$vpd = 444;",
        "$vpf = 22.5;",
    ]


def test_frames_and_framems_override_the_animation_defaults():
    req = make_request("Anim,Frames=8,FrameMS=50")
    assert (req.animation_frames, req.frame_ms) == (8, 50)


def test_fps_sets_the_frame_delay():
    assert make_request("Anim,FPS=20").frame_ms == 50


def test_numeric_vpr_stays_a_fixed_camera():
    req = make_request("VPR=[10,20,30]")
    assert req.camera == [0, 0, 0, 10.0, 20.0, 30.0, 444]
    assert req.animation_frames is None


def test_2d_looks_straight_down():
    assert make_request("2D").camera == [0, 0, 0, 0, 0, 0, 444]


def test_view_flags():
    req = make_request("3D,Edges,NoAxes,NoScales,Perspective")
    assert req.show_edges
    assert not req.show_axes and not req.show_scales and not req.orthographic


def test_leading_dashes_mark_hidden_setup_lines():
    req = make_request("3D", script=["--$fn=32;", "cube(1);"])
    assert req.script_lines == ["$fn=32;", "cube(1);"]


def test_norender_is_refused():
    from belfryscad.docsgen.imagemanager import image_manager
    with pytest.raises(Exception):
        image_manager.new_request("demo.scad", 1, "out/x.png", ["cube(1);"], "NORENDER")


# -- camera recovered from a script's own $vp* -------------------------

def test_camera_spec_from_dyn():
    spec = camera_spec_from_dyn({"$vpt": [1, 2, 3], "$vpr": [55, 0, 90], "$vpd": 200})
    assert [float(x) for x in spec.split(",")] == [1, 2, 3, 55, 0, 90, 200]


def test_camera_spec_from_dyn_is_none_when_the_script_set_nothing():
    assert camera_spec_from_dyn({"$fn": 32}) is None


def test_camera_spec_from_dyn_falls_back_per_value():
    spec = camera_spec_from_dyn({"$vpd": 100})
    assert [float(x) for x in spec.split(",")] == [0, 0, 0, 55, 0, 25, 100]


# -- animated PNG output ----------------------------------------------

def _chunks(data):
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    out, i = [], 8
    while i < len(data):
        size = struct.unpack(">I", data[i:i + 4])[0]
        tag, body = data[i + 4:i + 8], data[i + 8:i + 8 + size]
        assert zlib.crc32(tag + body) == struct.unpack(">I", data[i + 8 + size:i + 12 + size])[0], \
            f"bad CRC on {tag!r}"
        out.append((tag.decode(), body))
        i += 12 + size
    return out


def test_write_apng_frame_structure(tmp_path):
    w, h, frames = 2, 2, 3
    path = tmp_path / "anim.png"
    write_apng(str(path), [bytes([i * 40] * (w * h * 4)) for i in range(frames)],
               w, h, delay_ms=125)
    tags = [t for t, _ in _chunks(path.read_bytes())]

    # acTL must precede the first IDAT, frame 0 IS the IDAT, and every later
    # frame is one fcTL + one fdAT.
    assert tags.index("acTL") < tags.index("IDAT")
    assert tags.count("fcTL") == frames
    assert tags.count("fdAT") == frames - 1
    assert tags[0] == "IHDR" and tags[-1] == "IEND"


def test_write_apng_header_says_how_many_frames_and_loops_forever(tmp_path):
    path = tmp_path / "anim.png"
    write_apng(str(path), [b"\x00" * 16] * 4, 2, 2)
    actl = next(body for tag, body in _chunks(path.read_bytes()) if tag == "acTL")
    assert struct.unpack(">II", actl) == (4, 0)


def test_write_apng_delay_is_stored_as_milliseconds_over_1000(tmp_path):
    path = tmp_path / "anim.png"
    write_apng(str(path), [b"\x00" * 16] * 2, 2, 2, delay_ms=125)
    fctl = next(body for tag, body in _chunks(path.read_bytes()) if tag == "fcTL")
    delay_num, delay_den = struct.unpack(">HH", fctl[20:24])
    assert (delay_num, delay_den) == (125, 1000)


def test_write_apng_rejects_a_wrongly_sized_frame(tmp_path):
    with pytest.raises(ValueError):
        write_apng(str(tmp_path / "x.png"), [b"\x00" * 16, b"\x00" * 8], 2, 2)


def test_write_apng_needs_at_least_one_frame(tmp_path):
    with pytest.raises(ValueError):
        write_apng(str(tmp_path / "x.png"), [], 2, 2)


# -- $vp* visibility ---------------------------------------------------
#
# OpenSCAD defines $vpt/$vpr/$vpd/$vpf for every run, and BOSL2 reads them
# (debug_vnf() orients its labels by $vpr). Leaving them undef made those
# examples warn, and docsgen's hard-warnings rule turned that into a failed
# image. Values below were read off OpenSCAD 2026.02.01.

def test_default_viewport_matches_openscads_own():
    from belfryscad.export_name import DEFAULT_VIEWPORT
    assert DEFAULT_VIEWPORT == {"$vpt": [0.0, 0.0, 0.0], "$vpr": [55.0, 0.0, 25.0],
                                 "$vpd": 140.0, "$vpf": 22.5}


def test_seed_params_adds_the_viewport_but_never_overrides_a_caller():
    from belfryscad.export_name import seed_params
    out = seed_params({"$vpr": [1.0, 2.0, 3.0]}, "/a/w.scad")
    assert out["$vpr"] == [1.0, 2.0, 3.0]     # the GUI's live camera wins
    assert out["$vpd"] == 140.0               # the rest still filled in


def test_camera_7_value_form_is_passed_through_verbatim():
    from belfryscad.headless_render import viewport_params_from_camera
    assert viewport_params_from_camera("1,2,3,10,20,30,250") == {
        "$vpt": [1.0, 2.0, 3.0], "$vpr": [10.0, 20.0, 30.0], "$vpd": 250.0}


def test_camera_6_value_eye_center_form_is_converted():
    from belfryscad.headless_render import viewport_params_from_camera
    vp = viewport_params_from_camera("100,100,100,0,0,0")
    assert vp["$vpt"] == [0.0, 0.0, 0.0]
    assert vp["$vpd"] == pytest.approx(173.205, abs=1e-3)
    assert vp["$vpr"][0] == pytest.approx(54.7356, abs=1e-3)
    assert vp["$vpr"][1] == pytest.approx(0.0, abs=1e-9)
    assert vp["$vpr"][2] == pytest.approx(135.0, abs=1e-3)


def test_no_camera_means_no_override_so_the_defaults_apply():
    from belfryscad.headless_render import viewport_params_from_camera
    # OpenSCAD reports $vpd=140 with no --camera even under --viewall: the
    # camera it was GIVEN, not the one it fitted.
    assert viewport_params_from_camera(None) == {}
    assert viewport_params_from_camera("") == {}
    assert viewport_params_from_camera("garbage") == {}


def test_fixed_view_example_sees_its_own_camera():
    """A static Example's $vp* come from the camera docsgen renders it with."""
    req = make_request("3D")
    assert req.camera == [0, 0, 0, 55, 0, 25, 444]


# -- markdown fix-ups for Qt ------------------------------------------

def test_img_tags_become_markdown_images():
    md = '<img align="left" alt="cuboid() Example 1" src="images/x.png" width="320" height="240">'
    assert markdown_for_qt(md) == "![cuboid() Example 1](images/x.png)"


def test_image_alt_escapes_are_removed_so_qt_emits_one_image():
    """Qt splits an image at each backslash escape in its alt text and
    repeats it per fragment -- three copies of every BOSL2 example."""
    md = r'<img align="left" alt="ball\_bearing() Example 1" src="x.png">'
    assert markdown_for_qt(md) == "![ball_bearing() Example 1](x.png)"


def test_anchor_tags_become_markdown_links():
    assert markdown_for_qt('<a href="Topics#shapes">Shapes</a>') == "[Shapes](Topics#shapes)"


def test_abbr_and_sup_wrappers_are_dropped_but_their_text_kept():
    md = '<abbr title="These args can be used by position.">By&nbsp;Position</abbr>'
    assert markdown_for_qt(md) == "By Position"


def test_breaks_and_code_tags():
    assert markdown_for_qt('<br clear="all" /><br/>') == ""
    assert markdown_for_qt("<code>cuboid()</code>") == "`cuboid()`"


def test_include_syntax_in_a_code_block_is_not_mistaken_for_html():
    assert "<demo.scad>" in markdown_for_qt("    include <demo.scad>")


# -- mdimggen (markdown image generator) -------------------------------
#
# The rendering step is stubbed out: it needs an OpenGL context, and Qt/GL
# inside pytest takes the whole run down. Everything else -- the rc file,
# the block scanning, the rewritten markdown -- is exercised for real.

TUTORIAL_MD = """\
# Heading

Some prose.

```openscad-3D
cube(10);
```

More prose.

```openscad-3D;ImgOnly
sphere(5);
```
"""


@pytest.fixture
def no_render(monkeypatch):
    from belfryscad.docsgen import imagemanager
    monkeypatch.setattr(imagemanager.image_manager, "process_requests",
                        lambda *a, **k: imagemanager.image_manager.purge_requests())


def test_rc_defaults_reads_the_yaml_settings(tmp_path, monkeypatch):
    from belfryscad.docsgen import mdimggen
    (tmp_path / ".openscad_mdimggen_rc").write_text(
        'docs_dir: "BOSL2.wiki"\n'
        'image_root: "images/tutorials"\n'
        'file_prefix: "Tutorial-"\n'
        'source_files: "tutorials/*.md"\n'
        'png_animations: true\n')
    monkeypatch.chdir(tmp_path)
    d = mdimggen._rc_defaults()
    assert d["docs_dir"] == "BOSL2.wiki"
    assert d["file_prefix"] == "Tutorial-"
    assert d["source_files"] == "tutorials/*.md"


def test_rc_defaults_is_empty_when_absent(tmp_path, monkeypatch):
    from belfryscad.docsgen import mdimggen
    monkeypatch.chdir(tmp_path)
    assert mdimggen._rc_defaults() == {}


def test_mdimggen_rewrites_blocks_and_links_the_images(tmp_path, monkeypatch, no_render):
    from belfryscad.docsgen import mdimggen
    (tmp_path / "tutorials").mkdir()
    (tmp_path / "tutorials" / "Demo.md").write_text(TUTORIAL_MD)
    # The output directory must already exist -- upstream's mdimggen has no
    # makedirs for it either, and in practice it is a checked-out wiki repo.
    (tmp_path / "wiki").mkdir()
    monkeypatch.chdir(tmp_path)

    assert mdimggen.main(["-D", "wiki", "-P", "Tutorial-", "-I", "images/tutorials",
                          "tutorials/Demo.md"]) == 0

    out = (tmp_path / "wiki" / "Tutorial-Demo.md").read_text()
    assert "# Heading" in out and "Some prose." in out
    # Each block becomes an image link, numbered in source order.
    assert "![Figure 1](images/tutorials/Demo_1.png)" in out
    assert "![Figure 2](images/tutorials/Demo_2.png)" in out
    # A plain block keeps its script; an ImgOnly block drops it.
    assert "cube(10);" in out
    assert "sphere(5);" not in out


def test_mdimggen_reports_when_there_is_nothing_to_do(tmp_path, monkeypatch):
    from belfryscad.docsgen import EXIT_FAILURE, mdimggen
    monkeypatch.chdir(tmp_path)
    assert mdimggen.main([]) == EXIT_FAILURE


def test_failure_exit_code_matches_upstreams(tmp_path, monkeypatch):
    """Upstream exits with sys.exit(-1), which the shell sees as 255. A
    caller testing for the specific code, not just non-zero, must not
    notice the difference.

    Driven by a documentation error rather than a missing file: on Windows
    processFiles globs its arguments, and a name that matches nothing
    becomes an empty list rather than a "does not exist" failure (upstream
    behaves the same way).
    """
    from belfryscad.docsgen import EXIT_FAILURE, main
    assert EXIT_FAILURE == 255
    (tmp_path / "bad.scad").write_text(DEMO.replace("// Synopsis:", "// Bogusness:"))
    monkeypatch.chdir(tmp_path)
    assert main(["-m", "-q", "bad.scad"]) == EXIT_FAILURE


def test_image_urls_use_forward_slashes(tmp_path):
    """Generated image links are URLs, so they must never contain a
    backslash -- os.path.join would put one there on Windows and break
    every image in the docs."""
    src = tmp_path / "demo.scad"
    src.write_text(DEMO)
    preview = build_preview(DEMO, str(src), gen_images=False)
    assert "images/demo/widget.png" in preview.markdown
    assert "\\" not in preview.markdown


# -- Docs pane code-block styling --------------------------------------
#
# The tint is derived from the palette rather than hardcoded so it stays a
# subtle wash in a dark theme instead of a bright slab. Only the colour maths
# is checked here; how it looks is verified by a throwaway script, since Qt
# widgets inside pytest take the whole run down.

class _StubPalette:
    """Just enough QPalette for _code_tint -- no QApplication needed."""

    def __init__(self, base, text):
        from PySide6.QtGui import QColor
        self._base, self._text = QColor(*base), QColor(*text)

    def base(self):
        return type("B", (), {"color": lambda _self, c=self._base: c})()

    def text(self):
        return type("T", (), {"color": lambda _self, c=self._text: c})()


def test_code_tint_matches_githubs_bgcolor_muted_in_a_light_theme():
    """GitHub's --bgColor-muted is #f6f8fa -- lightness 246. The tint is a
    neutral blend of the palette so it survives theming, so it matches that
    lightness rather than the exact hue (GitHub's has a faint blue cast)."""
    from belfryscad.window.docs_pane import _code_tint
    from PySide6.QtGui import QColor
    tint = _code_tint(_StubPalette((255, 255, 255), (0, 0, 0)))
    assert abs(tint.lightness() - QColor("#f6f8fa").lightness()) <= 2
    assert tint.lightness() < 255            # darker than the white base


def test_code_tint_lightens_a_dark_theme():
    """The whole point of deriving it: on a dark base the tint must go UP,
    not produce the near-white it would if it were hardcoded."""
    from belfryscad.window.docs_pane import _code_tint
    base = (30, 31, 34)
    tint = _code_tint(_StubPalette(base, (220, 221, 222)))
    from PySide6.QtGui import QColor
    assert tint.lightness() > QColor(*base).lightness()
    assert tint.lightness() < 128            # still a tint, not a slab


def test_code_tint_stays_close_to_the_base():
    from belfryscad.window.docs_pane import _code_tint
    from PySide6.QtGui import QColor
    for base, text in (((255, 255, 255), (0, 0, 0)), ((30, 31, 34), (220, 221, 222))):
        tint = _code_tint(_StubPalette(base, text))
        delta = abs(tint.lightness() - QColor(*base).lightness())
        assert delta < 30, f"tint drifted {delta} from base {base}"


def test_headings_get_a_rule_and_leading_space_at_levels_one_and_two():
    """Level 1 and 2 only. BOSL2 files carry dozens of level-3 Module:/
    Function: headings, and ruling every one turns the page into a ladder."""
    from PySide6.QtGui import QTextDocument, QTextFormat
    from belfryscad.window.docs_pane import _style_headings, _HEADING_TOP_MARGIN

    doc = QTextDocument()
    doc.setMarkdown("# One\n\ntext\n\n## Two\n\ntext\n\n### Three\n\ntext\n",
                    QTextDocument.MarkdownFeature.MarkdownDialectGitHub)
    _style_headings(doc)

    seen = {}
    block = doc.begin()
    while block.isValid():
        fmt = block.blockFormat()
        if fmt.headingLevel():
            seen[fmt.headingLevel()] = (
                fmt.hasProperty(QTextFormat.Property.BlockTrailingHorizontalRulerWidth),
                fmt.topMargin(),
            )
        block = block.next()

    assert seen[1] == (True, _HEADING_TOP_MARGIN[1])
    assert seen[2] == (True, _HEADING_TOP_MARGIN[2])
    assert seen[3][0] is False, "level 3 must not be ruled"


def test_heading_scale_matches_githubs():
    """GitHub: h1 2em, h2 1.5em, h3 1.25em, font-weight 600. Qt sizes
    headings with a legacy adjustment that leaves them all at body size."""
    from PySide6.QtGui import QTextDocument
    from belfryscad.window.docs_pane import _style_headings, _HEADING_SCALE

    doc = QTextDocument()
    doc.setMarkdown("# One\n\n## Two\n\n### Three\n",
                    QTextDocument.MarkdownFeature.MarkdownDialectGitHub)
    base = doc.defaultFont().pointSizeF()
    _style_headings(doc)

    seen = {}
    block = doc.begin()
    while block.isValid():
        lvl = block.blockFormat().headingLevel()
        it = block.begin()
        if lvl and not it.atEnd():
            cf = it.fragment().charFormat()
            seen[lvl] = (round(cf.fontPointSize() / base, 3), cf.fontWeight())
        block = block.next()
    assert seen[1] == (_HEADING_SCALE[1], 600) and _HEADING_SCALE[1] == 2.0
    assert seen[2] == (_HEADING_SCALE[2], 600) and _HEADING_SCALE[2] == 1.5
    assert seen[3] == (_HEADING_SCALE[3], 600) and _HEADING_SCALE[3] == 1.25


def test_rule_grey_matches_githubs_border_color():
    """GitHub: border-bottom 1px solid --borderColor-muted, #d1d9e0 at 70%
    over white, about #dde2e7 -- a hairline, not Qt's default text-coloured
    bar."""
    from belfryscad.window.docs_pane import _blend, _RULE_FADE
    from PySide6.QtGui import QColor
    grey = _blend(QColor(0, 0, 0), QColor(255, 255, 255), _RULE_FADE)
    assert abs(grey.lightness() - QColor("#dde2e7").lightness()) <= 10


def test_rule_grey_is_between_the_text_and_background():
    from belfryscad.window.docs_pane import _blend, _RULE_FADE
    from PySide6.QtGui import QColor
    for text, base in (((0, 0, 0), (255, 255, 255)), ((220, 221, 222), (30, 31, 34))):
        grey = _blend(QColor(*text), QColor(*base), _RULE_FADE)
        lo, hi = sorted((QColor(*text).lightness(), QColor(*base).lightness()))
        assert lo < grey.lightness() < hi, f"{grey.name()} not between {text} and {base}"


def test_tables_stripe_alternate_body_rows_leaving_the_header_clear():
    from PySide6.QtGui import QTextDocument, QTextTable
    from belfryscad.window.docs_pane import _stripe_tables, _code_tint

    doc = QTextDocument()
    rows = "\n".join(f"| r{i} | v{i} |" for i in range(1, 7))
    doc.setMarkdown(f"| a | b |\n|---|---|\n{rows}\n",
                    QTextDocument.MarkdownFeature.MarkdownDialectGitHub)
    pal = _StubPalette((255, 255, 255), (0, 0, 0))
    _stripe_tables(doc, pal)

    table = next(f for f in doc.rootFrame().childFrames() if isinstance(f, QTextTable))
    tint = _code_tint(pal)
    tinted = [r for r in range(table.rows())
              if table.cellAt(r, 0).format().background().color() == tint]
    assert 0 not in tinted, "header row must stay clear"
    assert 1 not in tinted, "first body row stays clear, striping starts below it"
    assert tinted == [r for r in range(2, table.rows(), 2)]


# -- Docs pane image placeholders --------------------------------------

def test_unrendered_images_become_click_to_render_placeholders():
    from belfryscad.window.docs_pane import placeholder_markdown
    md, pending = placeholder_markdown("![Example 1](images/d/a.png)", "/nonexistent")
    assert "bfsrender:images/d/a.png" in md
    assert "![" not in md, "must not leave an image Qt would draw as a broken icon"
    assert pending == ["images/d/a.png"]


def test_already_rendered_images_are_left_alone(tmp_path):
    from belfryscad.window.docs_pane import placeholder_markdown
    (tmp_path / "images").mkdir()
    (tmp_path / "images" / "a.png").write_bytes(b"x")
    md, pending = placeholder_markdown("![Example 1](images/a.png)", str(tmp_path))
    assert md == "![Example 1](images/a.png)"
    assert pending == []


def test_remote_images_become_links_not_render_placeholders():
    """BOSL2's isosurface.scad embeds animated GIFs straight from
    raw.githubusercontent.com. QTextBrowser does no network fetching, so
    those rendered as broken icons -- and offering to 'render' them would be
    a lie, since there is no Example behind them."""
    from belfryscad.window.docs_pane import placeholder_markdown, _REMOTE_SCHEME
    url = "https://raw.githubusercontent.com/BelfrySCAD/BOSL2/master/images/metaball_demo.gif"
    md, pending = placeholder_markdown(f"![demo]({url})", "/nonexistent")
    assert f"{_REMOTE_SCHEME}:{url}" in md and "remote image" in md
    assert "bfsrender:" not in md, "a remote image is not renderable here"
    assert pending == [], "must not be counted as a pending render"


def test_prose_links_are_left_alone_and_unboxed():
    """The docs are full of ordinary links -- to wikipedia, to other wiki
    pages. Only image stand-ins get their own scheme, which is what keeps
    the boxing from spilling onto normal paragraphs."""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QTextDocument
    from belfryscad.window.docs_pane import (placeholder_markdown, _style_placeholders,
                                              _REMOTE_SCHEME)

    prose = "see [a trefoil knot](https://en.wikipedia.org/wiki/Trefoil_knot) for more"
    md, _ = placeholder_markdown(prose, "/nonexistent")
    assert md == prose, "a prose link must not be rewritten"

    doc = QTextDocument()
    doc.setMarkdown(md + f"\n\n[x](https://e.com/i.gif)\n",
                    QTextDocument.MarkdownFeature.MarkdownDialectGitHub)
    _style_placeholders(doc, _StubPalette((255, 255, 255), (0, 0, 0)))
    boxed = []
    block = doc.begin()
    while block.isValid():
        if block.blockFormat().background().style() != Qt.BrushStyle.NoBrush:
            boxed.append(block.text().strip())
        block = block.next()
    assert boxed == [], f"prose links must not be boxed, got {boxed}"


def test_placeholder_falls_back_to_the_filename_when_alt_is_empty():
    from belfryscad.window.docs_pane import placeholder_markdown
    md, _ = placeholder_markdown("![](images/d/widget_2.png)", "/nonexistent")
    assert "widget_2.png" in md


def test_placeholder_and_image_are_each_one_block():
    """The scroll anchor survives a rebuild because block NUMBERING does
    not change when a placeholder becomes an image -- both are a single
    paragraph. If that ever stopped being true, holding scroll position
    would silently start jumping."""
    from PySide6.QtGui import QTextDocument

    def blocks(md):
        doc = QTextDocument()
        doc.setMarkdown(md, QTextDocument.MarkdownFeature.MarkdownDialectGitHub)
        return doc.blockCount()

    body = "para one\n\n%s\n\npara two\n"
    assert blocks(body % "![Example 1](images/a.png)") == \
           blocks(body % "[\u25b6 Render Example 1](bfsrender:images/a.png)")


def test_placeholders_are_boxed_and_distinct_from_code_blocks():
    """The placeholder is a control and the code block is content, so they
    must not read as the same surface. Both are drawn with a background
    rather than a border: Qt has no block border, and wrapping placeholders
    in tables would change the block structure the scroll anchoring needs to
    stay stable."""
    from PySide6.QtGui import QTextDocument
    from belfryscad.window.docs_pane import (_style_placeholders, _code_tint,
                                              _PLACEHOLDER_TINT_MIX, _blend,
                                              _RENDER_SCHEME)

    pal = _StubPalette((255, 255, 255), (0, 0, 0))
    doc = QTextDocument()
    doc.setMarkdown(f"text\n\n[Render it]({_RENDER_SCHEME}:images/a.png)\n\nmore\n",
                    QTextDocument.MarkdownFeature.MarkdownDialectGitHub)
    _style_placeholders(doc, pal)

    # Brush STYLE, not colour: an unset background still reports an opaque
    # default colour, so testing the colour marks every block as boxed.
    from PySide6.QtCore import Qt
    boxed = []
    block = doc.begin()
    while block.isValid():
        if block.blockFormat().background().style() != Qt.BrushStyle.NoBrush:
            boxed.append(block.text().strip())
        block = block.next()
    assert boxed == ["Render it"], f"boxed the wrong blocks: {boxed}"

    placeholder = _blend(pal.base().color(), pal.text().color(), _PLACEHOLDER_TINT_MIX)
    assert placeholder.lightness() < _code_tint(pal).lightness(), \
        "placeholder box must be distinguishable from a code block"


def test_remote_image_stand_ins_are_boxed_too():
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QTextDocument
    from belfryscad.window.docs_pane import _style_placeholders, _REMOTE_SCHEME

    doc = QTextDocument()
    doc.setMarkdown(f"text\n\n[\U0001f517 demo (remote image)]({_REMOTE_SCHEME}:https://e.com/a.gif)\n",
                    QTextDocument.MarkdownFeature.MarkdownDialectGitHub)
    _style_placeholders(doc, _StubPalette((255, 255, 255), (0, 0, 0)))
    boxed = []
    block = doc.begin()
    while block.isValid():
        if block.blockFormat().background().style() != Qt.BrushStyle.NoBrush:
            boxed.append(block.text().strip())
        block = block.next()
    assert len(boxed) == 1 and "remote image" in boxed[0]


# -- end-to-end preview (no images) ------------------------------------

def test_preview_renders_the_documentation_and_finds_no_errors(tmp_path):
    src = tmp_path / "demo.scad"
    src.write_text(DEMO)
    preview = build_preview(DEMO, str(src), gen_images=False)

    assert preview.errors == []
    assert not preview.has_errors
    assert "LibFile: demo.scad" in preview.markdown
    assert "Module: widget()" in preview.markdown
    assert "Length of each side." in preview.markdown


@pytest.mark.parametrize("edit,expected", [
    (lambda t: t.replace("// Synopsis:", "// Bogusness:"), "Unrecognized block"),
    (lambda t: t.replace("// Topics: Shapes (3D)", "// See Also: no_such_thing()"), "Invalid Link"),
    (lambda t: t + "\n// Module: widget()\n// Synopsis: dup.\nmodule w2() cube(1);\n", "Redeclared"),
])
def test_preview_reports_docsgen_validation_errors(tmp_path, edit, expected):
    """The point of the pane: docsgen's own validation, run on unsaved text."""
    src = tmp_path / "demo.scad"
    src.write_text(DEMO)
    preview = build_preview(edit(DEMO), str(src), gen_images=False)

    assert preview.has_errors
    assert any(expected in msg for _f, _l, msg, _lvl in preview.errors), preview.errors


def test_preview_errors_carry_the_source_line_the_pane_jumps_to(tmp_path):
    src = tmp_path / "demo.scad"
    src.write_text(DEMO)
    text = DEMO.replace("// Synopsis:", "// Bogusness:")
    preview = build_preview(text, str(src), gen_images=False)
    line = preview.errors[0][1]
    # 1-based, and pointing at the offending line itself -- that is what
    # DocsPane's goto_line hands to the editor.
    assert text.splitlines()[line - 1].startswith("// Bogusness:")


def test_preview_of_an_undocumented_file_is_empty_but_not_an_error(tmp_path):
    src = tmp_path / "plain.scad"
    src.write_text("cube(1);\n")
    preview = build_preview("cube(1);\n", str(src), gen_images=False)
    assert preview.markdown == ""


def test_preview_never_writes_into_the_projects_real_docs_directory(tmp_path, monkeypatch):
    """An rc file's DocsDirectory must not capture the preview.

    It is re-read by parse_file for EVERY file parsed, so an override that
    is merely assigned gets undone by the first sibling -- which produced a
    stray BOSL2.wiki/ full of preview images in the working directory.
    """
    (tmp_path / ".openscad_docsgen_rc").write_text("DocsDirectory: leaked/\n")
    (tmp_path / "demo.scad").write_text(DEMO)
    # A sibling is what triggers the rc re-read.
    (tmp_path / "other.scad").write_text(DEMO.replace("demo.scad", "other.scad")
                                             .replace("widget", "gadget"))
    monkeypatch.chdir(tmp_path)

    preview = build_preview(DEMO, str(tmp_path / "demo.scad"), gen_images=False)

    assert "docs-preview" in preview.base_dir
    assert not (tmp_path / "leaked").exists()


def test_preview_writes_images_under_the_cache_not_beside_the_source(tmp_path):
    src = tmp_path / "demo.scad"
    src.write_text(DEMO)
    preview = build_preview(DEMO, str(src), gen_images=False)
    assert str(tmp_path) not in preview.base_dir
    assert "docs-preview" in preview.base_dir


def test_clicked_placeholder_becomes_a_rendering_label_with_cycling_dots():
    """A render takes seconds and "render all" takes minutes, so the
    placeholder has to say it is working."""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QTextDocument
    from belfryscad.window.docs_pane import (placeholder_markdown, _style_placeholders,
                                              mark_rendering, write_rendering_text,
                                              _ELLIPSIS)

    md, pending = placeholder_markdown(
        "![Example 8](img/e8.png)\n\n![Example 9](img/e9.png)\n", "/nonexistent")
    doc = QTextDocument()
    doc.setMarkdown(md, QTextDocument.MarkdownFeature.MarkdownDialectGitHub)
    _style_placeholders(doc, _StubPalette((255, 255, 255), (0, 0, 0)))

    def blocks():
        out, b = [], doc.begin()
        while b.isValid():
            if b.text().strip():
                it, href = b.begin(), ""
                while not it.atEnd():
                    href = href or it.fragment().charFormat().anchorHref()
                    it += 1
                boxed = b.blockFormat().background().style() != Qt.BrushStyle.NoBrush
                out.append((b.text(), href, boxed))
            b = b.next()
        return out

    targets = mark_rendering(doc, ["img/e8.png"])
    assert len(targets) == 1, "only the queued image is marked"
    write_rendering_text(doc, targets, 0)

    marked, untouched = blocks()
    assert marked[0] == "Rendering Example 8", marked
    assert marked[1] == "", "the marked block must stop being a link"
    assert marked[2], "it must keep its box"
    assert untouched[1].startswith("bfsrender:"), "the other placeholder is left clickable"

    for dots in range(1, 5):
        write_rendering_text(doc, targets, dots)
        assert blocks()[0][0] == "Rendering Example 8" + _ELLIPSIS[dots % len(_ELLIPSIS)]

    assert len(mark_rendering(doc, None)) == 1, \
        "render-all marks every placeholder still outstanding"


def test_lists_indent_two_em_like_github_not_qts_flat_40px():
    from PySide6.QtGui import QFontInfo, QTextDocument
    from belfryscad.window.docs_pane import _set_list_indent, _LIST_INDENT_EM

    doc = QTextDocument()
    doc.setMarkdown("- alpha\n  - nested\n", QTextDocument.MarkdownFeature.MarkdownDialectGitHub)
    assert doc.indentWidth() == 40.0, "Qt's default, for the comparison to mean anything"

    _set_list_indent(doc)
    em = QFontInfo(doc.defaultFont()).pixelSize()
    assert doc.indentWidth() == _LIST_INDENT_EM * em
    assert doc.indentWidth() < 40.0, "the whole point is that lists come in"


def test_table_spaces_collapse_but_code_spans_and_prose_do_not():
    """QTextDocument keeps every space it is given, so docsgen's column
    padding and two-space sentence gaps showed up literally."""
    from belfryscad.docsgen.preview import collapse_table_spaces

    md = (
        "`arg`      | What it does\n"
        "---------- | ------------\n"
        "`size`     | The size.  Default: 1\n"
        "\n"
        "prose  keeps  its  spaces\n"
        "\n"
        "| `a  b` | keeps  code |\n"
        "| --- | --- |\n"
        "| x | y  z |\n"
    )
    out = collapse_table_spaces(md).splitlines()
    assert out[0] == "`arg` | What it does"
    assert out[2] == "`size` | The size. Default: 1"
    assert out[4] == "prose  keeps  its  spaces", "only tables are touched"
    assert out[6] == "| `a  b` | keeps code |", "a code span keeps its spacing"
    assert out[8] == "| x | y z |"


def test_a_pipe_in_prose_is_not_mistaken_for_a_table():
    from belfryscad.docsgen.preview import collapse_table_spaces

    md = "- tube(h|l, od=, id=, ...)  [ATTACHMENTS];\n\nuse `r1`|`d1`.  Two  spaces.\n"
    assert collapse_table_spaces(md) == md


def test_fenced_code_is_never_collapsed():
    from belfryscad.docsgen.preview import collapse_table_spaces

    md = "```\na  |  b\n--- | ---\nkeep  me\n```\n"
    assert collapse_table_spaces(md) == md


# -- same-file anchor links -------------------------------------------

def test_heading_slug_matches_the_links_docsgen_emits():
    from belfryscad.window.docs_pane import heading_slug

    assert heading_slug("Module: cuboid()") == "module-cuboid"
    assert heading_slug("Function/Module: cube()") == "functionmodule-cube"
    assert heading_slug("Function: opp_hyp_to_ang()") == "function-opp_hyp_to_ang"
    assert heading_slug("Section: Cuboids, Prismoids and Pyramids") \
        == "section-cuboids-prismoids-and-pyramids"
    # Runs of spaces are NOT collapsed: stripping the backticks and the `$`
    # leaves two spaces, and docsgen's link really does have two hyphens.
    assert heading_slug("Section: Adaptive Children Using `$` Variables") \
        == "section-adaptive-children-using--variables"


def test_repeated_headings_get_githubs_numeric_suffixes():
    from PySide6.QtGui import QTextDocument
    from belfryscad.window.docs_pane import anchor_targets

    doc = QTextDocument()
    doc.setMarkdown("# Foo\n\n## Foo\n\n### Bar\n",
                    QTextDocument.MarkdownFeature.MarkdownDialectGitHub)
    t = anchor_targets(doc)
    assert set(t) == {"foo", "foo-1", "bar"}
    assert t["foo"] < t["foo-1"], "block numbers follow document order"


def test_only_headings_become_anchor_targets():
    from PySide6.QtGui import QTextDocument
    from belfryscad.window.docs_pane import anchor_targets

    doc = QTextDocument()
    doc.setMarkdown("# Real\n\nnot a heading\n",
                    QTextDocument.MarkdownFeature.MarkdownDialectGitHub)
    assert set(anchor_targets(doc)) == {"real"}


# -- Refresh discards rendered images ---------------------------------

def test_invalidate_cache_removes_the_rendered_images(tmp_path, monkeypatch):
    from belfryscad.docsgen import preview

    monkeypatch.setattr(preview, "CACHE_DIR", tmp_path / "cache")
    src = tmp_path / "thing.scad"
    src.write_text("// LibFile: thing.scad\n")

    cache = pathlib.Path(preview._cache_dir(str(src))) / "images" / "thing"
    cache.mkdir(parents=True)
    for n in ("a.png", "b.png"):
        (cache / n).write_bytes(b"\x89PNG\r\n\x1a\n")

    assert preview.invalidate_cache(str(src)) == 2
    assert not pathlib.Path(preview._cache_dir(str(src))).exists()
    # Idempotent: a second Refresh on an already-clean file is not an error.
    assert preview.invalidate_cache(str(src)) == 0


# -- $preview matches the mode the reference renders in ----------------
#
# openscad_docsgen drives OpenSCAD with `--preview ""` for Examples and
# Figures, `--preview throwntogether` for ThrownTogether, plain echo export
# for Log blocks -- all of which set $preview -- and `--render ""` only for
# an example marked Render, which clears it. Verified against OpenSCAD
# 2026.02.01. BOSL2's ruler() is `if ($preview)` all the way down, so every
# ruler() example rendered as bare axes until this was seeded.

def test_image_requests_are_preview_unless_marked_render():
    from belfryscad.docsgen.imagemanager import ImageRequest

    def req(meta):
        return ImageRequest("f.scad", 1, "out.png", ["cube(1);"], meta)

    assert req("3D").preview
    assert req("3D,Big").preview
    assert req("ThrownTogether").preview, "still a preview mode in the reference"
    assert not req("Render").preview
    assert not req("3D,Render,Big").preview


def test_runner_seeds_preview_true_by_default(tmp_path):
    from belfryscad.docsgen.runner import runner

    r = runner.run(["echo(P=$preview);", "cube(1);"], str(tmp_path))
    assert r.echos and "P = true" in r.echos[0], r.echos


def test_runner_seeds_preview_false_when_asked(tmp_path):
    from belfryscad.docsgen.runner import runner

    r = runner.run(["echo(P=$preview);", "cube(1);"], str(tmp_path), preview=False)
    assert r.echos and "P = false" in r.echos[0], r.echos


def test_a_preview_only_module_actually_produces_geometry(tmp_path):
    """The shape of the reported bug, without needing BOSL2: a module
    gated on $preview drew nothing at all."""
    from belfryscad.docsgen.runner import runner

    script = ["module gated() { if ($preview) cube(10); }", "gated();"]
    assert runner.run(script, str(tmp_path)).bodies, "preview run must build geometry"
    assert not runner.run(script, str(tmp_path), preview=False).bodies


# -- abandoned temp scripts -------------------------------------------
#
# run() unlinks its own script in a `finally`, so the only way one survives
# is the process dying outright. Nothing can be done from inside the run
# that died, so the next one cleans up after it -- but only for PIDs that
# are actually gone, or two docsgen runs sharing a directory would delete
# each other's live scripts.

def test_sweep_removes_scripts_left_by_dead_processes(tmp_path):
    import os
    from belfryscad.docsgen import runner as R

    dead = tmp_path / "tmp_docsgen_999999_abc.scad"      # no such PID
    mine = tmp_path / f"tmp_docsgen_{os.getpid()}_xyz.scad"
    alive = tmp_path / f"tmp_docsgen_{os.getppid()}_live.scad"   # our parent
    other = tmp_path / "unrelated.scad"
    for f in (dead, mine, alive, other):
        f.write_text("cube(1);\n")

    R._swept_dirs.discard(str(tmp_path))
    R._sweep_abandoned_temp_files(str(tmp_path))

    assert not dead.exists(), "a script whose owner is gone must be removed"
    assert mine.exists(), "never delete our own live script"
    assert alive.exists(), "never delete a running run's script"
    assert other.exists(), "only our own prefix is ours to delete"


def test_sweep_runs_once_per_directory(tmp_path):
    from belfryscad.docsgen import runner as R

    R._swept_dirs.discard(str(tmp_path))
    R._sweep_abandoned_temp_files(str(tmp_path))
    stale = tmp_path / "tmp_docsgen_999999_abc.scad"
    stale.write_text("cube(1);\n")
    R._sweep_abandoned_temp_files(str(tmp_path))          # already swept
    assert stale.exists(), "a second sweep of the same directory is a no-op"


def test_run_leaves_no_script_behind(tmp_path):
    from belfryscad.docsgen.runner import runner

    runner.run(["cube(1);"], str(tmp_path))
    runner.run(["this is not valid scad !!!"], str(tmp_path))
    assert list(tmp_path.glob("tmp_docsgen_*")) == []


def test_batch_modes_get_their_own_process_name():
    """A batch job used to be indistinguishable from the window in ps, so a
    `pkill -f BelfrySCAD` aimed at a stuck GUI also killed docs builds."""
    from belfryscad.main import _proc_name, PROC_NAME

    assert _proc_name([]) == PROC_NAME
    assert _proc_name(["model.scad"]) == PROC_NAME
    assert _proc_name(["--docsgen", "-m"]) == PROC_NAME + "-docsgen"
    assert _proc_name(["--mdimggen"]) == PROC_NAME + "-mdimggen"
    assert _proc_name(["-o", "out.stl", "m.scad"]) == PROC_NAME + "-headless"
    assert _proc_name(["--output=out.stl", "m.scad"]) == PROC_NAME + "-headless"
    # Every name still starts with the same word, so pgrep -f finds them all.
    for argv in ([], ["--docsgen"], ["-o", "x.stl"]):
        assert _proc_name(argv).startswith(PROC_NAME)


# -- render progress ---------------------------------------------------

def _fake_manager(names):
    from belfryscad.docsgen.imagemanager import ImageManager, ImageRequest

    mgr = ImageManager()
    mgr.requests = [ImageRequest("f.scad", 1, n, ["cube(1);"], "3D") for n in names]
    done = []
    mgr.process_request = done.append
    return mgr, done


def test_progress_counts_only_the_images_actually_selected():
    """`total` must be the real work, not the queue length -- clicking one
    placeholder in a 129-image file renders one, and saying "1 of 129"
    would be a lie."""
    mgr, rendered = _fake_manager(["images/a.png", "images/b.png", "images/c.png"])
    seen = []
    mgr.process_requests(only=["images/b.png"], progress=lambda d, t: seen.append((d, t)))

    assert seen == [(0, 1), (1, 1)]
    assert [r.image_file for r in rendered] == ["images/b.png"]


def test_progress_counts_every_image_when_rendering_all():
    mgr, rendered = _fake_manager(["a.png", "b.png", "c.png"])
    seen = []
    mgr.process_requests(only=None, progress=lambda d, t: seen.append((d, t)))

    assert seen == [(0, 3), (1, 3), (2, 3), (3, 3)]
    assert len(rendered) == 3


def test_progress_is_optional_and_an_empty_queue_still_reports_a_total():
    mgr, _ = _fake_manager([])
    seen = []
    mgr.process_requests(progress=lambda d, t: seen.append((d, t)))
    assert seen == [(0, 0)], "a zero total is what tells the label to stay quiet"

    mgr, _ = _fake_manager(["a.png"])
    mgr.process_requests()          # no progress callback at all



# -- offscreen rendering ----------------------------------------------
#
# Rendering runs in a SUBPROCESS, never in pytest's own. An offscreen GL
# context is process-global state -- the framebuffer cache, the context
# itself -- so two rendering tests in one interpreter interfere, and a
# driver that dislikes the setup takes the whole suite down with it rather
# than failing a test. A subprocess gets a clean context every time and
# cannot crash the run.

_RENDER_DRIVER = """
import json, sys
from belfryscad import headless_render as HR
HR.MSAA_SAMPLES = int(sys.argv[2])
from belfryscad.docsgen.imagemanager import ImageManager, ImageRequest
out_dir, _, *jobs = sys.argv[1:]
mgr = ImageManager()
for job in jobs:
    name, meta, script = json.loads(job)
    mgr.requests = [ImageRequest(out_dir + "/f.scad", 1, out_dir + "/" + name,
                                 script, meta)]
    mgr.process_requests()
"""


def _render_in_subprocess(tmp_path, jobs, samples=4):
    """Render `jobs` [(filename, meta, script_lines), ...]; skip if this
    machine has no offscreen GL. Returns the output directory."""
    import json, subprocess, sys
    import pytest

    tmp_path.mkdir(parents=True, exist_ok=True)
    driver = tmp_path / "_driver.py"
    driver.write_text(_RENDER_DRIVER)
    r = subprocess.run(
        [sys.executable, str(driver), str(tmp_path), str(samples)]
        + [json.dumps(j) for j in jobs],
        capture_output=True, text=True)
    if r.returncode != 0 or not all((tmp_path / j[0]).exists() for j in jobs):
        pytest.skip(f"offscreen rendering unavailable here: "
                    f"{(r.stderr or r.stdout).strip()[-200:]}")
    return tmp_path


def test_a_render_is_unaffected_by_a_differently_sized_one_before_it(tmp_path):
    """moderngl's ctx.viewport writes through to whichever framebuffer is
    bound at the time -- which, when this was set before fbo.use(), was the
    PREVIOUS image's. Every fbo ended up holding the next image's size, and
    fbo.use() restored that wrong value, so a 320x240 example rendered
    after a 640x480 one was drawn at double scale and cropped.

    Only a run that mixes sizes shows it, which is every real docs build:
    Example is 320x240, Example(Med) 480x360, Example(Big) 640x480.
    """
    script = ["cube(20, center=true);"]
    jobs = [("small_a.png", "3D", script),
            ("big.png", "3D,Big", script),
            ("small_b.png", "3D", script)]
    # Both sample counts. Multisampling adds a resolve blit whose own
    # binding changes which framebuffer a stray viewport write lands on, so
    # it happens to mask this -- the bug was found with it off, and only
    # covering the multisampled path would let it come straight back.
    for samples in (1, 4):
        _render_in_subprocess(tmp_path / f"s{samples}", jobs, samples=samples)
        _assert_same_framing(tmp_path / f"s{samples}", samples)


def _assert_same_framing(out_dir, samples):
    from PySide6.QtGui import QImage

    # Mean pixel difference, not byte equality: a different GL driver may
    # shade a hair differently between two renders, and that is not what
    # this is about. The bug drew the scene at double scale and cropped it,
    # which moves the mean by a mile (82/255 when it was live).
    a = QImage(str(out_dir / "small_a.png")).convertToFormat(QImage.Format.Format_RGB888)
    b = QImage(str(out_dir / "small_b.png")).convertToFormat(QImage.Format.Format_RGB888)
    assert (a.width(), a.height()) == (b.width(), b.height())
    xs = range(0, a.width(), 3)
    ys = range(0, a.height(), 3)
    total = sum(abs(((a.pixel(x, y) >> sh) & 255) - ((b.pixel(x, y) >> sh) & 255))
                for y in ys for x in xs for sh in (16, 8, 0))
    mean_diff = total / (len(xs) * len(ys) * 3)
    assert mean_diff < 2.0, (
        f"same script, same size -> same framing, whatever was rendered in "
        f"between (MSAA samples={samples}); "
        f"mean pixel difference {mean_diff:.1f}/255")


def test_rendered_images_are_antialiased(tmp_path):
    """A docs build's images sit next to OpenSCAD's on the same wiki page,
    and OpenSCAD's are antialiased. Ours rendered every edge hard: a plain
    cube came out as four flat colours with no blended edge pixels at all.
    """
    from PySide6.QtGui import QImage

    job = [("cube.png", "3D,NoAxes,NoScales", ["cube(20, center=true);"])]
    hard = _render_in_subprocess(tmp_path / "off", job, samples=1) / "cube.png"
    soft = _render_in_subprocess(tmp_path / "on", job, samples=4) / "cube.png"

    def colours(path):
        img = QImage(str(path))
        assert not img.isNull(), path
        return {img.pixel(x, y) for y in range(img.height()) for x in range(img.width())}

    n_hard, n_soft = len(colours(hard)), len(colours(soft))
    if n_soft == n_hard:
        import pytest
        pytest.skip("this GL context offers no multisampling")
    assert n_soft > n_hard * 2, f"expected blended edges: {n_hard} -> {n_soft} colours"


def test_batch_modes_get_their_own_process_name():
    """A batch job used to be indistinguishable from the window in ps, so a
    `pkill -f BelfrySCAD` aimed at a stuck GUI also killed docs builds."""
    from belfryscad.main import _proc_name, PROC_NAME

    assert _proc_name([]) == PROC_NAME
    assert _proc_name(["model.scad"]) == PROC_NAME
    assert _proc_name(["--docsgen", "-m"]) == PROC_NAME + "-docsgen"
    assert _proc_name(["--mdimggen"]) == PROC_NAME + "-mdimggen"
    assert _proc_name(["-o", "out.stl", "m.scad"]) == PROC_NAME + "-headless"
    assert _proc_name(["--output=out.stl", "m.scad"]) == PROC_NAME + "-headless"
    # Every name still starts with the same word, so pgrep -f finds them all.
    for argv in ([], ["--docsgen"], ["-o", "x.stl"]):
        assert _proc_name(argv).startswith(PROC_NAME)


# -- render progress ---------------------------------------------------

def _fake_manager(names):
    from belfryscad.docsgen.imagemanager import ImageManager, ImageRequest

    mgr = ImageManager()
    mgr.requests = [ImageRequest("f.scad", 1, n, ["cube(1);"], "3D") for n in names]
    done = []
    mgr.process_request = done.append
    return mgr, done


def test_progress_counts_only_the_images_actually_selected():
    """`total` must be the real work, not the queue length -- clicking one
    placeholder in a 129-image file renders one, and saying "1 of 129"
    would be a lie."""
    mgr, rendered = _fake_manager(["images/a.png", "images/b.png", "images/c.png"])
    seen = []
    mgr.process_requests(only=["images/b.png"], progress=lambda d, t: seen.append((d, t)))

    assert seen == [(0, 1), (1, 1)]
    assert [r.image_file for r in rendered] == ["images/b.png"]


def test_progress_counts_every_image_when_rendering_all():
    mgr, rendered = _fake_manager(["a.png", "b.png", "c.png"])
    seen = []
    mgr.process_requests(only=None, progress=lambda d, t: seen.append((d, t)))

    assert seen == [(0, 3), (1, 3), (2, 3), (3, 3)]
    assert len(rendered) == 3


def test_progress_is_optional_and_an_empty_queue_still_reports_a_total():
    mgr, _ = _fake_manager([])
    seen = []
    mgr.process_requests(progress=lambda d, t: seen.append((d, t)))
    assert seen == [(0, 0)], "a zero total is what tells the label to stay quiet"

    mgr, _ = _fake_manager(["a.png"])
    mgr.process_requests()          # no progress callback at all



def test_overlapping_2d_shapes_layer_in_source_order(tmp_path):
    """Every 2D shape in a script is drawn as the SAME wafer-thin slab, so
    two that overlap are exactly coplanar. Under the default '<' depth test
    the second one's fragments are rejected and whichever was drawn first
    wins, which threw away every layer of a figure built by stacking 2D
    shapes: BOSL2's cyl() chamfer figure lost its coloured overlay, its "A"
    labels and its arc arrows, keeping only the grey silhouette beneath.
    """
    from PySide6.QtGui import QImage

    _render_in_subprocess(tmp_path, [("layers.png", "2D,Big,NoAxes", [
        'color("lightgray") square(20, center=true);',
        'color("red") square(10, center=true);',
    ])])

    img = QImage(str(tmp_path / "layers.png"))
    assert not img.isNull()
    # The inner square is centred, so the middle pixel belongs to whichever
    # shape won. It must be the one written last.
    mid = img.pixelColor(img.width() // 2, img.height() // 2)
    assert mid.red() > 2 * mid.green() and mid.red() > 2 * mid.blue(), (
        f"the later 2D shape must be on top, got rgb"
        f"({mid.red()},{mid.green()},{mid.blue()})")


# -- the error pane ----------------------------------------------------

_ERROR_PANE_DRIVER = """
import json, sys
from PySide6.QtWidgets import QApplication
app = QApplication([])
from belfryscad.window.docs_pane import DocsPane

pane = DocsPane()
out = {}
out["order"] = [pane._error_pane.widget(i).__class__.__name__ for i in range(2)]
out["hidden_when_clean"] = pane._error_pane.isHidden()
pane._show_errors([])
out["still_hidden_after_empty"] = pane._error_pane.isHidden()

long_msg = "Failed OpenSCAD script:\\n  line 1\\n  line 2\\n  ...trace..."
pane._show_errors([("f.scad", 12, "Short one", "warning"),
                   ("f.scad", 44, long_msg, "error")])
out["shown_when_errors"] = not pane._error_pane.isHidden()
out["headers"] = [pane._errors.headerItem().text(i)
                  for i in range(pane._errors.columnCount())]
out["rows"] = pane._errors.topLevelItemCount()
out["first_selected"] = pane._errors.currentItem().text(0)
out["detail_is_full_message"] = pane._error_text.toPlainText() == long_msg
pane._errors.setCurrentItem(pane._errors.topLevelItem(1))
out["detail_follows_selection"] = pane._error_text.toPlainText() == "Short one"

pane._show_errors([])
out["hidden_again"] = pane._error_pane.isHidden()
out["detail_cleared"] = pane._error_text.toPlainText() == ""
pane.shutdown()
print(json.dumps(out))
"""


def test_error_pane_hides_when_clean_and_shows_full_messages(tmp_path):
    """The list used to carry the message in a column, elided to one line --
    useless for a failed example, which dumps its whole script and the
    evaluator's trace. The message moved to its own scrollable pane, and
    the whole thing stays hidden while there is nothing wrong.

    Driven in a subprocess: a QWidget needs a QApplication, and building one
    inside the test process is what the GL tests already avoid.
    """
    import json, subprocess, sys
    import pytest

    driver = tmp_path / "_errpane.py"
    driver.write_text(_ERROR_PANE_DRIVER)
    r = subprocess.run([sys.executable, str(driver)], capture_output=True, text=True)
    if r.returncode != 0:
        pytest.skip(f"no Qt GUI available here: {(r.stderr or '').strip()[-200:]}")

    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out["order"] == ["QTreeWidget", "QTextBrowser"], \
        "list on the left, message on the right"
    assert out["hidden_when_clean"], "a fresh pane shows no error list"
    assert out["still_hidden_after_empty"], "a clean build shows no error list"
    assert out["shown_when_errors"], "errors must bring the pane back"
    assert out["headers"] == ["Line", "Level"], "the message is no longer a column"
    assert out["rows"] == 2
    assert out["first_selected"] == "44", "errors sort ahead of warnings"
    assert out["detail_is_full_message"], "the detail pane shows every line"
    assert out["detail_follows_selection"]
    assert out["hidden_again"] and out["detail_cleared"]
