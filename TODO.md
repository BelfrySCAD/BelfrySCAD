# TODO

- Point to point measurement from the viewport
- Clean up object selection/reverse code lookup
- Come up with sane color schemes.
- NURBS viewer/editor support
- Code reformatting/prettyprinting in code editor
- GPU-upload diffing (Viewport.load_geometry/SceneRenderer.load_geometry still does a full wholesale re-upload of the entire flat body list every render, even though ManifoldCache now skips unchanged Manifold work one layer up)
- For libraries we have installed, make a list of files we can include/use into the source.
- AI Integration
- Debug session start (Shift+F6) reads tab.file_path directly off disk instead of the live editor buffer, same stale-content bug F6/Render had (fixed in main_window.py's _RenderWorker) -- not yet fixed for debug because it also needs current_file/main_file stack-frame-identity handling
