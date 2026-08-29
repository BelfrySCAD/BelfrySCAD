"""Headless PNG rendering -- the -o out.png / --imgsize / --view / --camera
/ --projection / --colorscheme / --autocenter / --viewall / --animate CLI
path.

Unlike belfryscad.headless's mesh export (STL/OBJ/3MF), this genuinely
needs Qt: SceneRenderer (engine/renderer.py) uses QImage/QPainter/QFont to
rasterize axis-tick label textures, so there's no way to render a scene
without at least QtGui available. `QT_QPA_PLATFORM=offscreen` (set here,
before any Qt import, unless the caller already set it) makes that work
without a real display server or window -- confirmed directly, including
end-to-end pixel output, on a machine with no attached display. moderngl's
own `create_context(standalone=True)` provides the GL context; no window,
no QApplication (a QGuiApplication is enough -- QtGui, not QtWidgets).
"""

import math
import os
import sys
from pathlib import Path

_VALID_VIEW_OPTIONS = {"axes", "crosshairs", "edges", "scales", "wireframe"}


def _parse_imgsize(spec: str):
    parts = spec.split(",")
    if len(parts) != 2:
        raise ValueError(f"--imgsize {spec!r}: expected width,height")
    try:
        w, h = int(parts[0]), int(parts[1])
    except ValueError:
        raise ValueError(f"--imgsize {spec!r}: expected two integers") from None
    if w < 1 or h < 1:
        raise ValueError(f"--imgsize {spec!r}: width and height must be positive")
    return w, h


def _parse_view(spec: str) -> set:
    keys = {k.strip() for k in spec.split(",") if k.strip()}
    unknown = keys - _VALID_VIEW_OPTIONS
    if unknown:
        raise ValueError(f"--view: unsupported option(s) {sorted(unknown)} "
                          f"(expected: {', '.join(sorted(_VALID_VIEW_OPTIONS))})")
    return keys


def _parse_projection(spec: str) -> bool:
    """Returns True for orthographic, False for perspective. Real OpenSCAD
    accepts a prefix of either word ('o'/'ortho'/'p'/'perspective')."""
    s = spec.lower()
    if "orthographic".startswith(s) and s:
        return True
    if "perspective".startswith(s) and s:
        return False
    raise ValueError(f"--projection {spec!r}: expected 'o'/'ortho'/'orthographic' or 'p'/'perspective'")


def _apply_camera_translate_rot_dist(camera, tx, ty, tz, rx, ry, rz, dist):
    """translate_x,y,z,rot_x,y,z,dist form -- identical math to
    MainWindow._apply_vp_params's own $vpt/$vpr/$vpd handling (these are
    literally the same values OpenSCAD's own $vpt/$vpr/$vpd special
    variables carry), duplicated here rather than imported since
    main_window.py pulls in the full Qt widget stack at module level."""
    import numpy as np
    camera.target = np.array([tx, ty, tz], dtype=np.float32)
    camera.elevation = (90.0 - rx) % 360.0
    camera.roll = ry % 360.0
    camera.azimuth = (rz + 270.0) % 360.0
    camera.distance = max(0.1, dist)


def _apply_camera_eye_center(camera, ex, ey, ez, cx, cy, cz):
    """eye_x,y,z,center_x,y,z form -- inverts Camera.eye_position()'s own
    eye = target + distance*[cos(el)cos(az), cos(el)sin(az), sin(el)]."""
    import numpy as np
    eye = np.array([ex, ey, ez], dtype=np.float64)
    center = np.array([cx, cy, cz], dtype=np.float64)
    d = eye - center
    distance = float(np.linalg.norm(d))
    if distance < 1e-9:
        raise ValueError("--camera: eye and center must not coincide")
    camera.target = center.astype(np.float32)
    camera.elevation = math.degrees(math.asin(max(-1.0, min(1.0, d[2] / distance))))
    camera.azimuth = math.degrees(math.atan2(d[1], d[0]))
    camera.distance = distance


def _apply_camera(camera, spec: str):
    try:
        parts = [float(x) for x in spec.split(",")]
    except ValueError:
        raise ValueError(f"--camera {spec!r}: expected comma-separated numbers") from None
    if len(parts) == 7:
        _apply_camera_translate_rot_dist(camera, *parts)
    elif len(parts) == 6:
        _apply_camera_eye_center(camera, *parts)
    else:
        raise ValueError(f"--camera {spec!r}: expected 7 values (translate,rot,dist) "
                          f"or 6 values (eye,center), got {len(parts)}")


