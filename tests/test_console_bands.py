"""An error's band covers its ERROR line, not the TRACE lines beneath it."""
from belfryscad.window.console import ConsoleWidget


def test_only_the_error_lines_of_a_failed_render_are_banded():
    c = ConsoleWidget()
    c.append_output("Eval error:  (5 ms)\n"
                    "ERROR: Assertion 'false' failed in file a.scad, line 2\n"
                    "TRACE: called by 'f' in file a.scad, line 4\n"
                    "TRACE: called by 'g' in file a.scad, line 6")
    assert c._severity == {0: "error", 1: "error"}


def test_a_warning_inside_an_echo_block_is_banded_alone():
    c = ConsoleWidget()
    c.append_output("ECHO: 1\nWARNING: something\nECHO: 2")
    assert c._severity == {0: "warning", 1: "warning"}


def _fmt_at(c, bn, col):
    from PySide6.QtGui import QTextCursor
    block = c.document().findBlockByNumber(bn)
    cur = QTextCursor(block)
    cur.setPosition(block.position() + col + 1)
    return cur.charFormat()


def test_a_failed_assert_mutes_its_condition_and_bolds_its_message():
    """#934: the message is what a library's user can act on; the condition
    is the library's internals."""
    from PySide6.QtGui import QFont
    c = ConsoleWidget()
    line = ("ERROR: Assertion '(x > 0)' failed: \"x must be positive\" "
            "in file a.scad, line 1")
    c.append_output(line)
    cond, msg, file_ = line.index("(x > 0)"), line.index("x must"), line.index("a.scad")
    assert c._assert_parts == {0: [(cond, cond + 7, "condition"), (msg, msg + 18, "message")]}
    plain = _fmt_at(c, 0, file_).foreground().color()
    assert _fmt_at(c, 0, cond).foreground().color() != plain
    assert _fmt_at(c, 0, msg).fontWeight() == QFont.Weight.Bold
    assert _fmt_at(c, 0, file_).fontWeight() != QFont.Weight.Bold


def test_a_message_on_its_own_line_is_banded_and_bolded():
    """BOSL2's messages start with a newline, which left the message as the
    one line of the error with no band."""
    from PySide6.QtGui import QFont
    c = ConsoleWidget()
    c.append_output("Eval error:  (5 ms)\n"
                    "ERROR: Assertion 'all_nonnegative(size)' failed: \"\n"
                    "All components of size= must be >=0.\" in file s.scad, line 372\n"
                    "TRACE: called by 'cuboid' in file c.scad, line 2")
    assert c._severity == {0: "error", 1: "error", 2: "error"}
    assert _fmt_at(c, 2, 0).fontWeight() == QFont.Weight.Bold


def test_an_assert_without_a_message_mutes_only_the_condition():
    c = ConsoleWidget()
    c.append_output("ERROR: Assertion '(1 == 2)' failed in file b.scad, line 1")
    assert c._assert_parts == {0: [(18, 26, "condition")]}


def test_the_parts_survive_a_theme_switch():
    from PySide6.QtGui import QFont
    c = ConsoleWidget()
    c.append_output("ERROR: Assertion 'p' failed: \"m\" in file a.scad, line 1")
    c._retheme()
    assert _fmt_at(c, 0, 18).foreground().color() != _fmt_at(c, 0, 40).foreground().color()
    assert _fmt_at(c, 0, 30).fontWeight() == QFont.Weight.Bold
