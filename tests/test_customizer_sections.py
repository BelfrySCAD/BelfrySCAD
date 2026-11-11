"""#1110: Customizer groups are collapsible sections in one list, not tabs."""
import json
import subprocess
import sys

DRIVER = '''
import json, os, tempfile
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication, QToolButton
app = QApplication([])
from belfryscad.settings import use_scratch_settings
use_scratch_settings(tempfile.mkdtemp(prefix="belfryscad-test-"), seed=False)
from belfryscad.window.customizer import CustomizerPane
pane = CustomizerPane(); pane.resize(300, 600); pane.show()
src = "/* [Size] */\\nw = 3;\\nh = 4;\\n/* [Look] */\\nr = 1;\\n/* [Extra] */\\nn = 2;\\n"
pane.set_source(src); app.processEvents()
def headers():
    return [b for b in pane._sections.widget().findChildren(QToolButton) if b.isCheckable()]
out = {"names": [b.text() for b in headers()]}
headers()[1].click(); app.processEvents()      # close Look
out["closed_after_click"] = sorted(pane._collapsed)
out["new_param_group"] = pane._current_tab_name()
pane.set_source(src + "/* [More] */\\nm = 1;\\n"); app.processEvents()   # a rebuild
out["after_rebuild"] = [(b.text(), b.isChecked()) for b in headers()]
pane.set_source("w = 3;\\n"); app.processEvents()
out["single_group_headers"] = len(headers())
print("RESULT" + json.dumps(out), flush=True)
from belfryscad.main import hard_exit; hard_exit(0)
'''


def test_groups_are_collapsible_sections():
    r = subprocess.run([sys.executable, "-c", DRIVER], capture_output=True, text=True, timeout=120)
    line = next((l for l in r.stdout.splitlines() if l.startswith("RESULT")), None)
    assert line, r.stdout + r.stderr
    out = json.loads(line[6:])
    assert out == {"names": ["Size", "Look", "Extra"], "closed_after_click": ["Look"],
                   "new_param_group": "Look",
                   "after_rebuild": [["Size", True], ["Look", False], ["Extra", True], ["More", True]],
                   "single_group_headers": 0}, out