def viewport_params_from_camera(spec: str | None) -> dict:
    """The `$vpt`/`$vpr`/`$vpd` a --camera spec makes visible to the script.

    OpenSCAD defines these from the camera it was given, so a script can
    read them; with no --camera at all it uses its own defaults, which
    export_name.DEFAULT_VIEWPORT carries (hence {} here for that case).

    Both spec forms were checked against OpenSCAD 2026.02.01:

        --camera=1,2,3,10,20,30,250      -> vpt=[1,2,3] vpr=[10,20,30] vpd=250
        --camera=100,100,100,0,0,0       -> vpt=[0,0,0] vpr=[54.7356,0,135]
                                            vpd=173.205

    The eye/center form is just _apply_camera_eye_center's own elevation/
    azimuth math run backwards through _apply_camera_translate_rot_dist's
    rules, so the two stay consistent by construction rather than by a
    second hand-derived formula.
    """
    if not spec:
        return {}
    try:
        parts = [float(x) for x in spec.split(",")]
    except ValueError:
        return {}
    if len(parts) == 7:
        tx, ty, tz, rx, ry, rz, dist = parts
        return {"$vpt": [tx, ty, tz], "$vpr": [rx, ry, rz], "$vpd": dist}
    if len(parts) == 6:
        import numpy as np
        eye = np.array(parts[:3], dtype=np.float64)
        center = np.array(parts[3:], dtype=np.float64)
        d = eye - center
        distance = float(np.linalg.norm(d))
        if distance < 1e-9:
            return {}
        elevation = math.degrees(math.asin(max(-1.0, min(1.0, d[2] / distance))))
        azimuth = math.degrees(math.atan2(d[1], d[0]))
        return {"$vpt": [float(x) for x in center],
                "$vpr": [(90.0 - elevation) % 360.0, 0.0, (azimuth - 270.0) % 360.0],
                "$vpd": distance}
    return {}


def _bounds(bodies):
    import numpy as np
    mins, maxs = [], []
    for b in bodies:
        if b.body.is_empty():
            continue
        v = np.asarray(b.body.to_mesh().vert_properties[:, :3])
        if len(v):
            mins.append(v.min(axis=0))
            maxs.append(v.max(axis=0))
    if not mins:
        return None
    return np.min(mins, axis=0).astype(np.float32), np.max(maxs, axis=0).astype(np.float32)


class _RenderOptions:
    """Parsed, validated --imgsize/--camera/--autocenter/--viewall/
    --projection/--view/--colorscheme, shared by render_png and
    render_png_animation so option parsing/validation happens once
    regardless of frame count."""

    def __init__(self, imgsize, camera, autocenter, viewall, projection, view, colorscheme):
        self.w, self.h = _parse_imgsize(imgsize)
        self.ortho = _parse_projection(projection) if projection is not None else False
        self.view_opts = _parse_view(view) if view is not None else set()
        self.camera_spec = camera
        self.autocenter = autocenter
        self.viewall = viewall
        self.theme = None
        if colorscheme is not None:
            from belfryscad.window.color_themes import all_themes
            themes = all_themes()
            self.theme = themes.get(colorscheme)
            if self.theme is None:
                raise ValueError(f"--colorscheme {colorscheme!r}: unknown theme "
                                  f"(available: {', '.join(sorted(themes))})")

    @classmethod
    def parse(cls, **kwargs):
        """Returns an instance, or None after printing a belfryscad: error."""
        try:
            return cls(**kwargs)
        except ValueError as e:
            print(f"belfryscad: {e}", file=sys.stderr)
            return None


