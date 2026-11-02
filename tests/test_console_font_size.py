"""#1022: Edit ▸ Increase/Decrease/Reset Font Size act on the console while
it has focus, and on the editor otherwise. Qt widgets, so a subprocess."""
import json
import os
import subprocess
import sys

DRIVER = r'''
import json, os, tempfile
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication
app = QApplication([])
from belfryscad.settings import use_scratch_settings
use_scratch_settings(tempfile.mkdtemp(prefix="belfryscad-test-"), seed=False)
from belfryscad.window.main_window import MainWindow
w = MainWindow(); w.skip_unsaved_prompts = True; w._render = lambda *a, **k: None
w.show(); app.processEvents()
con, ed = w._console, w._current_tab().editor
con.append("hello"); size = lambda x: x.font().pointSize()
out = {"console0": size(con), "editor0": size(ed)}
con.setFocus(); app.processEvents()
w._font_size_increase(); w._font_size_increase()
out["console_up"], out["editor_kept"] = size(con), size(ed)
out["text_follows"] = con.document().defaultFont().pointSize()
w._font_size_reset(); out["console_reset"] = size(con)
ed.setFocus(); app.processEvents()
w._font_size_decrease(); out["editor_down"], out["console_kept"] = size(ed), size(con)
w._font_size_reset(); out["editor_reset"] = size(ed)
print(json.dumps(out), flush=True)
from belfryscad.main import hard_exit; hard_exit(0)
'''


def test_font_size_follows_focus():
    proc = subprocess.run([sys.executable, "-c", DRIVER], capture_output=True, text=True,
                          env=dict(os.environ, QT_QPA_PLATFORM="offscreen"), timeout=120)
    assert proc.returncode == 0, proc.stderr[-2000:]
    o = json.loads(proc.stdout.strip().splitlines()[-1])
    c0, e0 = o["console0"], o["editor0"]
    assert (o["console_up"], o["editor_kept"], o["text_follows"]) == (c0 + 2, e0, c0 + 2)
    assert o["console_reset"] == c0 == 11
    assert (o["editor_down"], o["console_kept"], o["editor_reset"]) == (e0 - 1, c0, e0)
