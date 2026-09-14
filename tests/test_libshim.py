"""A library previewed from outside the libraries folder resolves to itself."""
import os

import pytest

from belfryscad.libshim import detect, library_shim, shim_dir


def test_detect_names_the_library_the_directory_provides(tmp_path):
    (tmp_path / "std.scad").write_text("// the library\n")
    assert detect(str(tmp_path), ["include <BOSL2/std.scad>", "cube(1);"]) == ("BOSL2", str(tmp_path))


def test_detect_ignores_a_file_that_merely_uses_the_library(tmp_path):
    # No std.scad beside it: this is a model, not the library itself.
    assert detect(str(tmp_path), ["include <BOSL2/std.scad>"]) is None


def test_detect_ignores_an_include_with_no_library_name(tmp_path):
    (tmp_path / "sibling.scad").write_text("")
    assert detect(str(tmp_path), ["include <sibling.scad>"]) is None


def test_detect_handles_a_nested_target(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "part.scad").write_text("")
    assert detect(str(tmp_path), ["use <MyLib/sub/part.scad>"]) == ("MyLib", str(tmp_path))


@pytest.mark.skipif(os.name == "nt", reason="symlink needs Developer Mode")
def test_shim_resolves_the_name_to_the_checkout(tmp_path):
    real = tmp_path / "BOSL2-pr2046"
    real.mkdir()
    (real / "std.scad").write_text("")
    shim = shim_dir("BOSL2", str(real))
    # This is the lookup the parser performs for `include <BOSL2/std.scad>`.
    assert os.path.isfile(os.path.join(shim, "BOSL2", "std.scad"))


@pytest.mark.skipif(os.name == "nt", reason="symlink needs Developer Mode")
def test_shim_scopes_the_env_and_keeps_the_installed_libraries(tmp_path, monkeypatch):
    real = tmp_path / "checkout"
    real.mkdir()
    monkeypatch.setenv("OPENSCADPATH", "/somewhere/libraries")
    with library_shim(("BOSL2", str(real))):
        parts = os.environ["OPENSCADPATH"].split(os.pathsep)
        # Prepended, so it wins; and the existing path survives, because
        # setting OPENSCADPATH replaces the default rather than adding to it.
        assert os.path.isdir(os.path.join(parts[0], "BOSL2"))
        assert "/somewhere/libraries" in parts
    assert os.environ["OPENSCADPATH"] == "/somewhere/libraries"


def test_shim_is_a_no_op_without_a_name(monkeypatch):
    monkeypatch.delenv("OPENSCADPATH", raising=False)
    with library_shim(None):
        assert "OPENSCADPATH" not in os.environ


def test_docs_pane_announces_a_redirected_library(tmp_path, qtbot_free=None):
    """The status line says so only when the folder name differs from the
    library name -- a checkout already called BOSL2 needs no explanation."""
    from belfryscad.window.docs_pane import DocsPane   # noqa: F401  (import check)
    import os.path
    from belfryscad.libshim import detect

    checkout = tmp_path / "BOSL2-pr2046"
    checkout.mkdir()
    (checkout / "std.scad").write_text("")
    src = checkout / "text.scad"
    src.write_text("// Includes:\n//   include <BOSL2/std.scad>\n")

    found = detect(os.path.dirname(str(src)), src.read_text().splitlines())
    assert found == ("BOSL2", str(checkout))
    assert os.path.basename(found[1]) != found[0]                # so: announce

    plain = tmp_path / "BOSL2"
    plain.mkdir()
    (plain / "std.scad").write_text("")
    same = plain / "text.scad"
    same.write_text("// Includes:\n//   include <BOSL2/std.scad>\n")
    found2 = detect(os.path.dirname(str(same)), same.read_text().splitlines())
    assert os.path.basename(found2[1]) == found2[0]              # so: stay quiet


def test_detect_climbs_to_the_library_root_from_a_test_suite(tmp_path):
    """A .scadtest runs from tests/, where std.scad is not -- only the
    parent identifies the library."""
    root = tmp_path / "BOSL2-pr2046"
    (root / "tests").mkdir(parents=True)
    (root / "std.scad").write_text("")
    assert detect(str(root / "tests"),
                  ["include <BOSL2/std.scad>"]) == ("BOSL2", str(root))


def test_detect_does_not_climb_out_of_the_world(tmp_path):
    """Four levels down is past the limit: no match rather than a wild one."""
    deep = tmp_path / "lib" / "a" / "b" / "c" / "d"
    deep.mkdir(parents=True)
    (tmp_path / "lib" / "std.scad").write_text("")
    assert detect(str(deep), ["include <BOSL2/std.scad>"]) is None


def test_coverage_files_a_shimmed_origin_under_its_real_path(tmp_path):
    """A library reached through the shim is the same file as one reached
    directly -- one row in the report, not two half-covered ones."""
    from belfryscad.coverage import CoverageReport
    import os

    real = tmp_path / "BOSL2-pr2046"
    real.mkdir()
    (real / "std.scad").write_text("")
    shim = shim_dir("BOSL2", str(real))
    via_shim = os.path.join(shim, "BOSL2", "std.scad")
    direct = str(real / "std.scad")

    report = CoverageReport()
    span = {"start": 0, "end": 9, "kind": "statement", "line": 1,
            "column": 1, "arm": 0, "hits": 1}
    report.merge_spans([dict(span, origin=via_shim)])
    report.merge_spans([dict(span, origin=direct)])

    assert len(report.spans) == 1
    (origin, *_), = report.spans
    assert origin == os.path.realpath(direct)
    assert shim not in origin
