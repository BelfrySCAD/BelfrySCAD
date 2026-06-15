import os
import sys
import setproctitle
from PySide6.QtGui import QSurfaceFormat
from PySide6.QtWidgets import QApplication
from neuscad.window.main_window import MainWindow


def _configure_gl_format():
    fmt = QSurfaceFormat()
    fmt.setVersion(3, 3)
    fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
    fmt.setDepthBufferSize(24)
    fmt.setSamples(4)
    QSurfaceFormat.setDefaultFormat(fmt)


def main():
    setproctitle.setproctitle("NeuSCAD")
    sys.setrecursionlimit(10000)
    _configure_gl_format()
    app = QApplication(sys.argv)
    app.setApplicationName("NeuSCAD")
    window = MainWindow()
    window.show()
    code = app.exec()
    # Skip normal interpreter finalization: its GC pass can crash inside
    # manifold3d's nanobind bindings if a background render thread was
    # recently active (see MainWindow.closeEvent). MainWindow.closeEvent
    # has already saved settings (with an explicit sync()) and released
    # geometry, so there's nothing left to clean up.
    os._exit(code)


if __name__ == "__main__":
    main()
