"""LaTeX-to-Unicode math for the Docs pane."""
import pytest

from belfryscad.docsgen.unicode_math import render_math, to_unicode


@pytest.mark.parametrize("latex, expected", [
    (r"r=\sqrt{x^2+y^2+z^2}", "r=√(x²+y²+z²)"),
    (r"1/d^{50}", "1/d⁵⁰"),
    (r"C^2", "C²"),
    (r"a_i + b_{n-1}", "aᵢ + bₙ₋₁"),
    (r"\frac{a}{b}", "a/b"),
    (r"\frac{x+1}{2}", "(x+1)/2"),
    (r"\pi r^2", "π r²"),
    (r"\theta \times \alpha", "θ × α"),
    (r"\sum_{i=0}^{n} x_i", "∑ᵢ₌₀ⁿ xᵢ"),
    (r"\int_0^1 f(x)dx", "∫₀¹ f(x)dx"),
    ("n + 1 + (n-1) k", "n + 1 + (n-1) k"),
    # Unicode's script alphabets have holes, so these keep every term in
    # ^(...) notation rather than being refused outright -- raw LaTeX on
    # screen would be the worse outcome.
    (r"1/d^{1/a}", "1/d^(1/a)"),      # no superscript "/"
    (r"x^{\alpha}", "x^α"),           # no superscript alpha
    (r"e^{-x^2}", "e^(-x²)"),
    (r"a_{1/b}", "a_(1/b)"),
])
def test_converts_the_subset_unicode_can_express(latex, expected):
    assert to_unicode(latex) == expected


@pytest.mark.parametrize("latex", [
    r"\begin{matrix}a\\b\end{matrix}",   # no Unicode form at all
    r"\frac{a}{b",                       # unbalanced
    r"\undefinedcommand{x}",
])
def test_refuses_rather_than_half_converting(latex):
    """A formula that silently lost a term is worse than raw LaTeX."""
    assert to_unicode(latex) is None


def test_refused_math_is_left_exactly_as_written():
    md = r"the matrix $\begin{matrix}a\\b\end{matrix}$ here"
    assert render_math(md) == md


def test_inline_math_becomes_italic_unicode():
    assert render_math("radius $r^2$ ok") == "radius *r²* ok"


def test_display_math_between_dollar_lines():
    out = render_math("before\n\n$$\n\\frac{a+b}{2}\n$$\n\nafter")
    assert out.splitlines() == ["before", "", "*(a+b)/2*", "", "after"]


def test_unterminated_display_block_is_given_back_untouched():
    md = "text\n\n$$\n\\frac{a}{b}\n"
    assert render_math(md) == md


# -- the OpenSCAD `$var` collision ------------------------------------
#
# `$fn`, `$fa` and `$fs` satisfy MathJax's delimiter rule exactly, so a
# naive reading turns real BOSL2 prose into gibberish.

def test_fenced_code_is_never_math():
    md = "```\nstroke(path,$fa=1,$fs=1);\n```\n"
    assert render_math(md) == md


def test_indented_example_scripts_are_never_math():
    """docsgen writes Example bodies as 4-space-indented blocks, not fences."""
    md = "text\n\n    path = circle(d=50,$fn=18);\n    stroke(path,$fa=1,$fs=1);\n"
    assert render_math(md) == md


def test_a_dollar_var_inside_backticks_cannot_supply_a_delimiter():
    md = "Specify `$fn` to set segments, and `$fa` too."
    assert render_math(md) == md


def test_prose_naming_openscad_variables_is_not_math():
    """Both of these are real BOSL2 lines that GitHub's own wiki gets wrong."""
    for md in ("Uses $fn/$fa/$fs to control the number of facets",
               "makes the hole larger by 4*$slop to account for printing"):
        assert render_math(md) == md


def test_real_math_still_survives_next_to_a_dollar_var():
    md = "with $fn=8 the radius $r^2$ grows"
    assert render_math(md) == "with $fn=8 the radius *r²* grows"


@pytest.mark.parametrize("latex, expected", [
    # The script argument is a whole command, not the one backslash --
    # reading a single character left a bare "\" that failed to parse.
    (r"C^\infty", "C^∞"),
    (r"x^\alpha", "x^α"),
    (r"a_\theta", "a_θ"),
    (r"\int \|\mathbf{C}''(t)\|^2 \, dt", "∫ ‖C''(t)‖² dt"),
])
def test_a_command_can_be_a_script_argument(latex, expected):
    assert to_unicode(latex) == expected
