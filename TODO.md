# TODO

- NURBS viewer/editor support
- VNF tile texture viewer/editor
- Cheaper GPU upload for many-small-bodies scenes. `Viewport.load_geometry`/
  `SceneRenderer.load_geometry` still re-upload every body from scratch each
  render, even though `ManifoldCache` skips unchanged Manifold work one layer
  up. **Measured first, and buffer *diffing* is not the answer:** the GL upload
  itself is only 1.3-6.6% of `load_geometry`, and two renders of the same script
  return Manifolds that are neither `==` nor `hash()`-equal, so a cache would
  need a content hash -- another full pass over the data it is trying to avoid
  touching. Cost tracks **body count, not triangle count** (144 bodies of 1,728
  triangles cost 6.7ms; 133,392 triangles in one body cost 40.9ms), so this only
  matters for parametric assemblies, where upload is ~55% of render time. What
  is left after the lazy-wireframe fix is CPU-side: normals + interleave (~40%)
  and `MeshBuffer.original_ids`, a pure-Python `set(int(x) for x in tri_ids)`
  over every triangle. Measure those two before building anything.

- Move BOSL2's `Regressions` job to `belfryscad --test`. It is the last
  thing in that repo still downloading the OpenSCAD 2021.01 AppImage;
  `--docsgen` and `--mdimggen` took over `CheckDocs` and `CheckTutorials`
  in BOSL2 #2034. All 909 of its tests now pass on this evaluator (exit 0,
  ~82s serial, against openscad-test's 8.3s for 226 with 5-way
  parallelism), so the swap is a workflow edit rather than a porting job:
  drop the AppImage, `libfuse2` and `pip install openscad-test`, and run
  `belfryscad --test tests/*.scadtest`. Needs belfryscad with
  openscad_cpp_evaluator >= 1.3.0.

- Stop shipping Qt twice in the AppImage. `BelfrySCAD-1.23.14-x86_64.AppImage`
  is 343MB, and ~300MB of that is **129 Qt libraries present in two copies**:
  the PySide6 wheel's own under `usr/app_packages/PySide6/Qt/lib/`, and
  linuxdeploy's stripped-and-patched copies under `usr/lib/`. Same SONAME, same
  Qt 6.9.3, and PySide6's `RUNPATH` lists `$ORIGIN/../../../../lib` (= `usr/lib`)
  ahead of `$ORIGIN`, so the loader dedups by SONAME and the `usr/lib` copy is
  the one that loads -- consistently, for every module. **Harmless, just dead
  weight**, and explicitly *not* the cause of #430 (checked while investigating
  it). The fix is on the briefcase/linuxdeploy side: stop it copying libraries it
  finds inside `app_packages`, or drop the wheel's `Qt/lib` after the bundle is
  built. Verify by re-running the duplicate scan on the built AppImage rather
  than trusting the size.
