"""#432: with word wrap on, the debugger highlighted the wrong line.

findBlockByLineNumber() counts LAYOUT lines -- the visual rows a wrapped
paragraph occupies -- not blocks. Wrap a long line and every lookup below
it lands on an earlier block, so the execution highlight drifts onto a
blank line or a comment. Wrap off, the two numberings coincide, which is
why it only ever showed up with wrapping enabled.

The same lookup drove the parse-error squiggle and scroll_to_line, so all
three drifted together.
"""
import json
import os
import subprocess
import sys

DRIVER = '''
import json, os, sys, tempfile, time
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QTextOption
app = QApplication([])
from belfryscad.settings import use_scratch_settings
use_scratch_settings(tempfile.mkdtemp(prefix="belfryscad-test-"), seed=False)
from belfryscad.window.editor import CodeEditor

out = {}
ed = CodeEditor()
ed.resize(220, 400)            # narrow, so the long lines below must wrap

# Line 1 is long enough to occupy several layout lines; 2-5 are short. With
# wrapping on, layout-line 5 is somewhere inside line 1 or 2, NOT line 5.
lines = [
    "// " + ("a very long comment that has to wrap several times " * 4),
    "",
    "// short comment",
    "",
    "cube(1);",
]
ed.setPlainText("\\n".join(lines))
ed.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
ed.show()
for _ in range(20): app.processEvents(); time.sleep(0.01)

doc = ed.document()
out["blocks"] = doc.blockCount()
out["layout_lines"] = doc.lineCount()
out["wrapping_happened"] = doc.lineCount() > doc.blockCount()

# The execution highlight must land on the block whose text is "cube(1);".
ed.set_execution_line(5)
sel = ed._exec_selection[0]
out["exec_block_number"] = sel.cursor.block().blockNumber()
out["exec_block_text"] = sel.cursor.block().text()

# The error squiggle, same lookup.
ed.set_error_location(5, 1)
esel = ed._error_selections[0]
out["error_block_text"] = esel.cursor.block().text()

# And scroll_to_line, which moves the cursor to the target block.
ed.scroll_to_line(3)
out["scrolled_block_text"] = ed.textCursor().block().text()
print(json.dumps(out)); sys.stdout.flush()
os._exit(0)
'''


def test_lookups_use_source_lines_not_wrapped_rows():
    proc = subprocess.run([sys.executable, "-c", DRIVER], capture_output=True, text=True,
                          env=dict(os.environ, QT_QPA_PLATFORM="offscreen"), timeout=120)
    assert proc.returncode == 0, proc.stderr[-3000:]
    out = json.loads(proc.stdout.strip().splitlines()[-1])

    assert out["blocks"] == 5
    assert out["wrapping_happened"], (
        f"the fixture must actually wrap or it proves nothing "
        f"({out['layout_lines']} layout lines for {out['blocks']} blocks)")

    # Line 5 is "cube(1);". Before the fix this landed on an earlier block --
    # a blank line or the comment -- because layout line 5 is still inside
    # the wrapped first line.
    assert out["exec_block_number"] == 4, "0-based block 4 is source line 5"
    assert out["exec_block_text"] == "cube(1);"
    assert out["error_block_text"] == "cube(1);"
    assert out["scrolled_block_text"] == "// short comment", "line 3"
