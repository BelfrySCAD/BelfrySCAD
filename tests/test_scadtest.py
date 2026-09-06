"""`--test`: the .scadtest runner."""
import textwrap

import pytest

from belfryscad.scadtest import parse_scadtest_file, run_test


def _write(tmp_path, body):
    p = tmp_path / "t.scadtest"
    p.write_text(textwrap.dedent(body))
    return p


def test_parses_config_defaults_and_overrides(tmp_path):
    f = _write(tmp_path, """
        [config]
        timeout = 5
        expect_success = false

        [[test]]
        name = "inherits"
        script = "x = 1;"

        [[test]]
        name = "overrides"
        script = "x = 1;"
        timeout = 99
        expect_success = true
    """)
    a, b = parse_scadtest_file(f)
    assert (a.name, a.timeout, a.expect_success) == ("inherits", 5, False)
    assert (b.name, b.timeout, b.expect_success) == ("overrides", 99, True)


def test_script_and_script_file_are_mutually_exclusive(tmp_path):
    f = _write(tmp_path, """
        [[test]]
        name = "both"
        script = "x = 1;"
        script_file = "other.scad"
    """)
    with pytest.raises(ValueError, match="not both"):
        parse_scadtest_file(f)


def test_one_of_them_is_required(tmp_path):
    f = _write(tmp_path, """
        [[test]]
        name = "neither"
    """)
    with pytest.raises(ValueError, match="must have either"):
        parse_scadtest_file(f)


def test_passing_and_failing_scripts(tmp_path):
    f = _write(tmp_path, """
        [[test]]
        name = "ok"
        script = "assert(1 == 1);"

        [[test]]
        name = "asserts"
        script = "assert(1 == 2);"
    """)
    ok, bad = (run_test(tc) for tc in parse_scadtest_file(f))
    assert ok.passed
    assert not bad.passed and bad.messages


def test_expect_success_false_inverts_the_verdict(tmp_path):
    f = _write(tmp_path, """
        [[test]]
        name = "meant to fail"
        script = "assert(false);"
        expect_success = false

        [[test]]
        name = "should have failed"
        script = "x = 1;"
        expect_success = false
    """)
    good, bad = (run_test(tc) for tc in parse_scadtest_file(f))
    assert good.passed
    assert not bad.passed
    assert "succeeded" in bad.messages[0]


def test_assert_echoes_matches_a_substring(tmp_path):
    f = _write(tmp_path, """
        [[test]]
        name = "found"
        script = 'echo("hello world");'
        assert_echoes = ["hello"]

        [[test]]
        name = "missing"
        script = 'echo("hello world");'
        assert_echoes = ["goodbye"]
    """)
    found, missing = (run_test(tc) for tc in parse_scadtest_file(f))
    assert found.passed
    assert not missing.passed
    assert "Expected echo not found" in missing.messages[0]


def test_assert_no_echoes_is_skipped_when_echoes_are_named(tmp_path):
    """The two checks would contradict each other, so naming an expected
    echo turns the no-echoes check off -- the reference does the same."""
    f = _write(tmp_path, """
        [[test]]
        name = "echoes but names one"
        script = 'echo("hi");'
        assert_echoes = ["hi"]

        [[test]]
        name = "echoes unexpectedly"
        script = 'echo("hi");'
    """)
    named, unexpected = (run_test(tc) for tc in parse_scadtest_file(f))
    assert named.passed
    assert not unexpected.passed
    assert "Expected no echoes" in unexpected.messages[0]


def test_set_vars_reach_the_script(tmp_path):
    f = _write(tmp_path, """
        [[test]]
        name = "uses the var"
        script = "assert(n == 7);"
        set_vars = { n = 7 }
    """)
    assert run_test(parse_scadtest_file(f)[0]).passed


def test_script_file_is_resolved_beside_the_scadtest(tmp_path):
    (tmp_path / "helper.scad").write_text("assert(1 == 1);\n")
    f = _write(tmp_path, """
        [[test]]
        name = "external"
        script_file = "helper.scad"
    """)
    tc = parse_scadtest_file(f)[0]
    assert tc.script_file.endswith("helper.scad")
    assert run_test(tc).passed


def test_timeout_is_reported_not_raised(tmp_path):
    """timeout = 0, not a script chosen to be slow: this evaluator does tail
    calls, so a deep recursion that would crawl in the reference returns at
    once here, and a race against it would be flaky either way. Zero makes
    the timeout branch certain while still exercising the real path."""
    f = _write(tmp_path, """
        [[test]]
        name = "slow"
        script = "x = [for (i = [0:1:50000]) i * 2]; assert(len(x) > 0);"
        timeout = 0
    """)
    res = run_test(parse_scadtest_file(f)[0])
    assert not res.passed
    assert "timed out" in res.messages[0]
