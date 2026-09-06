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

- Close the five evaluator gaps `belfryscad --test` found, so BOSL2's
  `Regressions` job can move off the OpenSCAD binary. The runner itself is
  done; over BOSL2's 909 tests it passes 841 in ~100s, against
  openscad-test's 8.3s for 226 with 5-way parallelism (so ~3x faster
  serially). openscad-test passes all 226 in the files where we fail, so
  every one of these is ours:
    - **61 tests** -- `str()` of a function literal. The reference prints
      the function's own source, we print `<function-literal>`. Needs a
      printer in the evaluator: the parser's `toString()` is
      precedence-minimal and used in 94 places there, while OpenSCAD
      parenthesises every binary and ternary, so neither can be reused.
      Rules, derived from the binary: binary `(a + b)` always, even as a
      call argument (`f((a + b))`); ternary `(c ? t : f)`; unary bare
      (`-x`, `-(a + b)`); index/member/call/vector bare; a comprehension
      body gets an extra wrap (`[for(i = [0 : a]) ((i + 1))]`); `let` body
      bare; ranges spaced `[1 : 2 : 9]`.
    - `str_strip`, `format`, `format_float` (3 tests, strings.scad)
    - `in_list(..., idx=)` (1)
    - `hstack`, `echo_matrix` (2)
    - `typeof([0:NAN:INF])` should be `"invalid"` (1)
  Fixing only the first still leaves the job unable to switch -- it is
  five fixes, not one.
