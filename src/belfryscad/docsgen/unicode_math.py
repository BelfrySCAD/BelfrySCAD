"""LaTeX math rendered as Unicode text, for the Docs pane.

GitHub's wiki renders `$inline$` and `$$display$$` with MathJax. Qt has no
MathJax, no MathML and no way to typeset, so this converts the subset that
Unicode can express -- superscripts, subscripts, roots, Greek letters and
the common operators -- and refuses everything else.

Refusing is the important half. `to_unicode` returns None the moment it
meets something it cannot represent faithfully, and the caller then leaves
the original LaTeX exactly as written. A half-converted formula is worse
than an unconverted one: the reader can decode `\\frac{a}{b}`, but not a
formula that silently lost a numerator.

The other half is not converting things that were never math. OpenSCAD
spells its special variables `$fn`, `$fa`, `$fs`, so a BOSL2 doc line like
`$fa=1,$fs=.5` looks exactly like inline math. Applying GitHub's own
delimiter rule to BOSL2 matches 166 of those; skipping code spans and
fenced blocks, as render_math does, brings it down to one.
"""
from __future__ import annotations

import re

#: Digits and a few symbols have dedicated Unicode superscript forms. The
#: gaps are real -- there is no superscript for most letters -- which is
#: why an unmapped character fails the whole conversion.
_SUPERSCRIPT = {
    "0": "⁰", "1": "¹", "2": "²", "3": "³", "4": "⁴",
    "5": "⁵", "6": "⁶", "7": "⁷", "8": "⁸", "9": "⁹",
    "+": "⁺", "-": "⁻", "=": "⁼", "(": "⁽", ")": "⁾",
    "n": "ⁿ", "i": "ⁱ", "a": "ᵃ", "b": "ᵇ", "c": "ᶜ",
    "d": "ᵈ", "e": "ᵉ", "f": "ᶠ", "g": "ᵍ", "h": "ʰ",
    "j": "ʲ", "k": "ᵏ", "l": "ˡ", "m": "ᵐ", "o": "ᵒ",
    "p": "ᵖ", "r": "ʳ", "s": "ˢ", "t": "ᵗ", "u": "ᵘ",
    "v": "ᵛ", "w": "ʷ", "x": "ˣ", "y": "ʸ", "z": "ᶻ",
    " ": " ",
}

_SUBSCRIPT = {
    "0": "₀", "1": "₁", "2": "₂", "3": "₃", "4": "₄",
    "5": "₅", "6": "₆", "7": "₇", "8": "₈", "9": "₉",
    "+": "₊", "-": "₋", "=": "₌", "(": "₍", ")": "₎",
    "a": "ₐ", "e": "ₑ", "h": "ₕ", "i": "ᵢ",
    "j": "ⱼ", "k": "ₖ", "l": "ₗ", "m": "ₘ", "n": "ₙ",
    "o": "ₒ", "p": "ₚ", "r": "ᵣ", "s": "ₛ", "t": "ₜ",
    "u": "ᵤ", "v": "ᵥ", "x": "ₓ", " ": " ",
}

#: Single-token commands that map straight onto a character.
_SYMBOLS = {
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ",
    "epsilon": "ε", "zeta": "ζ", "eta": "η", "theta": "θ",
    "iota": "ι", "kappa": "κ", "lambda": "λ", "mu": "μ",
    "nu": "ν", "xi": "ξ", "pi": "π", "rho": "ρ",
    "sigma": "σ", "tau": "τ", "upsilon": "υ", "phi": "φ",
    "chi": "χ", "psi": "ψ", "omega": "ω",
    "Gamma": "Γ", "Delta": "Δ", "Theta": "Θ", "Lambda": "Λ",
    "Xi": "Ξ", "Pi": "Π", "Sigma": "Σ", "Phi": "Φ",
    "Psi": "Ψ", "Omega": "Ω",
    "times": "×", "cdot": "·", "div": "÷", "pm": "±",
    "mp": "∓", "leq": "≤", "le": "≤", "geq": "≥",
    "ge": "≥", "neq": "≠", "ne": "≠", "approx": "≈",
    "equiv": "≡", "propto": "∝", "infty": "∞",
    "partial": "∂", "nabla": "∇", "sum": "∑", "prod": "∏",
    "int": "∫", "sqrt": "√", "angle": "∠", "degree": "°",
    "circ": "∘", "ldots": "…", "dots": "…", "cdots": "⋯",
    "rightarrow": "→", "to": "→", "leftarrow": "←",
    "Rightarrow": "⇒", "leftrightarrow": "↔",
    "in": "∈", "notin": "∉", "subset": "⊂", "cup": "∪",
    "cap": "∩", "forall": "∀", "exists": "∃",
    "|": "‖", "prime": "′", "ast": "∗", "star": "⋆",
}

