"""#435: `{{r}}` in a code span was eaten as a link.

parse_links scanned the whole line with no notion of backticks, so text a
library documents as literal -- an inline style code the reader is meant to
copy -- lost its braces AND logged "Invalid Link" as an error. A library
could not document its own syntax.
"""
from belfryscad.docsgen.preview import build_preview

HEAD = """\
//////////////////////////////////////////////////////////////////
// LibFile: w.scad
//   Inline style codes.
//////////////////////////////////////////////////////////////////

// Section: Text

// Module: write()
// Synopsis: Writes styled text.
// Usage:
//   write(string);
// Description:
"""


def _render(tmp_path, *body_lines):
    src = HEAD + "".join(f"//   {ln}\n" for ln in body_lines) + "module write(s) { text(s); }\n"
    path = tmp_path / "w.scad"
    path.write_text(src)
    return build_preview(src, str(path), gen_images=False)


def test_a_code_span_is_left_exactly_as_typed(tmp_path):
    pv = _render(tmp_path, "* `{{r}}` = regular", "* `{{bi}}` = bold italic")
    assert pv.errors == [], pv.errors
    assert "`{{r}}`" in pv.markdown
    assert "`{{bi}}`" in pv.markdown


def test_a_real_link_outside_a_span_still_resolves(tmp_path):
    """The fix must not switch linking off -- only hold it back inside
    backticks. write() is a real module here, so it becomes a link."""
    pv = _render(tmp_path, "See {{write()}} for details.")
    assert pv.errors == [], pv.errors
    line = next(ln for ln in pv.markdown.splitlines() if "for details" in ln)
    assert "{{" not in line, line
    assert "write" in line and "](" in line, f"expected a markdown link: {line}"


def test_both_on_one_line(tmp_path):
    pv = _render(tmp_path, "Use `{{b}}` with {{write()}} together.")
    assert pv.errors == [], pv.errors
    line = next(ln for ln in pv.markdown.splitlines() if "together" in ln)
    assert "`{{b}}`" in line, line          # the span survives
    assert "](" in line                      # and the link still resolved


def test_an_unmatched_backtick_does_not_swallow_the_line(tmp_path):
    """A lone backtick opens no span, so what follows is still processed."""
    pv = _render(tmp_path, "A stray ` tick then {{write()}}.")
    assert pv.errors == [], pv.errors
    line = next(ln for ln in pv.markdown.splitlines() if "stray" in ln)
    assert "](" in line, line


def test_a_double_backtick_span_also_holds(tmp_path):
    pv = _render(tmp_path, "Literal ``{{r}}`` here.")
    assert pv.errors == [], pv.errors
    assert "``{{r}}``" in pv.markdown
