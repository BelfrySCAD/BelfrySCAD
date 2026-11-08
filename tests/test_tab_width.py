"""Shorter document tabs (#1096): no .scad and fit-to-width by default; both can be turned off."""
import json
import subprocess
import sys

DRIVER = '''
import json, os, tempfile
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication
app = QApplication([])
from belfryscad.settings import use_scratch_settings, app_settings
use_scratch_settings(tempfile.mkdtemp(prefix="belfryscad-test-"), seed=False)
from belfryscad.window.main_window import MainWindow
MainWindow._render = lambda *a, **k: None
d = os.path.realpath(tempfile.mkdtemp())
for n in ("bracket.scad", "notes.txt", "Shout.SCAD"):
    open(os.path.join(d, n), "w").write("cube(1);\\n")
w = MainWindow(); w.show(); app.processEvents()
for n in ("bracket.scad", "notes.txt", "Shout.SCAD"):
    w.open_file_by_path(os.path.join(d, n))
bar = w._tabs.tab_bar
w._current_tab().editor.insertPlainText("x")
def labels(): return [bar.tabText(i) for i in range(bar.count()) if bar.tabText(i) != "Untitled"]
out = {"default": labels(), "tooltip_full": bar.tabToolTip(bar.count() - 1).endswith("Shout.SCAD"),
       "scroll": bar.usesScrollButtons(), "elide": bar.elideMode() == Qt.TextElideMode.ElideMiddle}
app_settings().setValue("editor/tabHideScadExtension", False)
app_settings().setValue("editor/tabsFitWidth", False)
w._apply_preferences()
out.update(shown=labels(), fit_scroll=bar.usesScrollButtons(),
           fit_elide=bar.elideMode() == Qt.TextElideMode.ElideNone)
print("RESULT" + json.dumps(out)); import sys; sys.stdout.flush()
from belfryscad.main import hard_exit; hard_exit(0)
'''


def test_tab_labels_and_fit():
    r = subprocess.run([sys.executable, "-c", DRIVER], capture_output=True, text=True, timeout=120)
    line = next((l for l in r.stdout.splitlines() if l.startswith("RESULT")), None)
    assert line, r.stdout + r.stderr
    out = json.loads(line[6:])
    assert out == {"default": ["bracket", "notes.txt", "Shout*"], "tooltip_full": True,
                   "scroll": False, "elide": True,
                   "shown": ["bracket.scad", "notes.txt", "Shout.SCAD*"],
                   "fit_scroll": True, "fit_elide": True}
