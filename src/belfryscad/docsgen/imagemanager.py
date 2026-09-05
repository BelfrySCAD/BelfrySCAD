"""Example/Figure image generation -- BelfrySCAD's replacement for
openscad_docsgen's imagemanager.

Upstream wrote each example to a temp .scad file and launched the OpenSCAD
binary on it, one process per image, then compared the result with
scipy/imageio/Pillow. Here the same scripts run through BelfrySCAD's own
evaluator and offscreen renderer, sharing a single GL context across the
whole run (see runner.py), and animations are written as APNG by the
project's own png_writer instead of GIF.

Class and method names match upstream so the vendored parser.py and
blocks.py import this unchanged.

Two deliberate differences from upstream, both invisible in the output:

  * `ThrownTogether` and `Render` render the same as `preview` -- the
    evaluator always performs full CSG, so there is no cheaper preview mode
    to select between.
  * `enabled_features` (OpenSCAD's `--enable=`) is ignored. Every feature
    docsgen would enable is either standard in this evaluator or one of its
    own documented extensions, so there is nothing to switch on.
"""
from __future__ import annotations

import filecmp
import os
import os.path
import re
from .errorlog import errorlog, ErrorLog
from .runner import runner, camera_spec_from_dyn

# Upstream's OpenSCAD default view, reproduced here so a docs build looks
# the same whichever tool made it.
_DEFAULT_VPT = [0, 0, 0]
_DEFAULT_VPR = [55, 0, 25]
_DEFAULT_VPD = 444
_DEFAULT_VPF = 22.5
_DEFAULT_FRAMES = 36

#: Warnings that must not fail a docs image, because OpenSCAD does not emit
#: them for the render docsgen actually asks for.
#:
#: docsgen renders examples in PREVIEW mode. This evaluator has no preview
#: mode -- it always performs full CSG -- and OpenSCAD emits these exact
#: warnings in its own `--render` mode too, verified word-for-word against
#: 2026.02.01. So they are a preview-vs-render artifact, not a defect in the
#: example being documented, and failing on them would reject examples that
#: are correct and deliberate: BOSL2's `vnf_halfspace(..., closed=false)`
#: and `vnf_tri_array()` produce open surfaces on purpose, and several gear
#: examples overlay a 2D path on a 3D part.
#:
#: This is upstream's own mechanism -- its imagemanager masks "Viewall and
#: autocenter disabled" and "failed with error, falling back to Nef
#: operation" for exactly the same reason. Anything NOT listed here still
#: fails the build.
_MASKED_WARNINGS = (
    "mesh is not closed",
    "Mixing 2D and 3D objects is not supported",
    "Ignoring 2D child object for 3D operation",
    "Ignoring 3D child object for 2D operation",
)


def _unmasked(warnings):
    return [w for w in warnings if not any(m in w for m in _MASKED_WARNINGS)]