#: Commands that only affect typesetting, and so simply vanish.
_IGNORED = {"left", "right", "displaystyle", "textstyle", "limits", "!"}

#: Spacing commands, all rendered as a single ordinary space.
_SPACING = {",", ";", ":", " ", "quad", "qquad"}

#: Characters that would otherwise be read as markdown once the converted
#: text is spliced back into the document. Deliberately short: escaping
#: harmless punctuation like `(` or `-` is what makes escaped text look
#: escaped, and only these can actually start markup mid-emphasis.
_MARKDOWN_SPECIAL = "\\`*_[]"


class _Unconvertible(Exception):
    """Raised the moment something has no faithful Unicode form."""


def _read_group(src: str, i: int) -> tuple[str, int]:
    """The `{...}` group at src[i], or the single character there."""
    if i >= len(src):
        raise _Unconvertible("ran off the end")
    if src[i] == "\\":
        # `C^\infty` -- the argument is the whole command, not the one
        # backslash. Taking a single character here left a bare "\" that
        # then failed to parse, refusing formulas that convert perfectly.
        cmd = re.match(r"\\([A-Za-z]+|.)", src[i:])
        if not cmd:
            raise _Unconvertible("stray backslash")
        return cmd.group(0), i + len(cmd.group(0))
    if src[i] != "{":
        return src[i], i + 1
    depth, j = 0, i
    while j < len(src):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[i + 1:j], j + 1
        j += 1
    raise _Unconvertible("unbalanced {")


def _script(body: str, table: dict, marker: str) -> str:
    """A raised or lowered `body`, falling back to `^(...)` notation.

    Unicode's script alphabets have holes -- there is no superscript "/"
    and no superscript alpha -- so `d^{1/a}` cannot be set as a script at
    all. Writing it `d^(1/a)` keeps every term, which is the thing that
    matters; only the typesetting is lost, and that was never available.
    Refusing here instead would leave the reader looking at raw `$...$`.
    """
    text = _convert(body)
    if text and all(ch in table for ch in text):
        return "".join(table[ch] for ch in text)
    return f"{marker}({text})" if len(text) > 1 else f"{marker}{text}"


def _convert(src: str) -> str:
    out = []
    i = 0
    while i < len(src):
        ch = src[i]
        if ch == "\\":
            name = re.match(r"\\([A-Za-z]+|.)", src[i:])
            if not name:
                raise _Unconvertible("stray backslash")
            cmd = name.group(1)
            i += len(name.group(0))
            if cmd == "frac":
                num, i = _read_group(src, i)
                den, i = _read_group(src, i)
                out.append(f"{_wrap(num)}/{_wrap(den)}")
            elif cmd == "sqrt":
                body, i = _read_group(src, i)
                out.append("√" + _wrap(body))
            elif cmd in ("text", "mathrm", "mathit", "mathbf", "operatorname"):
                body, i = _read_group(src, i)
                out.append(_convert(body))
            elif cmd in _SYMBOLS:
                out.append(_SYMBOLS[cmd])
            elif cmd in _IGNORED:
                pass
            elif cmd in _SPACING:
                out.append(" ")
            else:
                raise _Unconvertible(f"unknown command \\{cmd}")
        elif ch == "^":
            body, i = _read_group(src, i + 1)
            out.append(_script(body, _SUPERSCRIPT, "^"))
        elif ch == "_":
            body, i = _read_group(src, i + 1)
            out.append(_script(body, _SUBSCRIPT, "_"))
        elif ch in "{}":
            i += 1          # bare grouping braces carry no meaning here
        elif ch == "&" or ch == "\n":
            raise _Unconvertible("alignment or multi-line body")
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _wrap(body: str) -> str:
    """A converted sub-expression, parenthesised unless it is one token."""
    text = _convert(body)
    return text if len(text) == 1 or re.fullmatch(r"[\w.]+", text) else f"({text})"


