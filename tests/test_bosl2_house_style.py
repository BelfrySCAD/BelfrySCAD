"""Default reformats a function the way BOSL2/strings.scad is written.

Asked for directly: "use the typical formatting of code in that file as how
the Default formatting profile should look." Before this, a function whose
body did not fit was joined onto the signature line and then rescued by
wrapping whatever argument list happened to be there -- output worse than
the input it replaced.
"""
import re
from pathlib import Path

import pytest

from belfryscad.window.scad_format import (PROFILES, _same_shape, can_format,
                                           format_scad)

BOSL2 = Path("/Users/gminette/dev/git-repos/BOSL2/strings.scad")


def fmt(src, name="Default", width=80):
    return format_scad(src, 4, PROFILES[name], width)


def test_the_body_goes_on_its_own_indented_line():
    src = ('function substr(str, pos=0, len=undef) = assert(is_string(str)) '
           'is_list(pos) ? _substr(str, pos[0], pos[1]-pos[0]+1) : len == undef '
           '? _substr(str, pos, len(str)-pos) : _substr(str,pos,len);\n')
    assert fmt(src) == (
        "function substr(str, pos=0, len=undef) =\n"
        "    assert(is_string(str))\n"
        "    is_list(pos) ? _substr(str, pos[0], pos[1]-pos[0]+1) :\n"
        "    len == undef ? _substr(str, pos, len(str)-pos) :\n"
        "    _substr(str, pos, len);\n"), fmt(src)


def test_every_leading_assert_gets_its_own_line():
    """Only the first was recognised at one point -- the whitespace between
    clauses is a token too."""
    src = ('function m(str,start,pattern) = assert(_is_liststr(str), "a") '
           'assert(_is_liststr(pattern), "b") len(str)-start <len(pattern)? '
           'false : _recurse(str,start,pattern,len(pattern));\n')
    lines = fmt(src).splitlines()
    assert lines[1].strip().startswith("assert(") and lines[1].endswith('"a")')
    assert lines[2].strip().startswith("assert(") and lines[2].endswith('"b")')


def test_a_ternary_chain_breaks_after_the_colon():
    src = ('function str_join(list,sep="",_i=0, _result="") = assert(is_list(list)) '
           '_i >= len(list)-1 ? (_i==len(list) ? _result : str(_result,list[_i])) '
           ': str_join(list,sep,_i+1,str(_result,list[_i],sep));\n')
    out = fmt(src)
    assert out.rstrip().count("\n") == 3, out
    for line in out.splitlines()[2:-1]:
        assert line.rstrip().endswith(":"), out


def test_a_colon_inside_a_range_is_not_a_ternary():
    """`[0:1:len(str)]` must not be split -- only a depth-0 `:` is an arm."""
    src = ('function _str_find_all(str,pattern) = pattern == "" ? count(len(str)) '
           ': [for(i=[0:1:len(str)-len(pattern)]) if (substr_match(str,i,pattern)) i];\n')
    out = fmt(src)
    assert "[0:1:len(str)-len(pattern)]" in out, out
    assert out.rstrip().count("\n") == 2, out


def test_a_function_that_already_fits_is_left_on_one_line():
    src = "function suffix(str,len) = len>=len(str)? str : substr(str, len(str)-len,len);\n"
    assert fmt(src) == "function suffix(str, len) = len>=len(str)? str : substr(str, len(str)-len, len);\n"


def test_breaking_the_body_is_preferred_to_wrapping_an_argument_list():
    """The old output wrapped `_substr(\\n str, pos[0], ...\\n)` mid-ternary."""
    src = ('function substr(str, pos=0, len=undef) = is_list(pos) ? '
           '_substr(str, pos[0], pos[1]-pos[0]+1) : _substr(str,pos,len);\n')
    out = fmt(src)
    assert "_substr(\n" not in out, out


# -- Against the real file -------------------------------------------------

@pytest.mark.skipif(not BOSL2.exists(), reason="BOSL2 checkout not present")
def test_every_definition_in_strings_scad_survives_a_reformat():
    """Shape-preserving and idempotent for all 39 of them. This is the file
    the style was taken from, so it is the one that has to hold."""
    lines = BOSL2.read_text().split("\n")
    starts = [i for i, l in enumerate(lines) if re.match(r"^(function|module)\s", l)]
    assert len(starts) > 30, "file changed shape; retune this test"
    for a, b in zip(starts, starts[1:] + [len(lines)]):
        chunk = "\n".join(lines[a:b]).rstrip() + "\n"
        if not can_format(chunk):
            continue
        out = fmt(chunk)
        assert _same_shape(chunk, out), chunk
        assert fmt(out) == out, chunk
