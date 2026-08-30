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
demand — deliberately **not** on text changes.

**Images render on demand, not up front.** A plain refresh renders none of
them: the document appears immediately with a click-to-render placeholder in
place of each Example, and the status line says how many are outstanding.
Clicking one renders just that image; the status line's **render all N**
link does the lot. That is a link rather than a button because it belongs
with the sentence reporting how many are outstanding, and it has nothing to
offer once they are all rendered — the whole clause disappears then, where a
button would sit there greyed out.
Rendering every Example in a large BOSL2 file is minutes of work, so making
that the default made the pane feel broken.

Rendering an image rebuilds the whole document, which would snap the reader
back to the top, so the pane re-anchors afterwards. It records the first
block on screen and its offset, then puts that block back where it was --
anchoring on the BLOCK rather than the scrollbar value is what makes this
work, since swapping a one-line placeholder for a 240px image changes every
pixel offset below it but not the block numbering (`![alt](x)` and
`[Render x](...)` are each a single paragraph). A plain refresh for a new
file deliberately does not re-anchor and starts at the top.

State is simply which files exist in the cache directory, so renders
accumulate across refreshes and across sessions — there is no separate
bookkeeping to get out of step. `placeholder_markdown` decides per image:
already on disk, leave it as an image; renderable but absent, swap in a
`bfsrender:` link the pane intercepts in `anchorClicked`.

`_style_placeholders` boxes each one, finding them by link target rather
than by the visible wording. The box is a background tint and padding, not a
border: Qt gives blocks no border (only frames and table cells have one),
and wrapping each placeholder in a one-cell table would change the
document's block structure, which the scroll anchoring depends on staying
put. Its tint is deliberately stronger than a code block's — one is a
control, the other is content.

**Remote images become links.** BOSL2's `isosurface.scad` embeds animated
GIFs straight from `raw.githubusercontent.com`. QTextBrowser does no network
fetching, so those rendered as broken icons — and offering to "render" one
would be a lie, since there is no Example behind it. They become a labelled
link that opens in a browser, and are not counted as pending renders.

They are boxed like a render placeholder, and for that they need a scheme of
their own (`bfsremote:`) rather than the plain `https:` they point at. The
boxing is driven by link target, and the docs are full of ordinary prose
links — to Wikipedia, to sibling wiki pages — which must stay plain text; a
scheme is the only thing separating an image stand-in from those. The pane
strips the prefix and hands the real URL to `QDesktopServices`.

**LaTeX math renders as Unicode.** GitHub's wiki sets `$inline$` and
`$$display$$` with MathJax. Qt has no MathJax, no MathML and no way to
typeset, so `unicode_math` converts the subset Unicode can express --
superscripts, subscripts, roots, Greek, the common operators -- and
**refuses everything else**, leaving the original LaTeX untouched. Refusing
is the important half: a reader can decode `\frac{a}{b}`, but not a formula
that silently lost its numerator.

Unicode's script alphabets have holes -- no superscript `/`, no superscript
alpha -- so a script it cannot set is written `d^(1/a)` rather than refused.
Nothing is lost but the typesetting, which was never on offer; refusing
there would put raw `$...$` on screen instead. Only a genuine gap in
meaning (a matrix, an unbalanced brace, an unknown command) refuses.

The harder half is not converting things that were never math. OpenSCAD
spells its special variables `$fn`, `$fa`, `$fs`, so a BOSL2 line reading
`$fa=1,$fs=.5` satisfies MathJax's delimiter rule exactly. Three filters
bring 166 false matches across BOSL2 down to zero: code spans and fenced
blocks are held aside, 4-space-indented blocks are skipped (docsgen writes
Example scripts that way, not as fences), and a candidate whose content
begins with a known `$`-variable name is refused outright. That last filter
is why this pane is *more* correct than the published wiki, which renders
"Uses $fn/$fa/$fs to control the number of facets" as math.

Across all of BOSL2 this converts 139 lines, mis-converts none, and
leaves nothing convertible behind.

**Table whitespace collapses.** docsgen pads every cell out to a column and
writes two spaces after a sentence, which is what makes the raw markdown
readable. HTML throws all of that away; QTextDocument does not -- it is not
an HTML layout and keeps every space it is handed -- so an argument table
rendered as `the cube.  Default` where the wiki shows one space.
`collapse_table_spaces` squeezes runs of spaces in table rows only, and only
outside `code spans`, where the author's spacing has to survive. A table is
recognised by its separator row, so the many Usage lines and prose that
contain a literal `|` are left alone.

**Lists indent 2em, not Qt's 40px.** Qt indents each list level by a flat
`QTextDocument.indentWidth()`, defaulting to 40px whatever the font — half
again as deep as GitHub's `padding-left: 2em`, and unmoved by a font-size
change. Setting the document property is enough for every level, since Qt
multiplies it by the nesting depth itself.

**`$preview` follows the mode the reference renders in.** openscad_docsgen
drives OpenSCAD with `--preview ""` for Examples and Figures,
`--preview throwntogether` for `ThrownTogether`, and plain echo export for
Log blocks -- all three set `$preview` -- and only `--render ""` for an
example marked `Render`, which clears it. (Both flags take an optional
argument, which is why the reference passes an empty string: a bare
`--preview` swallows the filename and OpenSCAD prints its help instead.)

BelfrySCAD has no preview mode of its own and the evaluator defaults
`$preview` to false, so every example that branches on it took the wrong
arm. BOSL2's `ruler()` is `if ($preview)` all the way down: all seven
`ruler()` examples rendered as bare axes, and -- because they all produced
the same empty scene -- as *byte-identical* PNGs, which is what finally gave
it away. The runner now seeds `$preview` per request; no evaluator change
was needed, since `viewport_params` already seeds arbitrary `$`-names.

