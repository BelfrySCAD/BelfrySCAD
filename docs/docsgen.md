# Documentation Generation (`openscad_docsgen`)

BelfrySCAD generates OpenSCAD library documentation from `openscad_docsgen`
comment blocks — the format BOSL2 is written in — and previews the same
output live in the GUI while a library file is being edited.

Three entry points, one implementation:

* **`belfryscad --docsgen [options] [srcfiles...]`** — a drop-in replacement
  for the `openscad-docsgen` command, with the same options.
* **`belfryscad --mdimggen [options] [srcfiles...]`** — a drop-in replacement
  for `openscad-mdimggen`, which renders the ```` ```openscad ```` blocks in
  markdown files to images (BOSL2 builds its tutorials this way).
* **View ▸ Show Docs** — the Docs pane, which renders the current editor
  buffer's documentation, lists everything the parser objects to, and shows
  every Example and Figure as a real rendered image.

## Why it lives here

`openscad_docsgen` runs every Example by launching the OpenSCAD binary on a
temp file — one process per image. On a library the size of BOSL2 that is
thousands of process launches, and it is by a wide margin the slowest part
of a docs build. BelfrySCAD already owns an evaluator and an offscreen
renderer, so it can run the same scripts in-process.

It also unlocks the GUI case, which upstream cannot do at all: previewing
**unsaved** editor text needs a parser callable on a string, not a filename.

## What is vendored and what is not

`src/belfryscad/docsgen/` holds `openscad_docsgen`'s own modules, copied
unchanged:

    parser.py  blocks.py  errorlog.py  filehashes.py  utils.py
    target.py  target_wiki.py  target_githubwiki.py

Keeping them byte-identical is the point: the block syntax, the validation
rules and the generated markdown stay exactly what `openscad-docsgen`
produces, so a preview cannot disagree with a real docs build. Verified by
running both tools over identical trees and diffing: byte-identical
markdown, same errors. See "Measured against the real thing" below.

Only the two modules that shelled out to OpenSCAD are ours. They keep the
upstream module names, class names and method signatures, which is why the
vendored files need **no import edits at all**:

| Module | Upstream | Here |
| --- | --- | --- |
| `imagemanager.py` | `openscad_runner` subprocess per image; scipy/imageio/Pillow to compare | evaluator + offscreen GL; hand-written PNG/APNG writer |
| `logmanager.py` | `openscad -o - --export-format=echo` subprocess | the evaluator's `echo_fn` |

`runner.py` is the shared piece underneath both: it evaluates a script and
paints it, holding **one** OpenGL context and one `SceneRenderer` for the
whole run, with framebuffers cached per image size. Upstream's per-image
process launch is gone entirely.

`preview.py` is the GUI-facing wrapper — parse a buffer, return markdown
plus errors. It is Qt-free, so `window/docs_pane.py` can call it from a
worker thread.

`mdimggen.py` is vendored too, and its `MarkdownImageGen` class is upstream's
unchanged — it already drives `image_manager`/`log_manager`/`errorlog`/
`filehashes`, so replacing those two modules was enough to redirect it as
well. Only its entry point is ours. Its `.openscad_mdimggen_rc` is parsed as
real YAML (PyYAML) rather than hand-scanned: `source_files` is documented as
either a string or a block list, and quietly misreading a valid config is
worse than the dependency. Note that upstream requires the output directory
to already exist and this does too — in practice it is a checked-out wiki
repo.

Two upstream behaviours worth knowing, both kept as-is: on Windows,
`processFiles` globs its filename arguments, so a name matching nothing
becomes an empty list rather than a "does not exist" error and the run
exits 0; and a failure exits **255** (upstream's `sys.exit(-1)`), which
`EXIT_FAILURE` matches deliberately.

## Example and Figure rendering

`ImageRequest` parses the metadata in `// Example(...)` exactly as upstream
does — `Small`/`Med`/`Big`/`Huge`/`Size=WxH`, `2D`/`3D`, `Spin`/`FlatSpin`/
`XSpin`/`YSpin`/`Anim`, `Frames=`/`FPS=`/`FrameMS=`, `VPT=`/`VPR=`/`VPD=`/
`VPF=`, `Edges`, `NoAxes`, `NoScales`, `Perspective`, `ColorScheme=`,
`ScriptUnder`, `NORENDER`.

Two camera cases fall out of that:

* **Fixed view** — the metadata resolves to numbers, so the request carries
  a `--camera` 7-tuple and the script is run once.
* **Script-driven view** — the values are expressions in `$t` (any `Spin`,
  any `Anim`, an explicit `VPD=`). Upstream's trick is used unchanged:
  `$vpt`/`$vpr`/`$vpd`/`$vpf` assignments are **prepended to the script**,
  and the settled values are read back out of `Evaluator.dyn` afterwards
  and turned into a camera spec (`runner.camera_spec_from_dyn`).

