# TODO

- NURBS viewer/editor support
- GPU-upload diffing (Viewport.load_geometry/SceneRenderer.load_geometry still does a full wholesale re-upload of the entire flat body list every render, even though ManifoldCache now skips unchanged Manifold work one layer up)
