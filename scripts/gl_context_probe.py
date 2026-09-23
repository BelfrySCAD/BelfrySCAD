#!/usr/bin/env python3
"""Open the two kinds of GL widget BelfrySCAD has, and check both draw.

moderngl attaches to the first QOpenGLWidget one way and to every later one
another, so a platform can pass for the main viewport and still fail every
data viewer. This opens one of each -- the main `Viewport`, then a
`VNFViewer` -- under whatever QT_QPA_PLATFORM is set, and passes only if both
got a GL context, drew something other than a blank frame, and raised
nothing. CI runs it under X11 (xvfb) and Wayland (headless Weston); see
.github/workflows/linux-display.yml.

Exit status 0 on pass, 1 on fail.
"""
import sys
import traceback

from PySide6.QtCore import QTimer
from PySide6.QtGui import QSurfaceFormat
from PySide6.QtWidgets import QApplication

fmt = QSurfaceFormat()
fmt.setVersion(3, 3)
fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
fmt.setDepthBufferSize(24)
QSurfaceFormat.setDefaultFormat(fmt)        # as main.py, before QApplication

app = QApplication(sys.argv)
errors = []
sys.excepthook = lambda *e: errors.append("".join(traceback.format_exception(*e)))

from belfryscad.window.data_viewer_vnf import VNFViewer     # noqa: E402
from belfryscad.window.viewport import Viewport             # noqa: E402

main_vp = Viewport()
main_vp.resize(320, 240)
main_vp.show()
viewer = VNFViewer("probe", [[[0, 0, 0], [10, 0, 0], [0, 10, 0]], [[0, 1, 2]]])
viewer.show()


def check():
    ok = True
    print("platform:", QApplication.platformName())
    for name, vp in (("main viewport", main_vp), ("data viewer", viewer._vp)):
        ctx = vp._ctx
        if ctx is None:
            print(f"FAIL {name}: no GL context")
            ok = False
            continue
        img = vp.grabFramebuffer()
        colours = {img.pixel(x, y) for x in range(0, img.width(), 7)
                   for y in range(0, img.height(), 7)}
        print(f"{name}: {ctx.info['GL_RENDERER']} / {ctx.info['GL_VERSION']}, "
              f"{len(colours)} colours sampled")
        if len(colours) < 2:
            print(f"FAIL {name}: blank frame")
            ok = False
    for e in errors:
        print("FAIL exception:\n" + e)
    app.exit(0 if ok and not errors else 1)


QTimer.singleShot(1500, check)
sys.exit(app.exec())
