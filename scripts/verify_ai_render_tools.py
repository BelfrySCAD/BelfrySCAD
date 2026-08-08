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
    # These tabs are deliberately left modified, and closing one prompts.
    # QMessageBox.exec() spins its own nested event loop, so a prompt here
    # would hang the script rather than fail it.
    w.skip_unsaved_prompts = True
    # Or closing this window writes its scratch tabs, opened debugger
    # dock and raised AI dock over the user's real saved layout.
    w.persist_settings = False
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

    # The three newer bridges, driven from a worker like the tools do.
    def from_worker(fn):
        box = {}
        th = threading.Thread(target=lambda: box.update(r=fn()))
        th.start()
        while th.is_alive():
            app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 20)
        th.join()
        return box.get("r")

    tab.editor.setPlainText("cube(42);")
    live = from_worker(w._live_tabs_threadsafe)
    check("the live-tabs bridge answers a worker thread",
          isinstance(live, list) and live, str(live)[:100])
    check("and returns the text as it is right now",
          any("cube(42);" in t.text for t in live or []),
          str([t.text[:20] for t in live or []]))
    check("every tab carries the id the tools address it by",
          all(isinstance(t.id, int) for t in live or []))

    dirs = from_worker(w._project_dirs_threadsafe)
    check("the project-dirs bridge answers, empty for unsaved tabs",
          isinstance(dirs, list), str(dirs))

    check("the profile report is empty before any profiled render",
          w._profile_report() == "", w._profile_report())
    check("and the bridge agrees",
          from_worker(w._profile_report_threadsafe) == "")

    # An ordinary render must NOT produce a profile, or profile=true would
    # be doing nothing and the report would just be left over.
    tab.editor.setPlainText(
        "module ring(n) { for (i=[0:n]) rotate(i*10) translate([8,0,0]) cube(2); }\n"
        "ring(20);\n")
    w._render_threadsafe()
    pump(30, lambda: bool(w._geometry_summary()) and not w._render_busy())
    check("an uninstrumented render leaves no profile", w._profile_report() == "",
          w._profile_report()[:120])

    # Now the instrumented one, through the same call the tool makes.
    check("render_threadsafe accepts profile=True",
          w._render_threadsafe(profile=True) is True)
    got = pump(60, lambda: bool(w._profile_report()) and not w._render_busy())
    check("a profiled render produces a report", got, "no profile appeared")
    rep = w._profile_report()
    check("naming the module that was called", "ring" in rep, rep[:200])
    check("with call counts and self time",
          "called" in rep and "self " in rep and "ms" in rep, rep[:200])
    check("and a total for the render", "Profiled render:" in rep, rep[:80])
    check("the total covers geometry too, not just script time",
          "building geometry" in rep and "running the script" in rep, rep[:200])
    check("and says what the percentages are of",
          "of the" in rep and "script time" in rep, rep[:400])
    # The advice branch needs geometry to dominate, which a toy model will
    # not do reliably -- drive it with a stand-in result instead.
    from types import SimpleNamespace
    real = w._last_profile_result
    w._last_profile_result = SimpleNamespace(
        resolve_time=0.10, generate_time=0.90, total_time=1.00,
        unattributed_time=0.01,
        call_sites=[SimpleNamespace(name="slow", kind="module", call_count=3,
                                    caller_name="<toplevel>", call_origin=None,
                                    call_line=7, call_column=2,
                                    self_time=0.09, cumulative_time=0.09)])
    heavy = w._profile_report()
    check("when geometry dominates, it says so plainly",
          "90% ) went" .replace(" ", "") in heavy.replace(" ", "")
          or "90%) went on geometry" in heavy, heavy[:300])
    check("and says what actually helps",
          "fewer/cheaper booleans" in heavy and "not by rewriting" in heavy,
          heavy[:400])
    check("a call site with no origin still renders",
          "slow (module) called 3x" in heavy, heavy[-200:])
    w._last_profile_result = real
    check("the bridge returns it too",
          "Profiled render:" in (from_worker(w._profile_report_threadsafe) or ""))

    # The report window belongs to the user's own menu path; an AI-triggered
    # profile must not pop one at them mid-conversation.
    check("no report window was opened", app.activeModalWidget() is None)
    check("but the console says where to find it",
          "Show Profile Report" in w._console_tail(), w._console_tail()[-200:])

    # A real debug session, driven through the same bridge the tools use.
    # The file must live on a canonical path: on macOS tempfile hands back
    # /var/... while resolve() gives /private/var/..., and breakpoints are
    # matched by resolved path, so a symlinked temp dir makes them silently
    # never fire.
    import tempfile as _tf, os as _os
    dbg_dir = Path(_os.path.realpath(_tf.mkdtemp()))
    dbg_file = dbg_dir / "dbg.scad"
    dbg_file.write_text("h = 10;\nw = 4;\nmodule post(n) {\n"
                        "    d = n * 2;\n    cube([w, w, d]);\n}\npost(h);\n")
    w.open_file_by_path(str(dbg_file))
    pump(2.0)
    dtab = w._current_tab()
    check("the debug script opened", dtab.file_path == str(dbg_file),
          str(dtab.file_path))

    def dbg_call(action, arg=None):
        box = {}
        th = threading.Thread(
            target=lambda: box.update(r=w._debug_threadsafe(action, arg)))
        th.start()
        while th.is_alive():
            app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 20)
        th.join()
        return box.get("r") or {}

    st = dbg_call("start", {"id": dtab.chat_id, "breakpoints": [5]})
    check("a debug session starts and stops at the first line",
          st.get("status") == "paused" and st.get("line") == 1, str(st)[:200])
    check("naming the real file, not the temp one it parsed",
          st.get("file") == "dbg.scad", str(st.get("file")))
    check("the breakpoint appears in the editor gutter for the user to see",
          4 in dtab.editor._breakpoints, str(dtab.editor._breakpoints))

    check("debug_state reports without moving",
          dbg_call("state").get("line") == 1)

    st = dbg_call("resume", "over")
    check("stepping over advances a line", st.get("line") == 2, str(st)[:160])

    st = dbg_call("resume", "continue")
    check("continuing reaches the breakpoint", st.get("line") == 5, str(st)[:200])
    frame = (st.get("frames") or [{}])[0].get("variables") or {}
    check("the frame shows the module's own locals",
          frame.get("n") == "10.0" and frame.get("d") == "20.0", str(frame)[:200])
    check("and the caller's variables too", frame.get("w") == "4.0", str(frame)[:200])
    check("the script's own names come before the $ specials",
          list(frame)[0] in ("d", "h", "n", "w"), str(list(frame)[:4]))
    stack = st.get("stack") or []
    check("the call stack is readable, not repr'd Position objects",
          stack and "post()" in stack[0] and "object at 0x" not in " ".join(stack),
          str(stack))
    check("and names where the call came from", "dbg.scad:7" in stack[0], str(stack))

    from belfryscad.window import ai_tools as T
    txt = T._format_debug_state(st)
    check("which formats into something a model can act on",
          "Paused at dbg.scad:5." in txt and "n = 10.0" in txt, txt[:200])

    # to_child on a module with no children never matches its target and
    # comes back at the same place. Silently returning the unchanged state
    # reads as a successful step, and a model can loop on it.
    # to_child on a call with no children silently degrades to continue --
    # it runs to the next breakpoint or off the end of the script. Handing
    # that back as a successful step is how a model comes to believe it
    # stepped somewhere it did not.
    st = dbg_call("resume", "to_child")
    check("to_child with no children refuses instead of running on",
          "no children" in (st.get("message") or ""), str(st.get("message")))
    check("and the session is still paused where it was",
          st.get("status") == "paused" and st.get("line") == 5, str(st)[:140])
    check("so the session was not consumed",
          dbg_call("state").get("line") == 5, str(dbg_call("state"))[:120])

    check("a second session is refused while one is running",
          "already running" in (dbg_call(
              "start", {"id": dtab.chat_id}).get("message") or ""),
          str(dbg_call("state"))[:120])

    check("stopping ends the session", dbg_call("stop").get("status") == "stopped")
    check("and afterwards there is nothing to step",
          dbg_call("resume", "over").get("status") == "idle")
    check("starting an unknown script is refused",
          "not open" in (dbg_call("start", {"id": 9999}).get("message") or ""))

    # The whole way through the real tool, not the raw bridge: asking for a
    # breakpoint should land on it, not on the mandatory first stop.
    from belfryscad.window import ai_tools as _T
    tool_ctx = _T.AIToolContext(
        library_dir=Path("."),
        open_tabs=[_T.TabSnapshot(id=dtab.chat_id, name="dbg.scad",
                                  path=str(dbg_file), modified=False,
                                  text=dbg_file.read_text())],
        debug_control=w._debug_threadsafe)

    def tool_call(fn, *a):
        box = {}
        th = threading.Thread(target=lambda: box.update(r=fn(tool_ctx, *a)))
        th.start()
        while th.is_alive():
            app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 20)
        th.join()
        return box.get("r") or ""

    out = tool_call(_T.debug_start, dtab.chat_id, [5])
    check("debug_start lands on the breakpoint, not the first statement",
          "Paused at dbg.scad:5." in out, out[:200])
    check("and says it ran past the first stop",
          "first statement, line 1" in out, out[:200])
    check("with the frame that proves it got there",
          "d = 20.0" in out, out[:300])

    out = tool_call(_T.debug_resume, "to_child")
    check("to_child's refusal describes the moment, not the source",
          "no children are bound at this point" in out
          and "until the call itself is entered" in out, out[:300])

    tool_call(_T.debug_stop)

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
                         request_render=lambda i=None, pr=False: called.append((i, pr)) or True)
    out = T.render(ok)
    check("render() starts a render", called == [(None, False)], str(called))
    check("and says the result is not ready yet",
          "not finished" in out and 'schedule_followup(when="render")' in out, out)

    # Tab targeting: without it the model can only render whatever tab
    # happens to be active, and accepting a new script changes that silently.
    called.clear()
    T.render(ok, id=7)
    check("render(id=) targets that script", called == [(7, False)], str(called))
    called.clear()
    out_p = T.render(ok, profile=True)
    check("render(profile=True) asks for an instrumented render",
          called == [(None, True)], str(called))
    check("and points at read_profile rather than the geometry tools",
          "read_profile" in out_p and "describe_geometry" not in out_p, out_p)
    check("warning that instrumenting distorts the timings",
          "slower" in out_p, out_p)
    called.clear()
    bad = T.render(ok, id=99)
    check("render() rejects an unknown id instead of rendering the wrong tab",
          bad.startswith("Error:") and called == [], bad)

    nothing = T.AIToolContext(library_dir=Path("."),
                              request_render=lambda i=None, pr=False: False)
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

    import tempfile
    # --- search_library --------------------------------------------------
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

    # --- live tabs win over the turn-start snapshot -----------------------
    stale = [T.TabSnapshot(id=1, name="a.scad", path=None, modified=False,
                           text="old text")]
    fresh = [T.TabSnapshot(id=1, name="a.scad", path=None, modified=True,
                           text="new text")]
    lctx = T.AIToolContext(library_dir=Path("."), open_tabs=stale,
                           live_tabs=lambda: fresh)
    check("read_open_script reads the live buffer",
          T.read_open_script(lctx, 1) == "new text", T.read_open_script(lctx, 1))
    check("list_open_scripts reports live state too",
          '"modified": true' in T.list_open_scripts(lctx).lower(),
          T.list_open_scripts(lctx))
    check("without a live hook the snapshot is used",
          T.read_open_script(T.AIToolContext(library_dir=Path("."),
                                             open_tabs=stale), 1) == "old text")

    def blow():
        raise RuntimeError("gone")
    check("a live-tab read that throws falls back rather than failing",
          T.read_open_script(T.AIToolContext(
              library_dir=Path("."), open_tabs=stale,
              live_tabs=blow), 1) == "old text")

    # Everything that edits goes through _find_tab, so an anchored edit must
    # be matched against the live text, not the snapshot.
    aseen = []
    actx = T.AIToolContext(library_dir=Path("."), open_tabs=stale,
                           live_tabs=lambda: fresh, on_proposal=aseen.append)
    r = T.propose_script_replace(actx, 1, "old text", "x", "s")
    check("an anchor from the stale snapshot no longer matches",
          r.startswith("Error:") and aseen == [], r)
    r = T.propose_script_replace(actx, 1, "new text", "newer", "s")
    check("and one from the live text does",
          aseen and aseen[0].new_content == "newer", r)

    # --- project files -----------------------------------------------------
    with tempfile.TemporaryDirectory() as td:
        proj = Path(td) / "proj"
        (proj / "parts").mkdir(parents=True)
        (proj / "main.scad").write_text("include <common.scad>\n")
        (proj / "common.scad").write_text("wall = 2;\n")
        (proj / "parts" / "bracket.scad").write_text("module bracket() {}\n")
        (proj / "notes.txt").write_text("secret\n")
        (Path(td) / "outside.scad").write_text("nope\n")

        ptab = [T.TabSnapshot(id=1, name="main.scad",
                              path=str(proj / "main.scad"),
                              modified=False, text="include <common.scad>\n")]
        c = T.AIToolContext(library_dir=Path(td) / "libs", open_tabs=ptab)

        out = T.list_project_files(c)
        check("list_project_files finds the siblings",
              "common.scad" in out and "parts/bracket.scad" in out, out)
        check("and not non-.scad files", "notes.txt" not in out, out)
        check("and nothing above the project", "outside.scad" not in out, out)

        check("read_project_file reads a sibling",
              T.read_project_file(c, "common.scad") == "wall = 2;\n",
              T.read_project_file(c, "common.scad"))
        check("and one in a subdirectory",
              "module bracket" in T.read_project_file(c, "parts/bracket.scad"))

        # The guards. A traversal that ends in .scad passes the extension
        # check, so containment has to be what stops it.
        check("traversal out of the project is refused",
              T.read_project_file(c, "../outside.scad").startswith("Error:"),
              T.read_project_file(c, "../outside.scad"))
        check("an absolute path outside is refused",
              T.read_project_file(
                  c, str(Path(td) / "outside.scad")).startswith("Error:"))
        check("a non-.scad file is refused",
              T.read_project_file(c, "notes.txt").startswith("Error:"))
        check("a missing file is reported",
              "no such project file" in T.read_project_file(c, "nope.scad"))

        # An unsaved-only session has no project directory at all.
        none_ctx = T.AIToolContext(library_dir=Path(td), open_tabs=[
            T.TabSnapshot(id=1, name="Untitled", path=None, modified=True,
                          text="cube(1);")])
        check("with no saved script, there is no project to read",
              T.list_project_files(none_ctx).startswith("Error:"),
              T.list_project_files(none_ctx))
        check("and reading says the same",
              T.read_project_file(none_ctx, "x.scad").startswith("Error:"))

        # --- evaluate_expression ------------------------------------------
        # Driven against a script that includes a sibling, so this also
        # proves the temp file lands where relative includes resolve.
        (proj / "calc.scad").write_text(
            "include <common.scad>\n"
            "h = 12;\n"
            "function double(x) = x * 2;\n"
            "pts = [for (i=[0:4]) [i, i*i]];\n")
        etab = [T.TabSnapshot(id=2, name="calc.scad",
                              path=str(proj / "calc.scad"), modified=False,
                              text=(proj / "calc.scad").read_text())]
        e = T.AIToolContext(library_dir=Path(td), open_tabs=etab)

        check("a variable evaluates", T.evaluate_expression(e, 2, "h") == "12",
              T.evaluate_expression(e, 2, "h"))
        check("a function call evaluates",
              T.evaluate_expression(e, 2, "double(h)") == "24")
        check("a builtin over a comprehension evaluates",
              T.evaluate_expression(e, 2, "len(pts)") == "5")
        check("a list comes back whole",
              T.evaluate_expression(e, 2, "pts").startswith("[[0, 0]"),
              T.evaluate_expression(e, 2, "pts"))
        check("a value from an included sibling is in scope",
              T.evaluate_expression(e, 2, "wall") == "2",
              T.evaluate_expression(e, 2, "wall"))
        check("an unset name is undef rather than an error",
              T.evaluate_expression(e, 2, "nosuchvar") == "undef")
        check("a trailing semicolon is tolerated",
              T.evaluate_expression(e, 2, "h;") == "12")
        check("a syntax error is reported, not raised",
              T.evaluate_expression(e, 2, "h +").startswith("Error:"),
              T.evaluate_expression(e, 2, "h +"))
        check("an empty expression is refused",
              T.evaluate_expression(e, 2, "  ").startswith("Error:"))
        check("an unknown tab id is refused",
              T.evaluate_expression(e, 99, "h").startswith("Error:"))
        check("evaluating leaves no temp files behind",
              sorted(p.name for p in proj.glob("*.scad"))
              == ["calc.scad", "common.scad", "main.scad"],
              str(sorted(p.name for p in proj.glob("*.scad"))))

    # --- read_profile -------------------------------------------------------
    pr = T.AIToolContext(library_dir=Path("."),
                         profile_report=lambda: "Profiled render: 12.0 ms")
    check("read_profile returns the report", "12.0 ms" in T.read_profile(pr))
    check("with nothing profiled, it says how to get one",
          "Render with Profiling" in T.read_profile(
              T.AIToolContext(library_dir=Path("."), profile_report=lambda: "")))
    check("and an unwired profiler says so",
          T.read_profile(T.AIToolContext(library_dir=Path("."))).startswith("Error:"))

    def bang():
        raise RuntimeError("nope")
    check("a profile read that throws is reported, not raised",
          T.read_profile(T.AIToolContext(library_dir=Path("."),
                                         profile_report=bang)).startswith("Error:"))

    # --- debugger ----------------------------------------------------------
    seen_cmd = []

    def dbg(reply):
        return T.AIToolContext(
            library_dir=Path("."),
            open_tabs=[T.TabSnapshot(id=1, name="a.scad", path="/x/a.scad",
                                     modified=False, text="cube(1);")],
            debug_control=lambda a, arg=None: (seen_cmd.append((a, arg))
                                               or reply))

    paused = {"status": "paused", "file": "a.scad", "line": 12,
              "message": "", "stack": ["post() [module], called from a.scad:7",
                                       "<toplevel>"],
              "frames": [{"variables": {"n": "10.0", "d": "20.0"},
                          "truncated": 3}]}
    # A session always pauses at the first statement. Asking for a
    # breakpoint means asking to get there, so that stop is stepped past.
    first_stop = dict(paused, line=2)
    seq = [first_stop, paused]
    stepper = T.AIToolContext(
        library_dir=Path("."),
        open_tabs=[T.TabSnapshot(id=1, name="a.scad", path="/x/a.scad",
                                 modified=False, text="cube(1);")],
        debug_control=lambda a, arg=None: (seen_cmd.append((a, arg))
                                           or seq.pop(0)))
    seen_cmd.clear()
    out = T.debug_start(stepper, 1, [12])
    check("debug_start runs past the mandatory first stop",
          [a for a, _ in seen_cmd] == ["start", "resume"], str(seen_cmd))
    check("and lands on the breakpoint", "a.scad:12" in out, out)
    check("saying it passed the initial stop",
          "first statement, line 2" in out, out)

    # With no breakpoints there is nothing to run on to.
    seq2 = [first_stop]
    plain = T.AIToolContext(
        library_dir=Path("."), open_tabs=stepper.open_tabs,
        debug_control=lambda a, arg=None: (seen_cmd.append((a, arg))
                                           or seq2.pop(0)))
    seen_cmd.clear()
    T.debug_start(plain, 1, [])
    check("with no breakpoints it stays at the first stop",
          [a for a, _ in seen_cmd] == ["start"], str(seen_cmd))

    # Already on a breakpoint: nothing to skip.
    seq3 = [paused]
    onbp = T.AIToolContext(
        library_dir=Path("."), open_tabs=stepper.open_tabs,
        debug_control=lambda a, arg=None: (seen_cmd.append((a, arg))
                                           or seq3.pop(0)))
    seen_cmd.clear()
    T.debug_start(onbp, 1, [12])
    check("and it does not step past a first stop that is the breakpoint",
          [a for a, _ in seen_cmd] == ["start"], str(seen_cmd))

    seen_cmd.clear()
    out = T.debug_start(dbg(paused), 1, [12])
    check("debug_start reports where it stopped", "a.scad:12" in out, out)
    check("with the call stack", "post() [module]" in out and "<toplevel>" in out, out)
    check("and the variables", "n = 10.0" in out and "d = 20.0" in out, out)
    check("saying how many were held back", "3 more not shown" in out, out)
    check("the command carried the breakpoints",
          seen_cmd == [("start", {"id": 1, "breakpoints": [12]})], str(seen_cmd))

    seen_cmd.clear()
    T.debug_resume(dbg(paused), "over")
    check("debug_resume passes the command through",
          seen_cmd == [("resume", "over")], str(seen_cmd))
    check("an unknown command is refused before reaching the session",
          T.debug_resume(dbg(paused), "sideways").startswith("Error:")
          and len(seen_cmd) == 1, str(seen_cmd))
    check("the default command is continue",
          (seen_cmd.clear() or T.debug_resume(dbg(paused))) and
          seen_cmd == [("resume", "continue")], str(seen_cmd))

    # Every terminal state has to read as what it is, or the model will
    # keep stepping a session that has already ended.
    for state, want in [
        ({"status": "finished"}, "ran to completion"),
        ({"status": "idle"}, "No debug session"),
        ({"status": "stopped"}, "stopped"),
        ({"status": "error", "message": "boom"}, "Error: boom"),
        ({"status": "running", "message": "still going."}, "Still running"),
        ({"status": "error_break", "file": "a.scad", "line": 3,
          "message": "undefined", "stack": [], "frames": []},
         "Stopped by an error"),
    ]:
        check(f"a {state['status']} session reads correctly",
              want in T._format_debug_state(state), T._format_debug_state(state))

    check("a bad line number is refused",
          T.debug_start(dbg(paused), 1, [0]).startswith("Error:"))
    check("a non-numeric breakpoint is refused",
          T.debug_start(dbg(paused), 1, ["x"]).startswith("Error:"))
    check("an unknown script id is refused",
          T.debug_start(dbg(paused), 99, []).startswith("Error:"))
    for fn in (lambda c: T.debug_start(c, 1, []), T.debug_resume,
               T.debug_state, T.debug_stop):
        check("an unwired debugger says so, rather than crashing",
              fn(T.AIToolContext(library_dir=Path("."), open_tabs=[
                  T.TabSnapshot(id=1, name="a", path=None, modified=False,
                                text="x")])).startswith("Error:"))

    # An unsaved buffer cannot hold breakpoints -- they are collected per
    # saved path -- so asking for one has to be flagged, not silently ignored.
    unsaved = T.AIToolContext(
        library_dir=Path("."),
        open_tabs=[T.TabSnapshot(id=1, name="Untitled", path=None,
                                 modified=True, text="cube(1);")],
        debug_control=lambda a, arg=None: paused)
    check("breakpoints on an unsaved script are flagged",
          "never been saved" in T.debug_start(unsaved, 1, [3]),
          T.debug_start(unsaved, 1, [3]))
    check("but not mentioned when none were asked for",
          "never been saved" not in T.debug_start(unsaved, 1, []))

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