This affects only docs generation. An ordinary render, in the GUI or
through `-o`, is a real render and leaves `$preview` false.

**Refresh discards the rendered images.** The image cache is keyed on the
file's path, deliberately not its contents, which is what lets
click-to-render accumulate across edits and sessions. The cost is that an
Example whose code *has* changed keeps showing the old picture, and nothing
else ever clears it. The Refresh button is the way out: it deletes the
file's cache directory, so every image reverts to a placeholder and the
next click renders afresh.

Only that button does it. `refresh()` also runs when the Docs dock is shown
and when a file opens, and those must not cost the user every image they
have already rendered -- so the button sets a one-shot flag that the next
`refresh()` consumes, rather than the invalidation living in `refresh()`
itself.

**Same-file links scroll to their heading.** A BOSL2 file's docs carry
hundreds of `[cuboid](#module-cuboid)` links. Qt's markdown reader gives
headings no anchor names of their own, so `scrollToAnchor` has nothing to
find; `anchor_targets` walks the finished document instead and maps each
heading's slug to its block number, which the click then scrolls to.

`heading_slug` reproduces GitHub's rule, because that is what docsgen's
links assume: lower-case, drop punctuation, spaces become hyphens. Runs of
spaces are **not** collapsed -- "Section: Adaptive Children Using `$`
Variables" loses the backticks and the `$` but keeps both spaces around
them, and the emitted link really is
`#section-adaptive-children-using--variables`. Getting that wrong was the
difference between 5104 and 5105 of BOSL2's 5109 intra-file links
resolving; the last few point at headings that do not exist, and are
broken on the published wiki too.

**A queued placeholder says so.** Clicking one, or the status line's
render-all, rewrites each affected placeholder to `Rendering Example 8` with
a trailing ellipsis that grows a dot a second. Only the text and its link
are replaced — the block format is untouched, so the box stays, and dropping
the link is what stops a second click queueing the same image twice. The
render itself runs on the worker thread, so the clock keeps ticking; without
it a "render all" is minutes of a page with nothing moving on it.

**Animated examples really animate.** A `Spin`/`Anim` example is written as
an APNG, and Qt animates nothing: its image reader returns frame 0 and
QTextDocument has no notion of a moving image. `png_writer.read_apng_frames`
splits the file back into one standalone still PNG per frame — cheap,
because `write_apng` stores every frame full-size with dispose=NONE, so a
frame needs only its own data wrapped in a fresh IHDR/IDAT/IEND.

A single timer then drives every animation on the page, swapping the image
**resource** the document already points at rather than editing the
document. That matters twice over: no relayout per frame, and therefore no
disturbed scroll position. Each image keeps its own delay and advances when
that much has accumulated, so one clock serves any mix of speeds. Frames are
decoded on first use and then kept — a 36-frame example is ~170KB on disk
but ~11MB decoded, so decoding up front would stall the first tick and
charge full price for an animation nobody scrolls to.

**Level-1 and level-2 headings get a rule under them and space above.**
Qt gives headings no margins of their own, so they otherwise sit flush
against the preceding paragraph. The rule is Qt's own
`BlockTrailingHorizontalRulerWidth` — the very property its markdown reader
sets on a `---` block — applied to the heading block itself, so it is drawn
by the same code path rather than faked with a border or an inserted empty
block. Level 3 and deeper are deliberately left alone: a BOSL2 file carries
dozens of `Module:`/`Function:` headings and ruling each one turns the page
into a ladder.

The rule is drawn **grey**, not Qt's default hard black. Qt colours a
BlockTrailingHorizontalRulerWidth from the palette's `WindowText` role, NOT
`Text` — established by setting each role in turn and seeing which one moved
the rule. A QTextBrowser draws its document text with `Text`, so overriding
`WindowText` (`_apply_rule_color`) recolours every rule and leaves the prose
untouched.

**Alternating table body rows are tinted.** Done per cell in `_stripe_tables`,
because Qt's CSS subset has no `:nth-child`. Row 0 is the header and keeps
its own look; striping starts at the second body row.

**Code blocks get a tinted, indented box.** Qt's markdown reader renders
them as bare monospace text, so `_style_code_blocks` walks the finished
document and applies a block format, finding code blocks by Qt's own
`BlockCodeLanguage` property (set for indented and fenced blocks alike,
never for prose) rather than guessing from the font. Each line of a block is
its own `QTextBlock`, so the tint goes on per line with zero spacing between
them and they abut into one continuous box; only the first and last line of
a run carry the box's padding. The tint itself is the pane's base colour
nudged 6% toward its text colour (`_code_tint`), derived from the palette so
it stays a subtle wash in a dark theme rather than a bright slab —
`#f0f0f0` on white, `#292a2d` on `#1e1f22`.

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

### No stylesheet: why the styling is code, not CSS

`QTextDocument::setDefaultStyleSheet` has **no effect** on a document built
with `setMarkdown` — Qt only applies it when parsing HTML. Measured, not
assumed: with a stylesheet set, `h1 { color }` and `td { background-color }`
change nothing through `setMarkdown`, and both take effect if the same
markdown is round-tripped through `toHtml()`/`setHtml()`.

That round trip is a real option if a fuller theme is ever wanted, with two
caveats: Qt's CSS subset is small (a `code` selector was ignored in the same
test, and there is no `:nth-child`, so table striping would still be
per-cell), and re-parsing our own HTML risks changing rendering in ways the
direct markdown path does not. Until that trade is worth making, the styling
lives in `_style_headings`/`_style_code_blocks`/`_stripe_tables`, which
operate on the document Qt actually built.

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
