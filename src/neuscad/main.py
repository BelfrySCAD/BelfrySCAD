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
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
