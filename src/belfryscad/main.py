import argparse
import os
import sys
import setproctitle


def _parse_args(argv):
    parser = argparse.ArgumentParser(prog="belfryscad", add_help=False)
    parser.add_argument("file", nargs="?")
    parser.add_argument("-o", "--output", metavar="FILE",
                         help="Render FILE headlessly and export to this path (.stl/.obj/.3mf); no GUI window opens")
    parser.add_argument("-D", dest="defines", action="append", default=[], metavar="var=value",
                         help="Override a top-level variable (repeatable). Only applies together with -o")
    parser.add_argument("--render", action="store_true",
                         help="Accepted for OpenSCAD CLI compatibility -- BelfrySCAD has no separate "
                              "preview mode, so this has no effect (headless export always fully renders)")
    parser.add_argument("-h", "--help", action="store_true")
    # parse_known_args, not parse_args: GUI-launched app bundles can receive
    # OS-injected arguments unrelated to this app (e.g. macOS LaunchServices'
    # own -psn_... process serial number) -- match the pre-argparse loop's
    # own tolerance of anything it didn't recognize rather than erroring out.
    ns, _unknown = parser.parse_known_args(argv)
    if ns.help:
        parser.print_help()
        raise SystemExit(0)
    return ns


def _run_gui(initial_file: str | None):
    from PySide6.QtCore import QEvent, Signal
    from PySide6.QtGui import QSurfaceFormat
    from PySide6.QtWidgets import QApplication
    from belfryscad.window.main_window import MainWindow

    class BelfrySCADApp(QApplication):
        file_open_requested = Signal(str)

        def event(self, event):
            if event.type() == QEvent.Type.FileOpen:
                self.file_open_requested.emit(event.file())
                return True
            return super().event(event)

    fmt = QSurfaceFormat()
    fmt.setVersion(3, 3)
    fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
    fmt.setDepthBufferSize(24)
    fmt.setSamples(4)
    QSurfaceFormat.setDefaultFormat(fmt)

    app = BelfrySCADApp(sys.argv)
    app.setApplicationName("BelfrySCAD")
    window = MainWindow()
    app.file_open_requested.connect(window.open_file_by_path)
    window.show()
    if initial_file and initial_file.endswith(".scad") and os.path.isfile(initial_file):
        window.open_file_by_path(os.path.abspath(initial_file))
    code = app.exec()
    # Skip normal interpreter finalization: its GC pass can crash inside
    # manifold3d's nanobind bindings if a background render thread was
    # recently active (see MainWindow.closeEvent). MainWindow.closeEvent
    # has already saved settings (with an explicit sync()) and released
    # geometry, so there's nothing left to clean up.
    os._exit(code)


def main():
    setproctitle.setproctitle("BelfrySCAD")
    sys.setrecursionlimit(10000)
    args = _parse_args(sys.argv[1:])

    if args.output:
        # Headless export: deliberately never imports PySide6/creates a
        # QApplication -- no display or GPU needed, just the evaluator +
        # plain file I/O (see belfryscad.headless/belfryscad.exporters).
        if not args.file:
            print("belfryscad: -o/--output requires an input .scad file", file=sys.stderr)
            raise SystemExit(1)
        from belfryscad.headless import render_and_export
        raise SystemExit(render_and_export(args.file, args.output, defines=args.defines))

    if args.defines:
        print("belfryscad: -D only applies together with -o/--output; ignoring", file=sys.stderr)

    _run_gui(args.file)


if __name__ == "__main__":
    main()
