#!/usr/bin/env python3
"""--ai-echo mirrors the AI conversation to stdout, and --ai sends a prompt.

Asserting the flag parses would prove nothing: the point is that the pane's
turn events reach stdout. This drives the real AIChatPane's own handlers, so
a renamed or removed echo call fails here rather than in a live session with
a paid provider.

Qt widgets crash pytest in this project, so this runs standalone.
"""
import io
import sys
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from PySide6.QtGui import QSurfaceFormat  # noqa: E402

fmt = QSurfaceFormat()
fmt.setVersion(3, 3)
fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
QSurfaceFormat.setDefaultFormat(fmt)

from PySide6.QtWidgets import QApplication  # noqa: E402

failures = []


def check(label, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + label + (f"  -- {detail}" if not cond and detail else ""))
    if not cond:
        failures.append(label)


def main():
    from belfryscad.main import _parse_args, _wire_ai_echo, _send_ai_prompt

    check("--ai-echo parses", _parse_args(["--ai-echo"]).ai_echo is True)
    check("--ai takes a prompt", _parse_args(["--ai", "hi"]).ai == "hi")
    check("and still leaves the file argument alone",
          _parse_args(["--ai", "hi", "f.scad"]).file == "f.scad")
    check("neither is on by default",
          _parse_args([]).ai_echo is False and _parse_args([]).ai is None)

    app = QApplication.instance() or QApplication(sys.argv)
    from belfryscad.window.main_window import MainWindow
    from belfryscad.window.ai_tools import Proposal

    w = MainWindow()
    w.skip_unsaved_prompts = True
    w.persist_settings = False   # don't overwrite the user's layout
    w.show()   # a dock in an unshown window can never report visible
    _wire_ai_echo(w)
    pane = w._ai_chat_pane
    check("the pane got an echo hook", callable(pane.echo))

    def emit(fn, *a):
        buf = io.StringIO()
        with redirect_stdout(buf):
            fn(*a)
        return buf.getvalue()

    # Each of the pane's own handlers, not a reimplementation of them.
    out = emit(pane._echo, "user", "make it taller")
    check("a user message is echoed", "[you] make it taller" in out, out)

    out = emit(pane._on_tool_started, "describe_geometry")
    check("a tool call is echoed by name",
          "[tool] describe_geometry" in out, out)

    pane._reply_text = "Here is what I found."
    out = emit(pane._on_done)
    check("the assistant's reply is echoed",
          "[ai] Here is what I found." in out, out)

    out = emit(pane._on_error, "no API key")
    check("an error is echoed", "[error] no API key" in out, out)

    # A proposal is the moment the model actually did something; without
    # this the stdout log goes quiet exactly when it matters.
    out = emit(pane._on_proposal,
               Proposal(kind="edit", summary="raise the height",
                        new_content="x", diff_text="", filename="a.scad"))
    check("a proposal is echoed", "[proposed] raise the height" in out, out)
    check("naming the file it targets", "a.scad" in out, out)

    # Multi-line replies must stay readable, each line tagged.
    pane._reply_text = "line one\nline two"
    out = emit(pane._on_done)
    check("every line of a multi-line reply is tagged",
          "[ai] line one" in out and "[ai] line two" in out, out)

    # Nothing to say should say nothing.
    pane._reply_text = ""
    check("an empty reply prints nothing", emit(pane._on_done).strip() == "")

    # Echoing must never be able to break a conversation.
    def boom(kind, text):
        raise RuntimeError("stdout gone")
    pane.echo = boom
    try:
        pane._echo("user", "x")
        check("an echo that throws is swallowed", True)
    except Exception as e:      # noqa: BLE001
        check("an echo that throws is swallowed", False, str(e))

    # With no hook wired at all, the same calls must be silent no-ops.
    pane.echo = None
    check("with no hook, nothing is printed and nothing breaks",
          emit(pane._echo, "user", "x") == "")

    # _send_ai_prompt reaches the pane's own send path.
    #
    # MainWindow's own slot is disconnected first. send_requested is what
    # starts a turn, so leaving it connected would make this verifier issue
    # a real request to whatever provider the user has configured -- and
    # then exit while that worker thread was still starting, which aborts
    # in PySide's shutdown. A test must not spend the user's API budget.
    w._ai_chat_pane.send_requested.disconnect(w._on_ai_send)
    sent = []
    pane.send_requested.connect(sent.append)
    _send_ai_prompt(w, "hello")
    check("--ai's prompt goes through the pane's normal send path",
          sent == ["hello"], str(sent))
    check("and the chat dock is revealed", w._ai_chat_dock.isVisible())

    check("no AI turn was left running", not pane._streaming)

    # Quitting must stop a streaming turn: its worker is a QThread, and
    # destroying a running one is fatal in Qt. The app normally exits via
    # os._exit(), which skips PySide's shutdown and hides this -- anything
    # exiting normally aborts instead.
    cancelled = []
    pane.cancel_turn = lambda: cancelled.append(1)
    w.close()
    check("closing the window cancels any running AI turn",
          cancelled == [1], str(cancelled))
    print("\n" + ("ALL PASS" if not failures else f"{len(failures)} FAILED: {failures}"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