def to_unicode(latex: str) -> str | None:
    """`latex` as Unicode text, or None if it cannot be shown faithfully."""
    try:
        text = _convert(latex.strip())
    except _Unconvertible:
        return None
    return re.sub(r" {2,}", " ", text).strip() or None


def _escape(text: str) -> str:
    """Stop converted math being re-read as markdown."""
    return "".join("\\" + c if c in _MARKDOWN_SPECIAL else c for c in text)


#: GitHub's inline rule: the delimiters must hug their content, so
#: `$fa=1, $fs=1` is not math (the closing `$` follows a space).
_INLINE = re.compile(r"(?<![\\$])\$(?!\s)((?:[^$\n])+?)(?<!\s)\$(?!\$)")
_DISPLAY = re.compile(r"\$\$(.+?)\$\$", re.S)
_CODE_SPAN = re.compile(r"`+[^`]*`+")

#: An indented code block. docsgen writes every Example script this way.
_INDENTED_CODE = re.compile(r"(?: {4,}|\t)\S")


#: OpenSCAD's special variables, plus the ones BOSL2 adds. Prose that
#: mentions `$fn/$fa/$fs` or "larger by 4*$slop" satisfies MathJax's rule
#: exactly -- GitHub's own wiki renders those two BOSL2 lines as math -- so
#: a candidate beginning with one of these names is refused outright.
_OPENSCAD_VARS = (
    "fn", "fa", "fs", "t", "vpr", "vpt", "vpd", "vpf", "preview",
    "children", "parent_modules", "slop", "idx", "item", "row", "col",
    "anchor", "attach_to", "parent_size", "parent_anchor", "tags", "color",
    "overlap", "edge_angle", "edge_length", "profile_count",
)
_LOOKS_LIKE_VAR = re.compile(r"(?:%s)(?![A-Za-z0-9_])" % "|".join(_OPENSCAD_VARS))


def _render_line(line: str) -> str:
    def swap(m):
        if _LOOKS_LIKE_VAR.match(m.group(1)):
            return m.group(0)
        text = to_unicode(m.group(1))
        # Italic, the way MathJax sets variables -- and the escape keeps a
        # converted `(n-1)` from turning into markdown along the way.
        return f"*{_escape(text)}*" if text else m.group(0)

    # Code spans are held aside rather than skipped over, so a `$fn` inside
    # backticks cannot supply a delimiter to the text around it.
    parts = _CODE_SPAN.split(line)
    spans = _CODE_SPAN.findall(line)
    rendered = [_INLINE.sub(swap, p) for p in parts]
    return "".join(
        r + (spans[i] if i < len(spans) else "")
        for i, r in enumerate(rendered)
    )


def render_math(markdown: str) -> str:
    """Replace what math this can render with Unicode, in place.

    Code is left alone entirely -- fenced and 4-space-indented alike, since
    docsgen writes Example scripts as indented blocks -- because OpenSCAD
    source is full of `$fn`, `$fa` and `$fs`.
    """
    lines = markdown.split("\n")
    out, fenced, display = [], False, None
    for line in lines:
        if line.lstrip().startswith("```"):
            fenced = not fenced
            out.append(line)
            continue
        if fenced or _INDENTED_CODE.match(line):
            out.append(line)
            continue
        if display is not None:
            if line.strip() == "$$":
                text = to_unicode(" ".join(display))
                out.append(f"*{_escape(text)}*" if text else
                           "$$\n" + "\n".join(display) + "\n$$")
                display = None
            else:
                display.append(line.strip())
            continue
        if line.strip() == "$$":
            display = []
            continue
        # A whole display expression on one line.
        line = _DISPLAY.sub(
            lambda m: (lambda t: f"*{_escape(t)}*" if t else m.group(0))(to_unicode(m.group(1))),
            line)
        out.append(_render_line(line))
    if display is not None:      # unterminated $$ block: give it back as-is
        out.append("$$")
        out.extend(display)
    return "\n".join(out)
