#!/usr/bin/env python3
"""Bracket pairs are coloured by nesting depth, and matching pairs agree.

Reads the colours the highlighter actually applied to the document, rather
than re-deriving them: the point is what ends up on screen.

Qt widgets crash pytest in this project, so this runs standalone.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from PySide6.QtWidgets import QApplication  # noqa: E402
from PySide6.QtGui import QColor, QTextDocument  # noqa: E402

failures = []


def check(label, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + label + (f"  -- {detail}" if not cond and detail else ""))
    if not cond:
        failures.append(label)


def colours(doc, text, hl=None):
    """{char index: '#rrggbb'} for every character the highlighter coloured."""
    doc.setPlainText(text)
    if hl is not None:
        # Driving the document directly, there is no view to trigger a
        # layout, so nothing has been highlighted yet.
        hl.rehighlight()
    out = {}
    base = 0
    block = doc.firstBlock()
    while block.isValid():
        layout = block.layout()
        for r in (layout.formats() if layout else []):
            for k in range(r.start, r.start + r.length):
                col = r.format.foreground().color()
                out[base + k] = col.name()
        base += block.length()
        block = block.next()
    return out


def main():
    app = QApplication.instance() or QApplication(sys.argv)   # noqa: F841
    from belfryscad.window.editor import OpenSCADHighlighter

    doc = QTextDocument()
    hl = OpenSCADHighlighter(doc)
    palette = [f.foreground().color().name() for f in hl._bracket_formats]
    # Pinned rather than a floor: the count is a deliberate choice that has
    # moved (3, then 7, then 5), so a silent change should be noticed.
    check("there are five cycling colours", len(palette) == 5, str(palette))
    check("and they are all distinct", len(set(palette)) == len(palette), str(palette))

    # The property that actually matters: neighbouring depths must not look
    # alike. Includes the wrap from the last back to the first, which is
    # just as adjacent as any other pair.
    worst = None
    for i, name in enumerate(palette):
        a, b = QColor(name), QColor(palette[(i + 1) % len(palette)])
        d = abs(a.hue() - b.hue())
        d = min(d, 360 - d)
        if worst is None or d < worst[0]:
            worst = (d, name, palette[(i + 1) % len(palette)])
    # 144 would be the ceiling for five evenly spaced colours: five signed
    # steps of 180 sum to an odd multiple of 180, never a multiple of 360,
    # so the cycle cannot close with opposite-hue jumps, and 5 x 144 = 720
    # closes it in two turns.
    #
    # The palette gives that up on purpose. An even five-way split always
    # puts some hue within 36 degrees of the red that marks an unmatched
    # bracket, whichever way it is rotated, so the ring is deliberately
    # uneven to keep clear of red -- see the palette's own comment. The
    # magenta/pink band is vacated entirely (checked below), which costs
    # the tightest adjacent pair: 108 degrees rather than 144.
    check("every step bounces to the far side of the spectrum",
          worst[0] >= 105,
          f"closest pair {worst[1]}/{worst[2]} only {worst[0]} deg apart")

    # ...and the reason it is allowed to be uneven has to hold, or the
    # relaxation above is just a loosened assertion.
    red = QColor("#FF2D2D").hue()
    nearest = min(min(abs(QColor(n).hue() - red), 360 - abs(QColor(n).hue() - red))
                  for n in palette)
    check("no depth colour is close to the unmatched-bracket red",
          nearest >= 40, f"nearest is {nearest} deg away")

    # Nothing in the magenta/pink band: a saturated pink reads as red
    # regardless of its hue angle, which is what put the last two
    # replacements there in the first place.
    check("no depth colour is a magenta or pink",
          all(not (295 <= QColor(n).hue() <= 350) for n in palette),
          str([(n, QColor(n).hue()) for n in palette]))

    first = QColor(palette[0])
    check("the first colour is a darkened gold, not a bright one",
          max(first.red(), first.green(), first.blue()) <= 0.90 * 255,
          f"{palette[0]} value {max(first.red(), first.green(), first.blue())/255:.2f}")

    # The case from the request.
    src = "{ callit([3,4,5]); }"
    c = colours(doc, src, hl)
    got = {ch: c.get(src.index(ch)) for ch in "{(["}
    check("the three nesting levels get three different colours",
          len({got["{"], got["("], got["["]}) == 3, str(got))
    check("depth 0 is the first colour", got["{"] == palette[0], str(got))
    check("depth 1 is the second", got["("] == palette[1], str(got))
    check("depth 2 is the third", got["["] == palette[2], str(got))

    # A closer must match its own opener, not the depth it closes from.
    for opener, closer in (("{", "}"), ("(", ")"), ("[", "]")):
        check(f"{opener}{closer} share a colour",
              c[src.index(opener)] == c[src.rindex(closer)],
              f"{c[src.index(opener)]} vs {c[src.rindex(closer)]}")

    # Depth N wraps back to the first colour. Every one of these has to be
    # closed: an opener the document never closes is drawn in the error red
    # instead of its depth colour, so a bare "((((((" would probe nothing.
    n = len(palette)
    src = "(" * (n + 1) + ")" * (n + 1)
    c = colours(doc, src, hl)
    check(f"depth {n} cycles back to the first colour",
          c[n] == palette[0], f"{c.get(n)} vs {palette[0]}")
    check("and every depth below it is its own colour",
          [c[i] for i in range(n)] == palette,
          str([c.get(i) for i in range(n)]))

    # Brackets carry across lines -- the state has to survive a newline.
    src = "module m() {\n    a = [1,\n         2];\n}"
    c = colours(doc, src, hl)
    open_brace = src.index("{")
    close_brace = src.rindex("}")
    check("a brace pair spanning lines shares its colour",
          c[open_brace] == c[close_brace],
          f"{c.get(open_brace)} vs {c.get(close_brace)}")
    open_sq = src.index("[")
    check("and a bracket one level in gets the next colour",
          c[open_sq] == palette[1], str(c.get(open_sq)))

    # Brackets inside strings and comments are text, not nesting. If they
    # counted, everything after them would be coloured one level off.
    # Two brackets, not three: with three, miscounting them lands back on
    # the same colour (3 % 3 == 0) and the check passes either way. This
    # test was written with three first, and disabling the string skip did
    # not fail it.
    for label, src in (
        ("a string", 'echo("((");\n[1]'),
        ("a line comment", "// ((\n[1]"),
        ("a block comment", "/* (( */\n[1]"),
        ("a block comment spanning lines", "/* ((\n   (( */\n[1]"),
    ):
        c = colours(doc, src, hl)
        idx = src.rindex("[")
        check(f"brackets in {label} do not shift the depth",
              c.get(idx) == palette[0], f"{c.get(idx)} vs {palette[0]}")

    # ...and they are still coloured as string/comment, not as brackets.
    src = "// (("
    c = colours(doc, src, hl)
    check("a bracket inside a comment stays comment-coloured",
          c.get(3) not in palette, str(c.get(3)))

    # A stray closer must not drive the depth negative.
    src = ")\n[1]"
    c = colours(doc, src, hl)
    check("a stray closer does not recolour what follows",
          c.get(src.index("[")) == palette[0], str(c.get(src.index("["))))

    # Real-world shape: nested calls in a module body.
    src = "module post(n) {\n    translate([n, 0, 0])\n        cube([n, n, n]);\n}"
    c = colours(doc, src, hl)
    check("the module's own parens are depth 0",
          c[src.index("(")] == palette[0])
    check("its body brace is depth 0 too",
          c[src.index("{")] == palette[0])
    check("translate's parens are depth 1",
          c[src.index("translate(") + len("translate")] == palette[1])
    check("and its vector is depth 2",
          c[src.index("[")] == palette[2])

    print("\n" + ("ALL PASS" if not failures else f"{len(failures)} FAILED: {failures}"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
