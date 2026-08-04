"""Tests for belfryscad.window.ai_tools -- the tools the AI chat pane
exposes to the model.

Pure-function tests: AIToolContext is plain data by design (so tool
handlers never touch Qt objects from the worker thread), which makes the
whole module directly testable with a hand-built context and tmp_path.
"""
import base64
import json

import pytest

from belfryscad.window.ai_tools import (
    MODE_ACCEPT, MODE_AUTO, MODE_MANUAL, MODE_PLAN,
    TRIGGER_DELAY, TRIGGER_RENDER,
    AIToolContext, Proposal, TabSnapshot, ToolImage, is_path_within,
    list_library_files, list_open_scripts, propose_new_script,
    describe_geometry, propose_script_edit, read_library_file,
    read_console, read_open_script, run_tool, schedule_followup,
    view_viewport,
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


class TestViewViewport:
    def test_returns_the_captured_png(self, tmp_path):
        ctx = _ctx(tmp_path)
        ctx.viewport_png = b"\x89PNG\r\n\x1a\nFAKE"
        ctx.viewport_note = "Viewport 800x600"
        result = view_viewport(ctx)
        assert isinstance(result, ToolImage)
        assert base64.b64decode(result.data_b64) == ctx.viewport_png
        assert result.mime == "image/png"
        assert result.caption == "Viewport 800x600"

    def test_no_render_is_an_actionable_error(self, tmp_path):
        result = view_viewport(_ctx(tmp_path))
        assert isinstance(result, str)
        assert "no rendered view" in result

    def test_dispatches_through_run_tool(self, tmp_path):
        ctx = _ctx(tmp_path)
        ctx.viewport_png = b"png-bytes"
        assert isinstance(run_tool(ctx, "view_viewport", {}), ToolImage)



class TestDescribeGeometry:
    def test_returns_the_snapshot_summary(self, tmp_path):
        ctx = _ctx(tmp_path)
        ctx.geometry_summary = "Rendered model: 2 solid part(s)."
        assert describe_geometry(ctx) == "Rendered model: 2 solid part(s)."

    def test_nothing_rendered_is_actionable(self, tmp_path):
        assert "nothing has been rendered" in describe_geometry(_ctx(tmp_path))


class TestNamedViews:
    def test_named_view_goes_through_capture_view(self, tmp_path):
        asked = []
        ctx = _ctx(tmp_path)
        ctx.capture_view = lambda v, ov=None: (asked.append(v), ("QUJD", "Top view"))[1]
        result = view_viewport(ctx, "top")
        assert asked == ["top"]
        assert isinstance(result, ToolImage) and result.caption == "Top view"

    def test_current_view_uses_the_snapshot_not_a_rerender(self, tmp_path):
        ctx = _ctx(tmp_path)
        ctx.viewport_png = b"png"
        ctx.capture_view = lambda v, ov=None: pytest.fail("should not re-render")
        assert isinstance(view_viewport(ctx, "current"), ToolImage)

    def test_unknown_view_rejected(self, tmp_path):
        out = view_viewport(_ctx(tmp_path), "sideways")
        assert "unknown view" in out and "front" in out

    def test_named_view_without_capture_support(self, tmp_path):
        assert "only the current view" in view_viewport(_ctx(tmp_path), "top")

    def test_capture_failure_is_actionable(self, tmp_path):
        ctx = _ctx(tmp_path)
        ctx.capture_view = lambda v, ov=None: None
        assert "couldn't render" in view_viewport(ctx, "iso")

    def test_default_is_current(self, tmp_path):
        ctx = _ctx(tmp_path)
        ctx.viewport_png = b"png"
        assert isinstance(view_viewport(ctx), ToolImage)


class TestScheduleFollowup:
    """The model can schedule itself, so validation matters: an unattended
    loop against a paid API is the failure mode worth designing against.
    The chain cap that backs this up lives in the pane."""

    def _ctx_fu(self, tmp_path, got):
        ctx = _ctx(tmp_path)
        ctx.on_followup = got.append
        return ctx

    def test_schedules(self, tmp_path):
        got = []
        out = schedule_followup(self._ctx_fu(tmp_path, got), 30, "check it")
        assert "scheduled in 30s" in out
        assert got[0].delay_s == 30 and got[0].prompt == "check it"

    def test_delay_too_short_rejected(self, tmp_path):
        got = []
        assert "between" in schedule_followup(self._ctx_fu(tmp_path, got), 1, "x")
        assert got == []

    def test_delay_too_long_rejected(self, tmp_path):
        got = []
        assert "between" in schedule_followup(self._ctx_fu(tmp_path, got), 99999, "x")
        assert got == []

    def test_zero_cancels(self, tmp_path):
        got = []
        assert "cancelled" in schedule_followup(self._ctx_fu(tmp_path, got), 0)
        assert got == [None]

    def test_prompt_required_unless_cancelling(self, tmp_path):
        got = []
        assert "prompt is required" in schedule_followup(self._ctx_fu(tmp_path, got), 30, "  ")
        assert got == []

    def test_non_numeric_delay(self, tmp_path):
        got = []
        assert "must be a number" in schedule_followup(self._ctx_fu(tmp_path, got), "soon", "x")

    def test_unavailable_without_a_hook(self, tmp_path):
        assert "aren't available" in schedule_followup(_ctx(tmp_path), 30, "x")

    def test_prompt_is_stripped(self, tmp_path):
        got = []
        schedule_followup(self._ctx_fu(tmp_path, got), 30, "  padded  ")
        assert got[0].prompt == "padded"


class TestPlanMode:
    """Plan mode is enforced at the tool layer, not just in the UI -- the
    claude-CLI transport calls these same handlers over MCP, where no Qt
    code is in the loop to gate anything."""

    def _plan_ctx(self, tmp_path, got):
        ctx = _ctx(tmp_path,
                   tabs=[TabSnapshot(1, "a.scad", None, False, "cube(1);\n")],
                   proposals=got)
        ctx.mode = MODE_PLAN
        return ctx

    def test_edit_refused(self, tmp_path):
        got = []
        out = propose_script_edit(self._plan_ctx(tmp_path, got), 1, "cube(2);\n", "x")
        assert "Plan mode" in out and "Describe" in out
        assert got == []

    def test_new_script_refused(self, tmp_path):
        got = []
        out = propose_new_script(self._plan_ctx(tmp_path, got), "g.scad", "x", "y")
        assert "Plan mode" in out
        assert got == []

    def test_refusal_precedes_other_validation(self, tmp_path):
        # A non-.scad name in plan mode reports the mode, not the extension:
        # the mode is the reason nothing will happen either way.
        got = []
        out = propose_new_script(self._plan_ctx(tmp_path, got), "notes.txt", "x", "y")
        assert "Plan mode" in out

    def test_other_modes_still_propose(self, tmp_path):
        for mode in (MODE_MANUAL, MODE_ACCEPT, MODE_AUTO):
            got = []
            ctx = self._plan_ctx(tmp_path, got)
            ctx.mode = mode
            propose_script_edit(ctx, 1, "cube(2);\n", "x")
            assert len(got) == 1, mode

    def test_reads_are_never_gated(self, tmp_path):
        # Plan mode restricts changes, not looking around.
        ctx = self._plan_ctx(tmp_path, [])
        assert read_open_script(ctx, 1) == "cube(1);\n"

    def test_default_mode_is_manual(self, tmp_path):
        assert _ctx(tmp_path).mode == MODE_MANUAL


class TestRenderTriggeredFollowup:
    """when='render' waits for the next render instead of a clock, so the
    viewport image and measurements reflect the change just made."""

    def _ctx_fu(self, tmp_path, got):
        ctx = _ctx(tmp_path)
        ctx.on_followup = got.append
        return ctx

    def test_queues_a_render_trigger(self, tmp_path):
        got = []
        out = schedule_followup(self._ctx_fu(tmp_path, got),
                                when="render", prompt="check it")
        assert "next render" in out
        assert got[0].trigger == TRIGGER_RENDER and got[0].prompt == "check it"

    def test_no_delay_needed_for_render(self, tmp_path):
        # delay_seconds is irrelevant here, so omitting it must not read as
        # the "0 means cancel" case.
        got = []
        schedule_followup(self._ctx_fu(tmp_path, got), when="render", prompt="x")
        assert got and got[0] is not None

    def test_prompt_still_required(self, tmp_path):
        got = []
        out = schedule_followup(self._ctx_fu(tmp_path, got), when="render")
        assert "prompt is required" in out
        assert got == []

    def test_unknown_trigger_rejected(self, tmp_path):
        got = []
        out = schedule_followup(self._ctx_fu(tmp_path, got),
                                when="someday", prompt="x")
        assert "must be one of" in out
        assert got == []

    def test_delay_remains_the_default(self, tmp_path):
        got = []
        schedule_followup(self._ctx_fu(tmp_path, got), 30, "later")
        assert got[0].trigger == TRIGGER_DELAY and got[0].delay_s == 30

    def test_zero_delay_still_cancels(self, tmp_path):
        got = []
        assert "cancelled" in schedule_followup(self._ctx_fu(tmp_path, got), 0)
        assert got == [None]


class TestReadConsole:
    def test_returns_the_captured_tail(self, tmp_path):
        ctx = _ctx(tmp_path)
        ctx.console_text = "ERROR: Parser error in line 12"
        assert read_console(ctx) == "ERROR: Parser error in line 12"

    def test_empty_console_explains_itself(self, tmp_path):
        assert "console is empty" in read_console(_ctx(tmp_path))

    def test_whitespace_only_counts_as_empty(self, tmp_path):
        ctx = _ctx(tmp_path)
        ctx.console_text = "   \n\n  "
        assert "console is empty" in read_console(ctx)

    def test_dispatches_through_run_tool(self, tmp_path):
        ctx = _ctx(tmp_path)
        ctx.console_text = "WARNING: something"
        assert run_tool(ctx, "read_console", {}) == "WARNING: something"


class TestViewViewportOptions:
    """Angle and display overrides colour one AI-requested image only; the
    user's own camera and display settings are restored afterwards (that
    half lives in MainWindow._service_ai_view_request)."""

    def _ctx_cap(self, tmp_path, calls):
        ctx = _ctx(tmp_path)
        ctx.viewport_png = b"png"
        ctx.capture_view = lambda v, ov: (calls.append((v, dict(ov))),
                                          ("QUJD", "cap"))[1]
        return ctx

    def test_plain_current_uses_the_snapshot(self, tmp_path):
        calls = []
        assert isinstance(view_viewport(self._ctx_cap(tmp_path, calls)), ToolImage)
        assert calls == []          # no re-render needed

    def test_any_override_forces_a_rerender_of_current(self, tmp_path):
        calls = []
        view_viewport(self._ctx_cap(tmp_path, calls), projection="orthographic")
        assert calls[0][1]["projection"] == "orthographic"

    def test_arbitrary_angles_passed_through(self, tmp_path):
        calls = []
        view_viewport(self._ctx_cap(tmp_path, calls), azimuth=137.5, elevation=-22)
        assert calls[0][1]["azimuth"] == 137.5
        assert calls[0][1]["elevation"] == -22.0

    def test_display_flags_passed_through(self, tmp_path):
        calls = []
        view_viewport(self._ctx_cap(tmp_path, calls), view="top",
                      axes=False, edges=True)
        assert calls[0][0] == "top"
        assert calls[0][1]["axes"] is False and calls[0][1]["edges"] is True

    def test_bad_projection_rejected(self, tmp_path):
        calls = []
        out = view_viewport(self._ctx_cap(tmp_path, calls), projection="isometric")
        assert "projection must be one of" in out and calls == []

    def test_elevation_out_of_range_rejected(self, tmp_path):
        calls = []
        out = view_viewport(self._ctx_cap(tmp_path, calls), elevation=120)
        assert "between -90 and 90" in out and calls == []

    def test_non_numeric_angle_rejected(self, tmp_path):
        calls = []
        out = view_viewport(self._ctx_cap(tmp_path, calls), azimuth="sideways")
        assert "must be a number" in out and calls == []

    def test_elevation_limits_are_inclusive(self, tmp_path):
        calls = []
        ctx = self._ctx_cap(tmp_path, calls)
        assert isinstance(view_viewport(ctx, elevation=90), ToolImage)
        assert isinstance(view_viewport(ctx, elevation=-90), ToolImage)
