"""Shorter document tabs (#1096): no .scad by default, and tabs that shrink
longest first, down to a minimum, with scroll arrows past it."""
import json
import subprocess
import sys

from belfryscad.window.main_window import _water_level


def test_water_level_cuts_the_widest_first():
    assert _water_level([50, 100, 300], 1000) is None          # fits already
    assert _water_level([50, 100, 300], 350) == 200            # only the widest cut
    assert _water_level([50, 100, 300], 250) == 100            # down to the next widest
    assert _water_level([50, 100, 300], 150) == 50             # now every tab the same


DRIVER = '''
import json, os, tempfile
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication
app = QApplication([])
from belfryscad.settings import use_scratch_settings, app_settings
use_scratch_settings(tempfile.mkdtemp(prefix="belfryscad-test-"), seed=False)
from belfryscad.window.main_window import MainWindow
MainWindow._render = lambda *a, **k: None
d = os.path.realpath(tempfile.mkdtemp())
names = ("a.scad", "b.scad", "notes.txt", "Shout.SCAD", "a_very_long_name_for_a_bracket_part.scad")
for n in names:
    open(os.path.join(d, n), "w").write("cube(1);\\n")
w = MainWindow(); w.resize(1600, 700); w.show(); app.processEvents()
for n in names:
    w.open_file_by_path(os.path.join(d, n))
bar = w._tabs.tab_bar
w._tabs.setCurrentIndex(3); w._current_tab().editor.insertPlainText("x")
def labels(): return [bar.tabText(i) for i in range(bar.count()) if bar.tabText(i) != "Untitled"]
def widths():
    app.processEvents()
    return [bar.tabRect(i).width() for i in range(bar.count())]
def fits():
    return sum(widths()) <= bar.width() + 1
out = {"default": labels(), "tooltip_full": bar.tabToolTip(4).endswith(".scad")}
w._tabs.setCurrentIndex(4)             # the current tab is drawn bolder: keep it a long one
natural = widths()
# More long names than fit: the short tabs keep their width, the long ones
# are cut to one level.
# Added one at a time until they first overflow, so the test holds at any
# bar width (the Windows runner's screen is narrower).
k = 0
while sum(bar.__class__.__mro__[1].tabSizeHint(bar, i).width() for i in range(bar.count())) <= bar.width():
    p = os.path.join(d, f"another_quite_long_part_name_{k}.scad"); open(p, "w").write("x\\n")
    w.open_file_by_path(p); k += 1
n_long = bar.count()
cut = widths()
out["short_kept"] = cut[:4] == natural[:4]
nat = [bar.__class__.__mro__[1].tabSizeHint(bar, i).width() for i in range(bar.count())]
level = max(cut)
# Every tab keeps its own width or sits at the one level; the longest is cut.
out["long_level"] = (all(c == n or abs(c - level) <= 1 for c, n in zip(cut, nat))
                     and cut[4] < nat[4])
out["fits"] = fits()
# Far more than fit even at the minimum: the arrows take over.
for k in range(40):
    p = os.path.join(d, f"more_{k}.scad"); open(p, "w").write("x\\n")
    w.open_file_by_path(p)
tiny = widths()
fm = bar.fontMetrics()
# The long tabs stop at their floor -- 12 characters of the name -- not below.
out["min_kept"] = all(bar.minimumTabSizeHint(i).width() <= tiny[i] < cut[i] for i in range(4, n_long))
out["short_not_widened"] = tiny[:4] == natural[:4]
out["scrolls"] = not fits()
for i in range(bar.count() - 1, 4, -1):
    w._close_tab(i)
app_settings().setValue("editor/tabHideScadExtension", False)
w._apply_preferences()
out["shown"] = labels()
print("RESULT" + json.dumps(out)); import sys; sys.stdout.flush()
from belfryscad.main import hard_exit; hard_exit(0)
'''


def test_tab_labels_and_shrinking():
    r = subprocess.run([sys.executable, "-c", DRIVER], capture_output=True, text=True, timeout=120)
    line = next((l for l in r.stdout.splitlines() if l.startswith("RESULT")), None)
    assert line, r.stdout + r.stderr
    out = json.loads(line[6:])
    assert out == {"default": ["a", "b", "notes.txt", "Shout*", "a_very_long_name_for_a_bracket_part"],
                   "tooltip_full": True, "short_kept": True, "long_level": True, "fits": True,
                   "min_kept": True, "short_not_widened": True, "scrolls": True,
                   "shown": ["a.scad", "b.scad", "notes.txt", "Shout.SCAD*",
                             "a_very_long_name_for_a_bracket_part.scad"]}, out