class ImageRequest:
    _size_re = re.compile(r'Size *= *([0-9]+) *x *([0-9]+)')
    _frames_re = re.compile(r'Frames *= *([0-9]+)')
    _framems_re = re.compile(r'FrameMS *= *([0-9]+)')
    _fps_re = re.compile(r'FPS *= *([0-9.]+)')
    _vpt_re = re.compile(r'VPT *= *\[([^]]+)\]')
    _vpr_re = re.compile(r'VPR *= *\[([^]]+)\]')
    _vpd_re = re.compile(r'VPD *= *([a-zA-Z0-9_()+*/$.-]+)')
    _vpf_re = re.compile(r'VPF *= *([a-zA-Z0-9_()+*/$.-]+)')
    _color_scheme_re = re.compile(r'ColorScheme *= *([a-zA-Z0-9_ ]+)')

    def __init__(self, src_file, src_line, image_file, script_lines, image_meta,
                 starting_cb=None, completion_cb=None, verbose=False,
                 enabled_features=(), default_colorscheme="Cornfield"):
        self.src_file = src_file
        self.src_line = src_line
        self.image_file = image_file
        self.image_meta = image_meta
        self.enabled_features = list(enabled_features)
        # A leading "--" marks a line that runs but is not shown in the
        # docs; strip the marker before running it.
        self.script_lines = [
            line[2:] if line.startswith("--") else line
            for line in script_lines
        ]
        self.completion_cb = completion_cb
        self.starting_cb = starting_cb
        self.verbose = verbose

        self.imgsize = (320, 240)
        self.camera = None
        self.animation_frames = None
        self.frame_ms = 250
        # openscad_docsgen runs the reference with `--preview ""` by
        # default and `--preview throwntogether` for ThrownTogether -- both
        # set $preview -- and only `--render ""` for Render, which clears
        # it. Verified against OpenSCAD 2026.02.01 for all three.
        self.preview = "Render" not in image_meta
        self.show_edges = "Edges" in image_meta
        self.show_axes = "NoAxes" not in image_meta
        self.show_scales = "NoScales" not in image_meta
        self.orthographic = "Perspective" not in image_meta
        self.color_scheme = default_colorscheme

        m = self._size_re.search(image_meta)
        scale = 1.0
        if m:
            self.imgsize = (int(m.group(1)), int(m.group(2)))
        elif "Small" in image_meta:
            scale = 0.75
        elif "Med" in image_meta:
            scale = 1.5
        elif "Big" in image_meta:
            scale = 2.0
        elif "Huge" in image_meta:
            scale = 2.5
        self.imgsize = tuple(int(round(scale * x)) for x in self.imgsize)

        vpt, vpr = list(_DEFAULT_VPT), list(_DEFAULT_VPR)
        vpd, vpf = _DEFAULT_VPD, _DEFAULT_VPF
        dynamic_vp = False

        if "2D" in image_meta:
            vpr = [0, 0, 0]
        if "FlatSpin" in image_meta:
            vpr = [55, 0, "360*$t"]
            dynamic_vp = True
        elif "Spin" in image_meta:
            vpr = ["90-45*cos(360*$t)", 0, "360*$t"]
            dynamic_vp = True
        elif "XSpin" in image_meta:
            vpr = ["360*$t", 0, 25]
            dynamic_vp = True
        elif "YSpin" in image_meta:
            vpr = [55, "360*$t", 25]
            dynamic_vp = True
        if "Anim" in image_meta:
            dynamic_vp = True

        match = self._vpr_re.search(image_meta)
        if match:
            vpr, dyn = self._parse_vp_line(match.group(1), vpr, dynamic_vp)
            dynamic_vp = dynamic_vp or dyn
        match = self._vpt_re.search(image_meta)
        if match:
            vpt, dyn = self._parse_vp_line(match.group(1), vpt, dynamic_vp)
            dynamic_vp = dynamic_vp or dyn
        match = self._vpd_re.search(image_meta)
        if match:
            vpd = float(match.group(1))
            dynamic_vp = True
        match = self._vpf_re.search(image_meta)
        if match:
            vpf = float(match.group(1))
            dynamic_vp = True

        if dynamic_vp:
            # The viewport values are expressions in $t, so they can only be
            # resolved by running the script. Prepend them and read the
            # settled $vp* back out of the evaluator afterwards.
            self.camera = None
            self.script_lines[0:0] = [
                "$vpt = [{}, {}, {}];".format(*vpt),
                "$vpr = [{}, {}, {}];".format(*vpr),
                "$vpd = {};".format(vpd),
                "$vpf = {};".format(vpf),
            ]
        else:
            self.camera = [*vpt, *vpr, vpd]

        match = self._fps_re.search(image_meta)
        if match:
            self.frame_ms = int(1000 / float(match.group(1)))
        match = self._framems_re.search(image_meta)
        if match:
            self.frame_ms = int(match.group(1))

        if "Spin" in image_meta or "Anim" in image_meta:
            self.animation_frames = _DEFAULT_FRAMES
        match = self._frames_re.search(image_meta)
        if match:
            self.animation_frames = int(match.group(1))

        match = self._color_scheme_re.search(image_meta)
        if match:
            self.color_scheme = match.group(1).strip()

        # A long script is laid out under its image instead of beside it.
        # The 880px/9px-per-character budget is upstream's.
        longest = max((len(line) for line in self.script_lines), default=0)
        self.script_under = (longest > (880 - self.imgsize[0]) / 9
                             or "ScriptUnder" in image_meta)

        self.complete = False
        self.status = "INCOMPLETE"
        self.success = False
        # Upstream filled this with the OpenSCAD command line; there is no
        # subprocess now, but blocks.py prints it on failure.
        self.cmdline = ["belfryscad", "(in-process evaluator)"]
        self.return_code = None
        self.stdout = []
        self.stderr = []
        self.echos = []
        self.warnings = []
        self.errors = []

    @staticmethod
    def _parse_vp_line(line, old_trio, dynamic):
        comps = line.split(",")
        trio = []
        if len(comps) == 3:
            for comp in comps:
                comp = comp.strip()
                try:
                    trio.append(float(comp))
                except ValueError:
                    trio.append(comp)
                    dynamic = True
        return (trio if trio else old_trio), dynamic

    def starting(self):
        if self.starting_cb:
            self.starting_cb(self)

    def completed(self, status, result=None):
        self.complete = True
        self.status = status
        self.success = status not in ("FAIL",)
        self.return_code = 0 if self.success else -1
        if result is not None:
            self.echos = result.echos
            self.warnings = result.warnings
            self.errors = result.errors
            self.stdout = list(result.echos)
            self.stderr = result.warnings + result.errors
        if self.completion_cb:
            self.completion_cb(self)


