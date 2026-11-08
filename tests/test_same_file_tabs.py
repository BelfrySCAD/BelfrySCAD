"""One file in two editors (#1094): typing keeps its cursor, and Save As onto
a file another tab has open is refused."""
import json
import subprocess
import sys

DRIVER = '''
import json, os, tempfile
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtGui import QTextCursor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox
app = QApplication([])
from belfryscad.settings import use_scratch_settings
use_scratch_settings(tempfile.mkdtemp(prefix="belfryscad-test-"), seed=False)
from belfryscad.window.main_window import MainWindow
MainWindow._render = lambda *a, **k: None
def run(ms):
    l = QEventLoop(); QTimer.singleShot(ms, l.quit); l.exec()
d = os.path.realpath(tempfile.mkdtemp()); p = os.path.join(d, "t.scad"); open(p, "w").write("a1\\na2\\n")
w1 = MainWindow(); w1.show(); w2 = MainWindow(); w2.show(); run(100)
w1.open_file_by_path(p); w2.open_file_by_path(p); run(200)
e1, e2 = w1._current_tab().editor, w2._current_tab().editor
c = e1.textCursor(); c.movePosition(QTextCursor.MoveOperation.End); e1.setTextCursor(c); e1.setFocus()
for ch in "xy":
    QTest.keyClick(e1, ch); run(100)
out = {"pos": e1.textCursor().position(), "text1": e1.toPlainText(), "text2": e2.toPlainText()}
warned = []
QMessageBox.warning = staticmethod(lambda *a, **k: warned.append(a[1]))
QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: (p, ""))
w1._new_document(); b = w1._current_tab(); b.editor.setPlainText("other\\n")
out["saved"] = w1._save_file_as(); out["warned"] = warned; out["b_path"] = b.file_path
out["disk"] = open(p).read()
print("RESULT" + json.dumps(out)); import sys; sys.stdout.flush()
from belfryscad.main import hard_exit; hard_exit(0)
'''


def test_same_file_in_two_editors():
    r = subprocess.run([sys.executable, "-c", DRIVER], capture_output=True, text=True, timeout=120)
    line = next((l for l in r.stdout.splitlines() if l.startswith("RESULT")), None)
    assert line, r.stdout + r.stderr
    out = json.loads(line[6:])
    assert out == {"pos": 8, "text1": "a1\na2\nxy", "text2": "a1\na2\nxy",
                   "saved": False, "warned": ["File Already Open"], "b_path": None,
                   "disk": "a1\na2\n"}
