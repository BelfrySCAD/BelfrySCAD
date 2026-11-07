"""Drawing editors reopen at the size and place they were closed at."""
import json
import subprocess
import sys

DRIVER = '''
import json, os, tempfile
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication, QDialog
app = QApplication([])
from belfryscad.settings import use_scratch_settings
use_scratch_settings(tempfile.mkdtemp(prefix="belfryscad-test-"), seed=False)
from belfryscad.window.data_viewer_common import open_large
class Ed(QDialog): pass
class Other(QDialog): pass
out = {}
d = Ed(); open_large(d); d.show(); app.processEvents()
out["first"] = list(d.geometry().getRect())
d.setGeometry(100, 120, 640, 400); app.processEvents()
d.done(0)
e = Ed(); open_large(e); e.show(); app.processEvents()
out["again"] = list(e.geometry().getRect())
o = Other(); open_large(o)
out["other"] = list(o.geometry().getRect())
print("RESULT" + json.dumps(out)); import sys; sys.stdout.flush()
from belfryscad.main import hard_exit; hard_exit(0)
'''


def test_editor_reopens_where_it_was_closed():
    r = subprocess.run([sys.executable, "-c", DRIVER], capture_output=True, text=True, timeout=120)
    line = next((l for l in r.stdout.splitlines() if l.startswith("RESULT")), None)
    assert line, r.stdout + r.stderr
    out = json.loads(line[6:])
    assert out["again"] == [100, 120, 640, 400]
    assert out["other"] == out["first"] != out["again"]   # each kind remembers its own
