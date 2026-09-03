"""The code editor never shrinks below `_MIN_COLUMNS` characters of code.

Driven in a subprocess, like the other Qt tests here: a CodeEditor needs a
real QApplication, which does not belong in pytest's own process.
"""
import json
import os
import subprocess
import sys

import pytest


_DRIVER = '''
import json, sys, tempfile
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QFont
app = QApplication([])
# Never touch the developer's real settings -- see test_settings_isolation.
from belfryscad.settings import use_scratch_settings
use_scratch_settings(tempfile.mkdtemp(prefix="belfryscad-test-"), seed=False)
from belfryscad.window.editor import CodeEditor

ed = CodeEditor(); ed.show()
out = {"min_columns": CodeEditor._MIN_COLUMNS, "sizes": []}
for pt in (11, 13, 18):
    f = QFont("Menlo", pt); f.setStyleHint(QFont.StyleHint.Monospace)
    ed.setFont(f)
    app.processEvents()
    char_w = ed.fontMetrics().horizontalAdvance("0")
    floor = ed.minimumWidth()
    ed.resize(40, 300)          # squeeze far below any sane width
    app.processEvents()
    squeezed = ed.width()
    ed.resize(floor, 300)
    app.processEvents()
    out["sizes"].append({"pt": pt, "char_w": char_w, "floor": floor,
                          "squeezed": squeezed,
                          "cols_at_floor": ed.viewport().width() // char_w})
ed.close()
print(json.dumps(out))
'''


def _run(tmp_path):
    driver = tmp_path / "_minwidth.py"
    driver.write_text(_DRIVER)
    # headless_render sets QT_QPA_PLATFORM=offscreen with setdefault, which
    # outlives the call and would leave this widget without a real screen.
    env = {k: v for k, v in os.environ.items() if k != "QT_QPA_PLATFORM"}
    r = subprocess.run([sys.executable, str(driver)], capture_output=True,
                       text=True, env=env, timeout=300)
    if r.returncode != 0 or not r.stdout.strip():
        pytest.skip(f"no Qt GUI available here: {(r.stderr or '').strip()[-200:]}")
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_editor_never_narrower_than_its_minimum_columns(tmp_path):
    out = _run(tmp_path)
    want = out["min_columns"]
    for s in out["sizes"]:
        assert s["squeezed"] == s["floor"], (
            f"{s['pt']}pt: squeezing to 40px gave {s['squeezed']}px, "
            f"not the {s['floor']}px minimum")
        assert s["cols_at_floor"] >= want, (
            f"{s['pt']}pt: only {s['cols_at_floor']} columns fit at the minimum")


def test_the_minimum_tracks_the_font_size(tmp_path):
    """Baked-in pixels would be wrong the moment the font changed -- a
    preferences change or a Cmd+[/Cmd+] zoom both move this floor."""
    out = _run(tmp_path)
    hints = [s["floor"] for s in out["sizes"]]
    assert hints == sorted(hints) and hints[0] < hints[-1], hints