class ImageManager:
    def __init__(self):
        self.requests = []
        self.test_only = False

    def purge_requests(self):
        self.requests = []

    def new_request(self, src_file, src_line, image_file, script_lines, image_meta,
                    starting_cb=None, completion_cb=None, verbose=False,
                    enabled_features=(), default_colorscheme="Cornfield"):
        if "NORENDER" in image_meta:
            raise Exception("Cannot render scripts marked NORENDER")
        req = ImageRequest(src_file, src_line, image_file, script_lines, image_meta,
                           starting_cb, completion_cb, verbose=verbose,
                           enabled_features=enabled_features,
                           default_colorscheme=default_colorscheme)
        self.requests.append(req)
        return req

    def process_requests(self, test_only=False, only=None, progress=None):
        """Render the queued requests. `only` is an optional collection of
        image paths (matched against the tail of each request's image_file);
        anything not listed is dropped unrendered.

        That is what lets the GUI show a document immediately with a
        placeholder per example and render them one click at a time --
        rendering every Example in a big BOSL2 file up front costs minutes.

        `progress` is called as progress(done, total, frame, frames) before
        the first render and after each one, and again for each frame of an
        animated request while it renders (`frame`/`frames` are 0 outside
        one). The selection is resolved up front so that `total` is the real
        count of work, not the queue length.

        The per-frame calls matter because one animated Example is a single
        unit of `total` but 36 renders of work: without them a Spin example
        looks frozen at "1 of 1" for its whole duration.
        """
        self.test_only = test_only
        selected = [
            req for req in self.requests
            if only is None or any(str(req.image_file).endswith(str(o)) for o in only)
        ]
        total = len(selected)
        if progress:
            progress(0, total, 0, 0)
        for done, req in enumerate(selected, 1):
            frame_cb = None
            if progress and (req.animation_frames or 0) > 1:
                # `done - 1` images are finished while this one renders.
                frame_cb = (lambda n, t, finished=done - 1:
                            progress(finished, total, n, t))
            self.process_request(req, frame_progress=frame_cb)
            if progress:
                progress(done, total, 0, 0)
        self.requests = []

    def process_request(self, req, frame_progress=None):
        req.starting()
        src_dir = os.path.dirname(os.path.abspath(req.src_file)) or "."
        frames = req.animation_frames or 1

        # Upstream's rule, and it matters: a script that never mentions $vp
        # gets fitted to its own bounds, with the camera supplying only the
        # viewing angle. Verified against real OpenSCAD -- passing --camera
        # does NOT disable --viewall, so an unfitted render comes out at a
        # completely different scale from every published docs image.
        no_vp = not any("$vp" in line for line in req.script_lines)

        rgba_frames = []
        last = None
        for i in range(frames):
            if frame_progress:
                frame_progress(i + 1, frames)
            params = {"$t": i / frames} if req.animation_frames else {}
            # A fixed-view example must see the camera it is rendered with,
            # exactly as OpenSCAD's own --camera makes it visible. A
            # script-driven view needs nothing here: those requests carry
            # their own $vp* assignments prepended to the script.
            if req.camera:
                params["$vpt"] = [float(x) for x in req.camera[0:3]]
                params["$vpr"] = [float(x) for x in req.camera[3:6]]
                params["$vpd"] = float(req.camera[6])
            # Warnings are promoted to failures HERE, not inside run(), so
            # the mask can be applied first. Upstream's rule otherwise: an
            # example that warns is a documentation bug, and only a script
            # steering its own camera is exempt (those legitimately warn
            # about the override).
            last = runner.run(req.script_lines, src_dir, params, preview=req.preview,
                              hard_warnings=False, generate=not self.test_only)
            if no_vp:
                last.errors.extend(_unmasked(last.warnings))
            if last.errors or last.bodies is None:
                req.completed("FAIL", last)
                return
            if self.test_only:
                continue
            opts = self._render_options(req, last.dyn, no_vp)
            if opts is None:
                req.completed("FAIL", last)
                return
            rgba = runner.render_rgba(last.bodies, opts)
            if rgba is None:
                last.errors.append("ERROR: offscreen rendering failed")
                req.completed("FAIL", last)
                return
            rgba_frames.append(rgba)

        if self.test_only:
            req.completed("SKIP", last)
            return

        w, h = req.imgsize
        req.completed(self._write_image(req.image_file, rgba_frames, w, h, req.frame_ms), last)

    # -- helpers -------------------------------------------------------

    def _render_options(self, req, dyn, no_vp):
        """A headless_render._RenderOptions for one request, or None (after
        logging) if the requested colour scheme is unknown."""
        from belfryscad.headless_render import _RenderOptions

        view = []
        if req.show_axes:
            view.append("axes")
        if req.show_scales:
            view.append("scales")
        if req.show_edges:
            view.append("edges")

        # An explicit camera wins; otherwise take whatever the script's own
        # $vp* assignments settled on. autocenter/viewall then re-fit the
        # distance and target on top of that viewing angle, exactly as
        # OpenSCAD does, and only for a script that sets no $vp of its own.
        camera = (",".join(str(x) for x in req.camera) if req.camera
                  else camera_spec_from_dyn(dyn))
        try:
            return _RenderOptions(
                imgsize="{},{}".format(*req.imgsize),
                camera=camera, autocenter=no_vp, viewall=no_vp,
                projection="o" if req.orthographic else "p",
                view=",".join(view), colorscheme=req.color_scheme)
        except ValueError as e:
            errorlog.add_entry(req.src_file, req.src_line, str(e), ErrorLog.FAIL)
            return None

    @staticmethod
    def _write_image(image_file, rgba_frames, w, h, frame_ms) -> str:
        """Writes the image beside its destination, then keeps it only if it
        differs from what is already there -- so an unchanged docs rebuild
        leaves file timestamps (and any git status) alone. Returns the
        upstream status string: NEW, REPLACE or SKIP."""
        from belfryscad.png_writer import write_apng, write_png

        os.makedirs(os.path.dirname(image_file) or ".", exist_ok=True)
        tmp = image_file + ".tmp"
        if len(rgba_frames) > 1:
            write_apng(tmp, rgba_frames, w, h, delay_ms=frame_ms)
        else:
            write_png(tmp, rgba_frames[0], w, h)

        if not os.path.isfile(image_file):
            os.replace(tmp, image_file)
            return "NEW"
        # ponytail: exact byte compare, not upstream's tolerant pixel diff.
        # Upstream needed the tolerance because OpenSCAD's output jitters
        # between runs; this renderer plus a deterministic zlib level gives
        # identical bytes for identical input. If driver differences ever
        # make that untrue, the symptom is churn (every image reported
        # REPLACE with no visible change), and the fix is a pixel diff here.
        if filecmp.cmp(image_file, tmp, shallow=False):
            os.unlink(tmp)
            return "SKIP"
        os.replace(tmp, image_file)
        return "REPLACE"


image_manager = ImageManager()
