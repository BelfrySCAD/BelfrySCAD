"""Running docsgen Example/Figure/Log scripts inside BelfrySCAD.

openscad_docsgen runs every example by launching the real OpenSCAD binary
once per image, through a temp file and a subprocess. That is the slowest
part of a docs build by a wide margin. BelfrySCAD already owns an
evaluator and an offscreen renderer, so this module runs the same scripts
in-process instead: one parse+evaluate per script, and ONE OpenGL context
and SceneRenderer shared by every image in the whole run.

Everything here is Qt-widget-free (headless_render only needs QtGui), so
it works from the CLI and from the GUI's worker thread alike.
"""
from __future__ import annotations

import glob
import os
import tempfile
from dataclasses import dataclass, field


#: Temp-script prefix. Keeps docsgen's own `tmp_*.scad` IgnoreFiles rule
#: working (BOSL2's rc has one), and carries the owning PID so an
#: abandoned file can be told from one a concurrent run is still using.
_TEMP_PREFIX = f"tmp_docsgen_{os.getpid()}_"

#: Directories already swept this process; sweeping is worth doing once,
#: not once per example.
_swept_dirs = set()


def _pid_alive(pid: int) -> bool:
    """Whether `pid` is still running, portably.

    os.kill(pid, 0) says so on POSIX but not on Windows, where CPython
    implements os.kill through the process-handle API and raises OSError
    both for a PID that is gone and for one it cannot open. Guessing wrong
    in the "gone" direction would delete a live run's script, so Windows
    asks the API directly instead.
    """
    if os.name == "nt":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True          # someone else's process, but it exists
    except OSError:
        return True          # unknown: leave the file alone
    return True


def _sweep_abandoned_temp_files(directory: str):
    """Delete temp scripts left behind by runs that were killed.

    run() unlinks its own file in a `finally`, so the only way one survives
    is the process dying outright -- a SIGKILL, or a crash in the
    evaluator. Nothing can be done about that from inside the run that
    died, so the next one cleans up after it.

    The owning PID is in the filename, and a file is removed only if that
    PID is gone, so two docsgen runs sharing a directory cannot delete each
    other's live scripts.
    """
    if directory in _swept_dirs:
        return
    _swept_dirs.add(directory)
    for path in glob.glob(os.path.join(directory, "tmp_docsgen_*_*.scad")):
        name = os.path.basename(path)
        try:
            pid = int(name[len("tmp_docsgen_"):].split("_", 1)[0])
        except ValueError:
            continue                      # not one of ours after all
        if pid == os.getpid() or _pid_alive(pid):
            continue                      # ours, or still running
        try:
            os.unlink(path)
        except OSError:
            pass


@dataclass
class ScriptResult:
    """One evaluated script. `bodies` is None when evaluation failed."""
    bodies: object = None
    dyn: dict = field(default_factory=dict)
    echos: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    errors: list = field(default_factory=list)

    @property
    def success(self) -> bool:
        return self.bodies is not None and not self.errors


def camera_spec_from_dyn(dyn: dict) -> str | None:
    """A --camera 'translate,rot,dist' spec built from the $vpt/$vpr/$vpd a
    script assigned to itself, or None if it set none of them.

    docsgen's dynamic-viewport examples (Spin, Anim, an explicit VPR=) work
    by prepending `$vpr = ...;` to the script and letting OpenSCAD pick the
    values up. Feeding them back as a camera spec reproduces that, and has
    the side effect of suppressing headless_render's fit-to-bounds, which
    is also what OpenSCAD does once a script positions its own camera.

    $vpf (field of view) is deliberately ignored: docsgen renders every
    image orthographically, where fov has no effect.
    """
    if not any(k in dyn for k in ("$vpt", "$vpr", "$vpd")):
        return None

    def trio(key, default):
        v = dyn.get(key)
        if isinstance(v, (list, tuple)) and len(v) == 3:
            try:
                return [float(x) for x in v]
            except (TypeError, ValueError):
                return default
        return default

    vpt = trio("$vpt", [0.0, 0.0, 0.0])
    vpr = trio("$vpr", [55.0, 0.0, 25.0])
    try:
        vpd = float(dyn.get("$vpd", 444.0))
    except (TypeError, ValueError):
        vpd = 444.0
    return ",".join(str(x) for x in (*vpt, *vpr, vpd))


