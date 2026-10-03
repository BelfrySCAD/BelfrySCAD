"""The Docs pane's target folder: preview a file as if it lived elsewhere.

A docs build takes the .openscad_docsgen_rc, the folder examples run in, the
library a checkout stands for and the sibling files from where a file lives,
so the pane can preview a file -- saved anywhere, or not saved at all -- as if
it were already in the folder it is meant for (docsgen.preview.planned_path).
"""
import json
import os
import subprocess
import sys

from belfryscad.docsgen.preview import UNTITLED_NAME, find_rc, planned_path


def test_no_target_leaves_the_path_alone():
    assert planned_path("/a/b/lib.scad", None) == "/a/b/lib.scad"
    assert planned_path("", None) == ""


def test_a_target_moves_the_file_keeping_its_name():
    assert planned_path("/drafts/lib.scad", "/libs/BOSL2") == os.path.join("/libs/BOSL2", "lib.scad")


def test_an_unsaved_buffer_gets_a_name_in_the_target():
    assert planned_path("", "/libs/BOSL2") == os.path.join("/libs/BOSL2", UNTITLED_NAME)


def test_the_rc_is_found_from_the_planned_folder(tmp_path):
    """The point of the feature: the rc above the TARGET is used, not the one
    (or none) above where the file happens to be."""
    project = tmp_path / "project"
    (project / "sub").mkdir(parents=True)
    (project / ".openscad_docsgen_rc").write_text("ProjectName: Target\n")
    drafts = tmp_path / "drafts"
    drafts.mkdir()
    real = drafts / "new.scad"
    assert find_rc(str(real)) is None
    assert find_rc(planned_path(str(real), str(project / "sub"))) == str(project / ".openscad_docsgen_rc")


_PANE = r"""
import json, os, sys, tempfile
from PySide6.QtWidgets import QApplication
from belfryscad.settings import use_scratch_settings, app_settings
use_scratch_settings(tempfile.mkdtemp(prefix="belfryscad-test-"), seed=False)
from belfryscad.window.docs_pane import DocsPane
app = QApplication.instance() or QApplication([])
pane = DocsPane()
built = []
pane._queue = lambda text, path, images: built.append(path)
out = {}
target = tempfile.mkdtemp(prefix="target-")
# untitled buffer: refused until a folder is chosen, then built in it
pane.refresh("// x", "")
out["untitled_before"] = built[:]
pane._set_target_folder("", target)
pane.refresh("// x", "")
out["untitled_after"] = built[-1]
# a saved file: built at its planned path, remembered across panes
real = os.path.join(tempfile.mkdtemp(prefix="drafts-"), "lib.scad")
pane._set_target_folder(real, target)
pane.refresh("// x", real)
out["saved"] = built[-1]
out["button"] = pane._folder_btn.text()
out["persisted"] = json.loads(app_settings().value(DocsPane._TARGETS_KEY, "{}"))
out["second_pane"] = DocsPane().target_folder(real)
# choosing the file's own folder clears it
pane._set_target_folder(real, os.path.dirname(real))
pane.refresh("// x", real)
out["own_folder"] = built[-1]
out["real"], out["target"] = real, target
print(json.dumps(out), flush=True)
os._exit(0)   # the pane's worker thread is still running; skip Qt's teardown
"""


def test_the_pane_builds_at_the_planned_path_and_remembers_it():
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    r = subprocess.run([sys.executable, "-c", _PANE], capture_output=True, text=True, env=env, timeout=120)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out["untitled_before"] == []
    assert out["untitled_after"] == os.path.join(out["target"], UNTITLED_NAME)
    assert out["saved"] == os.path.join(out["target"], "lib.scad")
    assert out["button"] == "Folder: " + os.path.basename(out["target"])
    assert out["persisted"] == {out["real"]: out["target"]}       # the unsaved buffer's is not kept
    assert out["second_pane"] == out["target"]
    assert out["own_folder"] == out["real"]
