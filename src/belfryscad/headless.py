"""Headless (no-GUI) rendering and export -- the -o/-D/--animate/-q/
--hardwarnings/--export-format/--summary/--backend CLI path. No Qt
dependency, so this is plain pytest-testable unlike the GUI code (see
tests/ conventions -- real Qt widgets/GL contexts crash pytest here)."""

import sys
import time
from pathlib import Path

from belfryscad import scad_temp

_VALID_EXPORT_FORMATS = {"asciistl", "binstl"}
_VALID_SUMMARY_KEYS = {"time", "geometry", "bounding-box", "area", "camera"}


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
    path = scad_temp.write_temp_scad(
        src.read_text(encoding="utf-8") + "\n" + prelude, near=src)
    return path, path


class _HardWarning(RuntimeError):
    """Raised from inside the evaluator's echo_fn callback when
    --hardwarnings is set and a WARNING: line arrives. nanobind normalizes
    ANY exception raised inside a Python callback into the evaluator's own
    EvalError on the way back out (confirmed directly: the original
    message text survives verbatim, only the exception TYPE changes) --
    so _evaluate's existing `except EvalError` handler already reports
    this correctly with no further handling needed here."""


def _evaluate(parse_path: str, viewport_params: dict, quiet: bool = False, hard_warnings: bool = False):
    """Parse + evaluate parse_path. Returns (bodies, elapsed_seconds) on
    success, or None after printing an error to stderr."""
    from openscad_cpp_evaluator import Evaluator, EvalError, ParseError, parse as _oce_parse, to_renderable_bodies

    t0 = time.perf_counter()
    try:
        _oce_parse(parse_path)
    except ParseError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return None

    def echo_fn(m):
        if hard_warnings and m.startswith("WARNING:"):
            raise _HardWarning(m)
        if not quiet:
            print(m, file=sys.stderr)

    evaluator = Evaluator(echo_fn=echo_fn)
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


def _export(output_path: str, ext: str, bodies, export_format: str | None = None) -> bool:
    """Writes bodies to output_path. Returns True on success, False after
    printing an error to stderr."""
    from belfryscad import exporters

    try:
        if ext in (".3mf", ".obj", ".ply"):
            # Formats that can hold separate objects share one split: the
            # top-level union, cut so no two objects overlap, one per
            # colour and per connected part. See split_bodies_for_export.
            open_parts = []
            objects = exporters.split_bodies_for_export(bodies, open_parts)
            for n in open_parts:
                print(f"WARNING: part {n} is not a closed solid; its surface "
                      f"is written as-is.", file=sys.stderr)
            if not objects:
                print("ERROR: No geometry to export.", file=sys.stderr)
                return False
            if ext == ".3mf":
                exporters.write_3mf(output_path, objects)
            elif ext == ".obj":
                exporters.write_obj(output_path, objects)
            else:
                exporters.write_ply(output_path, objects)
        else:
            open_parts = []
            mesh = exporters.merge_bodies_to_mesh(bodies, open_parts)
            for n in open_parts:
                print(f"WARNING: part {n} is not a closed solid; its surface "
                      f"is written as-is.", file=sys.stderr)
            if mesh is None:
                print("ERROR: No geometry to export.", file=sys.stderr)
                return False
            if ext == ".stl" and export_format == "asciistl":
                exporters.write_stl_ascii(output_path, mesh)
            else:
                exporters.write_stl(output_path, mesh)
    except OSError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return False
    return True


def _validate_export_format(export_format: str | None, ext: str) -> bool:
    if export_format is None:
        return True
    if export_format not in _VALID_EXPORT_FORMATS:
        print(f"belfryscad: --export-format {export_format!r}: expected 'asciistl' or 'binstl'", file=sys.stderr)
        return False
    if ext != ".stl":
        print(f"belfryscad: --export-format only applies to .stl output; ignoring for {ext}", file=sys.stderr)
    return True


def _validate_backend(backend: str | None) -> bool:
    if backend is None or backend.lower() == "manifold":
        return True
    print(f"belfryscad: --backend {backend!r}: BelfrySCAD only supports Manifold (no CGAL backend)", file=sys.stderr)
    return False


