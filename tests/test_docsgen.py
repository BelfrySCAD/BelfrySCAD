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
