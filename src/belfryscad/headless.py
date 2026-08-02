"""Headless (no-GUI) rendering and export -- the -o/-D/--animate CLI path.
No Qt dependency, so this is plain pytest-testable unlike the GUI code (see
tests/ conventions -- real Qt widgets/GL contexts crash pytest here)."""

import sys
import tempfile
import time
from pathlib import Path


def build_define_prelude(defines: list[str]) -> str:
    """Turn ["x=5", 'name="foo"'] into OpenSCAD source text to append to
    the input file (see render_and_export's own doc comment for why
    appending, not prepending, is what makes this match real OpenSCAD's -D
    semantics). No separate expression parser needed here -- the real
    parser interprets each value exactly as if it had been typed in the
    script."""
    lines = []
    for d in defines:
        if "=" not in d:
            raise ValueError(f"-D {d!r}: expected var=value")
        name, value = d.split("=", 1)
        name = name.strip()
        if not name:
            raise ValueError(f"-D {d!r}: expected var=value")
        lines.append(f"{name}={value.strip()};\n")
    return "".join(lines)


def _prepare_source(source_path: str, defines: list[str]):
    """Returns (parse_path, tmp_path) on success -- tmp_path is None unless
    a -D prelude was appended, in which case the caller must unlink it when
    done. Returns (None, None) after printing an error."""
    src = Path(source_path)
    if not src.is_file():
        print(f"belfryscad: {source_path}: no such file", file=sys.stderr)
        return None, None

    try:
        prelude = build_define_prelude(list(defines))
    except ValueError as e:
        print(f"belfryscad: {e}", file=sys.stderr)
        return None, None

    if not prelude:
        return str(src), None

    # APPENDED, not prepended: OpenSCAD resolves top-level variables
    # declaratively, not sequentially -- the *last* top-level assignment
    # of a name wins for every use of that name throughout the whole
    # file, including geometry statements that appear textually BEFORE
    # it (confirmed directly against real OpenSCAD.app: `cube([x,1,1]);
    # x=5; cube([x,2,2]);` renders BOTH cubes at x=5, not just the
    # second). Appending here makes these overrides the textually-last
    # assignment of each name, so they win over anything the script
    # itself does with that name, matching real OpenSCAD's own -D
    # semantics exactly, with no evaluator changes needed.
    #
    # Written into the SAME directory as the real file so relative
    # use/include paths still resolve, matching _RenderWorker's own
    # unsaved-buffer convention (main_window.py).
    tmp = tempfile.NamedTemporaryFile(
        suffix=".scad", mode="w", encoding="utf-8", delete=False, dir=str(src.parent)
    )
    tmp.write(src.read_text(encoding="utf-8"))
    tmp.write("\n")
    tmp.write(prelude)
    tmp.close()
    return tmp.name, tmp.name


def _evaluate(parse_path: str, viewport_params: dict):
    """Parse + evaluate parse_path. Returns (bodies, elapsed_seconds) on
    success, or None after printing an error to stderr."""
    from openscad_cpp_evaluator import Evaluator, EvalError, ParseError, parse as _oce_parse, to_renderable_bodies

    t0 = time.perf_counter()
    try:
        _oce_parse(parse_path)
    except ParseError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return None

    evaluator = Evaluator(echo_fn=lambda m: print(m, file=sys.stderr))
    try:
        bodies, _id_to_node = evaluator.evaluate(parse_path, viewport_params)
    except RecursionError:
        print("ERROR: AST too deeply nested (recursion limit exceeded during evaluation).", file=sys.stderr)
        return None
    except EvalError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return None

    elapsed = time.perf_counter() - t0
    if not bodies:
        print("ERROR: Current top level object is not a 3D object.", file=sys.stderr)
        return None
    return to_renderable_bodies(bodies), elapsed


