"""Submenus switch after 0.2 s, not Qt's 1 s (the editor's View as.../Edit as...)."""
import subprocess
import sys

DRIVER = '''
from PySide6.QtWidgets import QApplication, QStyle
app = QApplication([])
before = app.style().name()
from belfryscad.main import _snappier_submenus, SUBMENU_SLOPPY_MS
_snappier_submenus(app)
H = QStyle.StyleHint
print("RESULT", app.style().baseStyle().name() == before,
      app.style().styleHint(H.SH_Menu_SubMenuSloppyCloseTimeout) == SUBMENU_SLOPPY_MS,
      app.style().styleHint(H.SH_Menu_SubMenuPopupDelay) == app.style().baseStyle().styleHint(H.SH_Menu_SubMenuPopupDelay))
'''


def test_sloppy_timeout_shortened_style_kept():
    r = subprocess.run([sys.executable, "-c", DRIVER], capture_output=True, text=True, timeout=60)
    assert "RESULT True True True" in r.stdout, r.stdout + r.stderr
