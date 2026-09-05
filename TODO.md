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

- Integrate openscad_test into belfryscad. It is the last thing BOSL2's CI
  still needs the OpenSCAD binary for: `belfryscad --docsgen` and
  `--mdimggen` now cover the `CheckDocs` and `CheckTutorials` jobs
  (BOSL2 #2034), leaving only `Regressions`, which runs
  `openscad-test ./scripts/run_tests.sh` against a downloaded
  OpenSCAD 2021.01 AppImage. Replacing it would drop the AppImage,
  `libfuse2` and the last non-BelfrySCAD evaluator from that workflow, and
  would let the regression suite run on the same evaluator the docs do.
