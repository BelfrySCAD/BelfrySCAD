"""#415: a documentation comment inside /* ... */ is not documentation.

The vendored parser knows only that a doc comment starts with `//`, so
commenting a chunk of a library out with a block comment left everything
in it documented, validated and rendering example images. Upstream
openscad_docsgen has the same gap.
"""
from belfryscad.docsgen.block_comments import strip_block_comments
from belfryscad.docsgen.preview import build_preview


def _stripped(text):
    return "".join(strip_block_comments(text.splitlines(keepends=True)))


def test_a_block_comment_is_blanked_but_the_lines_remain():
    src = "/* off\n// Function: gone()\n*/\n// Function: kept()\n"
    out = strip_block_comments(src.splitlines(keepends=True))
    assert len(out) == 4                       # line numbers cannot drift
    assert "gone" not in "".join(out)
    assert out[3] == "// Function: kept()\n"


def test_what_does_not_open_a_block_comment():
    # In a string, in a line comment, and a `*/` with no opener.
    assert _stripped('a = "/* x */";\n') == 'a = "/* x */";\n'
    assert _stripped("// a /* in prose\n") == "// a /* in prose\n"
    assert _stripped('s = "he said \\"/*\\" once";\n') == 's = "he said \\"/*\\" once";\n'
    # Opened and closed on one line: the code either side survives, the
    # comment does not, and the line keeps its length so columns still line up.
    line = "x = 1; /* t */ y = 2;\n"
    out = _stripped(line)
    assert len(out) == len(line)
    assert out.startswith("x = 1;") and out.rstrip().endswith("y = 2;")
    assert "/*" not in out and "t" not in out


def test_block_comments_do_not_nest():
    # C-style: the FIRST */ closes it, so `two()` is live code again.
    out = _stripped("/* a /* b */\n// Function: two()\n")
    assert "// Function: two()" in out


DEMO = """\
// LibFile: demo.scad
//   A tiny library.
// Includes:
//   include <demo.scad>
// FileGroup: Testing
// FileSummary: One module.

// Section: Shapes

/* (no longer used)
// Module: old_widget()
// Synopsis: Should not be documented.
// Usage:
//   old_widget();
// Description:
//   Commented out with everything else.
module old_widget() cube(1);
*/

// Module: widget()
// Synopsis: Makes a widget.
// Usage:
//   widget();
// Description:
//   Makes a widget.
module widget() cube(1);
"""


def test_a_commented_out_module_is_not_documented(tmp_path):
    src = tmp_path / "demo.scad"
    src.write_text(DEMO)
    preview = build_preview(DEMO, str(src), gen_images=False)

    assert preview.errors == []
    assert "widget()" in preview.markdown
    assert "old_widget" not in preview.markdown
    assert "Should not be documented" not in preview.markdown
