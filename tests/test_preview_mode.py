"""Preview (F5) and Render (F6), as in OpenSCAD (#884).

Preview is the norm -- every automatic render is one -- with `$preview` true
and `#`/`%` objects shown. Render has `$preview` false and shows the model as
it exports: a `#` object as an ordinary solid, `#` operand copies and `%`
objects gone. Export renders first when what is on screen is a Preview.

Qt widgets, so it runs in a subprocess (offscreen)."""
import json
import os
import subprocess
import sys

from openscad_cpp_evaluator import ColoredBody

from belfryscad.engine.renderer import as_rendered

DRIVER = r'''
import json, os, sys, tempfile, time
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication, QMenu
app = QApplication([])
from belfryscad.settings import use_scratch_settings
use_scratch_settings(tempfile.mkdtemp(prefix="belfryscad-test-"), seed=False)
import belfryscad.window.main_window as mw
d = tempfile.mkdtemp()
p = os.path.join(d, "mods.scad")
open(p, "w").write("""echo(preview=$preview);
#cube(8);
translate([12,0,0]) %cube(8);
difference() { translate([-12,0,0]) cube(8); #translate([-10,2,2]) cube([12,4,4]); }
""")
w = mw.MainWindow(); w.skip_unsaved_prompts = True
loaded = []
real_load = w._viewport.load_geometry
w._viewport.load_geometry = lambda bodies: (loaded.append(sorted(b.role for b in bodies)), real_load(bodies))

def wait(cond, secs=60):
    end = time.time() + secs
    while not cond() and time.time() < end:
        app.processEvents(); time.sleep(0.02)

def echo():
    return [l for l in w._console.toPlainText().splitlines() if l.startswith("ECHO: preview")]

def roles_after(start):
    n = len(loaded); start(); wait(lambda: len(loaded) > n)
    return loaded[-1]

out = {}
out["menu"] = sorted((a.text().replace("&", ""), a.shortcut().toString())
                     for m in w.menuBar().findChildren(QMenu) for a in m.actions()
                     if a.text().replace("&", "") in ("Preview", "Render", "Reload and Preview",
                                                      "Automatic Reload and Preview"))
w.open_file_by_path(p, render=False)
out["preview"] = roles_after(lambda: w._render())
wait(lambda: echo()); out["preview_echo"] = echo()
out["render"] = roles_after(lambda: w._render(preview=False))
wait(lambda: echo()); out["render_echo"] = echo()

# Export from a Preview renders first, then asks where to save.
asked = []
mw.QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: (asked.append(w._geometry_is_preview), ("", ""))[1])
w._render(); wait(lambda: w._geometry_is_preview and w._geometry is not None and not w._render_jobs)
w._export()
out["asked_at_once"] = list(asked)
wait(lambda: asked)
out["asked_after_render"] = asked
print(json.dumps(out)); sys.stdout.flush(); from belfryscad.main import hard_exit; hard_exit(0)
'''


def test_preview_and_render_modes():
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    proc = subprocess.run([sys.executable, "-c", DRIVER], capture_output=True, text=True, env=env, timeout=180)
    assert proc.returncode == 0, proc.stderr[-2000:]
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["menu"] == [["Automatic Reload and Preview", ""], ["Preview", "F5"],
                           ["Reload and Preview", "F4"], ["Render", "F6"]]
    assert "background" in out["preview"] and "highlight_ghost" in out["preview"]
    assert out["preview_echo"] == ["ECHO: preview = true"]
    assert set(out["render"]) == {"normal"}
    assert out["render_echo"] == ["ECHO: preview = false"]
    # Export waited for the Render instead of writing the Preview.
    assert out["asked_at_once"] == []
    assert out["asked_after_render"] == [False]


def test_as_rendered_keeps_only_what_exports():
    def body(role):
        return ColoredBody(body=None, color=None, role=role)
    got = as_rendered([body("normal"), body("highlight"), body("highlight_ghost"), body("background")])
    assert [b.role for b in got] == ["normal", "normal"]


def test_cli_preview_and_render_flags():
    """OpenSCAD's --preview/--render take a value only as `=VALUE`, so the
    word after them is still the input file."""
    from belfryscad.main import _parse_args
    ns = _parse_args(["-o", "x.png", "--render", "part.scad"])
    assert (ns.render, ns.preview, ns.file) == (True, False, "part.scad")
    ns = _parse_args(["--preview=throwntogether", "-o", "x.png", "part.scad"])
    assert (ns.preview, ns.throwntogether, ns.file) == (True, True, "part.scad")
    ns = _parse_args(["--preview", "part.scad", "-o", "x.png"])
    assert (ns.preview, ns.throwntogether, ns.file) == (True, False, "part.scad")