def _compute_summary(bodies, elapsed: float, keys: set, camera: dict | None = None) -> dict:
    import numpy as np

    result = {}
    if "time" in keys:
        result["time"] = {"total": round(elapsed, 3)}
    if "camera" in keys and camera is not None:
        result["camera"] = camera
    if "geometry" in keys or "bounding-box" in keys or "area" in keys:
        facets = 0
        vertices = 0
        area = 0.0
        mins, maxs = [], []
        for b in bodies:
            if b.body.is_empty():
                continue
            m = b.body.to_mesh()
            v = np.asarray(m.vert_properties[:, :3])
            tris = np.asarray(m.tri_verts)
            facets += len(tris)
            vertices += len(v)
            if "area" in keys and len(tris):
                a, bb_, c = v[tris[:, 0]], v[tris[:, 1]], v[tris[:, 2]]
                area += float(np.linalg.norm(np.cross(bb_ - a, c - a), axis=1).sum() / 2)
            if len(v):
                mins.append(v.min(axis=0))
                maxs.append(v.max(axis=0))
        if "geometry" in keys:
            result["geometry"] = {"bodies": len(bodies), "facets": facets, "vertices": vertices}
        if "area" in keys:
            result["area"] = round(area, 3)
        if "bounding-box" in keys:
            bb = {"min": None, "max": None}
            if mins:
                bb = {"min": np.min(mins, axis=0).tolist(), "max": np.max(maxs, axis=0).tolist()}
            result["bounding-box"] = bb
    return result


def _parse_summary_keys(summary: str) -> set | None:
    keys = {k.strip() for k in summary.split(",") if k.strip()}
    if "all" in keys:
        return set(_VALID_SUMMARY_KEYS)
    unknown = keys - _VALID_SUMMARY_KEYS
    if unknown:
        print(f"belfryscad: --summary: unsupported key(s) {sorted(unknown)} "
              f"(expected: all, {', '.join(sorted(_VALID_SUMMARY_KEYS))})", file=sys.stderr)
        return None
    return keys


def _emit_summary(bodies, elapsed: float, summary: str, summary_file: str | None,
                   camera: dict | None = None) -> bool:
    keys = _parse_summary_keys(summary)
    if keys is None:
        return False
    data = _compute_summary(bodies, elapsed, keys, camera=camera)
    if summary_file:
        import json
        text = json.dumps(data, indent=2)
        if summary_file == "-":
            print(text)
        else:
            Path(summary_file).write_text(text + "\n", encoding="utf-8")
    else:
        for key in ("time", "camera", "geometry", "area", "bounding-box"):
            if key in data:
                print(f"{key}: {data[key]}")
    return True


def render_and_export(source_path: str, output_path: str, defines: list[str] = (),
                       quiet: bool = False, hard_warnings: bool = False,
                       export_format: str | None = None, backend: str | None = None,
                       summary: str | None = None, summary_file: str | None = None) -> int:
    """Parse + evaluate source_path (with any -D overrides applied) and
    export the result to output_path. Returns a process exit code (0
    success, 1 failure); never raises for an ordinary parse/eval/export
    error, only for a caller mistake in defines (validated up front)."""
    if not _validate_backend(backend):
        return 1

    ext = Path(output_path).suffix.lower()
    if ext not in (".stl", ".obj", ".3mf", ".ply"):
        print(f"belfryscad: unsupported output extension {ext!r} (expected .stl, .obj, .3mf, or .ply)", file=sys.stderr)
        return 1
    if not _validate_export_format(export_format, ext):
        return 1

    parse_path, tmp_path = _prepare_source(source_path, list(defines))
    if parse_path is None:
        return 1
    try:
        result = _evaluate(parse_path, {}, quiet=quiet, hard_warnings=hard_warnings)
    finally:
        _cleanup(tmp_path)
    if result is None:
        return 1
    bodies, elapsed = result

    if not _export(output_path, ext, bodies, export_format=export_format):
        return 1
    if summary is not None and not _emit_summary(bodies, elapsed, summary, summary_file):
        return 1
    if not quiet:
        print(f"Exported to {output_path} ({elapsed:.3f}s)")
    return 0


def render_and_export_animation(source_path: str, output_path: str, steps: int,
                                 defines: list[str] = (), animate_dir: str | None = None,
                                 quiet: bool = False, hard_warnings: bool = False,
                                 export_format: str | None = None, backend: str | None = None) -> int:
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
    if not _validate_backend(backend):
        return 1
    if steps < 1:
        print(f"belfryscad: --animate {steps}: must be at least 1", file=sys.stderr)
        return 1

    out = Path(output_path)
    ext = out.suffix.lower()
    if ext not in (".stl", ".obj", ".3mf", ".ply"):
        print(f"belfryscad: unsupported output extension {ext!r} (expected .stl, .obj, .3mf, or .ply)", file=sys.stderr)
        return 1
    if not _validate_export_format(export_format, ext):
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
            result = _evaluate(parse_path, {"$t": i / steps}, quiet=quiet, hard_warnings=hard_warnings)
            if result is None:
                print(f"belfryscad: frame {i}: render failed", file=sys.stderr)
                ok = False
                continue
            bodies, elapsed = result
            if not _export(str(frame_path), ext, bodies, export_format=export_format):
                print(f"belfryscad: frame {i}: export failed", file=sys.stderr)
                ok = False
                continue
            if not quiet:
                print(f"Exported to {frame_path} ({elapsed:.3f}s)")
    finally:
        _cleanup(tmp_path)

    return 0 if ok else 1


def _cleanup(tmp_path: str | None):
    scad_temp.remove(tmp_path)