### `$vpt`/`$vpr`/`$vpd`/`$vpf` are always defined

OpenSCAD defines the viewport variables for **every** run — including a
plain `-o out.stl` mesh export — and scripts read them: BOSL2's
`debug_vnf()` orients its labels by `$vpr`. Leaving them `undef` made those
examples warn, and the hard-warnings rule then failed the image.

`export_name.DEFAULT_VIEWPORT` carries OpenSCAD's own starting values
(`[0,0,0]`, `[55,0,25]`, `140`, `22.5`), seeded by `seed_params` — the one
function the CLI, the GUI and the debugger all share — with `setdefault`, so
a caller that knows better wins. `headless_render.viewport_params_from_camera`
supplies what `--camera` resolved to, and `imagemanager` supplies the fixed
camera a static Example is rendered with. All three forms were read off
OpenSCAD 2026.02.01:

| `--camera` | `$vpt` | `$vpr` | `$vpd` |
| --- | --- | --- | --- |
| *(none)* | `[0,0,0]` | `[55,0,25]` | `140` |
| `1,2,3,10,20,30,250` | `[1,2,3]` | `[10,20,30]` | `250` |
| `100,100,100,0,0,0` | `[0,0,0]` | `[54.7356,0,135]` | `173.205` |

`$vpd` stays at the default `140` with no camera **even under `--viewall`**:
OpenSCAD reports the camera it was given, not the one it fitted.

### `--viewall` is not disabled by `--camera`

Worth stating because assuming otherwise produces images at completely the
wrong scale. A script that never mentions `$vp` is rendered with
autocenter **and** viewall on top of the camera angle: the camera supplies
the viewing direction, the fit supplies the distance and target. Verified
directly against OpenSCAD 2026.02.01 — passing `--camera` does not turn
`--viewall` off. Only a script that positions its own camera skips the fit.

The same `no_vp` test also gates hard-warnings, matching upstream: an
Example that emits a warning is a documentation bug, unless it is one of
the scripts that legitimately warns about overriding the view.

### Animations are APNG, never GIF

`png_writer.write_apng` adds `acTL`/`fcTL`/`fdAT` chunks to the existing
hand-written encoder. GIF would need an LZW encoder and a colour quantiser
for what is already a 24-bit render, so `Options.png_animation` is
hard-wired to `True` and a `UsePNGAnimations: No` in an rc file is reported
and ignored. Frame 0 is the plain `IDAT`, so a viewer with no APNG support
still shows a still image.

### Deliberate differences from upstream

One of these is a bug fix, and it only bites on Windows:

* **Image URLs are built with `posixpath`, not `os.path`.** Upstream uses
  `os.path.join` for the `rel_url` that goes straight into a markdown image
  link (`blocks.py`'s `image_url_rel`, `mdimggen.py`'s `img_rel_url`), so a
  docs build run on Windows emits `images\demo\widget.png` and every image
  in the output is broken. On POSIX `os.path.join` already produces exactly
  what `posixpath.join` does, so this changes nothing there — verified by
  re-diffing both generators' output against the reference after the change.
  Caught by this project's Windows CI, which upstream does not run.

The rest change nothing a reader sees:

* `ThrownTogether` and `Render` render the same as `preview` — the
  evaluator always does full CSG, so there is no cheaper mode to pick.
