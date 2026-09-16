"""The Docs pane renders at a size you can choose (#465).

"The text in the doc page is so small, it's hard to read even with my
computer glasses." A+/A- in the pane, persisted, with the headings scaling
along with the body rather than staying put.
"""
import json
import os
import subprocess
import sys

import pytest

from belfryscad.window.preferences import _DEFAULTS


def test_the_default_follows_the_application():
    """0 rather than a number: a fresh install should look like the rest of
    the app, not like somebody else's eyesight."""
    assert _DEFAULTS["docs/fontSize"] == 0


DRIVER = r'''
import json, os, sys, tempfile
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QTextCursor
app = QApplication([])
from belfryscad.settings import use_scratch_settings
use_scratch_settings(tempfile.mkdtemp(prefix="belfryscad-test-"), seed=False)
from belfryscad.window.docs_pane import DocsPane

out = {}

class Preview:
    markdown = "# Heading One\n\nSome body prose.\n\n## Heading Two\n\nMore.\n"
    base_dir = tempfile.mkdtemp()
    errors = []

pane = DocsPane()
out["default"] = pane.font_size()
out["app_default"] = round(QApplication.font().pointSizeF())
pane._last_preview = Preview()
pane._build_document(Preview())

def snapshot():
    doc = pane._view.document()
    heads, b = [], doc.begin()
    while b.isValid():
        if b.blockFormat().headingLevel():
            c = QTextCursor(b); c.select(QTextCursor.SelectionType.LineUnderCursor)
            heads.append(round(c.charFormat().fontPointSize(), 1))
        b = b.next()
    return {"base": doc.defaultFont().pointSize(), "headings": heads,
            "errors": pane._error_text.document().defaultFont().pointSize()}

out["at_default"] = snapshot()
pane.set_font_size(20)
out["at_20"] = snapshot()
out["persisted"] = pane.font_size()

# A second pane reads the stored size -- it has to survive a restart.
out["second_pane"] = DocsPane().font_size()

pane.set_font_size(1);   out["clamp_low"] = pane.font_size()
out["low_btn"] = pane._smaller_btn.isEnabled()
pane.set_font_size(999); out["clamp_high"] = pane.font_size()
out["high_btn"] = pane._bigger_btn.isEnabled()

# No preview yet: changing the size must not raise.
fresh = DocsPane()
fresh.set_font_size(18)
out["no_preview_ok"] = True

print(json.dumps(out)); sys.stdout.flush()
os._exit(0)
'''


@pytest.fixture(scope="module")
def d():
    proc = subprocess.run([sys.executable, "-c", DRIVER], capture_output=True,
                          text=True, timeout=180,
                          env=dict(os.environ, QT_QPA_PLATFORM="offscreen"))
    assert proc.returncode == 0, proc.stderr[-3000:]
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_an_unset_size_renders_at_the_application_default(d):
    assert d["default"] == d["app_default"]
    assert d["at_default"]["base"] == d["app_default"]


def test_the_headings_scale_with_the_body(d):
    """Qt's own zoomIn() moves the default font and leaves an explicitly
    sized run alone, so body text would grow past the headings. This goes
    through the render instead, where the headings are derived."""
    before, after = d["at_default"]["headings"], d["at_20"]["headings"]
    assert len(before) == 2 and 0 not in before, d
    ratio = d["at_20"]["base"] / d["at_default"]["base"]
    for b, a in zip(before, after):
        assert abs(a - b * ratio) < 0.5, (before, after, ratio)


def test_the_error_trace_scales_too(d):
    """It is evaluator output the same eyes have to read."""
    assert d["at_20"]["errors"] == 20


def test_the_size_is_remembered(d):
    assert d["persisted"] == 20
    assert d["second_pane"] == 20, "must survive a restart, not just a rebuild"


def test_the_size_is_clamped_and_the_buttons_say_so(d):
    assert d["clamp_low"] == 6 and d["low_btn"] is False
    assert d["clamp_high"] == 36 and d["high_btn"] is False


def test_resizing_before_anything_is_previewed_is_harmless(d):
    assert d["no_preview_ok"]
