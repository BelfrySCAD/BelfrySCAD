"""#1149 (BOSL2#1889): `// FileTitle:` names a file in headings, the TOC,
the sidebar and the cheat sheet in place of its filename, so a std.scad
part is not presented as a file to include on its own. Output files and
links keep the filename."""
import pytest

from belfryscad.docsgen import main

SRC = """\
// LibFile: shapes3d.scad
//   Basic solids.
// FileTitle: 3D Shapes
// FileGroup: Basics

// Section: Cubes

// Module: box()
// Synopsis: A box.
// Usage:
//   box(s);
// Description:
//   A box.
module box(s) cube(s);
"""


def _docs(tmp_path, monkeypatch, src):
    (tmp_path / "shapes3d.scad").write_text(src)
    monkeypatch.chdir(tmp_path)
    main(["-q", "-n", "-m", "-t", "-s", "-c", "-D", str(tmp_path / "docs"), "shapes3d.scad"])
    return {p.name: p.read_text() for p in (tmp_path / "docs").iterdir() if p.suffix == ".md"}


def test_a_file_title_replaces_the_filename_where_it_is_shown(tmp_path, monkeypatch):
    docs = _docs(tmp_path, monkeypatch, SRC)
    assert docs["shapes3d.scad.md"].splitlines()[0] == "# 3D Shapes"     # the page itself, by filename
    assert "[3D Shapes](#1-3d-shapes)" in docs["TOC.md"]
    assert "[3D Shapes](shapes3d.scad)" in docs["TOC.md"]                  # the link keeps the file
    assert "[3D Shapes](shapes3d.scad)" in docs["_Sidebar.md"]
    assert "3D Shapes" in docs["CheatSheet.md"]
    for name in ("TOC.md", "_Sidebar.md", "CheatSheet.md"):
        assert "[shapes3d.scad]" not in docs[name] and "LibFile: shapes3d.scad" not in docs[name], name


def test_without_one_the_filename_is_shown_as_before(tmp_path, monkeypatch):
    docs = _docs(tmp_path, monkeypatch, SRC.replace("// FileTitle: 3D Shapes\n", ""))
    assert docs["shapes3d.scad.md"].splitlines()[0] == "# LibFile: shapes3d.scad"
    assert "[shapes3d.scad](shapes3d.scad)" in docs["_Sidebar.md"]


def test_a_file_title_needs_a_title_and_no_body(tmp_path, monkeypatch, capsys):
    _docs(tmp_path, monkeypatch, SRC.replace("// FileTitle: 3D Shapes", "// FileTitle:"))
    assert 'Must provide a subtitle when declaring block: "FileTitle"' in capsys.readouterr().err
    _docs(tmp_path, monkeypatch, SRC.replace("// FileTitle: 3D Shapes", "// FileTitle: 3D Shapes\n//   More."))
    assert 'Body not supported, while declaring block: "FileTitle"' in capsys.readouterr().err
