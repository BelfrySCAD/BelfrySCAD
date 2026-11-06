"""The statement a file dropped on the editor becomes.

A mesh or 2D drawing becomes `import("...")`, a raster image
`surface(file="...")`. The path is relative to the script's folder when there
is one, as OpenSCAD resolves both relative to the file that names them, so
the script and its parts can move together. Qt-free, so it is testable
without a widget.
"""
import os

# What import() reads as geometry; .json/.csv import as values, not shapes,
# so a statement would be wrong for them. Mirrors openscad_cpp_evaluator's
# src/builtins/import.cpp.
IMPORT_EXTENSIONS = frozenset(
    (".stl", ".obj", ".off", ".3mf", ".amf", ".x3d", ".wrl", ".svg", ".dxf"))
# What surface() decodes as an image (evaluator's src/import/surface_load.cpp).
SURFACE_EXTENSIONS = frozenset((".png", ".jpg", ".jpeg", ".gif", ".bmp"))


def _scad_path(path: str, base_dir: str | None) -> str:
    if base_dir:
        try:
            path = os.path.relpath(path, base_dir)
        except ValueError:  # Windows: another drive has no relative path
            pass
    path = path.replace(os.sep, "/")
    return path.replace("\\", "\\\\").replace('"', '\\"')


def statement_for(path: str, base_dir: str | None) -> str | None:
    """The statement for dropped file `path`, or None if it has none."""
    ext = os.path.splitext(path)[1].lower()
    if ext in IMPORT_EXTENSIONS:
        return f'import("{_scad_path(path, base_dir)}");'
    if ext in SURFACE_EXTENSIONS:
        return f'surface(file="{_scad_path(path, base_dir)}");'
    return None