class ScriptRunner:
    """Evaluates scripts and paints images, reusing one evaluator process,
    one GL context and one renderer across every call.

    The GL side is created lazily on the first render, so a parse-only or
    log-only run never pays for it.
    """

    def __init__(self):
        self._gl = None          # (app, ctx, renderer)
        self._fbos = {}          # (w, h) -> framebuffer
        # Set by the GUI preview, which identifies the file by bare basename
        # (so generated image paths stay inside its cache directory) and so
        # cannot let scripts resolve their includes from that name alone.
        self.src_dir_override = None

    # -- evaluation ----------------------------------------------------

    def run(self, script_lines, src_dir: str, params: dict | None = None,
            hard_warnings: bool = False, preview: bool = True,
            generate: bool = True) -> ScriptResult:
        """Evaluate `script_lines`. The script is written to a temp file in
        `src_dir` so that its own relative `include <...>` paths resolve
        the same way they would for the file being documented -- the same
        reason openscad_docsgen's logmanager writes its temp file there."""
        from openscad_cpp_evaluator import (Evaluator, EvalError,
                                             to_renderable_bodies)

        src_dir = self.src_dir_override or src_dir
        result = ScriptResult()

        def echo_fn(msg):
            if msg.startswith("WARNING:"):
                result.warnings.append(msg)
            elif msg.startswith("ERROR:"):
                result.errors.append(msg)
            elif msg.startswith("ECHO:"):
                result.echos.append(msg[len("ECHO:"):].strip().strip('"'))
            else:
                result.echos.append(msg)

        _sweep_abandoned_temp_files(src_dir)
        fd, path = tempfile.mkstemp(suffix=".scad", prefix=_TEMP_PREFIX, dir=src_dir)
        try:
            with os.fdopen(fd, "w") as f:
                f.write("\n".join(script_lines) + "\n")
            # No oce_parse() pre-flight here. evaluate() parses the file
            # itself, and reports a syntax error as an EvalError whose text is
            # byte-identical to the ParseError a separate parse would raise --
            # so the pre-flight bought nothing and parsed every example twice.
            # It cost ~30% of the per-example time, the single largest item
            # after the library include itself.
            evaluator = Evaluator(echo_fn=echo_fn)
            try:
                # seed_params, not the raw dict: OpenSCAD defines $vpt/$vpr/
                # $vpd/$vpf for every run, and BOSL2 reads them (debug_vnf()
                # orients its labels by $vpr). An example that found them
                # undefined warned, and under docsgen's hard-warnings rule
                # that failed the whole image.
                from belfryscad.export_name import seed_params
                # $preview matches the mode openscad_docsgen runs the
                # reference in -- true for Examples, Figures and Log blocks,
                # false only for an example marked `Render`. BOSL2's
                # ruler() is `if ($preview)` all the way down, so getting
                # this wrong renders nothing at all and says nothing about
                # why. BelfrySCAD has no preview mode of its own; this is
                # about matching what the published docs show.
                seeded = dict(params or {})
                seeded["$preview"] = bool(preview)
                # generate=False stops after the resolve pass. The script
                # still runs, so every echo, warning and error it produces is
                # reported as usual -- only the Manifold work is skipped, and
                # `bodies` comes back empty. That is what --test-only wants,
                # and it matches what the reference does for
                # `openscad -o out.term`.
                bodies, _ids = evaluator.evaluate(path, seed_params(seeded, path),
                                                  generate=generate)
            except RecursionError:
                result.errors.append("ERROR: AST too deeply nested "
                                     "(recursion limit exceeded during evaluation).")
                return result
            except EvalError as e:
                result.errors.append(f"ERROR: {e}")
                return result
            result.dyn = dict(evaluator.dyn)
            result.bodies = to_renderable_bodies(bodies)
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

        # --hardwarnings parity: docsgen fails an example on any warning.
        if hard_warnings and result.warnings:
            result.errors.extend(result.warnings)
        return result

    # -- rendering -----------------------------------------------------

    def render_rgba(self, bodies, opts) -> bytes | None:
        """Paint `bodies` with `opts` (a headless_render._RenderOptions) and
        return top-to-bottom RGBA bytes, or None on failure.

        The framebuffer is cached per size, so a run of many same-sized
        examples allocates exactly one."""
        from belfryscad.headless_render import apply_view_options, make_render_target, paint_rgba

        gl = self._ensure_gl(opts)
        if gl is None:
            return None
        _app, ctx, renderer = gl
        apply_view_options(renderer, opts)
        key = (opts.w, opts.h)
        if key not in self._fbos:
            self._fbos[key] = make_render_target(ctx, opts.w, opts.h)
        draw_fbo, read_fbo = self._fbos[key]
        return paint_rgba(ctx, renderer, draw_fbo, opts, bodies, read_fbo)

    def _ensure_gl(self, opts):
        if self._gl is None:
            from belfryscad.headless_render import _make_offscreen_renderer
            setup = _make_offscreen_renderer(opts)
            if setup is None:
                return None
            app, ctx, renderer, draw_fbo, read_fbo = setup
            self._gl = (app, ctx, renderer)
            self._fbos[(opts.w, opts.h)] = (draw_fbo, read_fbo)
        return self._gl

    def close(self):
        for pair in self._fbos.values():
            for fbo in set(pair):      # the two are one object without MSAA
                try:
                    fbo.release()
                except Exception:
                    pass
        self._fbos.clear()
        self._gl = None


# One runner for a whole docsgen run -- imagemanager and logmanager both
# use it, so a docs build creates a single GL context no matter how many
# images and log blocks it renders.
runner = ScriptRunner()
