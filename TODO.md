# TODO

- Point to point measurement from the viewport
- Clean up object selection/reverse code lookup
- Evaluation cacheing.
- Come up with sane color schemes.
- NURBS viewer/editor support
- VNF editor face editing.
- Bezier path editing behaviours
- Affine editor translate/rotate/scale tools
- Code reformatting/prettyprinting in code editor
- GPU-upload diffing (Viewport.load_geometry/SceneRenderer.load_geometry still does a full wholesale re-upload of the entire flat body list every render, even though ManifoldCache now skips unchanged Manifold work one layer up)
