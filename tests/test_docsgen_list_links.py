"""#1121: a {{link}} in a BulletList or NumberedList section -- BOSL2's
Side Effects is one -- was printed as typed: those two blocks never ran
parse_links over their lines."""
from belfryscad.docsgen.preview import build_preview

SRC = """\
//////////////////////////////////////////////////////////////////
// LibFile: w.scad
//   Lists.
//////////////////////////////////////////////////////////////////

// DefineHeader(BulletList): Side Effects
// DefineHeader(NumList): Steps

// Section: Text

// Module: write()
// Synopsis: Writes text.
// Usage:
//   write(string);
// Description:
//   Writes it.
// Side Effects:
//   Sets `$x` through {{write()}}.
// Steps:
//   Call {{write()}}.
module write(s) { text(s); }
"""


def test_links_resolve_in_bullet_and_numbered_lists(tmp_path):
    path = tmp_path / "w.scad"
    path.write_text(SRC)
    pv = build_preview(SRC, str(path), gen_images=False)
    assert pv.errors == [], pv.errors
    for marker in ("Sets `$x`", "Call "):
        line = next(ln for ln in pv.markdown.splitlines() if marker in ln)
        assert "{{" not in line and "](" in line, line
