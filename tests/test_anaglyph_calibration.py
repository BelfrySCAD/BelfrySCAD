"""Calibrate Anaglyph Colors: the colours reach the anaglyph matrices, and
the dialog saves live and restores on Cancel."""
import json
import os
import subprocess
import sys
from types import SimpleNamespace

import numpy as np

from belfryscad.engine.renderer import ANAGLYPH_MATRICES, anaglyph_matrices


def _cam(left=(1.0, 0.0, 0.0), right=(0.0, 1.0, 1.0), style="gray"):
    return SimpleNamespace(anaglyph_style=style, anaglyph_left_rgb=left, anaglyph_right_rgb=right)


def test_default_colours_are_the_old_gray_matrices():
    for got, want in zip(anaglyph_matrices(_cam()), ANAGLYPH_MATRICES["gray"]):
        assert np.allclose(got, want)


def test_calibrated_colours_draw_each_eye():
    left, right = anaglyph_matrices(_cam(left=(0.9, 0.1, 0.0), right=(0.0, 0.8, 0.7)))
    white = np.ones(3)
    assert np.allclose(left @ white, (0.9, 0.1, 0.0))
    assert np.allclose(right @ white, (0.0, 0.8, 0.7))


def test_color_style_is_uncalibrated():
    assert anaglyph_matrices(_cam(left=(0.5, 0.5, 0.5), style="color")) is ANAGLYPH_MATRICES["color"]


def test_tint_and_brightness_round_trip():
    from belfryscad.window.anaglyph_calibration import eye_color, eye_settings
    assert eye_color(0, 0, 100) == (1.0, 0.0, 0.0)
    assert eye_color(180, 60, 100) == (0.0, 0.0, 1.0)        # cyan tinted all the way to blue
    assert np.allclose(eye_color(0, 0, 130), (1.0, 0.3, 0.3))  # past 100%: towards white
    for hue in (0, 180):
        for tint, bright in ((0, 100), (-25, 80), (40, 120), (-60, 10)):
            assert np.allclose(eye_settings(hue, eye_color(hue, tint, bright)), (tint, bright))


DRIVER = '''
import json, tempfile
from PySide6.QtWidgets import QApplication
app = QApplication([])
from belfryscad.settings import use_scratch_settings
use_scratch_settings(tempfile.mkdtemp(prefix="belfryscad-test-"), seed=False)
from belfryscad.window.preferences import load_preference, save_preferences
from belfryscad.window.anaglyph_calibration import AnaglyphCalibrationDialog, LEFT_KEY, RIGHT_KEY
d = AnaglyphCalibrationDialog(None, lambda k, v: save_preferences({k: v})); d.show(); app.processEvents()
out = {"intro_plus_six": d.pages.count() == 7 and d.pages.currentIndex() == 0 and not d.back.isEnabled()}
d.next.click(); d.sliders[LEFT_KEY, "brightness"].setValue(50)
out["live_save"] = load_preference(LEFT_KEY) == "#800000"
d.next.click(); d.sliders[LEFT_KEY, "tint"].setValue(10)
out["tint_keeps_brightness"] = load_preference(LEFT_KEY) == "#801500"
for _ in range(4):
    d.next.click()
app.processEvents(); d.preview.grab()          # paints the stereo preview
out["last_page"] = d.next.text() == "Done"
d.reset_all()
out["factory_reset"] = load_preference(LEFT_KEY) == "#ff0000" and load_preference(RIGHT_KEY) == "#00ffff"
d.sliders[LEFT_KEY, "brightness"].setValue(50)
d.reject()
out["cancel_restores"] = load_preference(LEFT_KEY) == "#ff0000"
d.show(); out["reopens_at_start"] = d.pages.currentIndex() == 0 and d.sliders[LEFT_KEY, "brightness"].value() == 100
d.next.click(); d.next.click(); d.next.click(); d.sliders[RIGHT_KEY, "brightness"].setValue(50)
for _ in range(5):
    d.next.click()
out["done_keeps"] = load_preference(RIGHT_KEY) == "#008080" and not d.isVisible()
print(json.dumps(out))
'''


def test_stereo_preview_composites_like_the_renderer():
    from belfryscad.window.anaglyph_calibration import stereo_preview
    img = stereo_preview(120, 80, (1, 0, 0), (0, 1, 1), 6)
    assert img.shape == (80, 120, 3)
    assert np.allclose(img[40, 2], (1, 1, 1))          # white outside the frame: both eyes
    assert np.allclose(img[10, 60], (0, 0, 0), atol=0.2)  # the frame: black for both


def test_dialog_walkthrough():
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    proc = subprocess.run([sys.executable, "-c", DRIVER], capture_output=True, text=True,
                          env=env, timeout=60)
    assert proc.returncode == 0, proc.stderr[-3000:]
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert all(out.values()), out
