"""Preferences > Viewport > Cut faces: baked into the render, re-renders on toggle.

MainWindow is a widget, so this drives it in a subprocess with scratch
settings, like the other GUI tests.
"""
import json
import os
import subprocess
import sys

DRIVER = '''
import json, os, sys, tempfile, time
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication
app = QApplication([])
from belfryscad.settings import use_scratch_settings
use_scratch_settings(tempfile.mkdtemp(prefix="belfryscad-test-"), seed=False)
from belfryscad.window.main_window import MainWindow
from belfryscad.window.preferences import save_preferences
out = {}
w = MainWindow(); w.skip_unsaved_prompts = True
d = tempfile.mkdtemp()
main = os.path.join(d, "cut.scad")
open(main, "w").write("difference() { color(\\"orange\\") cube(20, center=true); cylinder(30, r=8, center=true, $fn=24); }\\n")

def pump(pred, timeout=60):
    t0 = time.time()
    while not pred() and time.time() - t0 < timeout:
        app.processEvents(); time.sleep(0.01)
    return pred()

renders = []
orig = w._viewport.load_geometry
def capture(bodies):
    renders.append(bodies)
    return orig(bodies)
w._viewport.load_geometry = capture

def colours(bodies):
    cols = set()
    for b in bodies:
        if b.tri_colors is not None:
            cols |= {tuple(int(round(v * 255)) for v in c[:3]) for c in b.tri_colors}
        elif b.color is not None:
            cols.add(tuple(int(round(v * 255)) for v in b.color[:3]))
    return sorted(cols)

w.open_file_by_path(main)                          # renders on open, preference off
pump(lambda: len(renders) == 1 and not w._render_busy())
out["default_colours"] = colours(renders[-1])      # orange faces, green cut

save_preferences({"viewport/keepMinuendColor": True})
w._apply_preferences()                             # what the Preferences checkbox does
pump(lambda: len(renders) == 2 and not w._render_busy())
out["rerendered_on_toggle"] = len(renders) == 2
out["kept_colours"] = colours(renders[-1])         # orange only

w._apply_preferences()                             # same value: no render
app.processEvents()
out["no_rerender_without_change"] = len(renders) == 2
print(json.dumps(out)); sys.stdout.flush()
os._exit(0)
'''


def test_cut_faces_preference_rerenders_with_the_minuend_colour():
    proc = subprocess.run([sys.executable, "-c", DRIVER], capture_output=True, text=True,
                          env=dict(os.environ, QT_QPA_PLATFORM="offscreen"), timeout=180)
    assert proc.returncode == 0, proc.stderr[-3000:]
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["default_colours"] == [[157, 203, 81], [255, 165, 0]]
    assert out["rerendered_on_toggle"]
    assert out["kept_colours"] == [[255, 165, 0]]
    assert out["no_rerender_without_change"]
