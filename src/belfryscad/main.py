import argparse
import os
import sys
import setproctitle


def _parse_args(argv):
    parser = argparse.ArgumentParser(prog="belfryscad", add_help=False)
    parser.add_argument("file", nargs="?")
    parser.add_argument("-o", "--output", metavar="FILE",
                         help="Render FILE headlessly and export to this path (.stl/.obj/.3mf/.png); "
                              "no GUI window opens")
    parser.add_argument("-D", dest="defines", action="append", default=[], metavar="var=value",
                         help="Override a top-level variable (repeatable). Only applies together with -o")
    parser.add_argument("--render", action="store_true",
                         help="Accepted for OpenSCAD CLI compatibility -- BelfrySCAD has no separate "
                              "preview mode, so this has no effect (headless export always fully renders)")
    parser.add_argument("--animate", type=int, metavar="N",
                         help="Export N animated frames ($t = i/N) instead of a single render. "
                              "Only applies together with -o; frames are named {stem}{00000..N-1}{ext}")
    parser.add_argument("--animate_dir", metavar="DIR",
                         help="Write --animate frames to DIR instead of -o's own directory")
    parser.add_argument("-q", "--quiet", action="store_true",
                         help="Quiet mode -- don't print anything except errors. Only applies together with -o")
    parser.add_argument("--hardwarnings", action="store_true",
                         help="Stop on the first warning (treated as a fatal error). Only applies together with -o")
    parser.add_argument("--export-format", dest="export_format", metavar="FORMAT",
                         help="'asciistl' or 'binstl' -- overrides .stl export format (default binstl). "
                              "Only applies together with -o")
    parser.add_argument("--backend", metavar="NAME",
                         help="Accepted for OpenSCAD CLI compatibility -- must be 'Manifold' "
                              "(BelfrySCAD has no CGAL backend). Only applies together with -o")
    parser.add_argument("--summary", metavar="KEYS",
                         help="Comma-separated summary info to print after export: all, time, geometry, "
                              "bounding-box. Only applies together with -o")
    parser.add_argument("--summary-file", dest="summary_file", metavar="FILE",
                         help="Write --summary as JSON to FILE ('-' for stdout) instead of printing it plainly")
    parser.add_argument("--imgsize", metavar="W,H", default="1024,768",
                         help="=width,height of exported .png (default 1024,768)")
    parser.add_argument("--camera", metavar="SPEC",
                         help="Camera for .png export: =tx,ty,tz,rx,ry,rz,dist or =eye_x,y,z,center_x,y,z")
    parser.add_argument("--autocenter", action="store_true", help="Center the camera on the object (.png only)")
    parser.add_argument("--viewall", action="store_true", help="Fit the camera to the whole object (.png only)")
    parser.add_argument("--projection", metavar="(o)rtho|(p)erspective", help="Camera projection for .png export")
    parser.add_argument("--view", metavar="OPTS",
                         help="Comma-separated: axes, crosshairs, edges, scales, wireframe (.png only)")
    parser.add_argument("--colorscheme", metavar="NAME", help="Color theme for .png export")
    parser.add_argument("-v", "--version", action="store_true", help="Print the version and exit")
    parser.add_argument("--info", action="store_true", help="Print build/environment information and exit")
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


def _belfryscad_version() -> str:
    import importlib.metadata
    try:
        return importlib.metadata.version("belfryscad")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def _print_info():
    import platform
    print(f"BelfrySCAD {_belfryscad_version()}")
    print(f"Python {platform.python_version()} ({platform.platform()})")
    for pkg in ("PySide6", "moderngl", "openscad_cpp_evaluator", "manifold3d", "numpy"):
        import importlib.metadata
        try:
            print(f"{pkg} {importlib.metadata.version(pkg)}")
        except importlib.metadata.PackageNotFoundError:
            print(f"{pkg} not installed")


def main():
    setproctitle.setproctitle("BelfrySCAD")
    sys.setrecursionlimit(10000)
    args = _parse_args(sys.argv[1:])

    if args.version:
        print(f"BelfrySCAD {_belfryscad_version()}")
        raise SystemExit(0)
    if args.info:
        _print_info()
        raise SystemExit(0)

    if args.output:
        # Headless export: deliberately never imports PySide6/creates a
        # QApplication for mesh output (no display or GPU needed -- see
        # belfryscad.headless/belfryscad.exporters). .png output is the one
        # exception: it genuinely needs Qt (QImage/QPainter for axis-label
        # textures) and an offscreen GL context -- see
        # belfryscad.headless_render's own module doc comment.
        if not args.file:
            print("belfryscad: -o/--output requires an input .scad file", file=sys.stderr)
            raise SystemExit(1)
        common = dict(defines=args.defines, quiet=args.quiet, hard_warnings=args.hardwarnings, backend=args.backend)
        if args.output.lower().endswith(".png"):
            png_common = dict(
                common, imgsize=args.imgsize, camera=args.camera, autocenter=args.autocenter,
                viewall=args.viewall, projection=args.projection, view=args.view, colorscheme=args.colorscheme,
            )
            if args.animate is not None:
                from belfryscad.headless_render import render_png_animation
                raise SystemExit(render_png_animation(
                    args.file, args.output, args.animate, animate_dir=args.animate_dir, **png_common))
            from belfryscad.headless_render import render_png
            raise SystemExit(render_png(args.file, args.output, **png_common))

        mesh_common = dict(common, export_format=args.export_format)
        if args.animate is not None:
            from belfryscad.headless import render_and_export_animation
            raise SystemExit(render_and_export_animation(
                args.file, args.output, args.animate, animate_dir=args.animate_dir, **mesh_common))
        from belfryscad.headless import render_and_export
        raise SystemExit(render_and_export(
            args.file, args.output, summary=args.summary, summary_file=args.summary_file, **mesh_common))

    _only_with_output = {
        "-D": args.defines, "--animate/--animate_dir": args.animate is not None or args.animate_dir,
        "-q/--quiet": args.quiet, "--hardwarnings": args.hardwarnings,
        "--export-format": args.export_format, "--backend": args.backend,
        "--summary/--summary-file": args.summary or args.summary_file,
        "--imgsize/--camera/--autocenter/--viewall/--projection/--view/--colorscheme":
            args.camera or args.autocenter or args.viewall or args.projection or args.view or args.colorscheme,
    }
    ignored = [name for name, used in _only_with_output.items() if used]
    if ignored:
        print(f"belfryscad: {', '.join(ignored)} only apply together with -o/--output; ignoring", file=sys.stderr)

    _run_gui(args.file)


if __name__ == "__main__":
    main()
