"""A body line indented less than the first one should say WHICH line.

Two faults, one symptom. The parser built `origin` only after the
body-reading loop, so this error -- raised inside that loop -- reached a
handler whose very first act was to read the unassigned local, and docsgen
died with UnboundLocalError instead of reporting anything. Once reported,
it pointed at the line the BLOCK was declared on, which in a 1500-line
library is not guidance.
"""
from belfryscad.docsgen.errorlog import ErrorLog
from belfryscad.docsgen.preview import build_preview

SRC = """\
//////////////////////////////////////////////////////////////////
// LibFile: bad.scad
//   A library with one badly indented doc body.
//////////////////////////////////////////////////////////////////

// Section: Shapes

// Module: thing()
// Synopsis: Makes a thing.
// Usage:
//   thing(size);
// Description:
//     This line sets the indent to four.
//   This line has less, which is the error.
module thing(size=1) { cube(size); }
"""


def _parse(tmp_path, text, name="bad.scad"):
    """Through build_preview -- the Docs pane's own entry point -- so this
    covers the path the pane actually takes, not just the parser."""
    src = tmp_path / name
    src.write_text(text)
    return build_preview(text, str(src), gen_images=False).errors


def test_the_offending_line_is_named(tmp_path):
    entries = _parse(tmp_path, SRC)
    assert entries, "the error must be reported, not raised as UnboundLocalError"
    file, line, msg, level = entries[0]
    assert level == ErrorLog.FAIL
    # Line 14 is "//   This line has less, which is the error." -- NOT line
    # 12, where the Description block is declared.
    assert line == 14, f"expected the offending body line, got {line}"
    assert SRC.splitlines()[line - 1].strip().startswith("//   This line has less")
    assert "less indentation" in msg


def test_a_well_indented_block_reports_nothing(tmp_path):
    good = SRC.replace("//   This line has less, which is the error.\n",
                       "//     This line keeps the indent.\n")
    assert _parse(tmp_path, good, "good.scad") == []


def test_other_errors_still_point_at_the_block(tmp_path):
    """e.line is only set where a specific line is at fault; everything else
    keeps the declaration line it always reported."""
    missing_file_block = "// Section: Shapes\n\n// Module: thing()\n"
    entries = _parse(tmp_path, missing_file_block, "nofile.scad")
    assert entries, "a Section before any LibFile is still an error"
    assert entries[0][1] == 1, "and still points at the block that was declared"
