"""Tests for belfryscad.window.ai_tools -- the tools the AI chat pane
exposes to the model.

Pure-function tests: AIToolContext is plain data by design (so tool
handlers never touch Qt objects from the worker thread), which makes the
whole module directly testable with a hand-built context and tmp_path.
"""
import json

from belfryscad.window.ai_tools import (
    AIToolContext, Proposal, TabSnapshot, is_path_within,
    list_library_files, list_open_scripts, propose_new_script,
    propose_script_edit, read_library_file, read_open_script, run_tool,
)


def _ctx(tmp_path, tabs=None, proposals=None):
    return AIToolContext(
        library_dir=tmp_path,
        open_tabs=tabs or [],
        on_proposal=(proposals.append if proposals is not None else None),
    )


class TestIsPathWithin:
    def test_inside(self, tmp_path):
        (tmp_path / "a").mkdir()
        assert is_path_within(tmp_path, tmp_path / "a" / "b.scad") is True

    def test_outside(self, tmp_path):
        assert is_path_within(tmp_path, tmp_path.parent / "elsewhere.scad") is False

    def test_traversal_escape(self, tmp_path):
        assert is_path_within(tmp_path, tmp_path / ".." / "escaped.scad") is False


class TestLibraryTools:
    def test_lists_only_scad_files(self, tmp_path):
        (tmp_path / "BOSL2").mkdir()
        (tmp_path / "BOSL2" / "std.scad").write_text("// std")
        (tmp_path / "BOSL2" / "README.md").write_text("not scad")
        out = list_library_files(_ctx(tmp_path))
        assert "BOSL2/std.scad" in out
        assert "README.md" not in out

    def test_empty_library_dir(self, tmp_path):
        assert "No OpenSCAD libraries" in list_library_files(_ctx(tmp_path))

    def test_missing_library_dir(self, tmp_path):
        assert "No OpenSCAD libraries" in list_library_files(_ctx(tmp_path / "nope"))

    def test_read_file(self, tmp_path):
        (tmp_path / "a.scad").write_text("cube(1);")
        assert read_library_file(_ctx(tmp_path), "a.scad") == "cube(1);"

    def test_read_rejects_non_scad(self, tmp_path):
        (tmp_path / "secrets.txt").write_text("hunter2")
        out = read_library_file(_ctx(tmp_path), "secrets.txt")
        assert "only .scad files" in out
        assert "hunter2" not in out

    def test_read_rejects_traversal(self, tmp_path):
        outside = tmp_path.parent / "outside.scad"
        outside.write_text("secret")
        out = read_library_file(_ctx(tmp_path), "../outside.scad")
        assert "outside the library directory" in out
        assert "secret" not in out

    def test_read_missing_file(self, tmp_path):
        assert "no such library file" in read_library_file(_ctx(tmp_path), "nope.scad")

    def test_extension_check_precedes_containment_check(self, tmp_path):
        # A path that fails BOTH guards reports the extension one first,
        # so each guard has a distinguishable message.
        out = read_library_file(_ctx(tmp_path), "../outside.txt")
        assert "only .scad files" in out


class TestOpenScriptTools:
    def test_list(self, tmp_path):
        tabs = [TabSnapshot(1, "a.scad", "/tmp/a.scad", False, "cube(1);")]
        data = json.loads(list_open_scripts(_ctx(tmp_path, tabs)))
        assert data == [{"id": 1, "name": "a.scad", "path": "/tmp/a.scad",
                         "modified": False}]

    def test_list_when_none_open(self, tmp_path):
        assert "No scripts" in list_open_scripts(_ctx(tmp_path))

    def test_read(self, tmp_path):
        tabs = [TabSnapshot(7, "a.scad", None, True, "sphere(2);")]
        assert read_open_script(_ctx(tmp_path, tabs), 7) == "sphere(2);"

    def test_read_unknown_id(self, tmp_path):
        assert "no open script with id 99" in read_open_script(_ctx(tmp_path), 99)


class TestProposals:
    def test_edit_queues_proposal_with_diff(self, tmp_path):
        proposals = []
        tabs = [TabSnapshot(1, "a.scad", None, False, "cube(1);\n")]
        out = propose_script_edit(_ctx(tmp_path, tabs, proposals), 1,
                                   "cube(2);\n", "Make it bigger")
        assert "proposed to the user for review" in out
        assert len(proposals) == 1
        p = proposals[0]
        assert isinstance(p, Proposal)
        assert p.kind == "edit" and p.tab_id == 1
        assert p.summary == "Make it bigger"
        assert p.new_content == "cube(2);\n"
        assert "-cube(1);" in p.diff_text and "+cube(2);" in p.diff_text

    def test_edit_unknown_id_queues_nothing(self, tmp_path):
        proposals = []
        out = propose_script_edit(_ctx(tmp_path, [], proposals), 42, "x", "s")
        assert "no open script with id 42" in out
        assert proposals == []

    def test_edit_rejects_identical_content(self, tmp_path):
        proposals = []
        tabs = [TabSnapshot(1, "a.scad", None, False, "cube(1);\n")]
        out = propose_script_edit(_ctx(tmp_path, tabs, proposals), 1,
                                   "cube(1);\n", "No-op")
        assert "identical" in out
        assert proposals == []

    def test_new_script(self, tmp_path):
        proposals = []
        out = propose_new_script(_ctx(tmp_path, [], proposals), "gear.scad",
                                  "cylinder(h=1,r=5);\n", "A gear blank")
        assert "proposed to the user for review" in out
        assert proposals[0].kind == "new_file"
        assert proposals[0].filename == "gear.scad"

    def test_new_script_rejects_non_scad(self, tmp_path):
        proposals = []
        out = propose_new_script(_ctx(tmp_path, [], proposals), "notes.txt",
                                  "hello", "Some notes")
        assert "only .scad files" in out
        assert proposals == []

    def test_new_script_rejects_path_in_filename(self, tmp_path):
        proposals = []
        out = propose_new_script(_ctx(tmp_path, [], proposals),
                                  "../evil.scad", "x", "s")
        assert "must not contain a path" in out
        assert proposals == []


class TestRunTool:
    def test_dispatches(self, tmp_path):
        tabs = [TabSnapshot(3, "a.scad", None, False, "cube(1);")]
        assert run_tool(_ctx(tmp_path, tabs), "read_open_script", {"id": 3}) == "cube(1);"

    def test_unknown_tool(self, tmp_path):
        assert "unknown tool" in run_tool(_ctx(tmp_path), "nope", {})

    def test_bad_arguments_reported_not_raised(self, tmp_path):
        out = run_tool(_ctx(tmp_path), "read_open_script", {"wrong": 1})
        assert "bad arguments" in out
