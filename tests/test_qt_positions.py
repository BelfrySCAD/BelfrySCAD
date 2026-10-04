"""Text positions in three units: the evaluator's UTF-8 bytes, Python's code
points, Qt's UTF-16 units. An emoji is 4 / 1 / 2 of them, an accented letter
2 / 1 / 1, so every place one unit met another was off after such a
character: bracket matching raised IndexError after an emoji, error
underlines and every viewport selection landed to the right after an em
dash in a comment, and the Customizer found no parameters below one."""
import json
import os
import subprocess
import sys

from belfryscad.qt_positions import (byte_column_to_qt, byte_to_index, parse_ast_string, qt_length,
                                     to_index, to_qt)

TEXT = "a😀b é c"          # emoji: 1 Python character, 2 Qt units, 4 bytes


def test_index_and_qt_positions_round_trip():
    assert qt_length(TEXT) == len(TEXT) + 1
    for i in range(len(TEXT) + 1):
        assert to_index(TEXT, to_qt(TEXT, i)) == i
    assert to_qt(TEXT, 2) == 3 and to_index(TEXT, 3) == 2       # 'b' after the emoji
    assert to_index(TEXT, 2) == 2                               # inside the pair: the next character
    assert to_qt("plain", 3) == 3 and to_index("plain", 99) == 5


def test_bytes_to_indices_and_columns():
    convert = byte_to_index(TEXT)
    assert convert(TEXT.encode().index(b"b")) == 2 and convert(len(TEXT.encode())) == len(TEXT)
    assert byte_to_index("ascii only") is None
    # The parser's column for 'c' counts bytes: a1(1) + emoji(4) + b(1) + space + é(2) + space.
    assert byte_column_to_qt(TEXT, len("a😀b é ".encode())) == to_qt(TEXT, TEXT.index("c"))


def test_parsed_spans_are_python_indices():
    src = "// café — 😀\ncube(1);\n"
    spans = []

    def walk(node):
        if isinstance(node, dict):
            if "start_offset" in node:
                spans.append((node["start_offset"], node["end_offset"]))
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk(parse_ast_string(src))
    assert (src.index("cube"), src.index(";") + 1) in spans


def test_customizer_sees_parameters_below_non_ascii_text():
    from belfryscad.window.customizer import scan_parameters
    assert [p.name for p in scan_parameters("// café — 😀\nwidth = 10; // [5:20]\n")] == ["width"]


DRIVER = r'''
import json, os, sys
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QTextCursor
app = QApplication([])
errors = []
sys.excepthook = lambda *a: errors.append(str(a[1]))
from belfryscad.window.editor import CodeEditor
from belfryscad.window.main_window import _replace_text_keeping_scroll
out = {}
e = CodeEditor()
e.setPlainText('s = "😀"; f(1);\n')
# Bracket matching with the cursor after the emoji: raised IndexError.
c = e.textCursor(); c.setPosition(len('s = "😀"; f(1)') + 1); e.setTextCursor(c)
e._update_bracket_match()
sel = e._bracket_selections
out["bracket_pair"] = [s.cursor.selectedText() for s in sel]
# An edit after the emoji, through the undo system's replacement: lands exactly.
_replace_text_keeping_scroll(e, 's = "😀"; f(2);\n')
out["after_edit"] = e.toPlainText()
# Python-index replacement of the whole document leaves nothing behind.
e.replace_text(0, len(e.toPlainText()), "cube(1);\n")
out["after_replace_all"] = e.toPlainText()
# Error underline after non-ASCII: the parser reports byte column 19 for this
# line -- the ';' with nothing before it, 2 bytes right of where Python counts it.
e.setPlainText('a = 1;\nb = "é😀"; c = ;\n')
e.set_error_location(2, 19)
s = e._error_selections[0].cursor
blk = e.document().findBlockByNumber(1)
line = blk.text()
from belfryscad.qt_positions import to_index
out["error_from"] = line[to_index(line, s.selectionStart() - blk.position()):]
# A selection given in Python indices highlights the right text.
e.setPlainText('// 😀 é\ncube(1);\n')
start = e.toPlainText().index("cube")
e.set_selection(start, start + len("cube(1)"))
out["selected"] = e._selection_extra[0].cursor.selectedText()
out["errors"] = errors
print(json.dumps(out)); sys.stdout.flush()
os._exit(0)
'''


def test_editor_positions_after_emoji_and_accents():
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    proc = subprocess.run([sys.executable, "-c", DRIVER], capture_output=True, text=True, env=env, timeout=120)
    assert proc.returncode == 0, proc.stderr[-2000:]
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["errors"] == []
    assert sorted(out["bracket_pair"]) == ["(", ")"]
    assert out["after_edit"] == 's = "😀"; f(2);\n'
    assert out["after_replace_all"] == "cube(1);\n"
    assert out["error_from"] == ";"
    assert out["selected"] == "cube(1)"


def test_shape_spans_become_python_indices_for_their_tab():
    """Selecting a shape, dragging a gizmo and nudging all slice the source
    with its span; the evaluator's are bytes."""
    from types import SimpleNamespace
    from openscad_cpp_evaluator import _Position
    from belfryscad.window.main_window import _indexed_span
    src = "// café — 😀\ncube(1);\n"
    tab = SimpleNamespace(editor=SimpleNamespace(toPlainText=lambda: src))
    b = src.encode()
    span = _Position(2, 1, "x.scad", b.index(b"cube"), b.index(b";") + 1)
    got = _indexed_span(span, tab)
    assert src[got.start_offset:got.end_offset] == "cube(1);"
    assert span.start_offset == b.index(b"cube")          # the evaluator's own object is untouched