* `enabled_features` (OpenSCAD's `--enable=`) is accepted and ignored.
  Everything docsgen would switch on is either standard here or one of this
  evaluator's own documented extensions.
* Unchanged images are detected by exact byte comparison rather than
  upstream's tolerant pixel diff. Upstream needed the tolerance because
  OpenSCAD's output jitters between runs; this renderer plus a fixed zlib
  level is deterministic. The symptom if that ever stops being true is
  churn — every image reported `REPLACE` with no visible change — and the
  fix is a pixel diff in `ImageManager._write_image`.

## The Docs pane

`window/docs_pane.py`. Rendered documentation on top, the parser's
complaints in a table underneath; clicking a row moves the editor cursor to
that line. **Refresh** rebuilds, and **Render examples** can be unchecked
for a fast text-only validation pass.

It rebuilds when the pane becomes visible, when the tab changes, and on
demand — deliberately **not** on text changes, since a rebuild runs every
Example in the file.

All work happens on one dedicated worker thread. That is not only about
responsiveness: docsgen's parser, its error log and the offscreen renderer
are process-wide singletons, so two concurrent previews would corrupt each
other. Requests arriving mid-build are coalesced to the newest.

Three things the preview has to do differently from a CLI run:

* **Images go to a cache**, `~/.cache/BelfrySCAD/docs-preview/<file>-<hash>/`,
  never the project's real docs directory — which is why the rc file's own
  `DocsDirectory:` is overridden after the parser has read it.
* **The file is identified by bare basename**, so generated image paths stay
  inside that cache directory. Scripts still have to resolve their own
  `include <...>` though, so `runner.src_dir_override` carries the real
  folder separately.
* **Sibling `.scad` files are parsed too** (`_parse_siblings`), purely to
  populate the name table. Without it every cross-file `See Also:` reports
  `Invalid Link` — five false errors on one BOSL2 file. It costs about 0.2s
  for all 58 BOSL2 files, since no images or scripts are involved, and it
  only happens inside a real docsgen project (one with an
  `.openscad_docsgen_rc` at or above the file).

### Markdown for Qt

`preview.markdown_for_qt` rewrites the small, closed set of raw HTML the
docsgen targets emit (`<img>`, `<a>`, `<code>`, `<abbr>`, `<sup>`, `<br>`)
into markdown, because Qt's markdown reader drops raw HTML silently — which
would lose every example image.

One trap worth remembering: docsgen escapes underscores for GitHub, so an
alt text reads `ball\_bearing() Example 1`. Qt's parser splits an image
apart at each escape and emits **one copy per fragment** — three
side-by-side copies of every BOSL2 example image. The escapes are stripped
from alt text for that reason.

## Measured against the real thing

Both tools run over identical copies of BOSL2's `.scad` files, with
`openscad-docsgen` pointed at the same OpenSCAD 2026.02.01 build:

| workload | `openscad-docsgen` | `belfryscad --docsgen` | |
| --- | --- | --- | --- |
| `ball_bearings.scad`, 7 images | 17.0 s | 1.8 s | 9.3x |
| 4 files, 393 images | 9 m 50 s | 4 m 16 s | 2.3x |
| all 57 files, 2421 images | not run | 32 m | |

The ratio falls as files get bigger because upstream's per-image process
launch is a fixed cost: it dominates a small file and is amortised on a
large one, where actual CSG work takes over. So the honest figure is
roughly **2x on a realistic library**, much more on small ones.

**The generated markdown is byte-identical.** All four files diffed clean
against `openscad-docsgen`'s output, and both tools reported exactly the
same 33 `Invalid Link` errors (an artefact of parsing four files in
isolation, not a defect in either).

Images match in size, background (`#ffffe5` Cornfield), camera angle and
framing — model coverage 22.0% of frame vs 18.8% on a spot check. They are
not pixel-identical, and are not meant to be: BelfrySCAD's renderer draws
heavier axis ticks with larger, more numerous labels than OpenSCAD's thin
dotted axes.

### Preview vs render: why some warnings are masked

docsgen renders examples in OpenSCAD's **preview** mode. This evaluator has
no preview mode — it always performs full CSG — and that difference, not any
disagreement about geometry, was behind most of the examples that used to
fail here.

Run the same script through OpenSCAD's own `--render=cgal` and it emits the
very same warnings, word for word:

    WARNING: Mixing 2D and 3D objects is not supported
    WARNING: Ignoring 3D child object for 2D operation

On an open mesh it fails *harder* than this does — `ERROR: [manifold] Input
mesh is not closed!` — where this still draws the surface. So these are
preview-vs-render artifacts on correct, deliberate examples: BOSL2's
`vnf_halfspace(..., closed=false)` and `vnf_tri_array()` produce open
surfaces on purpose, and several gear examples overlay a 2D path on a 3D
part. `imagemanager._MASKED_WARNINGS` masks exactly those, using upstream's
own mechanism (it masks `"Viewall and autocenter disabled"` and the Nef
fallback for the same reason). Anything not on that list still fails.

Matching preview also means **lighting backfaces** rather than painting them
the magenta inverted-normal cue — `SceneRenderer.light_backfaces`, on for
headless rendering, off in the GUI where the cue is worth having. On one
open-surface frame that cue covered 6.5% of the image where OpenSCAD showed
the object colour.

Together these take a 4-file BOSL2 build (393 renders) from **18 failures to
1**.

### The one real divergence left

    rect([40,30], rounding=10, atype="perim") show_anchors();

fails with BOSL2's own `Cannot find corner point to anchor` assertion.
OpenSCAD renders it cleanly in both preview and render mode, so this is a
genuine `openscad_cpp_evaluator` bug, not a docsgen one. It is a hard error
rather than a warning, so it is not maskable and should not be: a docs build
over a large library is a useful conformance test for the evaluator.

## Verifying

`tests/test_docsgen.py` covers metadata parsing, APNG chunk structure, the
markdown fix-ups and an end-to-end preview. It never renders: Qt/GL inside
pytest takes the whole run down (see `feedback_gl_qt_tests_crash_pytest`).
The rendering path is checked by running `belfryscad --docsgen` against a
real library and diffing the output against `openscad-docsgen`'s, as above.
