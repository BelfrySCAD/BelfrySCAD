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
    ok = T.AIToolContext(library_dir=Path("."),
                         request_render=lambda: called.append(1) or True)
    out = T.render(ok)
    check("render() starts a render", called == [1])
    check("and says the result is not ready yet",
          "not finished" in out and 'schedule_followup(when="render")' in out, out)

    nothing = T.AIToolContext(library_dir=Path("."), request_render=lambda: False)
    check("render() with no script reports that, rather than claiming success",
          T.render(nothing).startswith("Error:"), T.render(nothing))
    check("render() is unavailable rather than crashing when unwired",
          T.render(T.AIToolContext(library_dir=Path("."))).startswith("Error:"))

    # --- registration ----------------------------------------------------
    names = [t["name"] for t in T.TOOLS]
    check("render is a registered tool", "render" in names, str(names))
    check("and is dispatchable by name",
          T.run_tool(ok, "render", {}) == out, T.run_tool(ok, "render", {}))
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
