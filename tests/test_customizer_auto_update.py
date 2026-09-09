"""#397: the Customizer's "Automatic update" box gates the debounced render.

MainWindow is a widget, so this runs in a subprocess with scratch settings,
like the other GUI-driving tests.
"""
import json
import os
import subprocess
import sys

DRIVER = '''
import json, os, sys, tempfile
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication
app = QApplication([])
from belfryscad.settings import use_scratch_settings
use_scratch_settings(tempfile.mkdtemp(prefix="belfryscad-test-"), seed=False)
from belfryscad.window.main_window import MainWindow
from belfryscad.window.preferences import load_preference
out = {}
w = MainWindow(); w.skip_unsaved_prompts = True
pane = w._customizer_pane
timer = w._customizer_render_timer
tab = w._current_tab()
tab.editor.setPlainText("size = 10;\\ncube(size);\\n")
out["default_on"] = pane.auto_update()

def field_change(value):
    timer.stop()
    pane.source_changed.emit(f"size = {value};\\ncube(size);\\n")
    return timer.isActive()

out["on_schedules_render"] = field_change(11)
out["source_written_on"] = tab.editor.toPlainText().startswith("size = 11;")
pane.set_auto_update(False)
out["off_schedules_render"] = field_change(12)
out["source_written_off"] = tab.editor.toPlainText().startswith("size = 12;")
out["pref_persisted_off"] = load_preference("customizer/autoUpdate", type_=bool)
pane.set_auto_update(True)
out["back_on_schedules_render"] = field_change(13)
out["pref_persisted_on"] = load_preference("customizer/autoUpdate", type_=bool)
print(json.dumps(out)); sys.stdout.flush()
os._exit(0)
'''


def test_automatic_update_box_gates_the_render_but_not_the_write_back():
    proc = subprocess.run([sys.executable, "-c", DRIVER], capture_output=True, text=True,
                          env=dict(os.environ, QT_QPA_PLATFORM="offscreen"), timeout=120)
    assert proc.returncode == 0, proc.stderr[-2000:]
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["default_on"] is True
    assert out["on_schedules_render"] is True
    assert out["source_written_on"] is True
    assert out["off_schedules_render"] is False
    assert out["source_written_off"] is True      # the value still reaches the source
    assert out["pref_persisted_off"] is False
    assert out["back_on_schedules_render"] is True
    assert out["pref_persisted_on"] is True
