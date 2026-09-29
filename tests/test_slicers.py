"""Design ▸ Send to Slicer: detection, launch commands, and the send itself."""
import json
import ntpath
import os
import subprocess
import sys
import zipfile

from belfryscad import slicers


def test_macos_finds_bundles_in_either_applications_folder():
    have = {"/Applications/OrcaSlicer.app", "/Users/me/Applications/UltiMaker Cura.app"}
    found = slicers.detect("darwin", {"HOME": "/Users/me"}, exists=have.__contains__)
    assert [(s.id, s.path, s.kind) for s in found] == [
        ("orcaslicer", "/Applications/OrcaSlicer.app", "app"),
        ("cura", "/Users/me/Applications/UltiMaker Cura.app", "app"),
    ]


def test_windows_takes_the_newest_cura_and_looks_in_local_programs():
    env = {"ProgramFiles": r"C:\PF", "LOCALAPPDATA": r"C:\Users\me\AppData\Local"}

    def globber(pattern):
        if pattern == ntpath.join(r"C:\PF", r"UltiMaker Cura*\UltiMaker-Cura.exe"):
            return [r"C:\PF\UltiMaker Cura 5.7.0\UltiMaker-Cura.exe", r"C:\PF\UltiMaker Cura 5.10.1\UltiMaker-Cura.exe"]
        if pattern == ntpath.join(r"C:\Users\me\AppData\Local", "Programs", r"OrcaSlicer\orca-slicer.exe"):
            return [pattern]
        return []
    found = {s.id: s.path for s in slicers.detect("win32", env, globber=globber)}
    assert found["orcaslicer"].endswith("orca-slicer.exe")
    # By version, not as strings: as strings "5.7.0" sorts after "5.10.1".
    assert found["cura"] == r"C:\PF\UltiMaker Cura 5.10.1\UltiMaker-Cura.exe"


def test_linux_prefers_path_then_flatpak():
    which = {"prusa-slicer": "/usr/bin/prusa-slicer"}.get
    desktop = "/var/lib/flatpak/exports/share/applications/com.bambulab.BambuStudio.desktop"
    found = slicers.detect("linux", {"HOME": "/home/me"}, exists=lambda p: p == desktop, which=which)
    assert [(s.id, s.kind) for s in found] == [("prusaslicer", "exe"), ("bambustudio", "flatpak")]
    assert found[1].path == "com.bambulab.BambuStudio"


def test_inside_a_flatpak_nothing_on_the_host_is_offered():
    assert slicers.detect("linux", {"FLATPAK_ID": "org.belfryscad.BelfrySCAD"},
                          exists=lambda p: True, which=lambda c: "/usr/bin/" + c) == []


def test_launch_commands():
    f = "/tmp/x/part.3mf"
    assert slicers.launch_command(slicers.Slicer("orcaslicer", "OrcaSlicer", "/Applications/OrcaSlicer.app", "app"), f) \
        == ["open", "-a", "/Applications/OrcaSlicer.app", f]
    assert slicers.launch_command(slicers.Slicer("p", "P", "/usr/bin/prusa-slicer", "exe"), f) == ["/usr/bin/prusa-slicer", f]
    assert slicers.launch_command(slicers.Slicer("b", "B", "com.bambulab.BambuStudio", "flatpak"), f) \
        == ["flatpak", "run", "--file-forwarding", "com.bambulab.BambuStudio", "@@", f, "@@"]
    assert slicers.launch_command(slicers.SYSTEM_DEFAULT, f) is None
    assert slicers.custom("/Applications/Foo Slicer.app/") == slicers.Slicer("custom", "Foo Slicer", "/Applications/Foo Slicer.app/", "app")


DRIVER = '''
import json, os, sys, tempfile, time
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication
app = QApplication([])
from belfryscad.settings import use_scratch_settings
use_scratch_settings(tempfile.mkdtemp(prefix="belfryscad-test-"), seed=False)  # never touch the real store
import belfryscad.window.main_window as mw
from belfryscad.window.preferences import load_preference
d = tempfile.mkdtemp()
# A stand-in slicer: records the file it was handed.
record = os.path.join(d, "argv.json")
fake = os.path.join(d, "fake-slicer")
open(fake, "w").write("#!" + sys.executable + "\\nimport json, sys\\njson.dump(sys.argv[1:], open(%r, 'w'))\\n" % record)
os.chmod(fake, 0o755)

w = mw.MainWindow(); w.skip_unsaved_prompts = True
warnings = []
mw.QMessageBox.warning = staticmethod(lambda parent, title, text: warnings.append(text))
mw.QMessageBox.critical = staticmethod(lambda parent, title, text: warnings.append(text))

def render(src, name="bracket.scad"):
    p = os.path.join(d, name); open(p, "w").write(src)
    w.open_file_by_path(p)
    deadline = time.time() + 60
    while w._geometry is None and time.time() < deadline:
        app.processEvents(); time.sleep(0.05)

out = {}
# First send, nothing chosen yet: asks, via "Other Application...".
render("color(\\"red\\") cube(10); color(\\"blue\\") translate([20,0,0]) cube(5);")
from PySide6.QtWidgets import QInputDialog
QInputDialog.getItem = staticmethod(lambda *a, **k: ("Other Application…", True))
mw.QFileDialog.getOpenFileName = staticmethod(lambda *a, **k: (fake, ""))
w._send_to_slicer()
deadline = time.time() + 20
while not os.path.exists(record) and time.time() < deadline:
    time.sleep(0.05)
sent = json.load(open(record))
out["argv_len"] = len(sent)
out["name"] = os.path.basename(sent[0])
out["is_3mf_zip"] = zipfile_ok = __import__("zipfile").is_zipfile(sent[0])
out["pref"] = [load_preference("print/slicer", str), load_preference("print/slicerPath", str) == fake]
# Second send: remembered, no question.
os.remove(record)
QInputDialog.getItem = staticmethod(lambda *a, **k: (_ for _ in ()).throw(AssertionError("asked again")))
w._send_to_slicer()
deadline = time.time() + 20
while not os.path.exists(record) and time.time() < deadline:
    time.sleep(0.05)
out["second_send"] = os.path.exists(record)
# A 2D model is refused, and nothing launched.
os.remove(record)
w._geometry = None; w._bodies = []
render("square(10);", "outline.scad")
w._send_to_slicer()
time.sleep(1)
out["2d_launched"] = os.path.exists(record)
out["warnings"] = warnings
print(json.dumps(out)); sys.stdout.flush(); os._exit(0)
'''


def test_send_to_slicer_exports_3mf_and_launches_the_chosen_app(tmp_path):
    if sys.platform.startswith("win"):
        return  # the stand-in slicer is a #! script
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    proc = subprocess.run([sys.executable, "-c", DRIVER], capture_output=True, text=True, env=env, timeout=180)
    assert proc.returncode == 0, proc.stderr[-2000:]
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["argv_len"] == 1
    assert out["name"] == "bracket.3mf"       # named like the export would be
    assert out["is_3mf_zip"]
    assert out["pref"] == ["custom", True]
    assert out["second_send"]
    assert out["2d_launched"] is False
    assert out["warnings"] == ["This model is 2D. A slicer needs a 3D model: extrude it first."]
