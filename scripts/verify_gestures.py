"""Throwaway: drive a real Viewport with synthesized gesture/wheel events
and assert the camera actually moves. Not a pytest test -- constructing a
QOpenGLWidget inside pytest crashes the runner."""
import sys
from PySide6.QtGui import QSurfaceFormat
fmt = QSurfaceFormat(); fmt.setVersion(3, 3)
fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
QSurfaceFormat.setDefaultFormat(fmt)

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt, QPointF, QPoint
from PySide6.QtGui import QNativeGestureEvent, QWheelEvent, QPointingDevice
from belfryscad.window.viewport import Viewport

app = QApplication(sys.argv)
vp = Viewport()
vp.resize(800, 600)
vp.show()
app.processEvents()

dev = QPointingDevice.primaryPointingDevice()
cam = vp._renderer.camera
POS = QPointF(400, 300)

def gesture(gtype, value):
    return QNativeGestureEvent(gtype, dev, 2, POS, POS, POS, value, QPointF(0, 0), 0)

def wheel(pixel, angle, mods=Qt.KeyboardModifier.NoModifier):
    return QWheelEvent(POS, POS, QPoint(*pixel), QPoint(*angle), Qt.MouseButton.NoButton,
                       mods, Qt.ScrollPhase.NoScrollPhase, False)

fails = []
def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  -> {detail}"))
    if not cond: fails.append(name)

# --- pinch zoom ---
cam.distance = 50.0
vp.event(gesture(Qt.NativeGestureType.ZoomNativeGesture, 0.10))
zoom_in = cam.distance
check("pinch out moves camera closer", zoom_in < 50.0, f"distance {zoom_in}")
cam.distance = 50.0; cam.target[:] = 0
vp.event(gesture(Qt.NativeGestureType.ZoomNativeGesture, -0.10))
check("pinch in moves camera away", cam.distance > 50.0, f"distance {cam.distance}")
# reciprocal, not (1-value): +0.10 then -0.0909… must round-trip
cam.distance = 50.0; cam.target[:] = 0
vp.event(gesture(Qt.NativeGestureType.ZoomNativeGesture, 0.25))
vp.event(gesture(Qt.NativeGestureType.ZoomNativeGesture, -0.2))
check("pinch zoom round-trips", abs(cam.distance - 50.0) < 1e-6, f"distance {cam.distance}")
# degenerate value must not divide by zero
cam.distance = 50.0
vp.event(gesture(Qt.NativeGestureType.ZoomNativeGesture, -1.0))
check("pinch value <= -1 is ignored", cam.distance == 50.0, f"distance {cam.distance}")

# --- rotate gesture ---
cam.roll = 0.0
vp.event(gesture(Qt.NativeGestureType.RotateNativeGesture, 10.0))
check("rotate changes roll", abs(cam.roll - 0.0) > 1e-9, f"roll {cam.roll}")
r1 = cam.roll
vp.event(gesture(Qt.NativeGestureType.RotateNativeGesture, -10.0))
check("rotate is reversible", abs(cam.roll) < 1e-9, f"roll {cam.roll}")
# disabled when orbit is locked (2D data viewers)
vp._orbit_enabled = False; cam.roll = 0.0
vp.event(gesture(Qt.NativeGestureType.RotateNativeGesture, 10.0))
check("rotate ignored when orbit locked", cam.roll == 0.0, f"roll {cam.roll}")
vp._orbit_enabled = True

# --- smart zoom ---
cam.distance = 999.0
vp.event(gesture(Qt.NativeGestureType.SmartZoomNativeGesture, 0.0))
check("smart zoom accepted (no crash)", True)

# --- begin/end swallowed ---
for g in (Qt.NativeGestureType.BeginNativeGesture, Qt.NativeGestureType.EndNativeGesture):
    check(f"{g.name} swallowed", vp.event(gesture(g, 0.0)) is True)

# --- wheel: mouse vs trackpad ---
cam.distance = 50.0; cam.target[:] = 0
vp.wheelEvent(wheel((0, 0), (0, 120)))                       # mouse wheel
# zoom-to-cursor deliberately nudges target too (Camera.zoom_to_point);
# with the cursor at screen centre that nudge is float noise, so assert
# on distance and only that target didn't *pan*.
check("mouse wheel zooms", cam.distance != 50.0 and abs(cam.target).max() < 1e-4,
      f"distance {cam.distance} target {cam.target}")

cam.distance = 50.0; cam.target[:] = 0
vp.wheelEvent(wheel((30, 40), (0, 0)))                       # trackpad scroll
check("trackpad scroll pans, not zooms",
      cam.distance == 50.0 and (cam.target != 0).any(),
      f"distance {cam.distance} target {cam.target}")

cam.distance = 50.0; cam.target[:] = 0
vp.wheelEvent(wheel((0, 40), (0, 0), Qt.KeyboardModifier.ControlModifier))
check("Cmd+trackpad scroll zooms", cam.distance != 50.0, f"distance {cam.distance}")

cam.fov = 22.5
vp.wheelEvent(wheel((0, 40), (0, 0), Qt.KeyboardModifier.ShiftModifier))
check("Shift+trackpad scroll changes FOV", cam.fov != 22.5, f"fov {cam.fov}")
cam.fov = 22.5
vp.wheelEvent(wheel((0, 0), (0, 120), Qt.KeyboardModifier.ShiftModifier))
check("Shift+wheel changes FOV", cam.fov != 22.5, f"fov {cam.fov}")

cam.distance = 50.0; cam.target[:] = 0
vp.wheelEvent(wheel((0, 0), (0, 2)))                         # inside deadspot
check("tiny wheel delta ignored", cam.distance == 50.0, f"distance {cam.distance}")

# --- _on_zoom_changed hook contract ---
# Data-viewer viewports size vertex markers in screen space and rebuild
# them from this hook. It must fire for every route that changes apparent
# scale -- crucially including pinch, which never reaches wheelEvent --
# and must NOT fire for pan, which fires constantly during a trackpad drag.
class _CountingViewport(Viewport):
    zooms = 0
    def _on_zoom_changed(self):
        self.zooms += 1

cv = _CountingViewport(); cv.resize(800, 600); cv.show()
app.processEvents()

def fired(fn):
    before = cv.zooms; fn(); return cv.zooms > before

check("hook fires on pinch", fired(lambda: cv.event(gesture(Qt.NativeGestureType.ZoomNativeGesture, 0.1))))
check("hook fires on wheel zoom", fired(lambda: cv.wheelEvent(wheel((0, 0), (0, 120)))))
check("hook fires on Cmd+trackpad zoom",
      fired(lambda: cv.wheelEvent(wheel((0, 40), (0, 0), Qt.KeyboardModifier.ControlModifier))))
check("hook fires on FOV change",
      fired(lambda: cv.wheelEvent(wheel((0, 0), (0, 120), Qt.KeyboardModifier.ShiftModifier))))
check("hook fires on smart zoom",
      fired(lambda: cv.event(gesture(Qt.NativeGestureType.SmartZoomNativeGesture, 0.0))))
check("hook does NOT fire on trackpad pan",
      not fired(lambda: cv.wheelEvent(wheel((30, 40), (0, 0)))))
check("hook does NOT fire on rotate",
      not fired(lambda: cv.event(gesture(Qt.NativeGestureType.RotateNativeGesture, 10.0))))

print()
print(f"{len(fails)} failure(s)" + (": " + ", ".join(fails) if fails else ""))
sys.exit(1 if fails else 0)
