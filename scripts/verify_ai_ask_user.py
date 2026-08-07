#!/usr/bin/env python3
"""The ask_user tool: the dialog's keyboard behaviour and the tool's contract.

Keys are sent as real QKeyEvents rather than by calling setChecked, because
the requirement is about what the keyboard does -- a test that sets the
state directly would pass on a dialog you could not drive at all.

Qt widgets crash pytest in this project, so this runs standalone.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from PySide6.QtGui import QSurfaceFormat

fmt = QSurfaceFormat()
fmt.setVersion(3, 3)
fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
QSurfaceFormat.setDefaultFormat(fmt)

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog  # noqa: E402

from belfryscad.window.ai_question_dialog import AIQuestionDialog  # noqa: E402
from belfryscad.window.ai_tools import AIToolContext, run_tool  # noqa: E402

ONE = [{"question": "Which units?", "header": "Units",
        "options": [{"label": "Millimetres", "description": "The usual"},
                    {"label": "Inches", "description": "Imperial"},
                    {"label": "Centimetres", "description": "Metric, coarse"}]}]

MULTI = [{"question": "Which features?", "multiSelect": True,
          "options": [{"label": "Fillets"}, {"label": "Chamfers"},
                      {"label": "Counterbores"}]}]

BOTH = ONE + MULTI

failures = []


def key(dlg, k):
    """Send a key the way the window system does: to the focused widget.

    Sending it to the dialog instead only exercises the dialog's own
    keyPressEvent, so a radio button -- which handles Space and the arrows
    itself -- would look broken while a checkbox, handled by the dialog,
    looked fine.
    """
    QTest.keyClick(dlg.focusWidget() or dlg, k)


def check(label, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + label + (f"  -- {detail}" if not cond and detail else ""))
    if not cond:
        failures.append(label)


def main():
    app = QApplication.instance() or QApplication(sys.argv)

    # --- radio: one choice, arrows move, space selects -------------------
    dlg = AIQuestionDialog(ONE)
    dlg.show()
    app.processEvents()
    b = dlg._blocks[0]
    check("radio question uses exclusive buttons", not b.multi)
    check("first option has focus on open", b.buttons[0].hasFocus())

    key(dlg, Qt.Key.Key_Space)
    app.processEvents()
    check("space selects the focused option", b.buttons[0].isChecked())

    key(dlg, Qt.Key.Key_Down)
    key(dlg, Qt.Key.Key_Space)
    app.processEvents()
    check("down arrow moves to the next option", b.buttons[1].isChecked())
    check("choosing another deselects the first", not b.buttons[0].isChecked())
    check("radio answer is the one label",
          dlg.answers[0]["selected"] == ["Inches"], str(dlg.answers[0]))

    key(dlg, Qt.Key.Key_Up)
    key(dlg, Qt.Key.Key_Space)
    app.processEvents()
    check("up arrow moves back", dlg.answers[0]["selected"] == ["Millimetres"],
          str(dlg.answers[0]))
    dlg.close()

    # --- checkboxes: independent, arrows move focus ----------------------
    dlg = AIQuestionDialog(MULTI)
    dlg.show()
    app.processEvents()
    b = dlg._blocks[0]
    check("multiSelect question uses checkboxes", b.multi)

    key(dlg, Qt.Key.Key_Space)
    key(dlg, Qt.Key.Key_Down)
    key(dlg, Qt.Key.Key_Down)
    key(dlg, Qt.Key.Key_Space)
    app.processEvents()
    check("two checkboxes can be selected at once",
          dlg.answers[0]["selected"] == ["Fillets", "Counterbores"],
          str(dlg.answers[0]))

    key(dlg, Qt.Key.Key_Space)
    app.processEvents()
    check("space toggles a checkbox back off",
          dlg.answers[0]["selected"] == ["Fillets"], str(dlg.answers[0]))
    dlg.close()

    # --- clarification text ----------------------------------------------
    dlg = AIQuestionDialog(ONE)
    dlg.show()
    app.processEvents()
    dlg._blocks[0].buttons[0].setChecked(True)
    dlg._blocks[0]._note.setText("  but only for the base  ")
    check("clarification text is captured and trimmed",
          dlg.answers[0]["note"] == "but only for the base", str(dlg.answers[0]))
    dlg.close()

    # --- escape cancels ---------------------------------------------------
    dlg = AIQuestionDialog(ONE)
    dlg.show()
    app.processEvents()
    key(dlg, Qt.Key.Key_Escape)
    app.processEvents()
    check("escape rejects the dialog", dlg.result() == QDialog.DialogCode.Rejected,
          str(dlg.result()))
    dlg.close()

    # --- several questions on one page ------------------------------------
    dlg = AIQuestionDialog(BOTH)
    dlg.show()
    app.processEvents()
    check("both questions are shown at once", len(dlg._blocks) == 2)
    check("each question keeps its own kind",
          not dlg._blocks[0].multi and dlg._blocks[1].multi)
    key(dlg, Qt.Key.Key_Space)              # question 1
    dlg._blocks[1].buttons[2].setFocus()
    key(dlg, Qt.Key.Key_Space)              # question 2
    app.processEvents()
    a = dlg.answers
    check("answers come back per question, in order",
          a[0]["selected"] == ["Millimetres"] and a[1]["selected"] == ["Counterbores"],
          str(a))
    dlg.close()

    # --- the tool contract -------------------------------------------------
    # The tool must NOT wait for an answer: it returns as soon as the
    # question is on screen. Blocking would hold an MCP request open for as
    # long as the user takes to think.
    shown = []

    def ctx_showing(ok=True):
        return AIToolContext(library_dir=Path("/tmp"),
                             ask_user=lambda qs: (shown.append(qs), ok)[1])

    import time
    t0 = time.monotonic()
    out = run_tool(ctx_showing(), "ask_user", {"questions": ONE})
    check("ask_user returns immediately, without waiting for an answer",
          time.monotonic() - t0 < 0.5, f"{time.monotonic()-t0:.2f}s")
    check("the questions reached the dialog layer", shown and shown[-1][0]["question"] == "Which units?")
    check("the model is told to stop and wait, not to guess",
          "stop" in out.lower() and "guess" in out.lower(), out)
    check("no answer is invented in the tool result",
          "Millimetres" not in out and "Inches" not in out, out)

    out = run_tool(ctx_showing(ok=False), "ask_user", {"questions": ONE})
    check("a second question while one is open is refused",
          out.startswith("Error"), out)

    out = run_tool(AIToolContext(library_dir=Path("/tmp")), "ask_user", {"questions": ONE})
    check("no ask_user hook is refused rather than silently ignored",
          out.startswith("Error"), out)

    out = run_tool(ctx_showing(), "ask_user", {"questions": []})
    check("an empty ask is refused", out.startswith("Error"), out)

    out = run_tool(ctx_showing(), "ask_user",
                   {"questions": [{"question": "One option?",
                                   "options": [{"label": "Only"}]}]})
    check("a question with one option is refused", out.startswith("Error"), out)

    out = run_tool(ctx_showing(), "ask_user", {"questions": ONE * 9})
    check("too many questions at once is refused", out.startswith("Error"), out)

    # --- answers become a user message; dismissal cancels the turn ---------
    from belfryscad.window.main_window import MainWindow

    fmt = MainWindow._format_ai_answers
    text = fmt(ONE, [{"selected": ["Inches"], "note": "for the flange"}])
    check("an answer reads like something the user typed",
          "Inches" in text and "for the flange" in text and "\n" not in text, repr(text))
    text = fmt(BOTH, [{"selected": ["Millimetres"], "note": ""},
                      {"selected": ["Fillets", "Chamfers"], "note": ""}])
    check("multiple answers become one message, a line each",
          text.count("\n") == 1 and "Fillets, Chamfers" in text, repr(text))
    check("a question answered with nothing is left out of the message",
          fmt(ONE, [{"selected": [], "note": ""}]) == "",
          repr(fmt(ONE, [{"selected": [], "note": ""}])))

    class _Pane:
        def __init__(self): self.sent, self.cancelled = [], 0
        def submit_user_text(self, t): self.sent.append(t)
        def cancel_turn(self): self.cancelled += 1

    class _Win:
        _format_ai_answers = staticmethod(fmt)
        _on_ai_ask_finished = MainWindow._on_ai_ask_finished

    class _Dlg:
        def __init__(self, qs, ans): self.questions, self.answers = qs, ans

    w = _Win(); w._ai_chat_pane = _Pane(); w._ai_ask_dialog = object()
    w._on_ai_ask_finished(QDialog.DialogCode.Accepted,
                          _Dlg(ONE, [{"selected": ["Inches"], "note": ""}]))
    check("accepting submits the answer as a user message",
          w._ai_chat_pane.sent and "Inches" in w._ai_chat_pane.sent[0],
          str(w._ai_chat_pane.sent))
    check("accepting does not cancel the turn", w._ai_chat_pane.cancelled == 0)
    check("the dialog reference is cleared so another can be asked",
          w._ai_ask_dialog is None)

    w = _Win(); w._ai_chat_pane = _Pane(); w._ai_ask_dialog = object()
    w._on_ai_ask_finished(QDialog.DialogCode.Rejected, _Dlg(ONE, [{}]))
    check("dismissing cancels the running turn", w._ai_chat_pane.cancelled == 1)
    check("dismissing sends nothing", w._ai_chat_pane.sent == [])

    w = _Win(); w._ai_chat_pane = _Pane(); w._ai_ask_dialog = object()
    w._on_ai_ask_finished(QDialog.DialogCode.Accepted,
                          _Dlg(ONE, [{"selected": [], "note": ""}]))
    check("accepting with nothing chosen cancels rather than sending nothing",
          w._ai_chat_pane.cancelled == 1 and w._ai_chat_pane.sent == [])

    # The tool must be offered to the CLI transports too, or Claude and
    # Copilot silently lack it while the direct API path has it.
    from belfryscad.window.ai_tools import TOOLS
    names = [t["name"] for t in TOOLS]
    check("ask_user is in the tool registry", "ask_user" in names)
    from belfryscad.window.ai_cli import _TOOL_NAMES as CLAUDE_TOOLS
    from belfryscad.window.ai_copilot_cli import _TOOL_NAMES as COPILOT_TOOLS
    check("Claude's allowlist includes it",
          any(n.endswith("ask_user") for n in CLAUDE_TOOLS))
    check("Copilot's allowlist includes it",
          any(n.endswith("ask_user") for n in COPILOT_TOOLS))

    print("\n" + ("ALL PASS" if not failures else f"{len(failures)} FAILED: {failures}"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
