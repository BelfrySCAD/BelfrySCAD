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