def _make_offscreen_renderer(opts: _RenderOptions):
    """Creates the (app, ctx, renderer, fbo) needed to paint frames -- kept
    alive by the caller for as long as more frames will be rendered (an
    animation reuses all four across every frame; a single render just
    uses them once). Returns None after printing an error."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtGui import QGuiApplication
    import moderngl
    from belfryscad.engine.renderer import SceneRenderer

    app = QGuiApplication.instance() or QGuiApplication([sys.argv[0]])
    try:
        ctx = moderngl.create_context(standalone=True, require=330)
    except Exception as first_error:
        # moderngl's default standalone backend is GLX on Linux, which needs
        # a real X server -- unavailable on a headless CI runner. Retry with
        # EGL (surfaceless, no X11 needed), confirmed on GitHub Actions'
        # ubuntu-latest with libegl1 installed.
        try:
            ctx = moderngl.create_context(standalone=True, require=330, backend="egl")
        except Exception:
            print(f"belfryscad: could not create an offscreen OpenGL context: {first_error}", file=sys.stderr)
            return None

    renderer = SceneRenderer()
    renderer.initialize(ctx)
    apply_view_options(renderer, opts)
    return app, ctx, renderer, make_fbo(ctx, opts.w, opts.h)


def apply_view_options(renderer, opts: _RenderOptions):
    """Push one _RenderOptions' size/projection/view-flags/theme onto an
    existing SceneRenderer. Split out of _make_offscreen_renderer so a
    caller rendering many differently-configured images (docsgen examples,
    each with its own size, edges/axes flags and colour scheme) can reuse a
    single GL context and renderer instead of rebuilding both per image."""
    renderer.set_viewport(opts.w, opts.h)
    renderer.camera.orthographic = opts.ortho
    renderer.show_axes = "axes" in opts.view_opts
    renderer.show_crosshairs = "crosshairs" in opts.view_opts
    renderer.show_edges = "edges" in opts.view_opts
    renderer.show_scale_markers = "scales" in opts.view_opts
    if opts.theme:
        renderer.bg_color = opts.theme["background"]
        renderer._default_color = opts.theme["object"]
        renderer.axes_color = opts.theme["axes"]
        renderer.unselected_vertex_color = opts.theme["unselected_vertex"]


def make_fbo(ctx, w: int, h: int):
    return ctx.framebuffer(
        color_attachments=[ctx.texture((w, h), 4)],
        depth_attachment=ctx.depth_renderbuffer((w, h)),
    )


def _paint_frame(ctx, renderer, fbo, opts: _RenderOptions, bodies, output_path: str) -> bool:
    """Loads bodies, positions the camera, paints, and writes output_path.
    Returns True on success.

    Camera positioning re-runs on EVERY call, including every frame of an
    animation -- confirmed directly against real OpenSCAD.app: `--animate
    N --viewall` re-fits the camera to each frame's own bounding box, not
    just the first (a model that orbits through space, e.g. `rotate([0,0,
    $t*360]) translate([5,0,0]) cube(2);`, would otherwise drift out of a
    camera framed once from frame 0 and never updated -- caught exactly
    that way during development, by rendering a real animation and finding
    the middle frames blank). An explicit --camera is harmless to
    re-apply every frame too (same fixed values each time)."""
    from belfryscad.png_writer import write_png

    rgba = paint_rgba(ctx, renderer, fbo, opts, bodies)
    if rgba is None:
        return False
    try:
        write_png(output_path, rgba, opts.w, opts.h)
    except OSError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return False
    return True


def paint_rgba(ctx, renderer, fbo, opts: _RenderOptions, bodies) -> bytes | None:
    """The camera + paint + framebuffer-readback half of _paint_frame,
    returning top-to-bottom RGBA bytes instead of writing a file. Split out
    so an animated PNG can collect every frame's pixels in memory (see
    png_writer.write_apng) without each frame going through a file."""
    import numpy as np

    renderer.load_geometry(bodies)

    if opts.camera_spec:
        try:
            _apply_camera(renderer.camera, opts.camera_spec)
        except ValueError as e:
            print(f"belfryscad: {e}", file=sys.stderr)
            return None
    if opts.autocenter or opts.viewall or not opts.camera_spec:
        bounds = _bounds(bodies)
        if bounds:
            bb_min, bb_max = bounds
            if opts.viewall or not opts.camera_spec:
                renderer.camera.frame_bounds(bb_min, bb_max)
            elif opts.autocenter:
                renderer.camera.target = ((bb_min + bb_max) / 2).astype(np.float32)

    ctx.viewport = (0, 0, opts.w, opts.h)
    ctx.wireframe = "wireframe" in opts.view_opts
    try:
        renderer.paint(fbo=fbo)
    finally:
        ctx.wireframe = False

    data = fbo.read(components=4, alignment=1)
    arr = np.frombuffer(data, dtype=np.uint8).reshape(opts.h, opts.w, 4)
    return arr[::-1, :, :].tobytes()  # GL reads bottom-to-top; PNG wants top-to-bottom


def render_png(source_path: str, output_path: str, imgsize: str = "1024,768",
                camera: str | None = None, autocenter: bool = False, viewall: bool = False,
                projection: str | None = None, view: str | None = None, colorscheme: str | None = None,
                defines: list[str] = (), quiet: bool = False, hard_warnings: bool = False,
                backend: str | None = None, summary: str | None = None,
                summary_file: str | None = None) -> int:
    """Parse + evaluate source_path and render a PNG screenshot to
    output_path. Returns a process exit code (0 success, 1 failure)."""
    from belfryscad.headless import _evaluate, _prepare_source, _cleanup, _validate_backend, _emit_summary

    if not _validate_backend(backend):
        return 1
    opts = _RenderOptions.parse(imgsize=imgsize, camera=camera, autocenter=autocenter, viewall=viewall,
                                 projection=projection, view=view, colorscheme=colorscheme)
    if opts is None:
        return 1

    parse_path, tmp_path = _prepare_source(source_path, list(defines))
    if parse_path is None:
        return 1
    try:
        result = _evaluate(parse_path, viewport_params_from_camera(opts.camera_spec),
                            quiet=quiet, hard_warnings=hard_warnings)
    finally:
        _cleanup(tmp_path)
    if result is None:
        return 1
    bodies, elapsed, _geometry = result

    setup = _make_offscreen_renderer(opts)
    if setup is None:
        return 1
    _app, ctx, renderer, fbo = setup
    if not _paint_frame(ctx, renderer, fbo, opts, bodies, output_path):
        return 1

    if summary is not None:
        cam = renderer.camera
        cam_info = {"translate": [float(x) for x in cam.target], "distance": float(cam.distance),
                    "azimuth": float(cam.azimuth), "elevation": float(cam.elevation)}
        if not _emit_summary(bodies, elapsed, summary, summary_file, camera=cam_info):
            return 1

    if not quiet:
        print(f"Exported to {output_path}")
    return 0


def render_png_animation(source_path: str, output_path: str, steps: int, imgsize: str = "1024,768",
                          camera: str | None = None, autocenter: bool = False, viewall: bool = False,
                          projection: str | None = None, view: str | None = None, colorscheme: str | None = None,
                          defines: list[str] = (), animate_dir: str | None = None,
                          quiet: bool = False, hard_warnings: bool = False, backend: str | None = None) -> int:
    """PNG counterpart to belfryscad.headless.render_and_export_animation --
    same $t = i/steps cycle and {stem}{i:05d}.png frame naming. The camera
    is re-fit (or an explicit --camera re-applied) on every frame -- see
    _paint_frame's own doc comment for why."""
    from belfryscad.headless import _evaluate, _prepare_source, _cleanup, _validate_backend

    if not _validate_backend(backend):
        return 1
    if steps < 1:
        print(f"belfryscad: --animate {steps}: must be at least 1", file=sys.stderr)
        return 1
    opts = _RenderOptions.parse(imgsize=imgsize, camera=camera, autocenter=autocenter, viewall=viewall,
                                 projection=projection, view=view, colorscheme=colorscheme)
    if opts is None:
        return 1

    out = Path(output_path)
    dest_dir = Path(animate_dir) if animate_dir else out.parent
    if animate_dir:
        dest_dir.mkdir(parents=True, exist_ok=True)

    parse_path, tmp_path = _prepare_source(source_path, list(defines))
    if parse_path is None:
        return 1

    setup = None
    ok = True
    try:
        for i in range(steps):
            frame_path = dest_dir / f"{out.stem}{i:05d}.png"
            frame_params = dict(viewport_params_from_camera(opts.camera_spec))
            frame_params["$t"] = i / steps
            result = _evaluate(parse_path, frame_params, quiet=quiet, hard_warnings=hard_warnings)
            if result is None:
                print(f"belfryscad: frame {i}: render failed", file=sys.stderr)
                ok = False
                continue
            bodies, _elapsed, _geometry = result

            if setup is None:
                setup = _make_offscreen_renderer(opts)
                if setup is None:
                    return 1
            _app, ctx, renderer, fbo = setup
            if not _paint_frame(ctx, renderer, fbo, opts, bodies, str(frame_path)):
                print(f"belfryscad: frame {i}: export failed", file=sys.stderr)
                ok = False
                continue
            if not quiet:
                print(f"Exported to {frame_path}")
    finally:
        _cleanup(tmp_path)

    return 0 if ok else 1
