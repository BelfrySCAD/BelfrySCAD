from PySide6.QtOpenGLWidgets import QOpenGLWidget
from PySide6.QtCore import Qt, QPoint
from PySide6.QtGui import QMouseEvent, QWheelEvent

from neuscad.engine.renderer import SceneRenderer


class Viewport(QOpenGLWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(400, 300)
        self._ctx = None
        self._renderer = SceneRenderer()
        self._last_mouse: QPoint | None = None
        self._mouse_button: Qt.MouseButton | None = None
        self.setMouseTracking(True)

    # ------------------------------------------------------------------
    # GL lifecycle
    # ------------------------------------------------------------------

    def initializeGL(self):
        import moderngl
        self._ctx = moderngl.create_context(require=330)
        self._renderer.initialize(self._ctx)

    def resizeGL(self, w, h):
        if self._ctx:
            self._ctx.viewport = (0, 0, w, h)
            self._renderer.set_viewport(w, h)

    def paintGL(self):
        try:
            fbo_id = self.defaultFramebufferObject()
            self._renderer.paint(qt_fbo_id=fbo_id)
        except Exception as e:
            import traceback
            print("paintGL error:", traceback.format_exc())

    def closeEvent(self, event):
        self.makeCurrent()
        self._renderer.release()
        self._ctx = None
        self.doneCurrent()
        super().closeEvent(event)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load_geometry(self, bodies):
        """Called from the render pipeline with a list of ColoredBody."""
        self.makeCurrent()
        self._renderer.load_geometry(bodies)
        self.doneCurrent()
        self.update()

    def frame_scene(self, bb_min, bb_max):
        self._renderer.camera.frame_bounds(bb_min, bb_max)
        self.update()

    def set_background_color(self, r, g, b):
        # stored on next paint
        self._renderer._bg_color = (r, g, b, 1.0)
        self.update()

    def camera_info(self) -> dict:
        cam = self._renderer.camera
        return {
            "azimuth": cam.azimuth,
            "elevation": cam.elevation,
            "distance": cam.distance,
            "target": cam.target.tolist(),
            "fov": cam.fov,
        }

    # ------------------------------------------------------------------
    # Camera view presets
    # ------------------------------------------------------------------

    def set_view_preset(self, preset: str):
        cam = self._renderer.camera
        if preset == "top":
            cam.azimuth, cam.elevation = 0, 90
        elif preset == "bottom":
            cam.azimuth, cam.elevation = 0, -90
        elif preset == "front":
            cam.azimuth, cam.elevation = 0, 0
        elif preset == "back":
            cam.azimuth, cam.elevation = 180, 0
        elif preset == "left":
            cam.azimuth, cam.elevation = 270, 0
        elif preset == "right":
            cam.azimuth, cam.elevation = 90, 0
        elif preset == "iso":
            cam.azimuth, cam.elevation = 45, 35
        elif preset == "all":
            # Frame to current bounding box — just reset distance
            cam.azimuth, cam.elevation = 45, 35
        self.update()

    def zoom(self, direction: int):
        cam = self._renderer.camera
        factor = 1.02 if direction < 0 else 0.98
        cam.distance = max(0.1, cam.distance * factor)
        self.update()

    # ------------------------------------------------------------------
    # Mouse input → camera orbit / pan / zoom
    # ------------------------------------------------------------------

    def mousePressEvent(self, event: QMouseEvent):
        self._last_mouse = event.position().toPoint()
        self._mouse_button = event.button()

    def mouseReleaseEvent(self, event: QMouseEvent):
        self._last_mouse = None
        self._mouse_button = None

    def mouseMoveEvent(self, event: QMouseEvent):
        if self._last_mouse is None:
            return
        pos = event.position().toPoint()
        dx = pos.x() - self._last_mouse.x()
        dy = pos.y() - self._last_mouse.y()
        self._last_mouse = pos

        cam = self._renderer.camera
        if self._mouse_button == Qt.MouseButton.LeftButton:
            # Orbit
            cam.azimuth -= dx * 0.5
            cam.elevation = max(-89, min(89, cam.elevation + dy * 0.5))
        elif self._mouse_button == Qt.MouseButton.RightButton:
            # Pan — move target in view-right and view-up directions
            import numpy as np, math
            az = math.radians(cam.azimuth)
            el = math.radians(cam.elevation)
            right = np.array([-math.sin(az), math.cos(az), 0], dtype=np.float32)
            up_approx = np.array([
                -math.sin(el) * math.cos(az),
                -math.sin(el) * math.sin(az),
                math.cos(el),
            ], dtype=np.float32)
            scale = cam.distance * 0.001
            cam.target -= right * dx * scale
            cam.target += up_approx * dy * scale

        self.update()

    def wheelEvent(self, event: QWheelEvent):
        delta = event.angleDelta().y()
        cam = self._renderer.camera
        factor = 1.03 if delta < 0 else 0.97
        cam.distance = max(0.1, cam.distance * factor)
        self.update()