def _export(output_path: str, ext: str, bodies) -> bool:
    """Writes bodies to output_path. Returns True on success, False after
    printing an error to stderr."""
    from belfryscad import exporters

    try:
        if ext == ".3mf":
            exporters.write_3mf(output_path, bodies)
        else:
            mesh = exporters.merge_bodies_to_mesh(bodies)
            if mesh is None:
                print("ERROR: No geometry to export.", file=sys.stderr)
                return False
            if ext == ".obj":
                exporters.write_obj(output_path, mesh)
            else:
                exporters.write_stl(output_path, mesh)
    except OSError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return False
    except ImportError:
        # lib3mf is a conditional dependency (not installed on aarch64/ARM64
        # -- see pyproject.toml), same gap the GUI's own _export() sidesteps
        # by only offering ".3mf" in its save dialog when the import
        # succeeds.
        print("belfryscad: .3mf export is unavailable (lib3mf not installed on this platform)", file=sys.stderr)
        return False
    return True


def render_and_export(source_path: str, output_path: str, defines: list[str] = ()) -> int:
    """Parse + evaluate source_path (with any -D overrides applied) and
    export the result to output_path. Returns a process exit code (0
    success, 1 failure); never raises for an ordinary parse/eval/export
    error, only for a caller mistake in defines (validated up front)."""
    ext = Path(output_path).suffix.lower()
    if ext not in (".stl", ".obj", ".3mf"):
        print(f"belfryscad: unsupported output extension {ext!r} (expected .stl, .obj, or .3mf)", file=sys.stderr)
        return 1

    parse_path, tmp_path = _prepare_source(source_path, list(defines))
    if parse_path is None:
        return 1
    try:
        result = _evaluate(parse_path, {})
    finally:
        _cleanup(tmp_path)
    if result is None:
        return 1
    bodies, elapsed = result

    if not _export(output_path, ext, bodies):
        return 1
    print(f"Exported to {output_path} ({elapsed:.3f}s)")
    return 0


def render_and_export_animation(source_path: str, output_path: str, steps: int,
                                 defines: list[str] = (), animate_dir: str | None = None) -> int:
    """Renders `steps` animation frames ($t = i/steps for i in 0..steps-1,
    same cycle AnimatePane.current_t() uses) and exports each to its own
    numbered file -- {stem}{i:05d}{ext}, 5-digit zero-padded regardless of
    `steps`, in animate_dir if given, else output_path's own directory.
    Matches real OpenSCAD's own --animate/-o file naming exactly (verified
    directly against OpenSCAD.app: `--animate 5 -o out.stl` produces
    out00000.stl .. out00004.stl, same width for --animate 150 too).

    Renders every frame even if an earlier one failed (prints each error,
    keeps going) -- one bad frame in a long batch shouldn't lose the rest.
    Returns 0 only if every frame succeeded.
    """
    if steps < 1:
        print(f"belfryscad: --animate {steps}: must be at least 1", file=sys.stderr)
        return 1

    out = Path(output_path)
    ext = out.suffix.lower()
    if ext not in (".stl", ".obj", ".3mf"):
        print(f"belfryscad: unsupported output extension {ext!r} (expected .stl, .obj, or .3mf)", file=sys.stderr)
        return 1

    dest_dir = Path(animate_dir) if animate_dir else out.parent
    if animate_dir:
        dest_dir.mkdir(parents=True, exist_ok=True)

    parse_path, tmp_path = _prepare_source(source_path, list(defines))
    if parse_path is None:
        return 1

    ok = True
    try:
        for i in range(steps):
            frame_path = dest_dir / f"{out.stem}{i:05d}{ext}"
            result = _evaluate(parse_path, {"$t": i / steps})
            if result is None:
                print(f"belfryscad: frame {i}: render failed", file=sys.stderr)
                ok = False
                continue
            bodies, elapsed = result
            if not _export(str(frame_path), ext, bodies):
                print(f"belfryscad: frame {i}: export failed", file=sys.stderr)
                ok = False
                continue
            print(f"Exported to {frame_path} ({elapsed:.3f}s)")
    finally:
        _cleanup(tmp_path)

    return 0 if ok else 1


def _cleanup(tmp_path: str | None):
    if not tmp_path:
        return
    import os
    try:
        os.unlink(tmp_path)
    except OSError:
        pass
