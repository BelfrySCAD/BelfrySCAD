#!/usr/bin/env python3
"""The AI's geometry/console tools read live state, and it can start a render.

The bug this covers: AIToolContext's geometry_summary and console_text are
captured once, on the GUI thread, when a turn begins. Accepting a proposal
re-renders, so a model that changed the script and then measured it was
shown the state from before its own edit -- or, if nothing had been rendered
when the turn started, was told "nothing has been rendered yet" no matter
what happened since.

Qt widgets crash pytest in this project, so this runs standalone.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

failures = []


def check(label, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + label + (f"  -- {detail}" if not cond and detail else ""))
    if not cond:
        failures.append(label)


def gui_checks():
    """The GUI half: the callables the context is actually wired to. The
    pure-function checks above pass against any lambda, so they say nothing
    about whether MainWindow supplies one that works."""
    from PySide6.QtGui import QSurfaceFormat
    fmt = QSurfaceFormat()
    fmt.setVersion(3, 3)
    fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
    QSurfaceFormat.setDefaultFormat(fmt)
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication(sys.argv)
    from belfryscad.window.main_window import MainWindow
    from PySide6.QtCore import QEventLoop

    w = MainWindow()
    # Shown, or the viewport never gets a GL context and _on_render_done
    # dies partway through -- leaving no geometry and no "Rendered
    # successfully" line, which reads exactly like the bug being tested for.
    w.show()
    deadline = __import__("time").monotonic
    def pump(seconds, until=lambda: False):
        end = deadline() + seconds
        while deadline() < end:
            app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 20)
            if until():
                return True
        return False
    pump(1.0)
    # _live_state_threadsafe blocks on the GUI thread servicing it, so it
    # cannot be called from the GUI thread itself. Run it where it really
    # runs -- a worker -- and pump events meanwhile.
    import threading
    out = {}

    def worker():
        out["state"] = w._live_state_threadsafe()
    t = threading.Thread(target=worker)
    t.start()
    while t.is_alive():
        app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 50)
    t.join()

    st = out.get("state")
    check("the live-state bridge answers a worker thread", isinstance(st, dict) and st, str(st))
    if st:
        check("and returns the three keys the tools read",
              set(st) == {"geometry", "console", "rendering"}, str(set(st)))
        check("with rendering as a bool", isinstance(st["rendering"], bool))
        check("and nothing rendered yet in a fresh window",
              st["geometry"] == "" and st["rendering"] is False, str(st))

    check("no render is busy in a fresh window", w._render_busy() is False)

    # request_render refuses an empty document rather than queueing a
    # no-op the model would then wait on forever.
    check("render_threadsafe declines when there is nothing to render",
          w._render_threadsafe() is False)

    # The slot invokeMethod targets must exist by name, or the queued call
    # is dropped with only a console warning -- the render would silently
    # never happen.
    from PySide6.QtCore import QMetaObject
    mo = w.metaObject()
    for slot in ("_render_for_ai", "_service_ai_state_request"):
        check(f"{slot} is a registered slot",
              mo.indexOfMethod(f"{slot}()") >= 0)

    # And it really does start one for a document with content.
    w._new_document()
    w._current_tab().editor.setPlainText("cube(10);")
    check("render_threadsafe accepts a document with content",
          w._render_threadsafe() is True)
    # Waiting on the geometry, not on _render_busy: a cube renders in ~1ms,
    # too fast to catch the busy flag between event-loop passes.
    landed = pump(30, lambda: bool(w._geometry_summary()) and not w._render_busy())
    check("the queued render actually runs", landed, "queued call never ran")

    # The timestamp the model keys off has to be in the console text the
    # tool returns, not just in the widget.
    tail = w._console_tail()
    check("the finished render is timestamped in the console",
          re.search(r"Rendered successfully at \d\d:\d\d:\d\d", tail) is not None,
          tail[-200:])

    # Non-empty is not enough. describe_geometry returned "" for every render
    # ever made -- the bodies are shims with no measurement API, so every
    # read threw into a bare `except: continue`. Check the numbers.
    summary = w._geometry_summary()
    check("the geometry is measured, with the right numbers",
          "volume 1000.000" in summary and "surface area 600.000" in summary
          and "12 triangles" in summary, summary)
    check("and reports a genus for a solid cube", "genus 0" in summary, summary)

    # Genus is the measurement most likely to be quietly wrong, and the one
    # a shim could never have produced.
    w._current_tab().editor.setPlainText(
        "difference() { cube(10, center=true); cylinder(h=20, r=2, center=true); }")
    w._render_threadsafe()
    pump(30, lambda: "genus 1" in w._geometry_summary())
    check("a drilled cube is measured as genus 1",
          "genus 1" in w._geometry_summary(), w._geometry_summary())

    # An open shell converts to an empty Manifold, which was reported as a
    # solid part with 0 triangles, genus 1 and an inverted-infinity bounding
    # box -- for a surface plainly visible in the viewport.
    w._current_tab().editor.setPlainText("""
        polyhedron(points=[[0,0,0],[10,0,0],[10,10,0],[0,10,0],
                           [0,0,10],[10,0,10],[10,10,10],[0,10,10]],
                   faces=[[0,2,1],[0,3,2],[4,5,6],[4,6,7],
                          [0,1,5],[0,5,4],[1,2,6],[1,6,5],
                          [2,3,7],[2,7,6]]);
    """)
    w._render_threadsafe()
    pump(30, lambda: "open surface" in w._geometry_summary())
    s = w._geometry_summary()
    check("an open shell is named as an open surface, not a solid",
          "open surface" in s and "0 solid part(s)" in s, s)
    check("with its real triangle count, not the empty Manifold's zero",
          "10 triangles" in s, s)
    check("and a real bounding box rather than inverted infinities",
          "inf" not in s and "10.000 x 10.000 x 10.000" in s, s)
    check("its area is measured from the triangles", "500.000" in s, s)

    # check_geometry, on the real thing. The open shell is still rendered
    # here, so it must come back unsound and name the reason.
    rep = w._geometry_check()
    check("check_geometry calls the open shell unsound",
          "NOT a closed manifold solid" in rep and "1 unsound" in rep, rep)
    check("and names the holes", "boundary edge" in rep, rep)
    check("and reports what a file would contain",
          "Merged for export:" in rep, rep)
    check("flagging that the surface is written as-is",
          "written as-is" in rep, rep)

    # A sound solid must come back clean, or the tool is just an alarm.
    w._current_tab().editor.setPlainText("cube(10);")
    w._render_threadsafe()
    pump(30, lambda: "volume 1000.000" in w._geometry_summary())
    rep = w._geometry_check()
    check("a sound cube checks out clean",
          "1 part(s) checked, 0 unsound." in rep and "closed manifold solid" in rep, rep)
    check("and its merged mesh is sound too",
          "Merged for export: 12 triangles, a closed manifold solid" in rep, rep)

    # The threadsafe wrapper is what the tool actually calls -- and it can
    # only be exercised from a worker, like the state bridge.
    got = {}
    t2 = threading.Thread(target=lambda: got.update(r=w._check_geometry_threadsafe()))
    t2.start()
    while t2.is_alive():
        app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 20)
    t2.join()
    check("the check bridge answers a worker thread",
          "0 unsound" in (got.get("r") or ""), str(got.get("r"))[:120])

    # An anchored edit must apply to the LIVE buffer, not to the whole-file
    # content built from the turn-start snapshot -- otherwise accepting it
    # silently reverts whatever the user typed while the turn was running.
    from belfryscad.window.ai_tools import Proposal
    w._current_tab().editor.setPlainText("a = 1;\nb = 2;\n")
    tab = w._current_tab()
    stale_whole_file = "a = 1;\nb = 3;\n"
    # The user types a new line while the turn is in flight.
    tab.editor.setPlainText("a = 1;\nb = 2;\nc = 9;  // typed by the user\n")
    w._on_ai_proposal_accepted(Proposal(
        kind="edit", summary="", new_content=stale_whole_file, diff_text="",
        tab_id=tab.chat_id, anchor="b = 2;", replacement="b = 3;"))
    text = tab.editor.toPlainText()
    check("an anchored edit applies to the live buffer", "b = 3;" in text, text)
    check("and does not revert what the user typed meanwhile",
          "typed by the user" in text, text)

    # And when the anchor is gone, it must refuse rather than guess.
    tab.editor.setPlainText("totally different\n")
    w._on_ai_proposal_accepted(Proposal(
        kind="edit", summary="", new_content="x", diff_text="",
        tab_id=tab.chat_id, anchor="b = 2;", replacement="b = 3;"))
    check("a vanished anchor leaves the script untouched",
          tab.editor.toPlainText() == "totally different\n",
          tab.editor.toPlainText())
    check("and says why", "no longer in the script" in w._console_tail(),
          w._console_tail()[-160:])

    # An ambiguous anchor at apply time is equally unsafe.
    tab.editor.setPlainText("b = 2;\nb = 2;\n")
    w._on_ai_proposal_accepted(Proposal(
        kind="edit", summary="", new_content="x", diff_text="",
        tab_id=tab.chat_id, anchor="b = 2;", replacement="b = 3;"))
    check("an anchor that became ambiguous is refused too",
          tab.editor.toPlainText() == "b = 2;\nb = 2;\n", tab.editor.toPlainText())

    # A parameter change is re-applied to the live buffer for the same
    # reason an anchored edit is -- and more urgently, since the Customizer
    # is the pane the user is most likely to be moving mid-turn.
    tab.editor.setPlainText("height = 20;  // [10:100]\nwall = 2;\n")
    w._on_ai_proposal_accepted(Proposal(
        kind="edit", summary="", diff_text="", tab_id=tab.chat_id,
        new_content="height = 45;  // [10:100]\nwall = 2;\n",
        param_changes={"height": 45}))
    check("a parameter change is applied",
          "height = 45;  // [10:100]" in tab.editor.toPlainText(),
          tab.editor.toPlainText())

    # The user moves a different slider while the turn is in flight.
    tab.editor.setPlainText("height = 20;  // [10:100]\nwall = 7;\n")
    w._on_ai_proposal_accepted(Proposal(
        kind="edit", summary="", diff_text="", tab_id=tab.chat_id,
        new_content="height = 45;  // [10:100]\nwall = 2;\n",
        param_changes={"height": 45}))
    text = tab.editor.toPlainText()
    check("it changes the parameter it meant to", "height = 45" in text, text)
    check("and leaves the value the user changed meanwhile",
          "wall = 7;" in text, text)

    # A parameter that has since been deleted must not be silently ignored.
    tab.editor.setPlainText("wall = 2;\n")
    w._on_ai_proposal_accepted(Proposal(
        kind="edit", summary="", diff_text="", tab_id=tab.chat_id,
        new_content="height = 45;\n", param_changes={"height": 45}))
    check("a parameter that no longer exists applies nothing",
          tab.editor.toPlainText() == "wall = 2;\n", tab.editor.toPlainText())
    check("and says so", "no longer" in w._console_tail(), w._console_tail()[-160:])

    # Whole-file proposals must still work exactly as before.
    tab.editor.setPlainText("old\n")
    w._on_ai_proposal_accepted(Proposal(
        kind="edit", summary="", new_content="brand new\n", diff_text="",
        tab_id=tab.chat_id))
    check("an unanchored proposal still replaces the whole file",
          tab.editor.toPlainText() == "brand new\n", tab.editor.toPlainText())

    # propose_new_script passes a filename that the accept path dropped, so
    # the tab it created read as "Untitled" and the argument did nothing.
    from belfryscad.window.ai_tools import Proposal
    w._on_ai_proposal_accepted(Proposal(
        kind="new_file", summary="", new_content="cube(3);",
        diff_text="", filename="probe.scad"))
    tab = w._current_tab()
    check("an accepted new script keeps the name the model gave it",
          tab.suggested_name == "probe.scad", str(tab.suggested_name))
    check("and the tab shows it instead of Untitled",
          tab.display_name().startswith("probe.scad"), tab.display_name())

    w.close()


def main():
    from belfryscad.window import ai_tools as T

    # --- live reads win over the turn-start snapshot --------------------
    # The snapshot says nothing was rendered; the live state says otherwise.
    # This is the exact situation in the reported session.
    ctx = T.AIToolContext(library_dir=Path("."), geometry_summary="",
                          console_text="",
                          live_state=lambda: {"geometry": "1 part, 10x10x10",
                                              "console": "Rendered successfully at 09:00:00",
                                              "rendering": False})
    check("describe_geometry reports the render that landed mid-turn",
          T.describe_geometry(ctx) == "1 part, 10x10x10", T.describe_geometry(ctx))
    check("read_console reports the console as it is now",
          "09:00:00" in T.read_console(ctx), T.read_console(ctx))

    # A stale snapshot must not leak through once live state exists.
    ctx.geometry_summary = "2 parts, from before the edit"
    check("the stale snapshot does not win over live state",
          "before the edit" not in T.describe_geometry(ctx))

    # --- a render in flight is not "nothing rendered" -------------------
    busy = T.AIToolContext(library_dir=Path("."),
                           live_state=lambda: {"geometry": "", "console": "",
                                               "rendering": True})
    g, c = T.describe_geometry(busy), T.read_console(busy)
    check("describe_geometry says a render is in progress", "in progress" in g, g)
    check("and points at the followup rather than at the user",
          'schedule_followup(when="render")' in g and "ask the user" not in g, g)
    check("read_console says the same", "in progress" in c, c)

    # --- genuinely nothing rendered ------------------------------------
    empty = T.AIToolContext(library_dir=Path("."),
                            live_state=lambda: {"geometry": "", "console": "",
                                                "rendering": False})
    g = T.describe_geometry(empty)
    check("with nothing rendered and nothing running, it still says so",
          "nothing has been rendered" in g, g)
    check("but now names the tool that fixes it", "render()" in g, g)

    # --- the snapshot remains the fallback ------------------------------
    # Sessions that predate live_state, and any failure to reach the GUI
    # thread, must still answer rather than error.
    old = T.AIToolContext(library_dir=Path("."), geometry_summary="1 part",
                          console_text="hello")
    check("without live_state the snapshot is used", T.describe_geometry(old) == "1 part")
    check("and for the console too", T.read_console(old) == "hello")

    def boom():
        raise RuntimeError("GUI thread gone")
    broke = T.AIToolContext(library_dir=Path("."), geometry_summary="1 part",
                            live_state=boom)
    check("a live read that throws falls back rather than failing the turn",
          T.describe_geometry(broke) == "1 part")

    timeout = T.AIToolContext(library_dir=Path("."), geometry_summary="1 part",
                              live_state=lambda: {})
    check("a live read that times out falls back too",
          T.describe_geometry(timeout) == "1 part")

    # --- the render tool ------------------------------------------------
    called = []
    tabs = [T.TabSnapshot(id=7, name="a.scad", path=None,
                          modified=False, text="cube(1);")]
    ok = T.AIToolContext(library_dir=Path("."), open_tabs=tabs,
                         request_render=lambda i=None: called.append(i) or True)
    out = T.render(ok)
    check("render() starts a render", called == [None], str(called))
    check("and says the result is not ready yet",
          "not finished" in out and 'schedule_followup(when="render")' in out, out)

    # Tab targeting: without it the model can only render whatever tab
    # happens to be active, and accepting a new script changes that silently.
    called.clear()
    T.render(ok, id=7)
    check("render(id=) targets that script", called == [7], str(called))
    called.clear()
    bad = T.render(ok, id=99)
    check("render() rejects an unknown id instead of rendering the wrong tab",
          bad.startswith("Error:") and called == [], bad)

    nothing = T.AIToolContext(library_dir=Path("."),
                              request_render=lambda i=None: False)
    check("render() with no script reports that, rather than claiming success",
          T.render(nothing).startswith("Error:"), T.render(nothing))
    check("render() is unavailable rather than crashing when unwired",
          T.render(T.AIToolContext(library_dir=Path("."))).startswith("Error:"))

    # An empty tool result reads as a failed call, not as an empty file.
    empty_tab = [T.TabSnapshot(id=3, name="e.scad", path=None,
                               modified=False, text="  \n")]
    ectx = T.AIToolContext(library_dir=Path("."), open_tabs=empty_tab)
    check("read_open_script says an empty script is empty",
          T.read_open_script(ectx, 3).strip() != "", repr(T.read_open_script(ectx, 3)))

    # --- search_library --------------------------------------------------
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        lib = Path(td)
        (lib / "sub").mkdir()
        (lib / "a.scad").write_text("module cuboid(size) {}\n// cuboid used here\n")
        (lib / "sub" / "b.scad").write_text("function cuboid_of(x) = x;\n")
        (lib / "notes.txt").write_text("cuboid should not be found here\n")
        s = T.AIToolContext(library_dir=lib)

        out = T.search_library(s, r"^\s*module\s+cuboid")
        check("search_library finds a definition", "a.scad:1:" in out, out)
        check("and does not return the whole file",
              "used here" not in out, out)

        out = T.search_library(s, "cuboid")
        check("it searches subdirectories too",
              "a.scad:1:" in out and "b.scad:1:" in out.replace("sub/", ""), out)
        check("and ignores non-.scad files", "notes.txt" not in out, out)

        out = T.search_library(s, "cuboid", path="sub")
        check("a path narrows the search", "a.scad" not in out and "b.scad" in out, out)

        check("a bad path is refused",
              T.search_library(s, "x", path="../..").startswith("Error:"),
              T.search_library(s, "x", path="../.."))
        check("a non-scad path is refused",
              T.search_library(s, "x", path="notes.txt").startswith("Error:"))
        check("a missing path is reported",
              T.search_library(s, "x", path="nope").startswith("Error:"))
        check("an invalid regex is an error, not a crash",
              T.search_library(s, "cuboid(").startswith("Error: invalid"),
              T.search_library(s, "cuboid("))
        check("an empty pattern is refused",
              T.search_library(s, "  ").startswith("Error:"))
        check("no matches says so rather than returning nothing",
              "No matches" in T.search_library(s, "zzzznotthere"))

        # The cap has to actually stop, or the tool costs what reading cost.
        (lib / "many.scad").write_text("\n".join(f"x{i} = 1;" for i in range(50)))
        out = T.search_library(s, r"^x\d", max_results=5)
        check("max_results caps the output",
              len([l for l in out.splitlines() if "many.scad:" in l]) == 5, out)
        check("and says it stopped early", "stopped at 5" in out, out)

        check("no libraries installed is reported",
              "No OpenSCAD libraries" in T.search_library(
                  T.AIToolContext(library_dir=lib / "nope"), "x"))

    # --- check_geometry ---------------------------------------------------
    live_ok = {"geometry": "1 part", "console": "", "rendering": False}
    cg = T.AIToolContext(library_dir=Path("."), live_state=lambda: live_ok,
                         check_geometry=lambda: "2 part(s) checked, 0 unsound.")
    check("check_geometry returns the report",
          "0 unsound" in T.check_geometry(cg), T.check_geometry(cg))

    nothing_yet = T.AIToolContext(
        library_dir=Path("."), check_geometry=lambda: "",
        live_state=lambda: {"geometry": "", "console": "", "rendering": False})
    check("check_geometry with nothing rendered points at render()",
          "render()" in T.check_geometry(nothing_yet), T.check_geometry(nothing_yet))

    mid = T.AIToolContext(
        library_dir=Path("."), check_geometry=lambda: "",
        live_state=lambda: {"geometry": "", "console": "", "rendering": True})
    check("and while rendering, waits rather than reporting nothing",
          "in progress" in T.check_geometry(mid), T.check_geometry(mid))

    def boom2():
        raise RuntimeError("kaboom")
    check("a check that throws is reported, not raised",
          T.check_geometry(T.AIToolContext(
              library_dir=Path("."), live_state=lambda: live_ok,
              check_geometry=boom2)).startswith("Error:"))
    check("and an unwired check says so",
          T.check_geometry(T.AIToolContext(library_dir=Path("."))).startswith("Error:"))

    # --- propose_script_replace -------------------------------------------
    SRC = "a = 1;\nmodule m() { cube(a); }\nb = 2;\n"
    seen = []
    rtabs = [T.TabSnapshot(id=4, name="s.scad", path=None, modified=False,
                           text=SRC)]

    def rctx(mode=T.MODE_MANUAL):
        return T.AIToolContext(library_dir=Path("."), open_tabs=rtabs,
                               mode=mode, on_proposal=seen.append)

    seen.clear()
    r = T.propose_script_replace(rctx(), 4, "b = 2;", "b = 3;", "bump b")
    check("propose_script_replace proposes the change", len(seen) == 1, r)
    if seen:
        p = seen[0]
        check("the whole file is still carried for the review diff",
              p.new_content == "a = 1;\nmodule m() { cube(a); }\nb = 3;\n",
              repr(p.new_content))
        check("and the anchor is carried for applying",
              p.anchor == "b = 2;" and p.replacement == "b = 3;", str(p.anchor))
        check("the diff shows only the changed line",
              "-b = 2;" in p.diff_text and "-a = 1;" not in p.diff_text, p.diff_text)

    # Ambiguity has to be refused, not resolved by picking the first: that
    # is the failure mode that silently edits the wrong place.
    dup = [T.TabSnapshot(id=5, name="d.scad", path=None, modified=False,
                         text="x = 1;\ny = 0;\nx = 1;\n")]
    dctx = T.AIToolContext(library_dir=Path("."), open_tabs=dup,
                           on_proposal=seen.append)
    seen.clear()
    r = T.propose_script_replace(dctx, 5, "x = 1;", "x = 2;", "s")
    check("an ambiguous anchor is refused",
          r.startswith("Error:") and "2 times" in r and seen == [], r)

    seen.clear()
    r = T.propose_script_replace(rctx(), 4, "nowhere", "x", "s")
    check("an anchor that isn't there is refused",
          r.startswith("Error:") and seen == [], r)
    check("and says to re-read rather than guess", "Read the script" in r, r)

    r = T.propose_script_replace(rctx(), 4, "b = 2;", "b = 2;", "s")
    check("a no-op edit is refused", r.startswith("Error:"), r)
    r = T.propose_script_replace(rctx(), 4, "", "x", "s")
    check("an empty anchor is refused", r.startswith("Error:"), r)
    r = T.propose_script_replace(rctx(), 99, "b = 2;", "x", "s")
    check("an unknown tab id is refused", r.startswith("Error:"), r)
    r = T.propose_script_replace(rctx(T.MODE_PLAN), 4, "b = 2;", "x", "s")
    check("Plan mode refuses it like the other propose tools",
          r.startswith("Error:") and "Plan" in r, r)

    seen.clear()
    r = T.propose_script_replace(rctx(), 4, "b = 2;\n", "", "delete b")
    check("an empty new_text deletes the passage",
          seen and seen[0].new_content == "a = 1;\nmodule m() { cube(a); }\n",
          repr(seen[0].new_content) if seen else r)

    # --- customizer parameters ---------------------------------------------
    PSRC = (
        "/* [Size] */\n"
        "// Overall height\n"
        "height = 20;  // [10:100]\n"
        "// Wall thickness\n"
        "wall = 2;\n"
        "/* [Style] */\n"
        "finish = \"matte\";  // [matte:Matte, gloss:Glossy]\n"
        "rounded = true;\n"
        "size = [1, 2, 3];  // [0:10]\n"
        "module body() { cube(height); }\n"
    )
    ptabs = [T.TabSnapshot(id=8, name="p.scad", path=None, modified=False,
                           text=PSRC)]
    pseen = []

    def pctx(mode=T.MODE_MANUAL):
        return T.AIToolContext(library_dir=Path("."), open_tabs=ptabs,
                               mode=mode, on_proposal=pseen.append)

    import json as _json
    listed = T.list_parameters(pctx(), 8)
    params = _json.loads(listed)
    by = {p["name"]: p for p in params}
    check("list_parameters finds the parameters",
          set(by) == {"height", "wall", "finish", "rounded", "size"}, listed)
    check("and not the module", "body" not in by, str(list(by)))
    check("it reports current values",
          by["height"]["value"] == 20 and by["rounded"]["value"] is True, listed)
    check("types are distinguished, and a bool is not a number",
          by["rounded"]["type"] == "boolean" and by["height"]["type"] == "number"
          and by["finish"]["type"] == "string" and by["size"]["type"] == "vector",
          str({k: v["type"] for k, v in by.items()}))
    check("a slider's range is carried",
          by["height"]["range"] == {"min": 10.0, "max": 100.0, "step": 1},
          str(by["height"].get("range")))
    check("a dropdown's options and labels are carried",
          [o["value"] for o in by["finish"]["options"]] == ["matte", "gloss"]
          and by["finish"]["options"][1]["label"] == "Glossy",
          str(by["finish"].get("options")))
    check("groups come from the tab headers",
          by["height"]["group"] == "Size" and by["finish"]["group"] == "Style",
          str({k: v["group"] for k, v in by.items()}))
    check("descriptions come from the preceding comment",
          by["height"]["description"] == "Overall height",
          repr(by["height"]["description"]))
    check("an unconstrained parameter simply has no range",
          "range" not in by["wall"] and "options" not in by["wall"], str(by["wall"]))

    check("a script with no parameters says so",
          "no customizer parameters" in T.list_parameters(
              T.AIToolContext(library_dir=Path("."), open_tabs=[
                  T.TabSnapshot(id=9, name="n.scad", path=None,
                                modified=False, text="cube(1);\n")]), 9))
    check("an unknown id is refused", T.list_parameters(pctx(), 99).startswith("Error:"))

    # Changing one
    pseen.clear()
    r = T.propose_parameter_change(pctx(), 8, {"height": 45}, "taller")
    check("propose_parameter_change proposes it", len(pseen) == 1, r)
    if pseen:
        p = pseen[0]
        check("the new value is written into the source",
              "height = 45;  // [10:100]" in p.new_content, p.new_content)
        check("the constraint comment survives",
              "[10:100]" in p.new_content, p.new_content)
        check("and the change is carried for applying",
              p.param_changes == {"height": 45}, str(p.param_changes))
        changed = [l for l in p.diff_text.splitlines()
                   if l[:1] in "+-" and not l.startswith(("---", "+++"))]
        check("the diff changes only that line",
              changed == ["-height = 20;  // [10:100]",
                          "+height = 45;  // [10:100]"], str(changed))

    pseen.clear()
    r = T.propose_parameter_change(pctx(), 8, {"height": 30, "rounded": False},
                                   "two at once")
    check("several parameters can change at once",
          pseen and "height = 30" in pseen[0].new_content
          and "rounded = false" in pseen[0].new_content,
          pseen[0].new_content if pseen else r)

    # Validation -- each of these would otherwise write nonsense into the
    # user's script for them to review.
    pseen.clear()
    for label, changes, expect in [
        ("a value outside a slider's range", {"height": 500}, "10.0..100.0"),
        ("a value below it", {"height": 1}, "10.0..100.0"),
        ("a string for a number", {"height": "tall"}, "is a number"),
        ("a bool for a number", {"height": True}, "is a number"),
        ("a number for a boolean", {"rounded": 1}, "is a boolean"),
        ("an option that isn't offered", {"finish": "satin"}, "must be one of"),
        ("a number for a string", {"finish": 3}, "is a string"),
        ("a vector of the wrong length", {"size": [1, 2]}, "3 element"),
        ("a vector element out of range", {"size": [1, 2, 99]}, "0.0..10.0"),
        ("a non-vector for a vector", {"size": 5}, "vector of numbers"),
        ("an unknown parameter", {"nope": 1}, "not a parameter"),
    ]:
        out = T.propose_parameter_change(pctx(), 8, changes, "s")
        check(label + " is refused",
              out.startswith("Error:") and expect in out, out)
    check("and none of those proposed anything", pseen == [], str(pseen))

    check("a change that changes nothing is refused",
          T.propose_parameter_change(pctx(), 8, {"height": 20},
                                     "s").startswith("Error:"))
    check("an empty change set is refused",
          T.propose_parameter_change(pctx(), 8, {}, "s").startswith("Error:"))
    check("a non-object change set is refused",
          T.propose_parameter_change(pctx(), 8, "height=5",
                                     "s").startswith("Error:"))
    check("Plan mode refuses parameter changes too",
          T.propose_parameter_change(pctx(T.MODE_PLAN), 8, {"height": 45},
                                     "s").startswith("Error:"))
    check("a script with no parameters is refused",
          T.propose_parameter_change(
              T.AIToolContext(library_dir=Path("."), open_tabs=[
                  T.TabSnapshot(id=9, name="n.scad", path=None, modified=False,
                                text="cube(1);\n")]), 9, {"x": 1},
              "s").startswith("Error:"))

    # --- registration ----------------------------------------------------
    names = [t["name"] for t in T.TOOLS]
    check("render is a registered tool", "render" in names, str(names))
    check("and is dispatchable by name",
          T.run_tool(ok, "render", {}) == T.render(ok), T.run_tool(ok, "render", {}))
    check("search_library is dispatchable by name",
          "Error" not in T.run_tool(T.AIToolContext(library_dir=Path("src")),
                                    "search_library", {"pattern": "zzz"}))
    # The CLI transports build their allowlists from TOOLS, so a tool that
    # is registered is permitted; a hardcoded list would have missed this.
    from belfryscad.window import ai_cli, ai_copilot_cli
    check("the claude CLI allows it",
          "mcp__belfryscad__render" in ai_cli._TOOL_NAMES)
    check("the copilot CLI allows it",
          "belfryscad-render" in ai_copilot_cli._TOOL_NAMES)

    from belfryscad.window.ai_chat import _TOOL_ACTIVITY
    for n in names:
        check(f"{n} has a progress label", n in _TOOL_ACTIVITY)

    check("the prompt tells the model reads are stale until the followup",
          "schedule_followup(when=\"render\")" in T.SYSTEM_PROMPT
          and "Rendered successfully at" in T.SYSTEM_PROMPT)

    gui_checks()

    print("\n" + ("ALL PASS" if not failures else f"{len(failures)} FAILED: {failures}"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
